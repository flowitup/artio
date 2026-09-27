"""/healthz: DB reachability and background-loop freshness, reported without any Access token."""

from __future__ import annotations

import asyncio
import time

from artio import jobs as jobs_module
from artio.routes.health import _loop_status


def test_healthz_succeeds_without_a_token(app_client):
    response = app_client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_healthz_reports_loops_disabled_when_the_worker_was_never_started(app_client):
    # app_client always builds with start_worker=False; the worker's run() was never called.
    assert app_client.app.state.worker.started_at is None
    response = app_client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["loops"] == "disabled"


def test_healthz_reports_ok_once_both_loops_have_ticked_recently(app_client):
    worker = app_client.app.state.worker
    worker.started_at = time.time()
    worker.last_dispatch_tick = time.time()
    worker.last_poll_tick = time.time()
    response = app_client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["loops"] == "ok"


def test_healthz_reports_stale_loops_as_503(app_client):
    worker = app_client.app.state.worker
    stale = time.time() - 1000
    worker.started_at = stale
    worker.last_dispatch_tick = stale
    worker.last_poll_tick = stale
    response = app_client.get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["loops"] == "stale"


def test_healthz_reports_stale_when_only_one_loop_has_ticked(app_client):
    worker = app_client.app.state.worker
    worker.started_at = time.time()
    worker.last_dispatch_tick = time.time()
    # last_poll_tick stays None: a real worker always starts both loops together, so this can only
    # happen if one loop is broken.
    response = app_client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["loops"] == "stale"


def test_healthz_reports_stale_when_started_but_not_yet_ticked(app_client):
    # A worker that was started an instant ago, before either loop completed its first tick, must not
    # be reported as "disabled": that word is reserved for a worker that was never started at all.
    app_client.app.state.worker.started_at = time.time()
    response = app_client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["loops"] == "stale"


def test_healthz_reports_stale_when_the_worker_started_but_every_tick_raises(app_client, monkeypatch):
    """A real Worker.run(), with both loop functions forced to raise on every attempt: proves /healthz
    distinguishes "started but broken" (stale, 503) from "never started" (disabled, 200)."""

    def always_raise(*args, **kwargs):
        raise RuntimeError("simulated loop failure")

    monkeypatch.setattr(jobs_module, "fail_stale_queued", always_raise)
    monkeypatch.setattr(jobs_module, "list_submitted", always_raise)

    worker = app_client.app.state.worker

    async def start_and_stop() -> None:
        await worker.run()
        await asyncio.sleep(0.05)  # lets both loop tasks attempt (and fail) at least once
        await worker.stop()

    asyncio.run(start_and_stop())

    assert worker.started_at is not None
    assert worker.last_dispatch_tick is None
    assert worker.last_poll_tick is None

    response = app_client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["loops"] == "stale"


def test_loop_status_is_disabled_only_when_started_at_is_none():
    class _StubWorker:
        started_at = None
        last_dispatch_tick = 12345.0
        last_poll_tick = 12345.0

    assert _loop_status(_StubWorker(), now=12345.0) == "disabled"


def test_healthz_reports_the_configured_version(app_client, settings):
    response = app_client.get("/healthz")
    assert response.json()["version"] == settings.version
