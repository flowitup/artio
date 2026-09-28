"""The versioned JSON API (`/api/v1`): exactly criterion 10's nine endpoints, reusing the same
services the HTML routes call (jobs, library, custom_workflows, gpu, storage) so there is only ever
one place that builds a batch, searches images or reads GPU status.

Both identities may reach every route here: the service identity (the plugin) is restricted to
exactly this allowlist by `auth.SERVICE_ROUTES`, and the owner identity, already unrestricted on
GET, is simply not blocked from it either -- a state-changing call still needs the same same-origin
check access_guard already applies to every owner POST, API included. Nothing here ever exposes
warm, stop, upload or delete: those stay owner-only HTML, matching the plan's "no plugin warm or
stop" decision.

Every error is `{"error": {"code", "message"}}`, never FastAPI's default `{"detail": ...}` shape
(register() installs the one translation needed for that: pydantic's own RequestValidationError,
raised before a handler here ever runs). Every other error path returns the envelope directly, so
the shape is uniform however it happens.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import random
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from artio import custom_workflows, db, gpu, jobs, library
from artio.registry import InvalidParams, Model, Registry, UnknownModel
from artio.storage import DiskGuardError, resolve_under
from artio.worker import Worker

router = APIRouter(prefix="/api/v1")

_MAX_ID = 2**63 - 1
_MAX_JOB_IDS_PER_REQUEST = 50
DEFAULT_IMAGES_LIMIT = 20
MAX_IMAGES_LIMIT = 50
# Bounds an id/offset value so it can never overflow sqlite3's signed 64-bit integer binding
# (OverflowError, otherwise an unhandled 500) -- the same bound the HTML routes' own Path/Query
# parameters already use (see pages.py's _MAX_PAGE and images.py's _MAX_ID).
_MAX_OFFSET = 10**12


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


async def _api_validation_handler(request: Request, exc: RequestValidationError) -> Response:
    """Scoped to /api/v1 only: every other path keeps FastAPI's own {"detail": ...} shape, which
    the HTML routes' own bounded query/path parameters already rely on (see pages.py)."""
    if request.scope["path"].startswith("/api/v1"):
        return _error(422, "validation_error", str(exc))
    return await request_validation_exception_handler(request, exc)


def register(app: FastAPI) -> None:
    app.include_router(router)
    app.add_exception_handler(RequestValidationError, _api_validation_handler)


# -- models -------------------------------------------------------------------------------------


def _model_payload(model: Model) -> dict:
    schema = model.param_schema
    return {
        "id": model.id,
        "label": model.label,
        "backend_id": model.backend_id,
        "steps": {"min": schema.steps_min, "max": schema.steps_max, "default": schema.steps_default},
        "cfg": {"min": schema.cfg_min, "max": schema.cfg_max, "default": schema.cfg_default},
        "size": {"min": schema.size_min, "max": schema.size_max, "multiple": schema.size_multiple},
        "presets": [{"name": p.name, "width": p.width, "height": p.height} for p in schema.presets],
        "default_preset": schema.default_preset,
        "resolutions": [
            {"name": t.name, "sizes": [{"name": p.name, "width": p.width, "height": p.height} for p in t.sizes or schema.presets]}
            for t in schema.tiers
        ],
        "default_resolution": schema.default_tier or None,
    }


@router.get("/models")
async def api_models(request: Request) -> Response:
    registry: Registry = request.app.state.registry
    return JSONResponse([_model_payload(m) for m in registry.models.values()])


# -- generate -------------------------------------------------------------------------------------


class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    prompt: str
    negative: str = ""
    size: str | None = None
    resolution: str | None = None
    width: int | None = None
    height: int | None = None
    steps: int | None = None
    cfg: float | None = None
    seed: int | None = None
    count: int = 1


def _size_from_body(model: Model, body: GenerateBody) -> tuple[int, int]:
    if body.size is not None:
        if body.width is not None or body.height is not None:
            raise ValueError("Specify either 'size' or 'width'/'height', not both.")
        schema = model.param_schema
        if body.resolution is not None and not any(t.name == body.resolution for t in schema.tiers):
            raise ValueError(f"Unknown resolution {body.resolution!r} for model {model.id!r}.")
        preset = schema.size(body.size, body.resolution or "")
        if preset is None:
            raise ValueError(f"Unknown size preset {body.size!r} for model {model.id!r}.")
        return preset.width, preset.height
    if body.resolution is not None:
        raise ValueError("'resolution' goes with 'size'; it does not apply to 'width'/'height'.")
    if body.width is not None and body.height is not None:
        return body.width, body.height
    if body.width is not None or body.height is not None:
        raise ValueError("Both 'width' and 'height' are required when not using 'size'.")
    default = model.param_schema.default_size()
    return default.width, default.height


