"""Worker: the dispatcher and poller loops, driven directly (dispatch_once()/poll_once()), never sleeping."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import time

import modal.exception
import pytest
from PIL import Image

from atelier import db, jobs, storage
from atelier.worker import Worker


class _Clock:
    """A controllable clock so timeout/stale-queue tests never need to sleep for real. Defaults to the
    real current time, not a fixed timestamp, so created_at values written by create_batch() (which
    always uses the real wall clock) stay comparable to this clock's "now" once a test advances it."""

    def __init__(self, start: float | None = None) -> None:
        self.now = start if start is not None else time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _request(**overrides) -> jobs.BatchRequest:
    fields = {
        "model_id": "qwen-image-2.1-uc",
        "prompt": "a red fox in snow",
        "negative": "no text",
        "width": 1088,
        "height": 1920,
        "steps": 25,
        "cfg": 1.0,
        "seed_mode": "fixed",
        "seed": 42,
        "count": 1,
    }
    fields.update(overrides)
    return jobs.BatchRequest(**fields)


def _sixteen_bit_png() -> bytes:
    im = Image.new("I;16", (32, 32), 2000)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# -- happy path -----------------------------------------------------------------------------------------


def test_job_moves_from_queued_to_submitted_to_done(conn, registry, settings, fake_gateway, rng, png_bytes):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()

    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    asyncio.run(worker.dispatch_once())

    job = conn.execute("SELECT * FROM jobs").fetchone()
    assert job["status"] == "submitted"
    assert job["call_id"] is not None

    clock.advance(16.0)
    fake_gateway.finish(job["call_id"], png_bytes)
    asyncio.run(worker.poll_once())

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "done"
    assert row["duration_s"] == pytest.approx(16.0)
    assert row["est_cost_usd"] == pytest.approx(16.0 * 1.95 / 3600)

    image = conn.execute("SELECT * FROM images WHERE job_id = ?", (job["id"],)).fetchone()
    params = json.loads(job["params_json"])
    assert image["model_id"] == "qwen-image-2.1-uc"
    assert image["prompt"] == "a red fox in snow"
    assert image["negative"] == "no text"
    assert image["seed"] == params["seed"] == 42
    assert image["width"] == 64  # the actual PNG's dimensions (png_bytes fixture), not the request's
    assert image["height"] == 64
    assert params["steps"] == 25
    assert params["cfg"] == 1.0


def test_dispatch_once_spawns_at_most_max_inflight_jobs_per_backend(conn, registry, settings, fake_gateway, rng):
    jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=8), rng)
    conn.commit()

    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())

    counts = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    by_status = {row["status"]: row["n"] for row in counts}
    assert by_status.get("submitted") == 4
    assert by_status.get("queued") == 4

    asyncio.run(worker.dispatch_once())  # capacity is full: must not spawn a 5th
    counts = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    by_status = {row["status"]: row["n"] for row in counts}
    assert by_status.get("submitted") == 4
    assert by_status.get("queued") == 4

    # A full backend still says why the rest are waiting, instead of leaving them silently queued.
    reason = conn.execute("SELECT error FROM jobs WHERE status = 'queued' LIMIT 1").fetchone()["error"]
    assert reason == "all 4 slots busy"
    assert worker.paused["qwen21-uc"] == "all 4 slots busy"


def test_queued_job_waiting_only_on_a_full_backend_fails_after_thirty_minutes_with_that_reason(
    conn, registry, settings, fake_gateway, rng
):
    jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=5), rng)
    conn.commit()

    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    asyncio.run(worker.dispatch_once())  # tick 1: 4 submitted (free was 4 before spawning)
    asyncio.run(worker.dispatch_once())  # tick 2: free is now 0, so the busy reason gets recorded

    clock.advance(1801)  # past the 30-minute queue limit; the backend is still fully busy
    asyncio.run(worker.dispatch_once())  # tick 3: fails with the reason recorded in tick 2

    row = conn.execute("SELECT status, error FROM jobs WHERE status = 'failed'").fetchone()
    assert row is not None
    assert "all 4 slots busy" in row["error"]


