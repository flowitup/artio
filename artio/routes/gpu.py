"""GPU status, warm-up and stop: owner-only HTML, always 200. Never under /api/v1 -- the plugin's
service identity reads status only (a later phase), and can never warm or stop a backend (enforced
by auth.py's SERVICE_ROUTES allowlist staying empty for these paths).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from artio import db, gpu
from artio.registry import Backend, Registry
from artio.worker import Worker

router = APIRouter()

_WARM_MINUTES = (5, 15, 30)


async def _flash(request: Request, message: str) -> HTMLResponse:
    """A refusal re-renders the whole panel with the message inside it. The GPU forms swap
    #gpu-panel itself, so a bare message would replace the panel, its buttons and its poll."""
    context = await _panel_context(request)
    context["notice"] = message
    return request.app.state.templates.TemplateResponse(request, "partials/gpu_panel.html", context)


def _backend_or_none(registry: Registry, backend_id: str) -> Backend | None:
    return registry.backends.get(backend_id)


async def _card(worker: Worker, conn, backend: Backend, *, force: bool = False) -> dict:
    now = time.time()
    status = await worker.status.get(backend, conn, force=force)
    worker.note_runners(backend.id, status.containers, now)
    warm_since = worker.warm_since.get(backend.id)
    return {
        "backend": backend,
        "status": status,
        "state": gpu.display_state(status),
        "warm_minutes": _WARM_MINUTES,
        "window_costs": {
            m: gpu.window_cost_estimate(m, backend.usd_per_hour, cold=status.containers == 0)
            for m in _WARM_MINUTES
        },
        "running_cost": gpu.running_cost_estimate(warm_since, now, backend.usd_per_hour)
        if warm_since is not None
        else None,
        # Kept server-side on the Worker (see recent_stop_outcome) and shown on every panel read
        # for a while, not just the one right after the POST: the panel's own poll and a form POST
        # race for the same #gpu-panel target, so a one-shot render-only-once outcome gets dropped.
        "stop_outcome": worker.recent_stop_outcome(backend.id),
    }


async def _panel_context(request: Request) -> dict:
    settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    worker: Worker = request.app.state.worker
    with db.session(settings) as conn:
        cards = [await _card(worker, conn, backend) for backend in registry.backends.values()]
    return {"cards": cards}


async def _render_panel(request: Request) -> HTMLResponse:
    context = await _panel_context(request)
    return request.app.state.templates.TemplateResponse(request, "partials/gpu_panel.html", context)


@router.get("/gpu")
async def gpu_page(request: Request) -> HTMLResponse:
    context = await _panel_context(request)
    return request.app.state.templates.TemplateResponse(request, "gpu.html", context)


@router.get("/gpu/panel")
async def gpu_panel(request: Request) -> HTMLResponse:
    return await _render_panel(request)


@router.post("/gpu/{backend_id}/warm")
async def warm_backend(request: Request, backend_id: str) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    worker: Worker = request.app.state.worker
    settings = request.app.state.settings
    backend = _backend_or_none(registry, backend_id)
    if backend is None:
        return await _flash(request, f"Unknown backend {backend_id!r}.")

    # Parsed by hand, like every other owner HTML form here: a bad value re-renders with a 200 and
    # a message instead of FastAPI's automatic 422.
    form = await request.form()
    try:
        minutes = int(form.get("minutes", ""))
    except ValueError:
        return await _flash(request, "Minutes must be a whole number.")
    if minutes not in _WARM_MINUTES:
        return await _flash(request, f"Minutes must be one of {_WARM_MINUTES}.")

    with db.session(settings) as conn:
        # A forced, not up-to-60s-stale read: warming a backend that just stopped must be refused
        # immediately, not based on a cached "deployed" that's no longer true.
        status = await worker.status.get(backend, conn, force=True)
        if status.error:
            # An unknown status must never be trusted as "deployed": that would silently spawn a
            # ping Modal is very likely to also refuse. Refuse up front with the real reason instead.
            return await _flash(request, f"Status unavailable: {status.error}")
        try:
            gpu.start_warm(
                conn, backend.id, minutes, time.time(), deployed=status.app_state.state == "deployed"
            )
        except gpu.BackendStopped:
            return await _flash(request, f"{backend.label} is stopped: deploy it before warming it up.")
    worker.ensure_pinger(backend.id)

    return await _render_panel(request)


@router.post("/gpu/{backend_id}/stop")
async def stop_backend(request: Request, backend_id: str) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    worker: Worker = request.app.state.worker
    backend = _backend_or_none(registry, backend_id)
    if backend is None:
        return await _flash(request, f"Unknown backend {backend_id!r}.")

    form = await request.form()
    confirm = form.get("confirm") == "1"
    outcome = await worker.stop_backend(backend, confirm=confirm)

    if outcome.kind == "needs_confirmation":
        return request.app.state.templates.TemplateResponse(
            request,
            "partials/gpu_stop_confirm.html",
            {"backend": backend, "queued": outcome.queued, "running": outcome.running},
        )
    return await _render_panel(request)