def _batch_request_from_body(registry: Registry, body: GenerateBody) -> jobs.BatchRequest:
    model = registry.model(body.model)  # raises UnknownModel
    width, height = _size_from_body(model, body)
    return jobs.BatchRequest(
        model_id=model.id,
        prompt=body.prompt,
        negative=body.negative,
        width=width,
        height=height,
        steps=body.steps if body.steps is not None else model.param_schema.steps_default,
        cfg=body.cfg if body.cfg is not None else model.param_schema.cfg_default,
        seed_mode="fixed" if body.seed is not None else "random",
        seed=body.seed,
        count=body.count,
    )


def _job_ids_for_batch(conn: sqlite3.Connection, batch_id: int) -> list[int]:
    return [row["id"] for row in conn.execute("SELECT id FROM jobs WHERE batch_id = ? ORDER BY id", (batch_id,))]


@router.post("/generate", status_code=201)
async def api_generate(request: Request, body: GenerateBody) -> Response:
    settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    try:
        batch_request = _batch_request_from_body(registry, body)
        with db.session(settings) as conn:
            batch_id = jobs.create_batch(conn, registry, settings, batch_request, random.Random())
            job_ids = _job_ids_for_batch(conn, batch_id)
    except (UnknownModel, InvalidParams, ValueError) as exc:
        return _error(422, "validation_error", str(exc))
    except DiskGuardError as exc:
        return _error(507, "disk_guard", str(exc))
    return JSONResponse({"batch_id": batch_id, "job_ids": job_ids}, status_code=201)


# -- jobs -----------------------------------------------------------------------------------------


def _job_payload(row: sqlite3.Row) -> dict:
    params = json.loads(row["params_json"])
    return {
        "id": row["id"],
        "status": row["status"],
        "error": row["error"],
        "image_id": row["image_id"],
        "seed": params.get("seed"),
        "model_id": row["model_id"],
        "duration_s": row["duration_s"],
        "est_cost_usd": row["est_cost_usd"],
    }


@router.get("/jobs")
async def api_jobs(request: Request, ids: str | None = Query(default=None)) -> Response:
    settings = request.app.state.settings
    if ids is None:
        return JSONResponse([])
    try:
        id_list = [int(part) for part in ids.split(",") if part.strip()]
    except ValueError:
        return _error(422, "validation_error", "ids must be a comma-separated list of integers.")
    if not id_list:
        return JSONResponse([])
    if len(id_list) > _MAX_JOB_IDS_PER_REQUEST:
        return _error(422, "validation_error", f"at most {_MAX_JOB_IDS_PER_REQUEST} ids per request.")
    if any(not (1 <= i <= _MAX_ID) for i in id_list):
        # A real job id is always in this range (SQLite's own signed 64-bit INTEGER PRIMARY KEY, and
        # never <= 0); binding anything outside it would raise OverflowError deep in sqlite3, an
        # unhandled 500, instead of the plain "no such job" this really is.
        return _error(422, "validation_error", f"ids must each be between 1 and {_MAX_ID}.")

    placeholders = ",".join("?" * len(id_list))
    with db.session(settings) as conn:
        rows = conn.execute(
            "SELECT jobs.*, images.id AS image_id FROM jobs LEFT JOIN images ON images.job_id = jobs.id "
            f"WHERE jobs.id IN ({placeholders}) ORDER BY jobs.id",
            id_list,
        ).fetchall()
    return JSONResponse([_job_payload(row) for row in rows])


# -- images -----------------------------------------------------------------------------------------


def _trimmed_image(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "model_id": row["model_id"],
        "prompt": row["prompt"],
        "seed": row["seed"],
        "width": row["width"],
        "height": row["height"],
        "starred": bool(row["starred"]),
        "created_at": row["created_at"],
        "batch_id": row["batch_id"],
        "workflow_name": row["workflow_name"],
    }


@router.get("/images")
async def api_images(
    request: Request,
    q: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    starred: bool = Query(default=False),
    model: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_IMAGES_LIMIT, ge=1, le=MAX_IMAGES_LIMIT),
    offset: int = Query(default=0, ge=0, le=_MAX_OFFSET),
) -> Response:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        rows = library.search(conn, q=q, tag=tag, starred=starred, model=model, limit=limit, offset=offset)
    return JSONResponse([_trimmed_image(row) for row in rows])