def test_batch_of_four_creates_four_jobs_with_distinct_seeds_and_all_reach_done(
    conn, registry, settings, fake_gateway, rng, png_bytes
):
    batch_id = jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=4), rng)
    conn.commit()
    seeds = {
        json.loads(row["params_json"])["seed"]
        for row in conn.execute("SELECT params_json FROM jobs WHERE batch_id = ?", (batch_id,))
    }
    assert len(seeds) == 4

    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())  # max_inflight=4: all four fit in one tick

    call_ids = [row["call_id"] for row in conn.execute("SELECT call_id FROM jobs WHERE batch_id = ?", (batch_id,))]
    assert all(call_ids)
    for call_id in call_ids:
        fake_gateway.finish(call_id, png_bytes)
    asyncio.run(worker.poll_once())

    statuses = [row["status"] for row in conn.execute("SELECT status FROM jobs WHERE batch_id = ?", (batch_id,))]
    assert statuses == ["done"] * 4


def test_a_finished_result_is_stored_even_after_the_timeout_has_passed(
    conn, registry, settings, fake_gateway, rng, png_bytes
):
    """The timeout applies only while a call is pending. A result that actually finished on Modal
    during a long outage must still be read and stored, not thrown away just because nobody polled it
    in time -- and a completely fresh Worker (simulating a restart) must reach the same outcome."""
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()

    clock = _Clock()
    dispatcher = Worker(settings, registry, fake_gateway, clock=clock)
    asyncio.run(dispatcher.dispatch_once())
    job = conn.execute("SELECT * FROM jobs").fetchone()

    fake_gateway.finish(job["call_id"], png_bytes)
    clock.advance(settings.job_timeout_s * 10)  # a long outage: way past the timeout

    fresh_worker = Worker(settings, registry, fake_gateway, clock=clock)
    asyncio.run(fresh_worker.poll_once())

    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "done"
    assert conn.execute("SELECT * FROM images WHERE job_id = ?", (job["id"],)).fetchone() is not None
    assert fake_gateway.calls == []  # still no Modal cancel anywhere on this path


# -- restart and resumption --------------------------------------------------------------------------------


def test_submitted_job_completes_after_worker_restart(conn, registry, settings, fake_gateway, rng, png_bytes):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()

    worker_before_restart = Worker(settings, registry, fake_gateway)
    asyncio.run(worker_before_restart.dispatch_once())

    job = conn.execute("SELECT * FROM jobs").fetchone()
    assert job["status"] == "submitted"
    fake_gateway.finish(job["call_id"], png_bytes)

    # A brand-new Worker, same DB, same fake backend state: poll_once() finds the job purely by
    # querying status='submitted', with no in-memory state carried over from the old instance.
    worker_after_restart = Worker(settings, registry, fake_gateway)
    asyncio.run(worker_after_restart.poll_once())

    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "done"


# -- isolation of one bad result --------------------------------------------------------------------------


def test_unstorable_result_fails_only_its_own_job(conn, registry, settings, fake_gateway, png_bytes):
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway, clock=lambda: 0.0)

    def seed_submitted(seed: int) -> int:
        batch_id = conn.execute(
            "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) "
            "VALUES (0, 'qwen-image-2.1-uc', 'generate', '{}', 1)"
        ).lastrowid
        params = json.dumps({"prompt": "p", "negative": "", "seed": seed})
        job_id = conn.execute(
            "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, status, "
            "created_at, call_id, submitted_at) VALUES (?, 'qwen-image-2.1-uc', ?, 'generate', ?, '{}', "
            "'submitted', 0, NULL, 0)",
            (batch_id, backend.id, params),
        ).lastrowid
        call_id = asyncio.run(fake_gateway.spawn_workflow(backend, {}))
        conn.execute("UPDATE jobs SET call_id = ? WHERE id = ?", (call_id, job_id))
        return job_id, call_id

    normal_id, normal_call = seed_submitted(1)
    oversized_id, oversized_call = seed_submitted(2)
    sixteen_bit_id, sixteen_bit_call = seed_submitted(3)
    conn.commit()

    fake_gateway.finish(normal_call, png_bytes)
    fake_gateway.finish(oversized_call, b"\0" * (65 * 2**20))
    fake_gateway.finish(sixteen_bit_call, _sixteen_bit_png())

    asyncio.run(worker.poll_once())

    normal_row = conn.execute("SELECT status FROM jobs WHERE id = ?", (normal_id,)).fetchone()
    oversized_row = conn.execute("SELECT status, error FROM jobs WHERE id = ?", (oversized_id,)).fetchone()
    sixteen_bit_row = conn.execute("SELECT status FROM jobs WHERE id = ?", (sixteen_bit_id,)).fetchone()

    assert normal_row["status"] == "done"
    assert oversized_row["status"] == "failed"
    assert "MB" in oversized_row["error"]
    assert sixteen_bit_row["status"] == "done"

    thumb_path = conn.execute("SELECT file_thumb FROM images WHERE job_id = ?", (sixteen_bit_id,)).fetchone()
    with Image.open(settings.data_dir / thumb_path["file_thumb"]) as thumb:
        assert thumb.mode == "RGB"


