"""Chat: the conversation view that is Artio's home, its composer, and the polled turn partial.

Sending a message is a plain form POST answered with a 303 back to the conversation, so the page works
without JavaScript; htmx only polls each running turn in place. A rejected message re-renders the page
with a 200 and the problem above the composer, same as the generate form.
"""

from __future__ import annotations

import random
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from artio import chat, db
from artio.registry import Registry, UnknownModel
from artio.routes.generate import _FormError, _parse_float, _parse_int, _size_from_form
from artio.storage import DiskGuardError

router = APIRouter()

_MAX_ID = 2**63 - 1


def _message_from_form(registry: Registry, form: dict) -> chat.Message:
    model = registry.model(form.get("model_id", "")) if form.get("model_id") else next(iter(registry.models.values()))
    width, height = _size_from_form(model, form)
    mode = form.get("mode", "new")
    if mode not in ("new", "refine"):
        raise _FormError(f"mode must be 'new' or 'refine', got {mode!r}")
    refine_raw = form.get("refine_image_id", "")
    return chat.Message(
        text=form.get("text", ""),
        mode=mode,
        refine_image_id=_parse_int(refine_raw, "image to refine") if refine_raw else None,
        width=width,
        height=height,
        steps=_parse_int(form.get("steps", ""), "steps"),
        cfg=_parse_float(form.get("cfg", ""), "cfg"),
        negative=form.get("negative", ""),
        count=_parse_int(form.get("count", ""), "count"),
    )


def _render_chat(
    request: Request,
    conn,
    session: chat.ChatSession | None,
    *,
    refine: int | None = None,
    error: str | None = None,
    text: str = "",
) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    session_turns = chat.turns(conn, session.id) if session else []
    return request.app.state.templates.TemplateResponse(
        request,
        "chat.html",
        {
            "session": session,
            "turns": session_turns,
            "composer": chat.composer(conn, registry, session_turns, refine),
            "error": error,
            "text": text,
        },
    )


@router.get("/chat")
async def new_chat(request: Request) -> HTMLResponse:
    with db.session(request.app.state.settings) as conn:
        return _render_chat(request, conn, None)


@router.get("/chat/{session_id}")
async def chat_page(
    request: Request,
    session_id: int = PathParam(ge=1, le=_MAX_ID),
    refine: int | None = Query(default=None, ge=1, le=_MAX_ID),
) -> HTMLResponse:
    with db.session(request.app.state.settings) as conn:
        session = chat.get_session(conn, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return _render_chat(request, conn, session, refine=refine)


async def _send(request: Request, session_id: int | None) -> Response:
    settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    form = dict(await request.form())
    with db.session(settings) as conn:
        session = chat.get_session(conn, session_id) if session_id is not None else None
        if session_id is not None and session is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        try:
            message = _message_from_form(registry, form)
            new_session_id, batch_id = chat.send(
                conn, registry, settings, session_id, message, random.Random(), time.time()
            )
        except (_FormError, chat.ChatError, UnknownModel, ValueError, DiskGuardError) as exc:
            conn.rollback()
            refine_raw = form.get("refine_image_id", "")
            refine = int(refine_raw) if str(refine_raw).isdigit() else None
            return _render_chat(request, conn, session, refine=refine, error=str(exc), text=form.get("text", ""))
    return RedirectResponse(f"/chat/{new_session_id}#turn-{batch_id}", status_code=303)


@router.post("/chat")
async def start_chat(request: Request) -> Response:
    return await _send(request, None)


@router.post("/chat/{session_id}")
async def send_message(request: Request, session_id: int = PathParam(ge=1, le=_MAX_ID)) -> Response:
    return await _send(request, session_id)


@router.post("/chat/{session_id}/delete")
async def delete_chat(request: Request, session_id: int = PathParam(ge=1, le=_MAX_ID)) -> Response:
    with db.session(request.app.state.settings) as conn:
        chat.delete_session(conn, session_id)
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/chat"
    return response


@router.get("/chat/turns/{batch_id}")
async def chat_turn(request: Request, batch_id: int = PathParam(ge=1, le=_MAX_ID)) -> Response:
    """One turn, polled while it renders. 286 tells htmx to stop polling once nothing is left running."""
    with db.session(request.app.state.settings) as conn:
        found = chat.turn(conn, batch_id)
    if found is None:
        raise HTTPException(status_code=404, detail="turn not found")
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/chat_turn.html",
        {"turn": found, "session_id": found.session_id},
        status_code=200 if found.active else 286,
    )


@router.get("/partials/chat-sessions")
async def chat_sessions(request: Request, current: int | None = Query(default=None, ge=1, le=_MAX_ID)) -> HTMLResponse:
    with db.session(request.app.state.settings) as conn:
        sessions = chat.list_sessions(conn)
    return request.app.state.templates.TemplateResponse(
        request, "partials/chat_sessions.html", {"sessions": sessions, "current": current}
    )


def home_url(conn) -> str:
    latest = chat.latest_session_id(conn)
    return f"/chat/{latest}" if latest is not None else "/chat"
