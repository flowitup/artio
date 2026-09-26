"""Queue view: live rows, cancel and retry.

Every route here answers 200. An unexpected outcome -- retrying a job that already finished, or acting
on an id that no longer exists -- is shown as an inline message in #flash via HX-Retarget, with the
(otherwise unchanged) queue panel re-sent alongside as an out-of-band swap, so htmx never silently
drops the response the way it would for a 4xx.
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response

from atelier import db
from atelier import jobs as jobs_service

router = APIRouter()

RECENT_JOBS_LIMIT = 50
PROMPT_EXCERPT_LIMIT = 60
_MAX_ID = 2**63 - 1
_MISSING_JOB_TEXT = "Job {} no longer exists."


def _prompt_excerpt(params_json: str) -> str:
    prompt = json.loads(params_json).get("prompt", "")
    if len(prompt) <= PROMPT_EXCERPT_LIMIT:
        return prompt
    return prompt[: PROMPT_EXCERPT_LIMIT - 1] + "…"


def _row_view(row, now: float) -> dict:
    params = json.loads(row["params_json"])
    end = row["finished_at"] if row["finished_at"] is not None else now
    start = row["submitted_at"] if row["submitted_at"] is not None else row["created_at"]
    return {
        "id": row["id"],
        "status": row["status"],
        "model_id": row["model_id"],
        "prompt_excerpt": _prompt_excerpt(row["params_json"]),
        "seed": params.get("seed"),
        "elapsed_s": round(max(0.0, end - start)),
        "was_submitted": row["submitted_at"] is not None,
        "error": row["error"],
        "image_id": row["image_id"],
    }


def _panel_context(conn, batch_id: int | None) -> dict:
    rows = jobs_service.list_recent(conn, batch_id=batch_id, limit=RECENT_JOBS_LIMIT)
    now = time.time()
    return {
        "jobs": [_row_view(row, now) for row in rows],
        "batch_id": batch_id,
        "active": jobs_service.has_active(conn, batch_id=batch_id),
        "oob": False,
    }


def _render_fragment(request: Request, name: str, context: dict) -> str:
    return request.app.state.templates.get_template(name).render({**context, "request": request})


def _panel_with_flash(request: Request, conn, batch_id: int | None, message: str) -> HTMLResponse:
    panel_html = _render_fragment(request, "partials/job_rows.html", {**_panel_context(conn, batch_id), "oob": True})
    flash_html = _render_fragment(request, "partials/flash.html", {"message": message})
    response = HTMLResponse(panel_html + flash_html, status_code=200)
    response.headers["HX-Retarget"] = "#flash"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@router.get("/queue")
async def queue_page(request: Request, batch: int | None = Query(default=None, ge=1, le=_MAX_ID)) -> HTMLResponse:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        context = _panel_context(conn, batch)
    return request.app.state.templates.TemplateResponse(request, "queue.html", context)


@router.get("/queue/rows")
async def queue_rows(request: Request, batch: int | None = Query(default=None, ge=1, le=_MAX_ID)) -> Response:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        context = _panel_context(conn, batch)
    status_code = 200 if context["active"] else 286
    return request.app.state.templates.TemplateResponse(
        request, "partials/job_rows.html", context, status_code=status_code
    )


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    request: Request, job_id: int, batch: int | None = Query(default=None, ge=1, le=_MAX_ID)
) -> HTMLResponse:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        try:
            jobs_service.cancel_job(conn, job_id)
        except (jobs_service.UnknownJob, OverflowError):
            # OverflowError: job_id is a plain int (no upper Path bound), so this route -- an htmx
            # target -- can still answer 200 with a message instead of FastAPI's automatic 422 for an
            # id past SQLite's 64-bit range.
            return _panel_with_flash(request, conn, batch, _MISSING_JOB_TEXT.format(job_id))
        context = _panel_context(conn, batch)
    return request.app.state.templates.TemplateResponse(request, "partials/job_rows.html", context)


@router.post("/jobs/{job_id}/retry")
async def retry_job(
    request: Request, job_id: int, batch: int | None = Query(default=None, ge=1, le=_MAX_ID)
) -> HTMLResponse:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        try:
            jobs_service.retry_job(conn, job_id)
        except (jobs_service.UnknownJob, OverflowError):
            return _panel_with_flash(request, conn, batch, _MISSING_JOB_TEXT.format(job_id))
        except jobs_service.RetryNotAllowed as exc:
            return _panel_with_flash(request, conn, batch, str(exc))
        context = _panel_context(conn, batch)
    return request.app.state.templates.TemplateResponse(request, "partials/job_rows.html", context)