# -- cancel and timeout never call Modal's cancel -------------------------------------------------------------


def test_a_failed_poll_stores_the_exact_comfyui_error_text(conn, registry, settings, fake_gateway, rng):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()

    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs").fetchone()

    comfyui_text = "ComfyUI rejected workflow: " + json.dumps({"node_errors": {"6": "bad seed"}})
    fake_gateway.fail(job["call_id"], comfyui_text)
    asyncio.run(worker.poll_once())

    row = conn.execute("SELECT status, error FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "failed"
    assert row["error"] == comfyui_text


def test_cancelling_a_running_job_discards_its_late_result_without_a_modal_cancel(
    conn, registry, settings, fake_gateway, rng, png_bytes
):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()

    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs").fetchone()

    jobs.cancel_job(conn, job["id"])
    conn.commit()

    fake_gateway.finish(job["call_id"], png_bytes)
    asyncio.run(worker.poll_once())

    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "cancelled"
    assert conn.execute("SELECT * FROM images WHERE job_id = ?", (job["id"],)).fetchone() is None
    assert fake_gateway.calls == []  # no Modal cancel was ever issued

    # The late result was written to disk (save_result ran before complete() saw the cancellation) and
    # then discarded: no file for this job should be left behind anywhere under images/.
    leftover = list((settings.data_dir / "images").rglob(f"job-{job['id']}.*"))
    assert leftover == []


def test_timed_out_job_fails_without_a_modal_cancel(conn, registry, settings, fake_gateway, rng):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()

    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs").fetchone()

    clock.advance(settings.job_timeout_s + 1)
    asyncio.run(worker.poll_once())

    row = conn.execute("SELECT status, error FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "failed"
    assert "timed out" in row["error"]
    assert fake_gateway.calls == []  # no Modal cancel was ever issued


def test_timeout_text_carries_the_last_transient_reason(conn, registry, settings, fake_gateway, rng):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()

    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs").fetchone()

    fake_gateway.stay_pending_with_reason(job["call_id"], "Modal unavailable, retrying: connection reset")
    asyncio.run(worker.poll_once())  # records the transient reason; still well within the timeout

    clock.advance(settings.job_timeout_s + 1)
    asyncio.run(worker.poll_once())  # now times out, carrying that reason along

    row = conn.execute("SELECT status, error FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "failed"
    assert "timed out" in row["error"]
    assert "connection reset" in row["error"]


# -- one bad job's cleanup never blocks the others ----------------------------------------------------------


def test_orphaned_files_are_removed_when_something_fails_after_save_result(
    conn, registry, settings, fake_gateway, rng, png_bytes, monkeypatch
):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()

    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs").fetchone()
    fake_gateway.finish(job["call_id"], png_bytes)

    def broken_complete(*args, **kwargs):
        raise RuntimeError("simulated failure writing the images row")

    monkeypatch.setattr(jobs, "complete", broken_complete)
    asyncio.run(worker.poll_once())  # save_result() succeeds; jobs.complete() then blows up

    # The files save_result() just wrote must not survive: nothing recorded them, so the disk guard
    # could never see them again.
    leftover = list((settings.data_dir / "images").rglob(f"job-{job['id']}.*"))
    assert leftover == []
    # The job itself is still "submitted": an infrastructure error here is not a store failure that
    # should immediately fail the job (the outer poll_once() handler logs and retries next tick).
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "submitted"


def test_a_cleanup_failure_for_one_job_does_not_starve_a_normal_job_in_the_same_tick(
    conn, registry, settings, fake_gateway, rng, png_bytes, monkeypatch
):
    jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=2), rng)
    conn.commit()
    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())

    rows = conn.execute("SELECT id, call_id FROM jobs ORDER BY id").fetchall()
    broken_job_id, broken_call = rows[0]["id"], rows[0]["call_id"]
    normal_job_id, normal_call = rows[1]["id"], rows[1]["call_id"]

    fake_gateway.fail(broken_call, "backend rejected the workflow")
    fake_gateway.finish(normal_call, png_bytes)

    real_remove = storage.remove_partial_files
    def flaky_remove(data_dir, job_id):
        if job_id == broken_job_id:
            raise OSError("simulated failure while cleaning up")
        return real_remove(data_dir, job_id)

    monkeypatch.setattr(storage, "remove_partial_files", flaky_remove)
    asyncio.run(worker.poll_once())

    broken_row = conn.execute("SELECT status, error FROM jobs WHERE id = ?", (broken_job_id,)).fetchone()
    normal_row = conn.execute("SELECT status FROM jobs WHERE id = ?", (normal_job_id,)).fetchone()
    # The failing cleanup is logged and swallowed; fail_submitted still runs (it's a separate,
    # independently guarded step), so the job's own error text survives untouched.
    assert broken_row["status"] == "failed"
    assert broken_row["error"] == "backend rejected the workflow"
    assert normal_row["status"] == "done"  # the other job in the same tick was never affected


