"""Cloudflare Access JWT verification and the owner-POST origin (CSRF) check.

Every test mints a real RS256 JWT (see conftest.mint); the only patch anywhere is the JWKS fetch
(conftest.jwks_without_network), so a bug in signature, aud, iss, exp or kid handling would show up
here exactly as it would against the real Cloudflare endpoint.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.routing import iter_route_contexts
from starlette.requests import Request as StarletteRequest
from starlette.testclient import TestClient

from artio.auth import AccessVerifier
from artio.config import VOLUME_SENTINEL_NAME, ConfigError
from artio.main import create_app
from tests.conftest import AUD, OWNER_EMAIL, PLUGIN_CLIENT_ID, PUBLIC_ORIGIN, TEAM_DOMAIN, mint


def _fake_request(headers: dict[str, str]) -> StarletteRequest:
    """A minimal real Starlette Request wrapping only headers, for unit-testing AccessVerifier.identify()
    directly -- needed for cases the full app can't observe (the service identity is authorized by
    route separately from being authenticated, so a full HTTP round trip can't isolate identify() alone)."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }

    async def receive():
        return {"type": "http.request", "body": b""}

    return StarletteRequest(scope, receive)

# -- requests that must be refused ------------------------------------------------------------------


def test_missing_token_is_refused(app_client):
    response = app_client.get("/gallery")
    assert response.status_code == 403
    assert response.text == "Forbidden"


def test_garbage_token_is_refused(app_client):
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": "not-a-jwt-at-all"})
    assert response.status_code == 403


def test_wrong_audience_is_refused(app_client, access_key):
    key, _ = access_key
    token = mint(key, aud=["someone-elses-aud"], email=OWNER_EMAIL)
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_wrong_issuer_is_refused(app_client, access_key):
    key, _ = access_key
    token = mint(key, iss="https://not-our-team.cloudflareaccess.com", email=OWNER_EMAIL)
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_expired_token_is_refused(app_client, access_key):
    key, _ = access_key
    now = int(time.time())
    token = mint(key, email=OWNER_EMAIL, iat=now - 3700, nbf=now - 3700, exp=now - 3600)
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_token_signed_by_another_key_under_the_same_kid_is_refused(app_client):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = mint(other_key, email=OWNER_EMAIL)  # kid="test-kid", but the JWKS only registered access_key
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_unknown_kid_is_refused(app_client, access_key):
    key, _ = access_key
    token = mint(key, kid="some-other-kid", email=OWNER_EMAIL)
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_hs256_token_is_refused(app_client):
    # kid="test-kid" matches the JWKS entry, but algorithms=["RS256"] rejects the HS256 header before
    # the key is ever used to verify anything -- this is what stops the classic RS256/HS256 confusion
    # attack (signing with the public key's bytes as an HMAC secret).
    now = int(time.time())
    body = {
        "aud": [AUD],
        "iss": TEAM_DOMAIN,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "type": "app",
        "email": OWNER_EMAIL,
    }
    token = jwt.encode(body, "an-hmac-secret-not-an-rsa-key", algorithm="HS256", headers={"kid": "test-kid"})
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_non_owner_email_is_refused(app_client, access_key):
    key, _ = access_key
    token = mint(key, email="someone-else@example.com")
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_service_token_with_another_common_name_is_refused(app_client, access_key):
    key, _ = access_key
    token = mint(key, common_name="not-the-plugin")
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_static_asset_without_a_token_is_refused(app_client):
    response = app_client.get("/static/app.css")
    assert response.status_code == 403


def test_owner_post_with_a_foreign_origin_is_refused(app_client, access_key):
    key, _ = access_key
    token = mint(key, email=OWNER_EMAIL)
    response = app_client.post(
        "/generate",
        headers={"Cf-Access-Jwt-Assertion": token, "Origin": "https://evil.example.com"},
        data={"model_id": "x"},
    )
    assert response.status_code == 403


