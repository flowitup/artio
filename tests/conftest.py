"""Shared fixtures for the Atelier test suite."""

import asyncio
import dataclasses
import importlib.util
import io
import json
import random
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from PIL import Image
from starlette.testclient import TestClient

from atelier import custom_workflows, db, jobs, library
from atelier.config import Settings, load_settings
from atelier.main import create_app
from atelier.registry import DEFAULT_REGISTRY, Registry
from atelier.worker import Worker
from tests.fakes import FakeModalGateway

BACKEND_SCRIPT = Path(__file__).resolve().parent.parent / "modal" / "qwen21_uc_app.py"

# Fixed identifiers the whole web-UI test suite mints tokens against and builds test apps with. The
# JWKS fetch is always patched (see jwks_without_network below), so TEAM_DOMAIN and AUD never need to
# resolve to anything real.
TEAM_DOMAIN = "https://flowitupteam-test.cloudflareaccess.com"
AUD = "test-atelier-aud"
OWNER_EMAIL = "owner@example.com"
PLUGIN_CLIENT_ID = "test-plugin-client-id.access"
PUBLIC_ORIGIN = "http://atelier.test"


@pytest.fixture(scope="session")
def backend_source() -> str:
    """Source text of the Modal backend script, for structural checks."""
    return BACKEND_SCRIPT.read_text()


@pytest.fixture(scope="session")
def backend_script():
    """The Modal backend script loaded by path. Defining a Modal app is lazy, so this needs no network."""
    spec = importlib.util.spec_from_file_location("qwen21_uc_app", BACKEND_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def settings(tmp_path) -> Settings:
    """A `test`-env Settings backed by a fresh temporary data directory."""
    return load_settings(
        {
            "ATELIER_ENV": "test",
            "ATELIER_DATA_DIR": str(tmp_path),
        }
    )


@pytest.fixture
def conn(settings):
    """A ready-to-use connection on a migrated database. Tests call conn.commit() themselves whenever a
    second connection (e.g. one opened internally by Worker) needs to see the change."""
    db.migrate(settings)
    connection = db.connect(settings)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def registry() -> Registry:
    return DEFAULT_REGISTRY


@pytest.fixture
def fake_gateway() -> FakeModalGateway:
    return FakeModalGateway()


@pytest.fixture
def png_bytes() -> bytes:
    """A real, small 8-bit RGB PNG, made with Pillow."""
    im = Image.new("RGB", (64, 64), color=(120, 60, 200))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def rng() -> random.Random:
    return random.Random(1234567890)


@pytest.fixture(scope="session")
def access_key():
    """One RSA keypair for the whole session, published as a JWKS document under kid "test-kid"."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "test-kid", "alg": "RS256"}
    return key, {"keys": [jwk]}


@pytest.fixture(autouse=True)
def jwks_without_network(monkeypatch, access_key):
    """The only patch in the auth tests: PyJWKClient.fetch_data never makes a real HTTPS call. Every
    other step -- signature, aud, iss, exp, kid lookup -- runs through the real PyJWT/cryptography code."""
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: access_key[1])


def mint(key, *, kid: str = "test-kid", **claims) -> str:
    """Mints a real RS256 Access-shaped JWT. `claims` overrides any default, so a test can set a wrong
    aud/iss/exp, an email, or a common_name."""
    now = int(time.time())
    body = {"aud": [AUD], "iss": TEAM_DOMAIN, "iat": now, "nbf": now, "exp": now + 300, "type": "app"} | claims
    return jwt.encode(body, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def app_client(settings, registry, fake_gateway):
    """A TestClient over a real create_app(), wired with test-fixed Access settings and start_worker
    disabled: tests drive the fake gateway and the worker's dispatch_once()/poll_once() themselves."""
    test_settings = dataclasses.replace(
        settings,
        public_origin=PUBLIC_ORIGIN,
        cf_team_domain=TEAM_DOMAIN,
        cf_aud=AUD,
        owner_email=OWNER_EMAIL,
        plugin_client_id=PLUGIN_CLIENT_ID,
    )
    app = create_app(test_settings, registry=registry, gateway=fake_gateway, start_worker=False)
    with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
        yield client


@pytest.fixture
def owner_headers(access_key) -> dict[str, str]:
    key, _ = access_key
    token = mint(key, email=OWNER_EMAIL)
    return {"Cf-Access-Jwt-Assertion": token, "Origin": PUBLIC_ORIGIN}


@pytest.fixture
def service_headers(access_key) -> dict[str, str]:
    key, _ = access_key
    token = mint(key, common_name=PLUGIN_CLIENT_ID)
    return {"Cf-Access-Jwt-Assertion": token}


_ROUTE_IDS_GRAPH = {
    "1": {"class_type": "KSampler", "inputs": {"seed": 1}},
    "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
}


@pytest.fixture
def route_ids(conn, registry, settings, fake_gateway, rng, png_bytes) -> dict[str, int]:
    """Seeds one finished job and its image, one preset and one stored workflow directly through the
    engine (not the app), so route_ids has a real row for every path-parameter name a GET route uses:
    image_id, batch_id, job_id, preset_id, workflow_id."""
    model = next(iter(registry.models.values()))
    size = model.param_schema.default_size()
    request = jobs.BatchRequest(
        model_id=model.id,
        prompt="route ids fixture",
        negative="",
        width=size.width,
        height=size.height,
        steps=model.param_schema.steps_default,
        cfg=model.param_schema.cfg_default,
        seed_mode="random",
        seed=None,
        count=1,
    )
    batch_id = jobs.create_batch(conn, registry, settings, request, rng)
    conn.commit()

    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    fake_gateway.finish(job["call_id"], png_bytes)
    asyncio.run(worker.poll_once())

    image = conn.execute("SELECT id FROM images WHERE job_id = ?", (job["id"],)).fetchone()

    preset_id = library.save_preset(
        conn,
        "route ids preset",
        model.id,
        {
            "prompt": "route ids preset prompt",
            "negative": "",
            "preset": "custom",
            "width": size.width,
            "height": size.height,
            "steps": model.param_schema.steps_default,
            "cfg": model.param_schema.cfg_default,
        },
        time.time(),
    )
    backend = registry.backend_for(model)
    workflow_id = custom_workflows.store_workflow(
        conn, registry, "route ids workflow", backend.id, _ROUTE_IDS_GRAPH, time.time()
    )
    conn.commit()

    return {
        "image_id": image["id"],
        "batch_id": batch_id,
        "job_id": job["id"],
        "preset_id": preset_id,
        "workflow_id": workflow_id,
    }
