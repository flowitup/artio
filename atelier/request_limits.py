"""ASGI middleware that rejects an oversized request body before any parser reads it.

A pure ASGI middleware, not a Starlette BaseHTTPMiddleware: it inspects Content-Length up front, then
wraps `receive` itself, so a chunked body with no Content-Length is bounded too, by the actual byte
count streamed. It is mounted outermost (see main.py), so the check runs before auth and before
Starlette or python-multipart ever spools the body.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

DEFAULT_LIMIT_BYTES = 64 * 1024

_TOO_LARGE_BODY = b"Request body too large\n"


class _BodyTooLarge(Exception):
    """Raised from inside the wrapped receive() once the streamed byte count passes the limit."""


class BodySizeLimitMiddleware:
    """Refuses a request whose body exceeds its path's limit with a 413, before any downstream parser
    (form, multipart, JSON) ever spools it. `per_path` holds exact-path overrides, for a route that
    legitimately needs a larger limit than the rest of the app (a future upload path, say)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_limit: int = DEFAULT_LIMIT_BYTES,
        per_path: Mapping[str, int] | None = None,
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.per_path = dict(per_path or {})

    def _limit_for(self, path: str) -> int:
        return self.per_path.get(path, self.default_limit)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self._limit_for(scope["path"])
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                if _declared_length_exceeds(value, limit):
                    await _send_413(send)
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

        try:
            await self.app(scope, counting_receive, send)
        except* _BodyTooLarge:
            # A downstream BaseHTTPMiddleware (e.g. the auth layer) reads the body through its own
            # nested anyio task groups, which re-wrap an exception raised from inside receive() as an
            # ExceptionGroup -- one or more levels deep -- rather than letting it propagate bare.
            # `except*` matches _BodyTooLarge whether it arrives bare or wrapped in any of those groups.
            await _send_413(send)


def _declared_length_exceeds(raw_value: bytes, limit: int) -> bool:
    try:
        declared = int(raw_value)
    except ValueError:
        return False  # a malformed header is not this middleware's concern; let the app reject it
    return declared > limit


async def _send_413(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": _TOO_LARGE_BODY})