def test_owner_post_with_neither_origin_nor_referer_is_refused(app_client, access_key):
    key, _ = access_key
    token = mint(key, email=OWNER_EMAIL)
    response = app_client.post("/generate", headers={"Cf-Access-Jwt-Assertion": token}, data={"model_id": "x"})
    assert response.status_code == 403


@pytest.mark.parametrize(
    "extra_headers,expected_status",
    [
        pytest.param({"Origin": f"{PUBLIC_ORIGIN}.evil.com"}, 403, id="origin-subdomain-confusion"),
        pytest.param({"Origin": "null"}, 403, id="origin-null"),
        pytest.param({"Origin": ""}, 403, id="origin-empty"),
        pytest.param({"Origin": f"{PUBLIC_ORIGIN}/"}, 403, id="origin-trailing-slash"),
        pytest.param({"Referer": f"{PUBLIC_ORIGIN}.evil.com/"}, 403, id="referer-subdomain-confusion-no-slash-boundary"),
        pytest.param({"Referer": "http://artio.test@evil.com/"}, 403, id="referer-userinfo-confusion"),
        pytest.param({"Referer": f"{PUBLIC_ORIGIN}/some/page"}, 200, id="referer-alone-is-accepted"),
    ],
)
def test_csrf_origin_and_referer_boundary(app_client, access_key, extra_headers, expected_status):
    key, _ = access_key
    token = mint(key, email=OWNER_EMAIL)
    headers = {"Cf-Access-Jwt-Assertion": token, **extra_headers}
    response = app_client.post("/generate", headers=headers, data={"model_id": "x"})
    assert response.status_code == expected_status


def _get_paths(app) -> list[str]:
    """The path of every GET route, resolved through FastAPI's included-router indirection: app.routes
    itself holds opaque wrapper objects, not the routes directly."""
    return [
        ctx.path
        for ctx in iter_route_contexts(app.routes)
        if ctx.methods and "GET" in ctx.methods and ctx.path is not None
    ]


def _fill(path: str, route_ids: dict[str, int]) -> str:
    for name, value in route_ids.items():
        path = path.replace(f"{{{name}}}", str(value))
    return path


def test_service_identity_is_refused_outside_the_api_allowlist(app_client, service_headers, route_ids):
    """Every HTML GET route stays owner-only for the service identity. /api/v1 routes are deliberately
    excluded from this crawl: those nine endpoints are exactly what SERVICE_ROUTES allows the service
    identity to call (proven instead by test_api_v1.py's own allowlist tests), so a 403 here would be
    the bug, not the fix."""
    visited = 0
    for raw_path in _get_paths(app_client.app):
        path = _fill(raw_path, route_ids)
        if path == "/healthz" or path.startswith("/api/v1"):
            continue
        response = app_client.get(path, headers=service_headers)
        assert response.status_code == 403, f"service identity was allowed on GET {path}"
        visited += 1
    assert visited >= 10  # guards against the crawl silently finding zero routes

    response = app_client.get("/static/app.css", headers=service_headers)
    assert response.status_code == 403

    response = app_client.post(f"/images/{route_ids['image_id']}/delete", headers=service_headers)
    assert response.status_code == 403


# -- requests that must succeed ----------------------------------------------------------------------


def test_owner_jwt_in_header_succeeds(app_client, access_key):
    key, _ = access_key
    token = mint(key, email=OWNER_EMAIL)
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 200


def test_owner_jwt_in_cookie_succeeds(app_client, access_key):
    key, _ = access_key
    token = mint(key, email=OWNER_EMAIL)
    app_client.cookies.set("CF_Authorization", token)
    response = app_client.get("/gallery")
    assert response.status_code == 200


def test_healthz_succeeds_without_a_token(app_client):
    response = app_client.get("/healthz")
    assert response.status_code == 200


def test_owner_email_match_is_case_insensitive(app_client, access_key):
    key, _ = access_key
    token = mint(key, email=OWNER_EMAIL.upper())
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 200


