"""Prompt library: presets (save/load/delete) and per-image stars and tags.

Every route here is owner-only HTML and always answers 200: a save or delete either succeeds or
re-renders its own fragment with the problem inline, never a 4xx a form submission would otherwise
have to special-case.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from artio import db, library

router = APIRouter()

_PRESET_FIELDS = ("prompt", "negative", "preset", "tier", "width", "height", "steps", "cfg")


def _library_context(conn) -> dict:
    return {"presets": library.list_presets(conn)}


@router.get("/library")
async def library_page(request: Request) -> HTMLResponse:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        context = _library_context(conn)
    return request.app.state.templates.TemplateResponse(request, "library.html", context)


@router.post("/presets")
async def save_preset(request: Request) -> HTMLResponse:
    """Saves the generate form's current fields as a named preset. The button lives inside the
    generate form and targets only its own small status area (#preset-status), so a save or its
    error never disturbs the rest of the form. No schema validation happens here: loading a preset
    fills the generate form, which validates on submit exactly like any other value typed there."""
    settings = request.app.state.settings
    form = dict(await request.form())
    name = form.get("preset_name", "")
    model_id = form.get("model_id", "").strip()
    message: str
    if not model_id:
        message = "Choose a model before saving a preset."
    else:
        params = {field: form.get(field, "") for field in _PRESET_FIELDS}
        try:
            with db.session(settings) as conn:
                library.save_preset(conn, name, model_id, params, time.time())
        except library.InvalidPresetName as exc:
            message = str(exc)
        else:
            message = f"Saved preset {name.strip()!r}."
    return request.app.state.templates.TemplateResponse(request, "partials/flash.html", {"message": message})


@router.post("/presets/{preset_id}/delete")
async def delete_preset(request: Request, preset_id: int) -> HTMLResponse:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        try:
            library.delete_preset(conn, preset_id)
        except (library.UnknownPreset, OverflowError):
            pass  # already gone; the list below reflects the current state either way
        context = _library_context(conn)
    return request.app.state.templates.TemplateResponse(request, "library.html", context)


# -- stars and tags ---------------------------------------------------------------------------------


def _missing_image_fragment(container_id: str, image_id: int) -> HTMLResponse:
    # image_id is always a real int here (FastAPI's path converter, or OverflowError catches the rest
    # before this is ever built), never attacker-controlled text, so this small hand-built fragment
    # needs no escaping.
    html = f'<span id="{container_id}" class="error-text" role="alert">Image {image_id} no longer exists.</span>'
    return HTMLResponse(html, status_code=200)


@router.post("/images/{image_id}/star")
async def toggle_star(request: Request, image_id: int) -> HTMLResponse:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        try:
            library.toggle_star(conn, image_id)
        except (library.UnknownImage, OverflowError):
            return _missing_image_fragment("star-button", image_id)
        image = library.image_detail(conn, image_id)
    return request.app.state.templates.TemplateResponse(request, "partials/star_button.html", {"image": image})


@router.post("/images/{image_id}/tags")
async def update_tags(request: Request, image_id: int) -> HTMLResponse:
    settings = request.app.state.settings
    form = await request.form()
    raw = form.get("tags", "")
    error: str | None = None
    with db.session(settings) as conn:
        try:
            library.set_tags(conn, image_id, raw)
        except library.InvalidTag as exc:
            error = str(exc)
        except (library.UnknownImage, OverflowError):
            return _missing_image_fragment("tag-editor", image_id)
        image = library.image_detail(conn, image_id)
    context = {"image": image, "error": error, "raw_tags": raw if error else None}
    return request.app.state.templates.TemplateResponse(request, "partials/tag_editor.html", context)