def test_generic_storage_fault_fails_the_job_only_after_three_ticks(
    conn, registry, settings, fake_gateway, rng, png_bytes, monkeypatch
):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs").fetchone()
    fake_gateway.finish(job["call_id"], png_bytes)

    def broken_save_result(*args, **kwargs):
        raise RuntimeError("simulated generic storage fault")

    monkeypatch.setattr(storage, "save_result", broken_save_result)

    asyncio.run(worker.poll_once())  # strike 1
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (job["id"],)).fetchone()["status"] == "submitted"

    asyncio.run(worker.poll_once())  # strike 2
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (job["id"],)).fetchone()["status"] == "submitted"

    asyncio.run(worker.poll_once())  # strike 3: gives up
    row = conn.execute("SELECT status, error FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "failed"
    assert "Could not store the result" in row["error"]


def test_a_persistent_failure_after_saving_fails_the_job_after_three_ticks(
    conn, registry, settings, fake_gateway, rng, png_bytes, monkeypatch
):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs").fetchone()
    fake_gateway.finish(job["call_id"], png_bytes)

    def broken_complete(*args, **kwargs):
        raise RuntimeError("simulated failure while recording the image")

    monkeypatch.setattr(jobs, "complete", broken_complete)

    for _ in range(2):
        asyncio.run(worker.poll_once())
        assert conn.execute("SELECT status FROM jobs WHERE id = ?", (job["id"],)).fetchone()["status"] == "submitted"

    asyncio.run(worker.poll_once())  # strike 3: the counter must survive failures after the save
    row = conn.execute("SELECT status, error FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "failed"
    assert "simulated failure while recording the image" in row["error"]
    assert not list(settings.data_dir.glob(f"images/**/job-{job['id']}.*")), "saved files must not be orphaned"


# -- tick timestamps -----------------------------------------------------------------------------------------


def test_last_dispatch_tick_is_set_only_after_a_completed_tick(conn, registry, settings, fake_gateway, rng, monkeypatch):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    worker = Worker(settings, registry, fake_gateway)
    assert worker.last_dispatch_tick is None

    def broken_fail_stale_queued(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs, "fail_stale_queued", broken_fail_stale_queued)
    with pytest.raises(RuntimeError):
        asyncio.run(worker.dispatch_once())
    assert worker.last_dispatch_tick is None  # the tick never completed

    monkeypatch.undo()
    asyncio.run(worker.dispatch_once())
    assert worker.last_dispatch_tick is not None  # a normal tick does set it


def test_last_poll_tick_is_set_only_after_a_completed_tick(conn, registry, settings, fake_gateway, monkeypatch):
    worker = Worker(settings, registry, fake_gateway)
    assert worker.last_poll_tick is None

    def broken_list_submitted(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs, "list_submitted", broken_list_submitted)
    with pytest.raises(RuntimeError):
        asyncio.run(worker.poll_once())
    assert worker.last_poll_tick is None

    monkeypatch.undo()
    asyncio.run(worker.poll_once())
    assert worker.last_poll_tick is not None


def test_run_keeps_looping_after_a_tick_raises(registry, settings, fake_gateway, monkeypatch):
    db.migrate(settings)  # run()'s loops open real sessions against the database
    attempts = {"n": 0}
    real_fail_stale_queued = jobs.fail_stale_queued

    def flaky_once(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("simulated failure on the very first tick")
        return real_fail_stale_queued(*args, **kwargs)

    monkeypatch.setattr(jobs, "fail_stale_queued", flaky_once)

    # Tiny intervals: this test proves run()'s loop survives an exception and keeps ticking, which
    # needs real scheduling, not dispatch_once()/poll_once() called directly.
    worker = Worker(settings, registry, fake_gateway, dispatch_interval_s=0.01, poll_interval_s=0.01)

    async def go():
        await worker.run()
        for _ in range(500):
            if worker.last_dispatch_tick is not None:
                break
            await asyncio.sleep(0.01)
        await worker.stop()

    asyncio.run(go())
    assert worker.last_dispatch_tick is not None  # a later tick succeeded despite the first one raising
    assert attempts["n"] >= 2


# -- spawn errors -----------------------------------------------------------------------------------------


def test_permanent_spawn_error_fails_the_job_with_its_text(conn, registry, settings, fake_gateway, rng):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    fake_gateway.raise_on_spawn(modal.exception.NotFoundError("app qwen21-uc is not deployed"))

    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())

    row = conn.execute("SELECT status, error FROM jobs").fetchone()
    assert row["status"] == "failed"
    assert "app qwen21-uc is not deployed" in row["error"]
    assert "qwen21-uc" in worker.alerts
    assert "app qwen21-uc is not deployed" in worker.alerts["qwen21-uc"]


def test_alert_is_cleared_by_the_next_successful_spawn_in_the_same_tick(conn, registry, settings, fake_gateway, rng):
    jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=2), rng)
    conn.commit()
    fake_gateway.raise_on_spawn(modal.exception.NotFoundError("app qwen21-uc is not deployed"))

    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())  # job 1 fails permanently (sets the alert); job 2 spawns fine

    statuses = {row["status"] for row in conn.execute("SELECT status FROM jobs")}
    assert statuses == {"failed", "submitted"}
    assert "qwen21-uc" not in worker.alerts


