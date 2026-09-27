"""Queue view: live status transitions, polling stop, cancel and retry."""

from __future__ import annotations

import asyncio

import pytest

from artio import jobs


def _dispatch(app_client):
    asyncio.run(app_client.app.state.worker.dispatch_once())


def _poll(app_client):
    asyncio.run(app_client.app.state.worker.poll_once())


def _submit(app_client, owner_headers, registry, *, count: int = 1, seed_mode: str = "random", seed=None):
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
            "seed_mode": seed_mode,
            "seed": "" if seed is None else seed,
            "count": count,
        },
    )
    assert response.status_code == 303
    return int(response.headers["location"].split("=")[1])


def test_a_job_shows_queued_then_running_then_done_across_polls(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn
):
    batch_id = _submit(app_client, owner_headers, registry)

    queued = app_client.get(f"/queue/rows?batch={batch_id}", headers=owner_headers)
    assert queued.status_code == 200
    assert "queued" in queued.text

    _dispatch(app_client)
    running = app_client.get(f"/queue/rows?batch={batch_id}", headers=owner_headers)
    assert running.status_code == 200
    assert "running" in running.text

    call_id = conn.execute("SELECT call_id FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()["call_id"]
    fake_gateway.finish(call_id, png_bytes)
    _poll(app_client)

    done = app_client.get(f"/queue/rows?batch={batch_id}", headers=owner_headers)
    assert "done" in done.text


def test_queue_rows_returns_286_when_nothing_is_active(app_client, owner_headers):
    response = app_client.get("/queue/rows", headers=owner_headers)
    assert response.status_code == 286


def test_queue_rows_returns_200_while_a_job_is_still_queued(app_client, owner_headers, registry):
    _submit(app_client, owner_headers, registry)
    response = app_client.get("/queue/rows", headers=owner_headers)
    assert response.status_code == 200


def test_failed_row_shows_the_comfyui_error_and_retry_creates_a_new_queued_job(
    app_client, owner_headers, registry, fake_gateway, conn
):
    batch_id = _submit(app_client, owner_headers, registry)
    _dispatch(app_client)
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()

    comfy_error = "ComfyUI rejected workflow: bad seed"
    fake_gateway.fail(job["call_id"], comfy_error)
    _poll(app_client)

    failed_view = app_client.get(f"/queue/rows?batch={batch_id}", headers=owner_headers)
    assert comfy_error in failed_view.text
    assert f"/jobs/{job['id']}/retry" in failed_view.text

    retry_response = app_client.post(f"/jobs/{job['id']}/retry?batch={batch_id}", headers=owner_headers)
    assert retry_response.status_code == 200
    statuses = [row["status"] for row in conn.execute("SELECT status FROM jobs WHERE batch_id = ?", (batch_id,))]
    assert statuses.count("queued") == 1
    assert "failed" in statuses


def test_cancel_a_queued_job_marks_it_cancelled(app_client, owner_headers, registry, conn):
    batch_id = _submit(app_client, owner_headers, registry)
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()

    response = app_client.post(f"/jobs/{job['id']}/cancel?batch={batch_id}", headers=owner_headers)
    assert response.status_code == 200
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "cancelled"


def test_cancel_a_running_job_shows_the_gpu_may_still_finish_note_and_sends_no_modal_cancel(
    app_client, owner_headers, registry, fake_gateway, conn
):
    batch_id = _submit(app_client, owner_headers, registry)
    _dispatch(app_client)
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    assert job["status"] == "submitted"

    response = app_client.post(f"/jobs/{job['id']}/cancel?batch={batch_id}", headers=owner_headers)
    assert response.status_code == 200
    assert "GPU may still finish" in response.text
    assert fake_gateway.calls == []  # cancel is DB-only: never calls the gateway

    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "cancelled"


def test_queued_row_shows_its_waiting_reason(app_client, owner_headers, registry, conn):
    batch_id = _submit(app_client, owner_headers, registry, count=5)  # max_inflight=4: one stays queued
    _dispatch(app_client)
    _dispatch(app_client)  # a second tick records the busy reason on the still-queued job

    response = app_client.get(f"/queue/rows?batch={batch_id}", headers=owner_headers)
    assert "waiting:" in response.text


def test_retry_on_a_queued_job_shows_an_inline_flash_message_and_keeps_the_panel(
    app_client, owner_headers, registry, conn
):
    batch_id = _submit(app_client, owner_headers, registry)
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    assert job["status"] == "queued"

    response = app_client.post(f"/jobs/{job['id']}/retry?batch={batch_id}", headers=owner_headers)
    assert response.status_code == 200
    assert response.headers["hx-retarget"] == "#flash"
    assert "jobs-panel" in response.text  # the (unchanged) panel is still re-sent, as an OOB swap


def test_cancel_on_an_unknown_job_shows_an_inline_flash_message(app_client, owner_headers):
    response = app_client.post("/jobs/999999/cancel", headers=owner_headers)
    assert response.status_code == 200
    assert response.headers["hx-retarget"] == "#flash"


def test_batch_of_four_creates_one_batch_group_visible_in_the_gallery(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn
):
    batch_id = _submit(app_client, owner_headers, registry, count=4)
    _dispatch(app_client)
    for row in conn.execute("SELECT call_id FROM jobs WHERE batch_id = ?", (batch_id,)):
        fake_gateway.finish(row["call_id"], png_bytes)
    _poll(app_client)

    gallery = app_client.get("/gallery", headers=owner_headers)
    assert gallery.status_code == 200
    assert gallery.text.count(f'/batches/{batch_id}"') == 1  # one batch header for all four images


def test_jobs_read_helpers_used_by_the_queue_view(conn):
    assert jobs.has_active(conn) is False
    assert jobs.list_recent(conn) == []


# -- polling only while active --------------------------------------------------------------------------


def test_idle_queue_rows_response_carries_no_hx_trigger(app_client, owner_headers):
    response = app_client.get("/queue/rows", headers=owner_headers)
    assert response.status_code == 286
    assert "hx-trigger" not in response.text


def test_active_queue_rows_response_carries_hx_trigger(app_client, owner_headers, registry):
    _submit(app_client, owner_headers, registry)
    response = app_client.get("/queue/rows", headers=owner_headers)
    assert response.status_code == 200
    assert "hx-trigger" in response.text


def test_retry_rerenders_a_panel_that_polls_again_once_a_job_is_active(
    app_client, owner_headers, registry, fake_gateway, conn
):
    batch_id = _submit(app_client, owner_headers, registry)
    _dispatch(app_client)
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    fake_gateway.fail(job["call_id"], "boom")
    _poll(app_client)

    response = app_client.post(f"/jobs/{job['id']}/retry?batch={batch_id}", headers=owner_headers)
    assert response.status_code == 200
    assert "hx-trigger" in response.text  # the retry itself created a new queued job


def test_cancel_rerenders_a_panel_that_polls_again_when_another_job_is_still_active(
    app_client, owner_headers, registry, conn
):
    batch_id = _submit(app_client, owner_headers, registry, count=2)
    first_id = conn.execute(
        "SELECT id FROM jobs WHERE batch_id = ? ORDER BY id", (batch_id,)
    ).fetchall()[0]["id"]

    response = app_client.post(f"/jobs/{first_id}/cancel?batch={batch_id}", headers=owner_headers)
    assert response.status_code == 200
    assert "hx-trigger" in response.text  # the second job is still queued


def test_cancel_rerenders_a_panel_with_no_trigger_once_nothing_remains_active(
    app_client, owner_headers, registry, conn
):
    batch_id = _submit(app_client, owner_headers, registry)
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()

    response = app_client.post(f"/jobs/{job['id']}/cancel?batch={batch_id}", headers=owner_headers)
    assert response.status_code == 200
    assert "hx-trigger" not in response.text


# -- the GPU note only after submission ------------------------------------------------------------------


def test_cancelling_a_queued_job_does_not_show_the_gpu_may_still_finish_note(app_client, owner_headers, registry, conn):
    batch_id = _submit(app_client, owner_headers, registry)
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    assert job["submitted_at"] is None

    response = app_client.post(f"/jobs/{job['id']}/cancel?batch={batch_id}", headers=owner_headers)
    assert "GPU may still finish" not in response.text
    assert "Cancelled." in response.text


# -- elapsed time on a cancelled row ---------------------------------------------------------------------


def test_cancelled_row_shows_a_dash_for_elapsed_time(app_client, owner_headers, registry, conn):
    batch_id = _submit(app_client, owner_headers, registry)
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    app_client.post(f"/jobs/{job['id']}/cancel?batch={batch_id}", headers=owner_headers)

    response = app_client.get(f"/queue/rows?batch={batch_id}", headers=owner_headers)
    assert "—" in response.text


# -- consistent wording for a missing job ----------------------------------------------------------------


def test_retry_on_an_unknown_job_flashes_the_same_wording_as_cancel(app_client, owner_headers):
    cancel_response = app_client.post("/jobs/999999/cancel", headers=owner_headers)
    retry_response = app_client.post("/jobs/999999/retry", headers=owner_headers)
    assert "Job 999999 no longer exists." in cancel_response.text
    assert "Job 999999 no longer exists." in retry_response.text


# -- oversized ids never 500 ------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["cancel", "retry"])
def test_an_oversized_job_id_is_refused_with_200_and_a_message_not_a_500(app_client, owner_headers, action):
    oversized = 2**64  # past SQLite's signed 64-bit range: would raise OverflowError on a raw bind
    response = app_client.post(f"/jobs/{oversized}/{action}", headers=owner_headers)
    assert response.status_code == 200
    assert f"Job {oversized} no longer exists." in response.text


def test_an_oversized_batch_query_param_is_refused_with_422_not_a_500(app_client, owner_headers):
    response = app_client.get(f"/queue/rows?batch={2**64}", headers=owner_headers)
    assert response.status_code == 422
