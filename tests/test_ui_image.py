"""Image detail: full settings, download, remix, delete and its cascade."""

from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from atelier import jobs
from atelier.main import create_app
from tests.conftest import PUBLIC_ORIGIN


def _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn):
    model = next(iter(registry.models.values()))
    preset = model.param_schema.default_size()
    response = app_client.post(
        "/generate",
        headers=owner_headers,
        follow_redirects=False,
        data={
            "model_id": model.id,
            "prompt": "a red fox in snow",
            "negative": "no text",
            "preset": preset.name,
            "width": preset.width,
            "height": preset.height,
            "steps": model.param_schema.steps_default,
            "cfg": model.param_schema.cfg_default,
            "seed_mode": "fixed",
            "seed": 42,
            "count": 1,
        },
    )
    assert response.status_code == 303
    asyncio.run(app_client.app.state.worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs").fetchone()
    fake_gateway.finish(job["call_id"], png_bytes)
    asyncio.run(app_client.app.state.worker.poll_once())
    image = conn.execute("SELECT id FROM images WHERE job_id = ?", (job["id"],)).fetchone()
    return image["id"], job["batch_id"]


def test_image_page_lists_full_settings(app_client, owner_headers, registry, fake_gateway, png_bytes, conn):
    image_id, _ = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn)
    model = next(iter(registry.models.values()))

    response = app_client.get(f"/images/{image_id}", headers=owner_headers)
    assert response.status_code == 200
    body = response.text
    assert model.id in body
    assert "a red fox in snow" in body
    assert "no text" in body
    assert "42" in body
    assert str(model.param_schema.steps_default) in body
    assert f"/generate?from={image_id}" in body
    assert f"/images/{image_id}/file" in body


def test_a_second_client_reading_the_same_data_dir_sees_the_same_image(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, settings
):
    image_id, _ = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn)

    second_app = create_app(app_client.app.state.settings, registry=registry, gateway=fake_gateway, start_worker=False)
    with TestClient(second_app, base_url=PUBLIC_ORIGIN) as second_client:
        response = second_client.get(f"/images/{image_id}", headers=owner_headers)
    assert response.status_code == 200
    assert "a red fox in snow" in response.text


def test_download_sets_content_disposition_with_id_and_seed(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn
):
    image_id, _ = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn)
    response = app_client.get(f"/images/{image_id}/file", headers=owner_headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == f'attachment; filename="atelier-{image_id}-42.png"'
    assert response.content == png_bytes


def test_thumb_serves_a_webp_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn):
    image_id, _ = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn)
    response = app_client.get(f"/images/{image_id}/thumb", headers=owner_headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"


@pytest.mark.parametrize("column,route_suffix", [("file_png", "file"), ("file_thumb", "thumb")])
def test_file_route_refuses_an_absolute_path_to_a_real_file_outside_data_dir(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, tmp_path_factory, column, route_suffix
):
    # An absolute stored path (a future bug) plus a file that genuinely exists there: the only thing
    # standing between this and actually serving it is the containment check, so the test can fail.
    image_id, _ = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn)
    outside = tmp_path_factory.mktemp("outside") / "secret.bin"
    outside.write_bytes(b"top secret, never serve me")
    conn.execute(f"UPDATE images SET {column} = ? WHERE id = ?", (str(outside), image_id))
    conn.commit()

    response = app_client.get(f"/images/{image_id}/{route_suffix}", headers=owner_headers)
    assert response.status_code == 404


@pytest.mark.parametrize("column,route_suffix", [("file_png", "file"), ("file_thumb", "thumb")])
def test_file_route_refuses_a_symlink_that_escapes_data_dir(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, settings, tmp_path_factory, column, route_suffix
):
    # A symlink planted inside data_dir but pointing at a real file outside it: Path.resolve() follows
    # the link, so the containment check must (and does) catch this after resolution, not before.
    image_id, _ = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn)
    outside = tmp_path_factory.mktemp("outside") / "secret.bin"
    outside.write_bytes(b"top secret, never serve me")
    link_name = f"escape-{route_suffix}.bin"
    (settings.data_dir / link_name).symlink_to(outside)
    conn.execute(f"UPDATE images SET {column} = ? WHERE id = ?", (link_name, image_id))
    conn.commit()

    response = app_client.get(f"/images/{image_id}/{route_suffix}", headers=owner_headers)
    assert response.status_code == 404


def test_unknown_image_id_answers_404(app_client, owner_headers):
    response = app_client.get("/images/999999", headers=owner_headers)
    assert response.status_code == 404


def test_an_oversized_image_id_path_param_answers_422_not_a_500(app_client, owner_headers):
    response = app_client.get(f"/images/{2**64}", headers=owner_headers)
    assert response.status_code == 422


def test_delete_with_an_oversized_image_id_is_refused_with_200_and_a_message_not_a_500(app_client, owner_headers):
    oversized = 2**64  # past SQLite's signed 64-bit range: would raise OverflowError on a raw bind
    response = app_client.post(f"/images/{oversized}/delete", headers=owner_headers)
    assert response.status_code == 200
    assert response.headers["hx-retarget"] == "#flash"
    assert f"Image {oversized} no longer exists." in response.text


def test_delete_confirmation_text_names_the_backup_retention(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn
):
    image_id, _ = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn)
    response = app_client.get(f"/images/{image_id}", headers=owner_headers)
    assert "6 months" in response.text


def test_delete_removes_image_job_and_empty_batch(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, settings
):
    image_id, batch_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn)
    job_id = conn.execute("SELECT job_id FROM images WHERE id = ?", (image_id,)).fetchone()["job_id"]
    file_png, file_thumb = conn.execute(
        "SELECT file_png, file_thumb FROM images WHERE id = ?", (image_id,)
    ).fetchone()

    response = app_client.post(f"/images/{image_id}/delete", headers=owner_headers)
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == "/gallery"

    assert conn.execute("SELECT 1 FROM images WHERE id = ?", (image_id,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None
    assert not (settings.data_dir / file_png).exists()
    assert not (settings.data_dir / file_thumb).exists()


def test_delete_leaves_the_batch_when_another_job_remains(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, rng, settings
):
    model = next(iter(registry.models.values()))
    size = model.param_schema.default_size()
    request = jobs.BatchRequest(
        model_id=model.id, prompt="p", negative="", width=size.width, height=size.height,
        steps=model.param_schema.steps_default, cfg=model.param_schema.cfg_default,
        seed_mode="random", seed=None, count=2,
    )
    batch_id = jobs.create_batch(conn, registry, settings, request, rng)
    conn.commit()
    asyncio.run(app_client.app.state.worker.dispatch_once())
    call_ids = [row["call_id"] for row in conn.execute("SELECT call_id FROM jobs WHERE batch_id = ?", (batch_id,))]
    for call_id in call_ids:
        fake_gateway.finish(call_id, png_bytes)
    asyncio.run(app_client.app.state.worker.poll_once())
    image_ids = [row["id"] for row in conn.execute("SELECT id FROM images ORDER BY id")]
    assert len(image_ids) == 2

    response = app_client.post(f"/images/{image_ids[0]}/delete", headers=owner_headers)
    assert response.status_code == 200
    assert conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is not None
    assert conn.execute("SELECT 1 FROM images WHERE id = ?", (image_ids[1],)).fetchone() is not None


def test_delete_on_an_unknown_image_shows_an_inline_flash_message(app_client, owner_headers):
    response = app_client.post("/images/999999/delete", headers=owner_headers)
    assert response.status_code == 200
    assert response.headers["hx-retarget"] == "#flash"
