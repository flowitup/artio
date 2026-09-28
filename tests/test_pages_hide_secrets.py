"""Crawls every GET route with the owner identity (service identity for /api/v1 paths) and proves no
response body ever contains a secret.

This walks the live app.routes rather than a hardcoded path list, so it cannot silently go stale: a
later phase's new GET route is swept in automatically, and the crawl fails outright if route_ids has no
value for one of its path parameters.
"""

from __future__ import annotations

import dataclasses

from fastapi.routing import iter_route_contexts
from starlette.testclient import TestClient

from artio.main import create_app
from tests.conftest import OWNER_EMAIL, PUBLIC_ORIGIN, mint

MODAL_TOKEN_SECRET_SENTINEL = "sk-modal-token-secret-should-never-appear-in-any-page"
CF_AUD_SENTINEL = "cf-aud-should-never-appear-in-any-page"
PLUGIN_CLIENT_ID_SENTINEL = "plugin-client-id-should-never-appear-in-any-page"

SENTINELS = (MODAL_TOKEN_SECRET_SENTINEL, CF_AUD_SENTINEL, PLUGIN_CLIENT_ID_SENTINEL)

# The GET routes that are polling partials: they legitimately answer 286 once idle.
POLLING_PARTIAL_PATHS = {"/queue/rows", "/chat/turns/{batch_id}"}


def _fill(path: str, route_ids: dict[str, int]) -> str:
    for name, value in route_ids.items():
        path = path.replace(f"{{{name}}}", str(value))
    return path


def test_pages_hide_secrets(monkeypatch, settings, registry, fake_gateway, access_key, route_ids):
    monkeypatch.setenv("MODAL_TOKEN_SECRET", MODAL_TOKEN_SECRET_SENTINEL)

    sentinel_settings = dataclasses.replace(
        settings,
        public_origin=PUBLIC_ORIGIN,
        cf_team_domain="https://flowitupteam-test.cloudflareaccess.com",
        cf_aud=CF_AUD_SENTINEL,
        owner_email=OWNER_EMAIL,
        plugin_client_id=PLUGIN_CLIENT_ID_SENTINEL,
    )
    app = create_app(sentinel_settings, registry=registry, gateway=fake_gateway, start_worker=False)

    key, _ = access_key
    owner_token = mint(key, aud=[CF_AUD_SENTINEL], email=OWNER_EMAIL)
    service_token = mint(key, aud=[CF_AUD_SENTINEL], common_name=PLUGIN_CLIENT_ID_SENTINEL)
    owner_headers = {"Cf-Access-Jwt-Assertion": owner_token, "Origin": PUBLIC_ORIGIN}
    service_headers = {"Cf-Access-Jwt-Assertion": service_token}

    visited = 0
    with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
        for ctx in iter_route_contexts(app.routes):
            if ctx.path is None or not ctx.methods or "GET" not in ctx.methods:
                continue
            path = _fill(ctx.path, route_ids)
            assert "{" not in path, f"route_ids has no value for a path parameter in {ctx.path}"

            headers = service_headers if path.startswith("/api/v1") else owner_headers
            # follow_redirects: "/" redirects to the latest chat. A page returning only 403s (auth broken)
            # must not pass this crawl just because a 403 body happens to contain no secret.
            response = client.get(path, headers=headers, follow_redirects=True)
            visited += 1
            expected = (200, 286) if ctx.path in POLLING_PARTIAL_PATHS else (200,)
            assert response.status_code in expected, f"unexpected status {response.status_code} on GET {path}"
            for sentinel in SENTINELS:
                assert sentinel not in response.text, f"{sentinel!r} leaked on GET {path}"

        static_response = client.get("/static/app.css", headers=owner_headers)
        assert static_response.status_code == 200
        for sentinel in SENTINELS:
            assert sentinel not in static_response.text

    assert visited >= 10  # guards against the crawl silently finding zero routes
