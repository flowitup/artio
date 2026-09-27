"""Queue view: live rows, cancel and retry.

Every route here answers 200. An unexpected outcome -- retrying a job that already finished, or acting
on an id that no longer exists -- is shown as an inline message in #flash via HX-Retarget, with the
(otherwise unchanged) queue panel re-sent alongside as an out-of-band swap, so htmx never silently
drops the response the way it would for a 4xx.
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, Response

from atelier import db
from atelier import jobs as jobs_service

router = APIRouter()

RECENT_JOBS_LIMIT = 50
PROMPT_EXCERPT_LIMIT = 60
_MAX_ID = 2**63 - 1
_MISSING_JOB_TEXT = "Job {} no longer exists."


def _prompt_excerpt(params_json: str) -> str:
    # A custom-workflow job's params carry a workflow name instead of a prompt (jobs.create_workflow_batch),
    # so the queue's "Prompt" column shows that instead of sitting blank.
    params = json.loads(params_json)
    prompt = params.get("prompt") or (f"workflow: {params['workflow']}" if "workflow" in params else "")
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


@router.get("/jobs/{job_id}/graph.json")
async def job_graph(request: Request, job_id: int = PathParam(ge=1, le=_MAX_ID)) -> Response:
    """The exact graph a job sent to its backend, byte-for-byte as stored: a generate job's built
    graph, or a custom-workflow job's graph with its seed override already applied.

    application/json plus an attachment disposition (id-based filename, never user text), same as
    /workflows/{id}/download: a graph is untrusted text that may itself contain "<script>"-shaped
    content (a prompt, a class_type), and must never be rendered as HTML by anything that fetches
    this URL directly. x-content-type-options: nosniff (SecurityHeadersMiddleware, every response)
    backs this up against a client that would otherwise sniff the body instead of trusting the type."""
    settings = request.app.state.settings
    with db.session(settings) as conn:
        row = conn.execute("SELECT graph_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return Response(
        content=row["graph_json"],
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="job-{job_id}-graph.json"'},
    )


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
