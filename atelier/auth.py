"""Cloudflare Access JWT verification: identity, service-route authorization and the CSRF origin check.

Only two identities exist: the owner (matched by email) and the plugin's service token (matched by
`common_name`). Authentication alone is not authorization: HTML routes and static files stay owner-only,
and the service identity may reach only the explicit SERVICE_ROUTES allowlist, so adding an `/api/v1`
route later never accidentally opens it to the browser identity, or vice versa.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
from concurrent.futures import Executor
from dataclasses import dataclass
from typing import Literal

import jwt
from fastapi import Request
from fastapi.responses import PlainTextResponse
from jwt import PyJWKClient

from atelier.config import Settings

log = logging.getLogger(__name__)

_JWKS_TIMEOUT_S = 5

# Sent on every response, including error responses from the body-size limiter and this guard, and on
# /healthz: defense in depth against clickjacking and MIME-sniffing even though the live Access cookie
# is already SameSite=Lax.
_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
)


@dataclass(frozen=True, slots=True)
class Identity:
    kind: Literal["owner", "service"]
    subject: str


class AccessDenied(Exception):
    """Raised by AccessVerifier.identify() for any request that must get a 403. The reason is for the
    server log only: the middleware never echoes it back to the client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# The (method, path) pairs the plugin's service identity may call. Empty for now: a future JSON API
# will fill it with the plugin's allowed endpoints. Every other route -- HTML pages, static files,
# delete and upload routes -- stays owner-only no matter what is added here.
SERVICE_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = ()


def service_may_call(method: str, path: str) -> bool:
    return any(method == m and pattern.fullmatch(path) for m, pattern in SERVICE_ROUTES)


class AccessVerifier:
    """Verifies a Cloudflare Access JWT and maps its claims to one of Atelier's two identities.

    `executor` runs the blocking decode (and any JWKS refetch it triggers) on a small dedicated thread
    pool rather than the loop's shared default executor, so a slow or stuck Cloudflare fetch can never
    starve the storage thread pool image saves use (or vice versa). None (the default) falls back to
    the loop's own default executor, which is fine for tests that construct a verifier directly."""

    def __init__(
        self,
        settings: Settings,
        jwks: PyJWKClient | None = None,
        executor: Executor | None = None,
    ) -> None:
        self.settings = settings
        self.issuer = settings.cf_team_domain.rstrip("/")
        self.jwks = jwks or PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", timeout=_JWKS_TIMEOUT_S)
        self.executor = executor

    def _decode(self, token: str) -> dict:
        # Runs off the event loop (see identify()): get_signing_key_from_jwt() can block on a real
        # HTTPS fetch once the JWK-set cache expires.
        key = self.jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=self.settings.cf_aud,
            issuer=self.issuer,
            leeway=30,
            options={"require": ["exp", "iss", "aud"]},
        )

    async def identify(self, request: Request) -> Identity:
        # dev_identity is also gated at load_settings() time (ATELIER_ENV=development is required to
        # set it at all), but a Settings built by hand (bypassing load_settings, e.g. dataclasses.replace
        # in a test) could carry it alongside any env -- checking env here too keeps the bypass honored
        # only in the one environment it is meant for, regardless of how Settings was constructed.
        if self.settings.dev_identity is not None and self.settings.env == "development":
            return Identity(self.settings.dev_identity, "dev")

        token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization")
        if not token:
            raise AccessDenied("missing Access JWT")
        try:
            loop = asyncio.get_running_loop()
            claims = await loop.run_in_executor(self.executor, self._decode, token)
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            # PyJWTError covers PyJWKClientError (fetch/kid failures); ValueError/TypeError covers a
            # JWKS endpoint that doesn't even return valid JSON, and any other unexpected decode fault.
            raise AccessDenied(f"invalid Access JWT: {type(exc).__name__}") from None

        email = claims.get("email")
        common_name = claims.get("common_name")
        # A claim is only ever used when it actually is a string: a JWT is attacker-influenced input,
        # and a well-formed Access token never sends email/common_name as anything else, so treating a
        # wrong-typed claim as simply absent costs nothing and avoids a 500 from .casefold()/.encode()
        # on a non-string value (a list, an int, ...).
        if (
            isinstance(email, str)
            and self.settings.owner_email
            and email.casefold() == self.settings.owner_email.casefold()
        ):
            return Identity("owner", email)
        if (
            isinstance(common_name, str)
            and self.settings.plugin_client_id
            and hmac.compare_digest(common_name.encode(), self.settings.plugin_client_id.encode())
        ):
            return Identity("service", common_name)
        raise AccessDenied("identity not allowed")


def same_origin(request: Request, public_origin: str) -> bool:
    """True when the request's Origin equals public_origin, or, if Origin is absent, when Referer
    starts with it. This is the CSRF check for owner state-changing requests."""
    origin = request.headers.get("origin")
    if origin is not None:
        return origin == public_origin
    referer = request.headers.get("referer")
    return referer is not None and referer.startswith(public_origin + "/")


async def access_guard(request: Request, call_next):
    """The one auth middleware: every route except /healthz needs a verified identity, HTML routes and
    static files are owner-only, and an owner state change needs a same-origin request.

    Reads the path from `request.scope["path"]`, not `request.url.path`: `Request.url` re-derives a URL
    from the scope and could in principle normalize it differently than the router itself does, and the
    router is what actually decides which endpoint a request reaches. Comparing and logging the exact
    same string the router matched against removes that gap entirely."""
    path = request.scope["path"]
    if path == "/healthz":
        return await call_next(request)

    verifier: AccessVerifier = request.app.state.verifier
    try:
        identity = await verifier.identify(request)
    except AccessDenied as exc:
        log.warning("access denied path=%s reason=%s", path, exc.reason)
        return PlainTextResponse("Forbidden", status_code=403)

    if identity.kind == "service" and not service_may_call(request.method, path):
        log.warning("service identity refused path=%s", path)
        return PlainTextResponse("Forbidden", status_code=403)

    if (
        identity.kind == "owner"
        and request.method not in ("GET", "HEAD", "OPTIONS")
        and not same_origin(request, request.app.state.settings.public_origin)
    ):
        log.warning("owner request refused: cross-origin path=%s", path)
        return PlainTextResponse("Forbidden", status_code=403)

    request.state.identity = identity
    return await call_next(request)


class SecurityHeadersMiddleware:
    """Adds anti-clickjacking and MIME-sniffing headers to every response.

    A pure ASGI middleware, registered outermost (see main.py), so it applies uniformly to every
    response including the body-size limiter's 413 and the auth guard's 403 -- neither of which passes
    through access_guard's own request handling."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.extend(_SECURITY_HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_security_headers)
