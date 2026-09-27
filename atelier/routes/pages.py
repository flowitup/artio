"""Home redirect, gallery, batch detail and the polled header-status partial."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, RedirectResponse

from atelier import db, gpu, library, storage
from atelier.registry import Registry

router = APIRouter()

_MAX_ID = 2**63 - 1


@router.get("/")
async def home() -> RedirectResponse:
    return RedirectResponse("/generate", status_code=303)


@router.get("/gallery")
async def gallery(
    request: Request,
    model: str | None = Query(default=None),
    page: int = Query(default=1, ge=1, le=_MAX_ID),
) -> HTMLResponse:
    settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    with db.session(settings) as conn:
        groups = library.list_batches(conn, model, page)
    return request.app.state.templates.TemplateResponse(
        request,
        "gallery.html",
        {
            "groups": groups,
            "models": registry.models.values(),
            "selected_model": model,
            "page": page,
        },
    )


@router.get("/batches/{batch_id}")
async def batch_detail(request: Request, batch_id: int = PathParam(ge=1, le=_MAX_ID)) -> HTMLResponse:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        detail = library.batch_detail(conn, batch_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return request.app.state.templates.TemplateResponse(request, "batch.html", {"detail": detail})


@router.get("/partials/header-status")
async def header_status(request: Request) -> HTMLResponse:
    settings = request.app.state.settings
    worker = request.app.state.worker
    registry: Registry = request.app.state.registry
    with db.session(settings) as conn:
        disk = storage.disk_status(conn, settings)
        gpu_summaries = [await gpu.summarize(worker.status, backend, conn) for backend in registry.backends.values()]
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/header_status.html",
        {"disk": disk, "paused": worker.paused, "alerts": worker.alerts, "gpu_summaries": gpu_summaries},
    )
