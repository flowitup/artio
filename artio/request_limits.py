"""ASGI middleware that rejects an oversized request body before any parser reads it.

A pure ASGI middleware, not a Starlette BaseHTTPMiddleware: it inspects Content-Length up front, then
wraps `receive` itself, so a chunked body with no Content-Length is bounded too, by the actual byte
count streamed. It is mounted outermost (see main.py), so the check runs before auth and before
Starlette or python-multipart ever spools the body.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

DEFAULT_LIMIT_BYTES = 64 * 1024

_TOO_LARGE_BODY_TEXT = b"Request body too large\n"
_TOO_LARGE_BODY_JSON = b'{"error": {"code": "request_too_large", "message": "Request body too large"}}'


class _BodyTooLarge(Exception):
    """Raised from inside the wrapped receive() once the streamed byte count passes the limit."""


class BodySizeLimitMiddleware:
    """Refuses a request whose body exceeds its path's limit with a 413, before any downstream parser
    (form, multipart, JSON) ever spools it. `per_path` holds exact-path overrides, for a route that
    legitimately needs a larger limit than the rest of the app (a future upload path, say).
    `per_pattern` does the same for a path with an id in it: (regex, limit) pairs, each matched
    against the whole path, checked after `per_path` and in order."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_limit: int = DEFAULT_LIMIT_BYTES,
        per_path: Mapping[str, int] | None = None,
        per_pattern: Sequence[tuple[str, int]] = (),
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.per_path = dict(per_path or {})
        self.per_pattern = [(re.compile(pattern), limit) for pattern, limit in per_pattern]

    def _limit_for(self, path: str) -> int:
        if path in self.per_path:
            return self.per_path[path]
        for pattern, limit in self.per_pattern:
            if pattern.fullmatch(path):
                return limit
        return self.default_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        limit = self._limit_for(path)
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                if _declared_length_exceeds(value, limit):
                    await _send_413(send, path)
                    return
                break

        seen = 0

        async def counting_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > limit:
                    raise _BodyTooLarge
            return message

        response_started = False

        async def guarded_send(message: Message) -> None:
            # Once the streamed body passed the limit, the limit decides the answer: a parser that
            # catches the error itself (FastAPI turns any failure while reading a JSON body into its
            # own 400) must not override the 413, so its response is replaced by ours.
            nonlocal response_started
            if seen > limit:
                if message["type"] == "http.response.start" and not response_started:
                    response_started = True
                    await _send_413(send, path)
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except* _BodyTooLarge:
            # A downstream BaseHTTPMiddleware (e.g. the auth layer) reads the body through its own
            # nested anyio task groups, which re-wrap an exception raised from inside receive() as an
            # ExceptionGroup -- one or more levels deep -- rather than letting it propagate bare.
            # `except*` matches _BodyTooLarge whether it arrives bare or wrapped in any of those groups.
            if not response_started:
                await _send_413(send, path)


def _declared_length_exceeds(raw_value: bytes, limit: int) -> bool:
    try:
        declared = int(raw_value)
    except ValueError:
        return False  # a malformed header is not this middleware's concern; let the app reject it
    return declared > limit


async def _send_413(send: Send, path: str) -> None:
    """The JSON API's own {"error": {"code","message"}} envelope for a path under /api/v1 (an owner
    decision about that API's own error shape); every HTML route keeps the plain-text body it
    always had."""
    is_api = path.startswith("/api/v1")
    body = _TOO_LARGE_BODY_JSON if is_api else _TOO_LARGE_BODY_TEXT
    content_type = b"application/json" if is_api else b"text/plain; charset=utf-8"
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", content_type)],
        }
    )
    await send({"type": "http.response.body", "body": body})
