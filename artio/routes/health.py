"""Liveness: DB reachability, the production volume sentinel, and background-loop freshness.

A release whose dispatcher or poller loop died still answers HTTP requests, so a plain "does it
respond" check would miss it. /healthz instead reports the age of the worker's last completed tick of
each loop, which is what a deploy's health check relies on to catch a broken release.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from artio import __version__, db
from artio.config import VOLUME_SENTINEL_NAME

router = APIRouter()

STALE_AFTER_S = 30


def _loop_status(worker, now: float) -> str:
    if worker.started_at is None:
        return "disabled"  # the worker was never started: true only in tests (start_worker=False)
    if worker.last_dispatch_tick is None or worker.last_poll_tick is None:
        return "stale"  # started, but a loop has not completed even one tick
    fresh = (
        now - worker.last_dispatch_tick <= STALE_AFTER_S and now - worker.last_poll_tick <= STALE_AFTER_S
    )
    return "ok" if fresh else "stale"


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    worker = request.app.state.worker

    db_ok = True
    try:
        with db.session(settings) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:  # noqa: BLE001 -- any failure here means "the database is unreachable"
        db_ok = False

    sentinel_ok = True
    if settings.is_production:
        sentinel_ok = (settings.data_dir / VOLUME_SENTINEL_NAME).exists()

    loops = _loop_status(worker, time.time())
    healthy = db_ok and sentinel_ok and loops in ("ok", "disabled")

    body = {"status": "ok" if healthy else "degraded", "version": settings.version, "release": __version__, "loops": loops}
    return JSONResponse(body, status_code=200 if healthy else 503)
