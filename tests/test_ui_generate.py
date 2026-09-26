"""The generate form: model/size/seed picker, batch submission, validation and remix."""

from __future__ import annotations

import asyncio
import dataclasses
import json

from starlette.testclient import TestClient

from atelier import db
from atelier.main import create_app
from tests.conftest import OWNER_EMAIL, PUBLIC_ORIGIN, mint


def test_generate_form_lists_every_registry_model(app_client, owner_headers, registry):
    response = app_client.get("/generate", headers=owner_headers)
    assert response.status_code == 200
    for model in registry.models.values():
        assert model.label in response.text


def test_generate_params_partial_reflects_the_selected_model(app_client, owner_headers, registry):
    model = next(iter(registry.models.values()))
    response = app_client.get(f"/generate/params?model={model.id}", headers=owner_headers)
    assert response.status_code == 200
    for preset in model.param_schema.presets:
        assert preset.name in response.text


def test_post_generate_with_count_creates_n_jobs_with_distinct_seeds(app_client, owner_headers, registry, conn):
    model = next(iter(registry.models.values()))
    preset = model.param_schema.default_size()
    response = app_client.post(
        "/generate",
        headers=owner_headers,
        follow_redirects=False,
        data={
            "model_id": model.id,
            "prompt": "a red fox in snow",
            "negative": "",
            "preset": preset.name,
            "width": preset.width,
            "height": preset.height,
            "steps": model.param_schema.steps_default,
            "cfg": model.param_schema.cfg_default,
            "seed_mode": "random",
            "seed": "",
            "count": 4,
        },
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/queue?batch=")
    batch_id = int(response.headers["location"].split("=")[1])

    rows = conn.execute("SELECT params_json FROM jobs WHERE batch_id = ?", (batch_id,)).fetchall()
    assert len(rows) == 4
    seeds = {json.loads(row["params_json"])["seed"] for row in rows}
    assert len(seeds) == 4


def test_generate_from_prefills_the_form_from_the_source_image(app_client, owner_headers, route_ids, conn):
    image_id = route_ids["image_id"]
    source = conn.execute(
        "SELECT images.model_id, jobs.params_json FROM images JOIN jobs ON jobs.id = images.job_id "
        "WHERE images.id = ?",
        (image_id,),
    ).fetchone()
    params = json.loads(source["params_json"])

    response = app_client.get(f"/generate?from={image_id}", headers=owner_headers)
    assert response.status_code == 200
    body = response.text
    assert f'value="{params["seed"]}"' in body
    assert f'value="{params["width"]}"' in body
    assert f'value="{params["height"]}"' in body
    assert f'value="{params["steps"]}"' in body
    assert f'value="{params["cfg"]}"' in body
    fixed_radio = body.split('value="fixed"')[1][:40]
    assert "checked" in fixed_radio


def test_remix_preserves_a_seed_of_zero_and_a_cfg_of_zero(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn
):
    model = next(iter(registry.models.values()))
    preset = model.param_schema.default_size()
    response = app_client.post(
        "/generate",
        headers=owner_headers,
        follow_redirects=False,
        data={
            "model_id": model.id,
            "prompt": "p",
            "negative": "",
            "preset": preset.name,
            "width": preset.width,
            "height": preset.height,
            "steps": model.param_schema.steps_default,
            "cfg": 0.0,
            "seed_mode": "fixed",
            "seed": 0,
            "count": 1,
        },
    )
    assert response.status_code == 303
    asyncio.run(app_client.app.state.worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs").fetchone()
    fake_gateway.finish(job["call_id"], png_bytes)
    asyncio.run(app_client.app.state.worker.poll_once())
    image_id = conn.execute("SELECT id FROM images WHERE job_id = ?", (job["id"],)).fetchone()["id"]

    remix = app_client.get(f"/generate?from={image_id}", headers=owner_headers)
    assert remix.status_code == 200
    assert 'value="0"' in remix.text  # the seed field, not blanked
    assert 'value="0.0"' in remix.text  # the cfg field, not reset to the model default


def test_generate_form_on_a_fresh_page_still_shows_the_schema_default_cfg(app_client, owner_headers, registry):
    model = next(iter(registry.models.values()))
    response = app_client.get("/generate", headers=owner_headers)
    assert response.status_code == 200
    assert f'value="{model.param_schema.cfg_default}"' in response.text


def test_generate_from_an_oversized_image_id_answers_422_not_a_500(app_client, owner_headers):
    response = app_client.get(f"/generate?from={2**64}", headers=owner_headers)
    assert response.status_code == 422


def test_post_generate_invalid_params_rerenders_with_200_and_message(app_client, owner_headers, registry):
    model = next(iter(registry.models.values()))
    response = app_client.post(
        "/generate",
        headers=owner_headers,
        data={
            "model_id": model.id,
            "prompt": "x",
            "negative": "",
            "preset": "custom",
            "width": model.param_schema.size_max + 1000,
            "height": model.param_schema.size_max + 1000,
            "steps": model.param_schema.steps_default,
            "cfg": model.param_schema.cfg_default,
            "seed_mode": "random",
            "seed": "",
            "count": 1,
        },
    )
    assert response.status_code == 200
    assert "must be between" in response.text


def test_post_generate_disk_guard_error_rerenders_with_200_and_no_job_created(registry, fake_gateway, settings, access_key):
    """With the cap set below current usage: the POST re-renders with the refusal message at 200 and
    creates no job, and the header badge still reports usage."""
    tiny_cap_settings = dataclasses.replace(
        settings,
        public_origin=PUBLIC_ORIGIN,
        cf_team_domain="https://flowitupteam-test.cloudflareaccess.com",
        cf_aud="test-atelier-aud",
        owner_email=OWNER_EMAIL,
        plugin_client_id="test-plugin-client-id.access",
        data_cap_gb=0,
    )
    db.migrate(tiny_cap_settings)
    seed_conn = db.connect(tiny_cap_settings)
    seed_conn.execute(
        "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) VALUES (0, 'm', 'generate', '{}', 1)"
    )
    seed_conn.execute(
        "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, status, created_at) "
        "VALUES (1, 'm', 'qwen21-uc', 'generate', '{}', '{}', 'done', 0)"
    )
    seed_conn.execute(
        "INSERT INTO images (job_id, model_id, file_png, file_thumb, width, height, bytes, sha256, created_at) "
        "VALUES (1, 'm', 'a.png', 'a.webp', 8, 8, 1, 'sha', 0)"
    )
    seed_conn.commit()
    seed_conn.close()

    key, _ = access_key
    owner_token = mint(key, email=OWNER_EMAIL)
    headers = {"Cf-Access-Jwt-Assertion": owner_token, "Origin": PUBLIC_ORIGIN}

    app = create_app(tiny_cap_settings, registry=registry, gateway=fake_gateway, start_worker=False)
    model = next(iter(registry.models.values()))
    preset = model.param_schema.default_size()
    with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
        response = client.post(
            "/generate",
            headers=headers,
            data={
                "model_id": model.id,
                "prompt": "a red fox in snow",
                "negative": "",
                "preset": preset.name,
                "width": preset.width,
                "height": preset.height,
                "steps": model.param_schema.steps_default,
                "cfg": model.param_schema.cfg_default,
                "seed_mode": "random",
                "seed": "",
                "count": 1,
            },
        )
        assert response.status_code == 200
        assert "cap" in response.text

        job_count = db.connect(tiny_cap_settings).execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        assert job_count == 1  # only the pre-seeded row: the POST created nothing

        header_status = client.get("/partials/header-status", headers=headers).text
    assert "disk" in header_status
