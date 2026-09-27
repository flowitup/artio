"""Custom ComfyUI workflows: upload, run, download and delete.

Upload and run are plain (non-htmx) forms: success redirects with a 303, and a rejected value
re-renders the same page with the message inline at 200 -- the same rule generate.py's own POST
follows. Only the delete button is hx-post, since it just needs to refresh the stored-workflow list.
"""

from __future__ import annotations

import json
import random
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile

from artio import custom_workflows, db, jobs
from artio.registry import Registry
from artio.storage import DiskGuardError

router = APIRouter()

_MAX_ID = 2**63 - 1
_SEED_MODES = ("random", "fixed", "keep")


def _workflows_context(
    conn, registry: Registry, *, error: str | None = None, name: str = "", backend_id: str = ""
) -> dict:
    return {
        "workflows": custom_workflows.list_workflows(conn),
        "backends": list(registry.backends.values()),
        "seed_modes": _SEED_MODES,
        "error": error,
        "name": name,
        "backend_id": backend_id,
    }


@router.get("/workflows")
async def workflows_page(request: Request) -> HTMLResponse:
    settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    with db.session(settings) as conn:
        context = _workflows_context(conn, registry)
    return request.app.state.templates.TemplateResponse(request, "workflows.html", context)


@router.post("/workflows")
async def upload_workflow(request: Request) -> Response:
    settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    form = await request.form()
    name = form.get("name", "")
    backend_id = form.get("backend_id", "")
    upload = form.get("graph_file")
    # Both fields are always plain strings from a text/select input; guard anyway in case a
    # malformed multipart request sends a file part under these names instead.
    name = name if isinstance(name, str) else ""
    backend_id = backend_id if isinstance(backend_id, str) else ""

    def rerender(message: str) -> HTMLResponse:
        with db.session(settings) as conn:
            context = _workflows_context(conn, registry, error=message, name=name, backend_id=backend_id)
        return request.app.state.templates.TemplateResponse(
            request, "workflows.html", context, status_code=200
        )

    if not isinstance(upload, UploadFile):
        return rerender("Choose a workflow file (.json) to upload.")
    raw = await upload.read()

    try:
        graph = custom_workflows.validate_api_graph(raw)
        with db.session(settings) as conn:
            custom_workflows.store_workflow(conn, registry, name, backend_id, graph, time.time())
    except custom_workflows.WorkflowError as exc:
        return rerender(str(exc))

    return RedirectResponse("/workflows", status_code=303)


@router.get("/workflows/{workflow_id}/download")
async def download_workflow(request: Request, workflow_id: int = PathParam(ge=1, le=_MAX_ID)) -> Response:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        workflow = custom_workflows.get_workflow(conn, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    # The download's file name comes from the id, never the owner-chosen name: a name is free text
    # and must never be interpolated into a response header unsanitized.
    body = json.dumps(workflow.graph, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="workflow-{workflow_id}.json"'},
    )


@router.post("/workflows/{workflow_id}/run")
async def run_workflow(request: Request, workflow_id: int) -> Response:
    # workflow_id has no upper Path bound (unlike the GET download route above): a bad id here must
    # re-render this plain-form POST with a 200 and a message, never FastAPI's automatic 422.
    settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    form = await request.form()
    seed_mode = form.get("seed_mode", "random")
    seed_raw = form.get("seed", "")
    count_raw = form.get("count", "1")

    def rerender(message: str) -> HTMLResponse:
        with db.session(settings) as conn:
            context = _workflows_context(conn, registry, error=message)
        return request.app.state.templates.TemplateResponse(
            request, "workflows.html", context, status_code=200
        )

    try:
        with db.session(settings) as conn:
            workflow = custom_workflows.get_workflow(conn, workflow_id)
    except OverflowError:
        workflow = None
    if workflow is None:
        return rerender(f"Workflow {workflow_id} no longer exists.")

    if seed_mode not in _SEED_MODES:
        return rerender(f"Seed mode must be one of {_SEED_MODES}.")
    try:
        count = int(count_raw)
    except ValueError:
        return rerender(f"Count must be a whole number, got {count_raw!r}.")

    seed = None
    if seed_mode == "fixed":
        try:
            seed = int(seed_raw)
        except ValueError:
            return rerender(f"Seed must be a whole number, got {seed_raw!r}.")

    try:
        with db.session(settings) as conn:
            batch_id = jobs.create_workflow_batch(
                conn, registry, settings, workflow, seed_mode, seed, count, random.Random()
            )
    except (ValueError, DiskGuardError) as exc:
        return rerender(str(exc))

    return RedirectResponse(f"/queue?batch={batch_id}", status_code=303)


@router.post("/workflows/{workflow_id}/delete")
async def delete_workflow(request: Request, workflow_id: int) -> HTMLResponse:
    settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    with db.session(settings) as conn:
        try:
            custom_workflows.delete_workflow(conn, workflow_id)
        except (custom_workflows.UnknownWorkflow, OverflowError):
            pass  # already gone; the refreshed list below reflects the current state either way
        context = _workflows_context(conn, registry)
    return request.app.state.templates.TemplateResponse(request, "workflows.html", context)