@router.get("/images/{image_id}")
async def api_image_detail(request: Request, image_id: int = PathParam(ge=1, le=_MAX_ID)) -> Response:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        detail = library.image_detail(conn, image_id)
    if detail is None:
        return _error(404, "not_found", f"Image {image_id} not found.")
    return JSONResponse(dataclasses.asdict(detail))


def _resolve_image_file(data_dir: Path, relative: str) -> Path | None:
    """Confines a stored image path to the image store itself (data_dir/images/...), not merely to
    data_dir as a whole: `resolve_under` alone would still let a corrupted or malicious images row
    (file_png pointing at "../artio.db", say) serve the database back as "image/png". Only
    `storage.save_result` ever writes an images row, and it always writes under "images/", so this
    is defense in depth, not something a normal row could ever fail."""
    resolved = resolve_under(data_dir, relative)
    if resolved is None:
        return None
    images_root = (data_dir / "images").resolve()
    if not resolved.is_relative_to(images_root):
        return None
    return resolved


@router.get("/images/{image_id}/file")
async def api_image_file(request: Request, image_id: int = PathParam(ge=1, le=_MAX_ID)) -> Response:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        row = conn.execute("SELECT file_png, seed FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        return _error(404, "not_found", f"Image {image_id} not found.")
    path = _resolve_image_file(settings.data_dir, row["file_png"])
    if path is None or not path.is_file():
        return _error(404, "not_found", "Image file missing.")
    filename = f"artio-{image_id}-{row['seed'] if row['seed'] is not None else 'na'}.png"
    return FileResponse(path, media_type="image/png", filename=filename)


# -- workflows --------------------------------------------------------------------------------------


def _workflow_listing_from_rows(rows: list[sqlite3.Row]) -> list[dict]:
    """Parses every stored graph exactly once, off the event loop (see the caller): unlike the
    lightweight `custom_workflows.list_workflows` the web UI uses (which never touches graph_json),
    `has_seed_input` genuinely needs each graph's own content, so this can't avoid the parse
    entirely -- but it costs one query for every workflow instead of the web listing's query plus
    one extra `get_workflow` call per row, and it never blocks the loop while doing it."""
    result = []
    for row in rows:
        graph = json.loads(row["graph_json"])
        result.append(
            {
                "id": row["id"],
                "name": row["name"],
                "backend_id": row["backend_id"],
                "has_seed_input": bool(custom_workflows.seed_targets(graph)),
            }
        )
    return result


@router.get("/workflows")
async def api_workflows(request: Request) -> Response:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        rows = conn.execute(
            "SELECT id, name, backend_id, graph_json FROM workflows ORDER BY name"
        ).fetchall()
    result = await asyncio.to_thread(_workflow_listing_from_rows, rows)
    return JSONResponse(result)


class WorkflowRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed_mode: Literal["random", "fixed", "keep"] = "random"
    seed: int | None = None
    count: int = 1


@router.post("/workflows/{workflow_id}/run", status_code=201)
async def api_run_workflow(
    request: Request, body: WorkflowRunBody, workflow_id: int = PathParam(ge=1, le=_MAX_ID)
) -> Response:
    settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    with db.session(settings) as conn:
        workflow = custom_workflows.get_workflow(conn, workflow_id)
    if workflow is None:
        return _error(404, "not_found", f"Workflow {workflow_id} not found.")

    try:
        with db.session(settings) as conn:
            batch_id = jobs.create_workflow_batch(
                conn, registry, settings, workflow, body.seed_mode, body.seed, body.count, random.Random()
            )
            job_ids = _job_ids_for_batch(conn, batch_id)
    except ValueError as exc:
        return _error(422, "validation_error", str(exc))
    except DiskGuardError as exc:
        return _error(507, "disk_guard", str(exc))
    return JSONResponse({"batch_id": batch_id, "job_ids": job_ids}, status_code=201)


# -- gpu --------------------------------------------------------------------------------------------


@router.get("/gpu")
async def api_gpu_status(request: Request) -> Response:
    """Status only, computed on read exactly like the owner panel (gpu.py): no warm or stop route
    exists under /api/v1 at all, by design (see this module's docstring and auth.SERVICE_ROUTES)."""
    settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    worker: Worker = request.app.state.worker
    result = []
    with db.session(settings) as conn:
        for backend in registry.backends.values():
            status = await worker.status.get(backend, conn)
            result.append(
                {
                    "backend_id": backend.id,
                    "label": backend.label,
                    "state": gpu.display_state(status),
                    "containers": status.containers,
                    "running_inputs": status.running_inputs,
                    "backlog": status.backlog,
                    "warm_until": status.warm_until,
                    "window_open": status.window_open,
                    "unhealthy": status.unhealthy,
                }
            )
    return JSONResponse(result)
