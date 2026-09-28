"""Generate form: model and size picker, batch submission, and the remix prefill.

The form is parsed by hand from the raw string values (never FastAPI's `Form(...)` type coercion), so
a bad value re-renders the page with a 200 and a message instead of FastAPI's automatic 422 -- HTML
handlers here never answer with a status code for an expected, correctable condition.
"""

from __future__ import annotations

import json
import random

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from artio import db, jobs, library
from artio.registry import Model, Registry, UnknownModel
from artio.storage import DiskGuardError

router = APIRouter()

_MAX_ID = 2**63 - 1


class _FormError(Exception):
    """Raised for a submitted value the form itself must reject, before the engine ever sees it."""


def _first_model(registry: Registry) -> Model:
    return next(iter(registry.models.values()))


def _model_from_form(registry: Registry, form: dict) -> Model:
    model_id = form.get("model_id", "")
    try:
        return registry.model(model_id)
    except UnknownModel:
        raise _FormError(f"Unknown model {model_id!r}") from None


def _parse_int(raw: str, field: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise _FormError(f"{field} must be a whole number, got {raw!r}") from None


def _parse_float(raw: str, field: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise _FormError(f"{field} must be a number, got {raw!r}") from None


def _size_from_form(model: Model, form: dict) -> tuple[int, int]:
    preset_name = form.get("preset", "")
    if preset_name == "custom":
        return _parse_int(form.get("width", ""), "width"), _parse_int(form.get("height", ""), "height")
    schema = model.param_schema
    tier = form.get("tier", "")
    if tier and not any(t.name == tier for t in schema.tiers):
        raise _FormError(f"Unknown resolution {tier!r}")
    preset = schema.size(preset_name, tier)
    if preset is None:
        raise _FormError(f"Unknown size preset {preset_name!r}")
    return preset.width, preset.height


def _seed_from_form(form: dict) -> tuple[str, int | None]:
    seed_mode = form.get("seed_mode", "random")
    if seed_mode not in ("random", "fixed"):
        raise _FormError(f"seed mode must be 'random' or 'fixed', got {seed_mode!r}")
    if seed_mode == "random":
        return seed_mode, None
    return seed_mode, _parse_int(form.get("seed", ""), "seed")


def _batch_request_from_form(registry: Registry, form: dict) -> jobs.BatchRequest:
    model = _model_from_form(registry, form)
    width, height = _size_from_form(model, form)
    seed_mode, seed = _seed_from_form(form)
    return jobs.BatchRequest(
        model_id=model.id,
        prompt=form.get("prompt", ""),
        negative=form.get("negative", ""),
        width=width,
        height=height,
        steps=_parse_int(form.get("steps", ""), "steps"),
        cfg=_parse_float(form.get("cfg", ""), "cfg"),
        seed_mode=seed_mode,
        seed=seed,
        count=_parse_int(form.get("count", "1"), "count"),
    )


def _remix_values(conn, image_id: int) -> dict | None:
    """Prefills the form from a past image's own job params, in fixed seed mode."""
    row = conn.execute(
        "SELECT images.model_id, jobs.params_json FROM images JOIN jobs ON jobs.id = images.job_id "
        "WHERE images.id = ?",
        (image_id,),
    ).fetchone()
    if row is None:
        return None
    params = json.loads(row["params_json"])
    return {
        "model_id": row["model_id"],
        "prompt": params.get("prompt", ""),
        "negative": params.get("negative", ""),
        "preset": "custom",
        "width": params.get("width"),
        "height": params.get("height"),
        "steps": params.get("steps"),
        "cfg": params.get("cfg"),
        "seed_mode": "fixed",
        "seed": params.get("seed"),
        "count": 1,
    }


def _preset_values(conn, preset_id: int) -> dict | None:
    """Prefills the form from a saved preset: its own model plus its stored prompt/size/steps/cfg.
    Unlike remix, this leaves the seed and count at their form defaults (random, 1): a preset is a
    reusable template, not a specific past image to reproduce."""
    preset = library.get_preset(conn, preset_id)
    if preset is None:
        return None
    return {"model_id": preset.model_id, **preset.params}


def _model_for_values(registry: Registry, values: dict) -> Model:
    model_id = values.get("model_id")
    if model_id and model_id in registry.models:
        return registry.model(model_id)
    return _first_model(registry)


@router.get("/generate")
async def generate_form(
    request: Request,
    from_: int | None = Query(default=None, alias="from", ge=1, le=_MAX_ID),
    preset: int | None = Query(default=None, ge=1, le=_MAX_ID),
) -> HTMLResponse:
    settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    values: dict = {}
    with db.session(settings) as conn:
        if preset is not None:
            values = _preset_values(conn, preset) or {}
        elif from_ is not None:
            values = _remix_values(conn, from_) or {}
    return request.app.state.templates.TemplateResponse(
        request,
        "generate.html",
        {
            "models": registry.models.values(),
            "model": _model_for_values(registry, values),
            "values": values,
            "error": None,
        },
    )


@router.get("/generate/params")
async def generate_params(
    request: Request,
    model: str | None = Query(default=None),
    model_id: str | None = Query(default=None),
) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    selected = _model_for_values(registry, {"model_id": model_id or model})
    return request.app.state.templates.TemplateResponse(
        request, "partials/gen_params.html", {"model": selected, "values": {}}
    )


@router.post("/generate")
async def submit_generate(request: Request) -> Response:
    settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    form = dict(await request.form())

    try:
        batch_request = _batch_request_from_form(registry, form)
        with db.session(settings) as conn:
            batch_id = jobs.create_batch(conn, registry, settings, batch_request, random.Random())
    except (_FormError, UnknownModel, ValueError, DiskGuardError) as exc:
        return request.app.state.templates.TemplateResponse(
            request,
            "generate.html",
            {
                "models": registry.models.values(),
                "model": _model_for_values(registry, form),
                "values": form,
                "error": str(exc),
            },
            status_code=200,
        )

    return RedirectResponse(f"/queue?batch={batch_id}", status_code=303)
