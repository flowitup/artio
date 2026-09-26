"""Gallery: model picker, per-model filter, batch grouping and the batch detail page."""

from __future__ import annotations

import asyncio

import pytest

from atelier import jobs
from atelier.registry import Backend, Model, ParamSchema, Registry, SizePreset
from atelier.worker import Worker
from atelier.workflows import qwen_image_21


@pytest.fixture
def registry() -> Registry:
    """Overrides the session-wide single-model registry with a two-model one, so the model filter can
    be proven to actually filter rather than trivially showing everything."""
    schema = ParamSchema(
        steps_min=1,
        steps_max=60,
        steps_default=25,
        cfg_min=0.0,
        cfg_max=10.0,
        cfg_default=1.0,
        size_min=512,
        size_max=2048,
        size_multiple=16,
        presets=(SizePreset("9:16", 1088, 1920),),
        default_preset="9:16",
    )
    backend = Backend(
        id="qwen21-uc", label="Qwen-Image 2.1 UC", modal_app="qwen21-uc", modal_class="Qwen21UC",
        usd_per_hour=1.95, max_inflight=4,
    )
    model_a = Model(id="model-a", label="Model A", backend_id=backend.id, build_graph=qwen_image_21.build_graph, param_schema=schema)
    model_b = Model(id="model-b", label="Model B", backend_id=backend.id, build_graph=qwen_image_21.build_graph, param_schema=schema)
    return Registry(backends={backend.id: backend}, models={model_a.id: model_a, model_b.id: model_b})


def _make_done_image(conn, registry, settings, fake_gateway, png_bytes, rng, model_id: str) -> int:
    """Creates and completes one job for the given model, returning its image id."""
    model = registry.model(model_id)
    size = model.param_schema.default_size()
    request = jobs.BatchRequest(
        model_id=model.id,
        prompt=f"an image from {model_id}",
        negative="",
        width=size.width,
        height=size.height,
        steps=model.param_schema.steps_default,
        cfg=model.param_schema.cfg_default,
        seed_mode="random",
        seed=None,
        count=1,
    )
    jobs.create_batch(conn, registry, settings, request, rng)
    conn.commit()

    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())
    job = conn.execute(
        "SELECT * FROM jobs WHERE model_id = ? ORDER BY id DESC LIMIT 1", (model_id,)
    ).fetchone()
    fake_gateway.finish(job["call_id"], png_bytes)
    asyncio.run(worker.poll_once())

    return conn.execute("SELECT id FROM images WHERE job_id = ?", (job["id"],)).fetchone()["id"]


def test_gallery_model_picker_lists_every_registry_model(app_client, owner_headers, registry):
    response = app_client.get("/gallery", headers=owner_headers)
    assert response.status_code == 200
    for model in registry.models.values():
        assert model.label in response.text


def test_gallery_model_filter_shows_only_that_models_images(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, settings, rng
):
    _make_done_image(conn, registry, settings, fake_gateway, png_bytes, rng, "model-a")
    _make_done_image(conn, registry, settings, fake_gateway, png_bytes, rng, "model-b")

    response = app_client.get("/gallery?model=model-b", headers=owner_headers)
    assert response.status_code == 200
    assert "an image from model-b" in response.text
    assert "an image from model-a" not in response.text


def test_gallery_with_no_filter_shows_every_models_images(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, settings, rng
):
    _make_done_image(conn, registry, settings, fake_gateway, png_bytes, rng, "model-a")
    _make_done_image(conn, registry, settings, fake_gateway, png_bytes, rng, "model-b")

    response = app_client.get("/gallery", headers=owner_headers)
    assert "an image from model-a" in response.text
    assert "an image from model-b" in response.text


def test_gallery_shows_no_images_yet_when_empty(app_client, owner_headers):
    response = app_client.get("/gallery", headers=owner_headers)
    assert response.status_code == 200
    assert "No images yet" in response.text


def test_batch_page_shows_every_job_regardless_of_status(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, settings, rng
):
    image_id = _make_done_image(conn, registry, settings, fake_gateway, png_bytes, rng, "model-a")
    batch_id = conn.execute(
        "SELECT jobs.batch_id FROM images JOIN jobs ON jobs.id = images.job_id WHERE images.id = ?", (image_id,)
    ).fetchone()["batch_id"]

    response = app_client.get(f"/batches/{batch_id}", headers=owner_headers)
    assert response.status_code == 200
    assert "done" in response.text


def test_unknown_batch_id_answers_404(app_client, owner_headers):
    response = app_client.get("/batches/999999", headers=owner_headers)
    assert response.status_code == 404
