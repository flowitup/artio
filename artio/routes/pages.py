"""Home redirect, gallery, batch detail and the polled header-status partial."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, RedirectResponse

from artio import db, gpu, library, storage
from artio.registry import Registry

router = APIRouter()

_MAX_ID = 2**63 - 1
# (page - 1) * PAGE_SIZE must itself stay well inside SQLite's signed 64-bit range: unlike an id (a
# real row can legitimately use any value up to _MAX_ID), a page number this large is never anything
# but a hand-edited URL, so FastAPI's own 422 is the right outcome (owner decision), not a 500 from
# the offset arithmetic overflowing when it's bound as a query parameter.
_MAX_PAGE = 10**12


@router.get("/")
async def home() -> RedirectResponse:
    return RedirectResponse("/generate", status_code=303)


def _gallery_url(*, model: str | None, q: str | None, tag: str | None, starred: bool, page: int) -> str:
    """Builds a /gallery link that preserves every active filter. Uses urlencode rather than plain
    string interpolation so a search term with '&', '#', '+' or non-ASCII text survives the round trip."""
    params: dict[str, str | int] = {}
    if model:
        params["model"] = model
    if q:
        params["q"] = q
    if tag:
        params["tag"] = tag
    if starred:
        params["starred"] = "1"
    params["page"] = page
    return "/gallery?" + urlencode(params)


@router.get("/gallery")
async def gallery(
    request: Request,
    model: str | None = Query(default=None),
    q: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    starred: bool = Query(default=False),
    page: int = Query(default=1, ge=1, le=_MAX_PAGE),
) -> HTMLResponse:
    settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    with db.session(settings) as conn:
        groups, has_next = library.gallery_page(conn, model_id=model, page=page, q=q, tag=tag, starred=starred)
    url_kwargs = {"model": model, "q": q, "tag": tag, "starred": starred}
    return request.app.state.templates.TemplateResponse(
        request,
        "gallery.html",
        {
            "groups": groups,
            "models": registry.models.values(),
            "selected_model": model,
            "workflow_filter": library.WORKFLOW_MODEL_FILTER,
            "q": q or "",
            "tag": tag or "",
            "starred": starred,
            "page": page,
            "prev_url": _gallery_url(**url_kwargs, page=page - 1) if page > 1 else None,
            "next_url": _gallery_url(**url_kwargs, page=page + 1) if has_next else None,
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