def test_resource_exhausted_error_is_transient_like_a_connection_error(conn, registry, settings, fake_gateway, rng):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    fake_gateway.raise_on_spawn(modal.exception.ResourceExhaustedError("workspace spend limit reached"))

    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())

    row = conn.execute("SELECT status, error FROM jobs").fetchone()
    assert row["status"] == "queued"
    assert "workspace spend limit reached" in row["error"]


def test_paused_keeps_showing_the_backoff_reason_across_ticks_inside_the_window(
    conn, registry, settings, fake_gateway, rng
):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    fake_gateway.raise_on_spawn(modal.exception.ConnectionError("no route to Modal"))

    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    asyncio.run(worker.dispatch_once())  # tick 1: the spawn fails, backoff starts
    first_reason = worker.paused["qwen21-uc"]
    assert first_reason is not None and "no route to Modal" in first_reason

    clock.advance(0.5)  # still well inside the backoff window (starts at 2s)
    asyncio.run(worker.dispatch_once())  # tick 2: no new spawn attempt, but the reason must persist

    assert worker.paused["qwen21-uc"] == first_reason  # by value, not just "still truthy"
    assert fake_gateway.spawn_count == 0  # honored: no spawn was attempted while backing off
    row = conn.execute("SELECT status FROM jobs").fetchone()
    assert row["status"] == "queued"


def test_spawn_backoff_grows_and_is_honored_until_it_elapses(conn, registry, settings, fake_gateway, rng):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)

    fake_gateway.raise_on_spawn(modal.exception.ConnectionError("x"))
    asyncio.run(worker.dispatch_once())
    first_wait = worker._retry_at["qwen21-uc"] - clock.now
    assert first_wait == pytest.approx(2.0)  # the initial backoff

    clock.advance(first_wait + 0.01)  # let the first window elapse
    fake_gateway.raise_on_spawn(modal.exception.ConnectionError("x"))
    asyncio.run(worker.dispatch_once())
    # The backoff having grown at all proves a second attempt actually reached the gateway and failed
    # again: _back_off() is only ever called from inside a failed spawn attempt.
    second_wait = worker._retry_at["qwen21-uc"] - clock.now
    assert second_wait == pytest.approx(4.0)  # doubled

    clock.advance(second_wait + 0.01)  # let the second window elapse too: this attempt is not scripted
    asyncio.run(worker.dispatch_once())  # to fail, so it succeeds normally
    assert fake_gateway.spawn_count == 1  # the only spawn that ever actually succeeds
    row = conn.execute("SELECT status FROM jobs").fetchone()
    assert row["status"] == "submitted"  # the third attempt was honored (not skipped)
    assert "qwen21-uc" not in worker._backoff_s  # cleared on the successful spawn


