"""Gallery: model picker, per-model filter, batch grouping, pagination and the batch detail page."""

from __future__ import annotations

import asyncio
import html
import re

import pytest

from artio import jobs
from artio.library import PAGE_SIZE
from artio.registry import Backend, Model, ParamSchema, Registry, SizePreset
from artio.worker import Worker
from artio.workflows import qwen_image_21


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


# -- has_next pagination ------------------------------------------------------------------------------
#
# Regression coverage for a known issue: the template used to show "Next" whenever the current page had
# any images at all ({% if groups %}), even on the last page. gallery_page now fetches one row past the
# page size to compute a real has_next, so these prove the fix (they fail under the old `if groups` rule).


def _seed_images(conn, count: int, *, prompt: str = "seeded") -> list[int]:
    """Inserts `count` done, single-image batches directly (bypassing the job engine and Modal): fast
    enough to build pages of 48+ rows for pagination tests, which don't need real files or renders."""
    ids = []
    for _ in range(count):
        conn.execute(
            "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) "
            "VALUES (0, 'm', 'generate', '{}', 1)"
        )
        batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, status, created_at) "
            "VALUES (?, 'm', 'qwen21-uc', 'generate', '{}', '{}', 'done', 0)",
            (batch_id,),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO images (job_id, model_id, file_png, file_thumb, width, height, bytes, sha256, "
            "prompt, created_at) VALUES (?, 'm', 'a.png', 'a.webp', 8, 8, 1, 'sha', ?, 0)",
            (job_id, prompt),
        )
        ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    return ids


def test_gallery_last_page_shows_no_next_link(app_client, owner_headers, conn):
    _seed_images(conn, 5)
    response = app_client.get("/gallery", headers=owner_headers)
    assert response.status_code == 200
    assert ">Next<" not in response.text


def test_gallery_exactly_one_full_page_shows_no_next_link(app_client, owner_headers, conn):
    _seed_images(conn, PAGE_SIZE)  # a full page, but nothing beyond it
    response = app_client.get("/gallery", headers=owner_headers)
    assert response.status_code == 200
    assert ">Next<" not in response.text


def test_gallery_full_page_with_more_rows_shows_next_link(app_client, owner_headers, conn):
    _seed_images(conn, PAGE_SIZE + 1)
    response = app_client.get("/gallery", headers=owner_headers)
    assert response.status_code == 200
    assert ">Next<" in response.text


def test_gallery_pagination_links_preserve_the_active_filters(app_client, owner_headers, conn):
    _seed_images(conn, PAGE_SIZE + 5, prompt="preserveme")
    page1 = app_client.get("/gallery", headers=owner_headers, params={"q": "preserveme", "page": 1})
    assert page1.status_code == 200
    # href values are HTML-attribute-escaped ("&" -> "&amp;"), same as a browser would receive them:
    # unescape before using one as a request URL, exactly as a browser does when it follows the link.
    next_href = html.unescape(re.search(r'<a href="([^"]+)">Next</a>', page1.text).group(1))
    assert "q=preserveme" in next_href
    assert "page=2" in next_href

    page2 = app_client.get(next_href, headers=owner_headers)
    assert page2.status_code == 200
    assert ">Next<" not in page2.text  # exactly 5 rows on page 2 (48 + 5, page size 48)
    prev_href = html.unescape(re.search(r'<a href="([^"]+)">Previous</a>', page2.text).group(1))
    assert "q=preserveme" in prev_href
    assert "page=1" in prev_href

    prev_page = app_client.get(prev_href, headers=owner_headers)
    assert prev_page.status_code == 200
    assert ">Next<" in prev_page.text  # back on page 1, which does have a further page


def test_gallery_huge_page_number_answers_422_not_500(app_client, owner_headers):
    """(page - 1) * PAGE_SIZE is bound as a SQLite query parameter; a page number anywhere near
    SQLite's own signed-64-bit ceiling overflows that bind and used to surface as an uncaught 500.
    Hand-edited URLs are the only way to reach this, so FastAPI's own 422 (from a tighter Query
    bound) is the accepted outcome, the same as any other out-of-domain query value here."""
    response = app_client.get("/gallery", headers=owner_headers, params={"page": 2**63 - 1})
    assert response.status_code == 422


def test_gallery_page_at_the_new_bound_still_answers_200(app_client, owner_headers):
    from artio.routes.pages import _MAX_PAGE

    response = app_client.get("/gallery", headers=owner_headers, params={"page": _MAX_PAGE})
    assert response.status_code == 200  # a page this far out is simply empty, never an error
    assert "No images yet" in response.text
