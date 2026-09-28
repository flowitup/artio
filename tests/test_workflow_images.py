"""Input images for custom workflows: storage, Load Image slots, the run form, dispatch to Modal and
the 0003 backfill."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import re
import time
from pathlib import Path

import pytest
from PIL import Image

from artio import custom_workflows, db, jobs, storage
from artio.worker import Worker

EDIT_GRAPH = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "photo.png"}, "_meta": {"title": "Ảnh gốc"}},
    "2": {"class_type": "LoadImageMask", "inputs": {"image": "mask.png", "channel": "alpha"}},
    "3": {"class_type": "LoadImage", "inputs": {"image": ["9", 0]}},  # linked: not a slot
    "4": {"class_type": "KSampler", "inputs": {"seed": 5, "steps": 20}},
    "5": {"class_type": "SaveImage", "inputs": {"images": ["4", 0]}},
}


def _image_bytes(fmt: str = "PNG", size=(32, 24), color=(10, 200, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


# -- storage ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("fmt", "ext"), [("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp")])
def test_save_input_image_stores_by_content_hash(tmp_path, fmt, ext):
    data = _image_bytes(fmt)
    name = storage.save_input_image(tmp_path, data)
    assert re.fullmatch(rf"artio-in-[0-9a-f]{{64}}\.{ext}", name)
    assert storage.INPUT_IMAGE_NAME_RE.match(name)
    assert (tmp_path / "inputs" / name).read_bytes() == data
    assert storage.save_input_image(tmp_path, data) == name  # same bytes, same file
    assert storage.read_input_image(tmp_path, name) == data


def test_save_input_image_rejects_what_it_cannot_use(tmp_path):
    with pytest.raises(storage.InvalidInputImage, match="not a readable"):
        storage.save_input_image(tmp_path, b"not an image")
    with pytest.raises(storage.InvalidInputImage, match="GIF"):
        storage.save_input_image(tmp_path, _image_bytes("GIF"))
    with pytest.raises(storage.InvalidInputImage, match="larger than 10 MB"):
        storage.save_input_image(tmp_path, b"x" * (storage.MAX_INPUT_IMAGE_BYTES + 1))
    assert not (tmp_path / "inputs").exists()


def test_save_input_image_rejects_too_many_pixels(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "MAX_INPUT_IMAGE_PIXELS", 100)
    with pytest.raises(storage.InvalidInputImage, match="megapixel"):
        storage.save_input_image(tmp_path, _image_bytes(size=(20, 20)))


@pytest.mark.parametrize(
    "name", ["../secret.png", "photo.png", "artio-in-abc.png", "artio-in-" + "0" * 64 + ".gif"]
)
def test_read_input_image_refuses_names_it_never_issued(tmp_path, name):
    assert storage.read_input_image(tmp_path, name) is None


# -- slots -----------------------------------------------------------------------------------------------


def test_image_slots_finds_load_image_nodes_with_their_titles():
    slots = custom_workflows.image_slots(EDIT_GRAPH)
    assert slots == [
        custom_workflows.ImageSlot("1", "Ảnh gốc"),
        custom_workflows.ImageSlot("2", "LoadImageMask"),
    ]


def test_validate_api_graph_caps_the_number_of_image_slots():
    graph = {str(i): {"class_type": "LoadImage", "inputs": {"image": "x.png"}} for i in range(17)}
    graph["out"] = {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}}
    with pytest.raises(custom_workflows.WorkflowError, match="more than 16"):
        custom_workflows.validate_api_graph(json.dumps(graph).encode())


def test_with_images_fills_slots_without_touching_the_original():
    name = "artio-in-" + "a" * 64 + ".png"
    out = custom_workflows.with_images(EDIT_GRAPH, {"1": name})
    assert out["1"]["inputs"]["image"] == name
    assert EDIT_GRAPH["1"]["inputs"]["image"] == "photo.png"
    assert [s.node_id for s in custom_workflows.missing_images(out)] == ["2"]


def test_listing_carries_each_workflows_slots(conn, registry):
    backend = next(iter(registry.backends.values()))
    custom_workflows.store_workflow(conn, registry, "edit", backend.id, EDIT_GRAPH, time.time())
    (summary,) = custom_workflows.list_workflows(conn)
    assert [s.node_id for s in summary.image_slots] == ["1", "2"]
    assert summary.image_slots[0].title == "Ảnh gốc"


def test_a_run_with_unfilled_slots_is_refused_before_any_batch(conn, registry, settings, rng):
    backend = next(iter(registry.backends.values()))
    wid = custom_workflows.store_workflow(conn, registry, "edit", backend.id, EDIT_GRAPH, time.time())
    workflow = custom_workflows.get_workflow(conn, wid)
    with pytest.raises(ValueError, match="needs an uploaded image for: Ảnh gốc"):
        jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


# -- routes ----------------------------------------------------------------------------------------------


def _upload_edit_workflow(app_client, owner_headers, registry) -> int:
    backend = next(iter(registry.backends.values()))
    files = {"graph_file": ("wf.json", json.dumps(EDIT_GRAPH).encode(), "application/json")}
    data = {"name": "edit wf", "backend_id": backend.id}
    app_client.post("/workflows", headers=owner_headers, data=data, files=files, follow_redirects=False)
    listing = app_client.get("/workflows", headers=owner_headers)
    return max(int(i) for i in re.findall(r'href="/workflows/(\d+)"', listing.text))


def test_workflows_page_shows_one_file_input_per_slot(app_client, owner_headers, registry):
    _upload_edit_workflow(app_client, owner_headers, registry)
    page = app_client.get("/workflows", headers=owner_headers).text
    assert 'name="image:1"' in page and 'name="image:2"' in page
    assert 'name="image:3"' not in page
    assert "Ảnh gốc" in page
    assert 'enctype="multipart/form-data"' in page


def test_run_route_stores_the_images_and_points_the_job_graph_at_them(
    app_client, owner_headers, registry, settings, conn
):
    wid = _upload_edit_workflow(app_client, owner_headers, registry)
    photo, mask = _image_bytes(), _image_bytes("WEBP", color=(0, 0, 0))
    response = app_client.post(
        f"/workflows/{wid}/run",
        headers=owner_headers,
        data={"seed_mode": "random", "count": "2"},
        files={"image:1": ("me.png", photo, "image/png"), "image:2": ("m.webp", mask, "image/webp")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    rows = conn.execute("SELECT graph_json FROM jobs ORDER BY id").fetchall()
    assert len(rows) == 2
    for row in rows:
        graph = json.loads(row["graph_json"])
        assert storage.read_input_image(settings.data_dir, graph["1"]["inputs"]["image"]) == photo
        assert storage.read_input_image(settings.data_dir, graph["2"]["inputs"]["image"]) == mask
        assert graph["3"]["inputs"]["image"] == ["9", 0]
    # the stored workflow itself is unchanged
    stored = json.loads(conn.execute("SELECT graph_json FROM workflows").fetchone()["graph_json"])
    assert stored["1"]["inputs"]["image"] == "photo.png"


def test_run_route_without_an_image_shows_a_message_and_creates_nothing(
    app_client, owner_headers, registry, conn
):
    wid = _upload_edit_workflow(app_client, owner_headers, registry)
    response = app_client.post(
        f"/workflows/{wid}/run",
        headers=owner_headers,
        data={"seed_mode": "random", "count": "1"},
        files={"image:1": ("me.png", _image_bytes(), "image/png")},
    )
    assert response.status_code == 200
    assert "Choose an image for" in response.text and "node 2" in response.text
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


def test_run_route_with_a_non_image_shows_a_message(app_client, owner_headers, registry, conn):
    wid = _upload_edit_workflow(app_client, owner_headers, registry)
    response = app_client.post(
        f"/workflows/{wid}/run",
        headers=owner_headers,
        data={"seed_mode": "random", "count": "1"},
        files={
            "image:1": ("me.png", b"hello", "image/png"),
            "image:2": ("m.png", _image_bytes(), "image/png"),
        },
    )
    assert response.status_code == 200
    assert "not a readable PNG, JPEG or WebP image" in response.text
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


def test_run_route_accepts_a_body_over_the_default_64_kib_limit(app_client, owner_headers, registry, conn):
    wid = _upload_edit_workflow(app_client, owner_headers, registry)
    big = _image_bytes(size=(400, 400))
    noisy = Image.frombytes("RGB", (200, 200), os.urandom(200 * 200 * 3))  # noise does not compress
    buf = io.BytesIO()
    noisy.save(buf, format="PNG")
    assert len(buf.getvalue()) > 64 * 1024
    response = app_client.post(
        f"/workflows/{wid}/run",
        headers=owner_headers,
        data={"seed_mode": "random", "count": "1"},
        files={"image:1": ("a.png", buf.getvalue(), "image/png"), "image:2": ("b.png", big, "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303


# -- dispatch --------------------------------------------------------------------------------------------


def _filled_workflow(conn, registry, settings):
    backend = next(iter(registry.backends.values()))
    wid = custom_workflows.store_workflow(conn, registry, "edit", backend.id, EDIT_GRAPH, time.time())
    photo, mask = _image_bytes(), _image_bytes("JPEG")
    names = {
        "1": storage.save_input_image(settings.data_dir, photo),
        "2": storage.save_input_image(settings.data_dir, mask),
    }
    workflow = custom_workflows.get_workflow(conn, wid)
    return (
        dataclasses.replace(workflow, graph=custom_workflows.with_images(workflow.graph, names)),
        names,
        photo,
        mask,
    )


def test_dispatch_sends_the_referenced_images_with_the_graph(conn, registry, settings, fake_gateway, rng):
    workflow, names, photo, mask = _filled_workflow(conn, registry, settings)
    jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    conn.commit()

    asyncio.run(Worker(settings, registry, fake_gateway).dispatch_once())

    row = conn.execute("SELECT status, call_id FROM jobs").fetchone()
    assert row["status"] == "submitted"
    assert fake_gateway.images_for(row["call_id"]) == {names["1"]: photo, names["2"]: mask}


def test_dispatch_sends_no_images_for_a_plain_graph(conn, registry, settings, fake_gateway, rng):
    backend = next(iter(registry.backends.values()))
    graph = {k: v for k, v in EDIT_GRAPH.items() if k in ("4", "5")}
    wid = custom_workflows.store_workflow(conn, registry, "plain", backend.id, graph, time.time())
    workflow = custom_workflows.get_workflow(conn, wid)
    jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    conn.commit()

    asyncio.run(Worker(settings, registry, fake_gateway).dispatch_once())

    call_id = conn.execute("SELECT call_id FROM jobs").fetchone()["call_id"]
    assert fake_gateway.images_for(call_id) == {}


def test_dispatch_fails_a_job_whose_input_image_is_gone(conn, registry, settings, fake_gateway, rng):
    workflow, names, _, _ = _filled_workflow(conn, registry, settings)
    jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    conn.commit()
    (settings.data_dir / storage.INPUT_IMAGES_DIR / names["2"]).unlink()

    asyncio.run(Worker(settings, registry, fake_gateway).dispatch_once())

    row = conn.execute("SELECT status, error FROM jobs").fetchone()
    assert row["status"] == "failed"
    assert "no longer stored" in row["error"]
    assert fake_gateway.spawn_count == 0


# -- migration 0003 backfill and the Modal side ----------------------------------------------------------


def test_migration_backfills_slots_for_workflows_stored_before_it(settings):
    db.migrate(settings)
    conn = db.connect(settings)
    try:
        conn.execute(
            "INSERT INTO workflows (name, backend_id, graph_json, created_at) VALUES (?, ?, ?, ?)",
            ("old", "qwen21-uc", json.dumps(EDIT_GRAPH, ensure_ascii=False), time.time()),
        )
        migration = (Path(db.__file__).parent / "migrations" / "0003_workflow_image_inputs.sql").read_text()
        conn.execute(migration[migration.index("UPDATE workflows") :])
        (summary,) = [s for s in custom_workflows.list_workflows(conn) if s.name == "old"]
    finally:
        conn.close()
    assert summary.image_slots == tuple(custom_workflows.image_slots(EDIT_GRAPH))


def test_backend_accepts_exactly_the_names_storage_issues(backend_script, tmp_path):
    name = storage.save_input_image(tmp_path, _image_bytes())
    match = backend_script.INPUT_NAME_RE.match(name)
    assert match is not None and name.startswith(f"artio-in-{match.group(1)}")
    for bad in ("../x.png", "photo.png", "artio-in-" + "0" * 64 + ".gif"):
        assert backend_script.INPUT_NAME_RE.match(bad) is None