def test_transient_spawn_error_keeps_the_job_queued_with_a_visible_reason(conn, registry, settings, fake_gateway, rng):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    fake_gateway.raise_on_spawn(modal.exception.ConnectionError("no route to Modal"))

    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())

    row = conn.execute("SELECT status, error FROM jobs").fetchone()
    assert row["status"] == "queued"
    assert row["error"] is not None
    assert "no route to Modal" in row["error"]
    assert worker.paused["qwen21-uc"] is not None


def test_queued_job_fails_after_thirty_minutes_with_its_last_reason(conn, registry, settings, fake_gateway):
    clock = _Clock()
    batch_id = conn.execute(
        "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) "
        "VALUES (?, 'qwen-image-2.1-uc', 'generate', '{}', 1)",
        (clock.now - 3600,),
    ).lastrowid
    job_id = conn.execute(
        "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, status, "
        "created_at, error) VALUES (?, 'qwen-image-2.1-uc', 'qwen21-uc', 'generate', '{}', '{}', "
        "'queued', ?, ?)",
        (batch_id, clock.now - 3600, "Modal unavailable, retrying: boom"),
    ).lastrowid
    conn.commit()

    worker = Worker(settings, registry, fake_gateway, clock=clock)
    asyncio.run(worker.dispatch_once())

    row = conn.execute("SELECT status, error FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "failed"
    assert "not dispatched" in row["error"]
    assert "boom" in row["error"]


# -- disk guard pauses dispatch, per backend --------------------------------------------------------------


def test_dispatch_pauses_a_backend_when_the_disk_guard_is_tripped(conn, registry, settings, fake_gateway, rng):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()

    # A floor far above any real machine's free space trips deterministically off the real filesystem.
    huge_floor_settings = dataclasses.replace(settings, min_free_gb=100_000_000)
    worker = Worker(huge_floor_settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())

    row = conn.execute("SELECT status, error FROM jobs").fetchone()
    assert row["status"] == "queued"
    assert row["error"] is not None
    assert worker.paused["qwen21-uc"] is not None


# -- the per-backend lock is held across check -> spawn -> record --------------------------------------------


class _SlowSpawnGateway:
    """Wraps a FakeModalGateway with an artificial delay in spawn_workflow, widening the race window a
    missing lock would need to double-spawn the same job."""

    def __init__(self, inner):
        self._inner = inner

    async def spawn_workflow(self, backend, graph):
        await asyncio.sleep(0.02)
        return await self._inner.spawn_workflow(backend, graph)

    async def poll(self, call_id):
        return await self._inner.poll(call_id)

    async def cancel(self, call_id, *, terminate_containers=False):
        await self._inner.cancel(call_id, terminate_containers=terminate_containers)

    def invalidate(self, backend):
        self._inner.invalidate(backend)


def test_dispatch_lock_prevents_a_concurrent_tick_from_double_spawning(conn, registry, settings, fake_gateway, rng):
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()

    worker = Worker(settings, registry, _SlowSpawnGateway(fake_gateway))

    async def run_two_ticks_concurrently():
        await asyncio.gather(worker.dispatch_once(), worker.dispatch_once())

    asyncio.run(run_two_ticks_concurrently())

    assert fake_gateway.spawn_count == 1
    statuses = [row["status"] for row in conn.execute("SELECT status FROM jobs")]
    assert statuses == ["submitted"]


# -- run()/stop() lifecycle -------------------------------------------------------------------------------


def test_run_starts_loops_that_stop_cleanly_cancels(registry, settings, fake_gateway):
    worker = Worker(settings, registry, fake_gateway)

    async def start_and_stop():
        await worker.run()
        assert len(worker._tasks) == 2
        await asyncio.sleep(0.01)
        await worker.stop()
        assert worker._tasks == []

    asyncio.run(start_and_stop())