# -- dev identity guard -------------------------------------------------------------------------------


def test_create_app_raises_config_error_for_dev_identity_in_production(monkeypatch, tmp_path):
    # Every other production requirement is satisfied here (the three env vars, plus the volume
    # sentinel), so the only way this can still raise is the dev-identity check itself: removing that
    # check would make this test actually observe create_app() succeed, not "raise for some other,
    # unrelated missing-config reason" the way the original bare-minimum env setup could.
    (tmp_path / VOLUME_SENTINEL_NAME).touch()
    monkeypatch.setenv("ARTIO_ENV", "production")
    monkeypatch.setenv("ARTIO_DEV_IDENTITY", "owner")
    monkeypatch.setenv("ARTIO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARTIO_CF_AUD", "prod-aud")
    monkeypatch.setenv("ARTIO_OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("ARTIO_PLUGIN_CLIENT_ID", "prod-plugin-client-id")
    with pytest.raises(ConfigError, match="development"):
        create_app()


def test_dev_identity_bypasses_jwt_verification_in_development(registry, fake_gateway, settings):
    dev_settings = dataclasses.replace(
        settings,
        env="development",
        dev_identity="owner",
        public_origin=PUBLIC_ORIGIN,
    )
    app = create_app(dev_settings, registry=registry, gateway=fake_gateway, start_worker=False)
    with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
        response = client.get("/gallery")  # no token at all
    assert response.status_code == 200


def test_dev_identity_is_ignored_outside_development_even_when_set_directly(registry, fake_gateway, settings):
    """load_settings() already refuses dev_identity outside ARTIO_ENV=development, but a Settings
    built by hand (bypassing load_settings, as here) can still carry both -- identify() itself must
    also require env == "development", not trust that Settings was necessarily built the normal way."""
    bad_settings = dataclasses.replace(
        settings,
        env="test",
        dev_identity="owner",
        public_origin=PUBLIC_ORIGIN,
    )
    app = create_app(bad_settings, registry=registry, gateway=fake_gateway, start_worker=False)
    with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
        response = client.get("/gallery")  # no token: must fall through to real verification and fail
    assert response.status_code == 403


def test_create_app_refuses_start_worker_false_outside_test_and_development(monkeypatch, tmp_path):
    (tmp_path / VOLUME_SENTINEL_NAME).touch()
    monkeypatch.setenv("ARTIO_ENV", "production")
    monkeypatch.setenv("ARTIO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARTIO_CF_AUD", "prod-aud")
    monkeypatch.setenv("ARTIO_OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("ARTIO_PLUGIN_CLIENT_ID", "prod-plugin-client-id")
    with pytest.raises(ConfigError, match="start_worker"):
        create_app(start_worker=False)


# -- malformed claims never crash the guard --------------------------------------------------------------


def test_a_list_email_claim_is_treated_as_absent_not_a_crash(app_client, access_key):
    key, _ = access_key
    token = mint(key, email=[OWNER_EMAIL])
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_an_int_common_name_claim_is_treated_as_absent_not_a_crash(app_client, access_key):
    key, _ = access_key
    token = mint(key, common_name=123456)
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_a_mismatched_non_ascii_common_name_is_refused_not_a_crash(app_client, access_key):
    key, _ = access_key
    token = mint(key, common_name="plugin-id-é-ñ")  # non-ASCII, and not the configured value
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_a_matching_non_ascii_common_name_is_accepted(settings, access_key):
    """Proves the positive path too: hmac.compare_digest on the UTF-8 encoded bytes accepts a
    non-ASCII common_name when it genuinely matches, not just refuses a mismatch without crashing."""
    key, _ = access_key
    unicode_client_id = "plugin-id-é-ñ"
    verifier_settings = dataclasses.replace(
        settings,
        cf_team_domain=TEAM_DOMAIN,
        cf_aud=AUD,
        owner_email=OWNER_EMAIL,
        plugin_client_id=unicode_client_id,
    )
    verifier = AccessVerifier(verifier_settings)
    token = mint(key, common_name=unicode_client_id)
    request = _fake_request({"cf-access-jwt-assertion": token})

    identity = asyncio.run(verifier.identify(request))
    assert identity.kind == "service"
    assert identity.subject == unicode_client_id


def test_a_non_json_jwks_body_is_refused_not_a_crash(app_client, monkeypatch, access_key):
    key, _ = access_key

    def broken_fetch(self):
        raise json.JSONDecodeError("Expecting value", "not json", 0)

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", broken_fetch)
    token = mint(key, email=OWNER_EMAIL)
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


# -- a dedicated thread pool for JWT verification ---------------------------------------------------------


def test_verifier_is_wired_with_a_dedicated_two_worker_executor(app_client):
    executor = app_client.app.state.verifier.executor
    assert executor is not None
    assert executor._max_workers == 2


def test_jwt_executor_is_shut_down_when_the_app_stops(settings, registry, fake_gateway):
    test_settings = dataclasses.replace(
        settings,
        public_origin=PUBLIC_ORIGIN,
        cf_team_domain=TEAM_DOMAIN,
        cf_aud=AUD,
        owner_email=OWNER_EMAIL,
        plugin_client_id=PLUGIN_CLIENT_ID,
    )
    app = create_app(test_settings, registry=registry, gateway=fake_gateway, start_worker=False)
    executor = app.state.verifier.executor
    with TestClient(app, base_url=PUBLIC_ORIGIN):
        assert not executor._shutdown
    assert executor._shutdown


def test_jwks_client_uses_a_five_second_timeout(app_client):
    assert app_client.app.state.verifier.jwks.timeout == 5


# -- required claims --------------------------------------------------------------------------------------


def test_owner_email_configured_in_uppercase_still_matches_a_lowercase_claim(registry, fake_gateway, settings, access_key):
    key, _ = access_key
    uppercase_owner_settings = dataclasses.replace(
        settings,
        public_origin=PUBLIC_ORIGIN,
        cf_team_domain=TEAM_DOMAIN,
        cf_aud=AUD,
        owner_email=OWNER_EMAIL.upper(),
        plugin_client_id=PLUGIN_CLIENT_ID,
    )
    app = create_app(uppercase_owner_settings, registry=registry, gateway=fake_gateway, start_worker=False)
    token = mint(key, email=OWNER_EMAIL.lower())
    with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
        response = client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 200


def test_jwt_missing_exp_is_refused(app_client, access_key):
    # exp must be genuinely absent from the payload (not present with a null value): PyJWT's own
    # verify_exp already rejects an explicit "exp": null via its own type check, regardless of whether
    # "exp" is even in the `require` list, so that would not isolate the require=["exp", ...] behavior.
    key, _ = access_key
    now = int(time.time())
    body = {"aud": [AUD], "iss": TEAM_DOMAIN, "iat": now, "nbf": now, "type": "app", "email": OWNER_EMAIL}
    assert "exp" not in body
    token = jwt.encode(body, key, algorithm="RS256", headers={"kid": "test-kid"})
    response = app_client.get("/gallery", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


# -- security response headers on every response ----------------------------------------------------------


def _assert_security_headers(response) -> None:
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_security_headers_are_present_on_a_normal_page(app_client, owner_headers):
    response = app_client.get("/gallery", headers=owner_headers)
    assert response.status_code == 200
    _assert_security_headers(response)


def test_security_headers_are_present_on_a_403(app_client):
    response = app_client.get("/gallery")
    assert response.status_code == 403
    _assert_security_headers(response)


def test_security_headers_are_present_on_a_static_file(app_client, owner_headers):
    response = app_client.get("/static/app.css", headers=owner_headers)
    assert response.status_code == 200
    _assert_security_headers(response)


def test_security_headers_are_present_on_healthz(app_client):
    response = app_client.get("/healthz")
    assert response.status_code == 200
    _assert_security_headers(response)
