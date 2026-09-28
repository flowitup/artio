"""BodySizeLimitMiddleware: isolated unit tests against a trivial ASGI app, plus the two named
end-to-end cases against the real app (Content-Length and chunked, with no Content-Length at all)."""

from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from artio.request_limits import DEFAULT_LIMIT_BYTES, BodySizeLimitMiddleware

# -- isolated middleware unit tests -------------------------------------------------------------------


async def _echo(request):
    body = await request.body()
    return PlainTextResponse(f"{len(body)} bytes")


def _make_client(**middleware_kwargs) -> TestClient:
    app = Starlette(routes=[Route("/echo", _echo, methods=["POST"])])
    app.add_middleware(BodySizeLimitMiddleware, **middleware_kwargs)
    return TestClient(app)


def test_default_limit_constant_is_64_kib():
    assert DEFAULT_LIMIT_BYTES == 64 * 1024


def test_body_at_exactly_the_limit_is_accepted():
    client = _make_client(default_limit=100)
    response = client.post("/echo", content=b"x" * 100)
    assert response.status_code == 200
    assert response.text == "100 bytes"


def test_body_one_byte_over_the_content_length_limit_is_rejected():
    client = _make_client(default_limit=100)
    response = client.post("/echo", content=b"x" * 101)
    assert response.status_code == 413


def test_streamed_body_with_no_content_length_is_bounded_by_the_running_total():
    # A generator content still arrives at the ASGI layer as one already-concatenated http.request
    # message through TestClient (verified: it does not preserve chunk boundaries), so this exercises
    # the same code path as test_body_one_byte_over_the_content_length_limit_is_rejected. The per-message
    # counter itself -- the running total actually persisting across several separate messages -- is
    # proven directly below, at the raw ASGI level.
    client = _make_client(default_limit=100)

    def chunks():
        yield b"x" * 60
        yield b"x" * 41  # 101 total: over the limit only once both chunks are accounted for

    response = client.post("/echo", content=chunks())
    assert response.status_code == 413


def test_running_byte_count_persists_across_several_separate_asgi_messages():
    """A raw ASGI call, bypassing TestClient/httpx entirely: proves the byte counter accumulates across
    multiple distinct http.request messages (each with more_body=True), not just within one message."""

    async def echo_app(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body") or b""
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    middleware = BodySizeLimitMiddleware(echo_app, default_limit=100)

    incoming = [
        {"type": "http.request", "body": b"x" * 60, "more_body": True},
        {"type": "http.request", "body": b"x" * 60, "more_body": True},  # 120 total: over the limit
        {"type": "http.request", "body": b"", "more_body": False},
    ]

    async def receive():
        return incoming.pop(0)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/echo", "headers": []}
    asyncio.run(middleware(scope, receive, send))

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413
    # The third (final, empty) message was never even requested: receive() raised on the second call,
    # as soon as the running total (60 + 60 = 120) passed the limit -- proving the count is cumulative
    # across messages, not reset or checked only once at the end.
    assert incoming == [{"type": "http.request", "body": b"", "more_body": False}]


def test_a_single_message_under_the_limit_still_passes_through_when_split_across_several_messages():
    """The counterpart to the previous test: several small messages that never individually nor
    cumulatively exceed the limit must all reach the inner app untouched."""

    async def echo_app(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body") or b""
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    middleware = BodySizeLimitMiddleware(echo_app, default_limit=100)

    incoming = [
        {"type": "http.request", "body": b"x" * 30, "more_body": True},
        {"type": "http.request", "body": b"x" * 30, "more_body": True},
        {"type": "http.request", "body": b"x" * 30, "more_body": False},  # 90 total: under the limit
    ]

    async def receive():
        return incoming.pop(0)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/echo", "headers": []}
    asyncio.run(middleware(scope, receive, send))

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    body_message = next(m for m in sent if m["type"] == "http.response.body")
    assert body_message["body"] == b"x" * 90


def test_per_path_override_raises_the_limit_for_one_path_only():
    app = Starlette(
        routes=[
            Route("/echo", _echo, methods=["POST"]),
            Route("/upload", _echo, methods=["POST"]),
        ]
    )
    app.add_middleware(BodySizeLimitMiddleware, default_limit=100, per_path={"/upload": 1000})
    client = TestClient(app)

    assert client.post("/echo", content=b"x" * 200).status_code == 413
    assert client.post("/upload", content=b"x" * 200).status_code == 200


# -- end to end against the real app -------------------------------------------------------------------

_OVERSIZED = b"x" * (128 * 1024)  # well over the 64 KiB default


def _post_with_content_length(app_client, headers):
    # No auth headers: the limiter is the outermost middleware, so this must be refused before the
    # request ever reaches the auth layer, let alone a form parser.
    return app_client.post("/generate", content=_OVERSIZED)


def _post_chunked(app_client, headers):
    # Owner auth is required here: only once the identity check passes does the request reach the
    # endpoint's own form parsing, which is what actually streams (and counts) the body. A real
    # Content-Type is required too: Starlette's form parser only reads the body stream at all for a
    # recognized form content type, so this matches what a browser actually sends.
    def chunks():
        for i in range(0, len(_OVERSIZED), 8192):
            yield _OVERSIZED[i : i + 8192]

    request_headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    return app_client.post("/generate", content=chunks(), headers=request_headers)


@pytest.mark.parametrize("send", [_post_with_content_length, _post_chunked], ids=["content_length", "chunked"])
def test_oversized_body_is_rejected_before_parsing(app_client, owner_headers, send):
    response = send(app_client, owner_headers)
    assert response.status_code == 413


def test_a_413_response_still_carries_the_security_headers(app_client):
    response = app_client.post("/generate", content=_OVERSIZED)
    assert response.status_code == 413
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_per_pattern_override_raises_the_limit_for_matching_paths_only():
    app = Starlette(
        routes=[
            Route("/echo", _echo, methods=["POST"]),
            Route("/workflows/{wid}/run", _echo, methods=["POST"]),
        ]
    )
    app.add_middleware(BodySizeLimitMiddleware, default_limit=100, per_pattern=[(r"/workflows/\d+/run", 1000)])
    client = TestClient(app)

    assert client.post("/workflows/7/run", content=b"x" * 200).status_code == 200
    assert client.post("/workflows/7/run", content=b"x" * 1001).status_code == 413
    assert client.post("/echo", content=b"x" * 200).status_code == 413
    # fullmatch: a path that merely contains the pattern keeps the default limit
    assert client.post("/workflows/abc/run", content=b"x" * 200).status_code in (404, 413)
