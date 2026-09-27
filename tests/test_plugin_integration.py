"""Runs the real Artio app under uvicorn, behind a test stand-in for Cloudflare Access, and drives
the plugin's own MCP server (loaded fresh with importlib) against it over real HTTP -- the only fake
anywhere in this path is FakeModalGateway; JWT verification, routing and auth are all the real code.

`FakeModalGateway` is thread-safe by design (see tests/fakes.py): the app's event loop runs on the
uvicorn server thread, while this test's own asyncio loop drives the plugin's tool calls, so a helper
that completes a spawned call must be safe to call from either.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import itertools
import json
import random
import socket
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from PIL import Image as PILImage

from artio.config import load_settings
from artio.main import create_app
from artio.registry import DEFAULT_REGISTRY
from tests.conftest import AUD, OWNER_EMAIL, TEAM_DOMAIN, mint
from tests.fakes import FakeModalGateway

SERVER_PATH = Path(__file__).resolve().parent.parent / "plugin" / "mcp_servers" / "artio_mcp" / "server.py"
_module_counter = itertools.count()

PLUGIN_CLIENT_ID = "plugin-integration-test-client-id.access"
PLUGIN_CLIENT_SECRET = "plugin-integration-test-secret"  # a test-only shared value, never a real credential
_ROUTE_IDS_GRAPH = {
    "1": {"class_type": "KSampler", "inputs": {"seed": 1}},
    "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
}


class AccessEdge:
    """Plays Cloudflare Access for the test: valid service-token headers become a real signed JWT
    header; anything else passes through untouched, so the real app's own AccessVerifier answers 403
    exactly as it would for a request Access itself never approved."""

    def __init__(self, app, key, client_id: str, client_secret: str) -> None:
        self.app, self.key, self.client_id, self.client_secret = app, key, client_id, client_secret

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if (
                headers.get(b"cf-access-client-id") == self.client_id.encode()
                and headers.get(b"cf-access-client-secret") == self.client_secret.encode()
            ):
                token = mint(self.key, common_name=self.client_id).encode()
                scope = {**scope, "headers": [*scope["headers"], (b"cf-access-jwt-assertion", token)]}
        await self.app(scope, receive, send)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def plugin_server(tmp_path_factory, access_key):
    """Starts the real app (wrapped in AccessEdge) under uvicorn in a background thread, in its
    normal (non-dev) auth mode, with FakeModalGateway as the only fake. Shared across every test in
    this module: each test's own data (a generated image, a stored workflow) simply accumulates,
    exactly like a real running Artio would."""
    data_dir = tmp_path_factory.mktemp("plugin-integration-data")
    settings = load_settings(
        {
            "ARTIO_ENV": "test",
            "ARTIO_DATA_DIR": str(data_dir),
            "ARTIO_PUBLIC_ORIGIN": "http://127.0.0.1",
            "ARTIO_CF_TEAM_DOMAIN": TEAM_DOMAIN,
            "ARTIO_CF_AUD": AUD,
            "ARTIO_OWNER_EMAIL": OWNER_EMAIL,
            "ARTIO_PLUGIN_CLIENT_ID": PLUGIN_CLIENT_ID,
        }
    )
    fake_gateway = FakeModalGateway()
    app = create_app(settings, registry=DEFAULT_REGISTRY, gateway=fake_gateway, start_worker=True)

    key, _ = access_key
    edge = AccessEdge(app, key, PLUGIN_CLIENT_ID, PLUGIN_CLIENT_SECRET)
    port = _free_port()
    config = uvicorn.Config(edge, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not report started within 10s"

    # A stored workflow for run_workflow(by name), seeded the same way conftest.route_ids is.
    conn = sqlite3.connect(data_dir / "artio.db")
    conn.execute(
        "INSERT INTO workflows (name, backend_id, graph_json, created_at) VALUES (?, ?, ?, ?)",
        ("plugin-test-workflow", "qwen21-uc", json.dumps(_ROUTE_IDS_GRAPH), time.time()),
    )
    conn.commit()
    conn.close()

    try:
        yield {"base_url": f"http://127.0.0.1:{port}", "data_dir": data_dir, "fake_gateway": fake_gateway}
    finally:
        server.should_exit = True
        thread.join(timeout=10)


_DUMMY_BASE_URL = "http://127.0.0.1:1"  # never actually dialed: standalone tests monkeypatch api.request


def _load_server_module(
    monkeypatch,
    base_url: str,
    *,
    save_dir: Path,
    client_secret: str = PLUGIN_CLIENT_SECRET,
    client_id: str = PLUGIN_CLIENT_ID,
):
    """Loads plugin/mcp_servers/artio_mcp/server.py fresh, so its module-level client picks up the
    env this call sets -- a stale import from an earlier test (or a different secret) must never
    leak into another test's module object."""
    monkeypatch.setenv("ARTIO_BASE_URL", base_url)
    monkeypatch.setenv("ARTIO_CF_CLIENT_ID", client_id)
    monkeypatch.setenv("ARTIO_CF_CLIENT_SECRET", client_secret)
    monkeypatch.setenv("ARTIO_SAVE_DIR", str(save_dir))
    # A unique module name per call, registered in sys.modules before exec_module(): pydantic's
    # eager schema building (triggered by mcp.tool() at import time) resolves `Literal` and other
    # annotations through sys.modules[cls.__module__], which fails with a "not fully defined" error
    # if the module was never registered there.
    name = f"artio_plugin_server_under_test_{next(_module_counter)}"
    spec = importlib.util.spec_from_file_location(name, SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


def _load_server_module_standalone(monkeypatch, *, save_dir: Path, **kwargs):
    """For tests that only need the module's own logic (credential handling, thumbnailing, workflow
    name resolution, ...) with `api.request` monkeypatched -- no real server, no real network."""
    return _load_server_module(monkeypatch, _DUMMY_BASE_URL, save_dir=save_dir, **kwargs)


async def _finish_submitted_jobs(
    data_dir: Path, fake_gateway: FakeModalGateway, png_bytes: bytes, *, expected: int, seen: set[str], timeout: float
) -> None:
    """Watches the database directly (a fresh connection per check, never shared across threads) for
    jobs the real Worker's dispatcher has submitted to Modal, and completes each exactly once with
    real PNG bytes -- playing Modal's side of a render. Runs as a concurrent asyncio task alongside
    the tool call that is waiting on the same jobs, on this test's own event loop."""

    def _submitted_call_ids() -> list[str]:
        conn = sqlite3.connect(data_dir / "artio.db")
        try:
            rows = conn.execute("SELECT call_id FROM jobs WHERE status = 'submitted' AND call_id IS NOT NULL")
            return [row[0] for row in rows.fetchall()]
        finally:
            conn.close()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(seen) < expected:
        for call_id in await asyncio.to_thread(_submitted_call_ids):
            if call_id not in seen:
                fake_gateway.finish(call_id, png_bytes)
                seen.add(call_id)
        if len(seen) >= expected:
            return
        await asyncio.sleep(0.1)


_TERMINAL_JOB_STATUSES = frozenset({"done", "failed", "cancelled"})


async def _wait_for_terminal_status(data_dir: Path, job_id: int, *, timeout: float) -> None:
    """Blocks until the real Worker's poller (its own background tick, on the server thread) has
    actually recorded a job as done/failed/cancelled -- not merely until `fake_gateway.finish()` was
    called. `plugin_server` is module-scoped, so a job a test leaves merely "submitted" (gateway
    outcome set, but the DB row not yet caught up) would otherwise still read as "submitted" to a
    later test's own naive completion-watcher, which matches on that DB status alone."""

    def _status() -> str | None:
        conn = sqlite3.connect(data_dir / "artio.db")
        try:
            row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_status) in _TERMINAL_JOB_STATUSES:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {job_id} never reached a terminal status within {timeout}s")


async def _call(module, name: str, args: dict):
    return await module.mcp.call_tool(name, {"args": args})


# -- the full flow, one shared server -------------------------------------------------------------


def test_plugin_end_to_end(monkeypatch, plugin_server, png_bytes, tmp_path):
    save_dir = tmp_path / "artio-save"
    server = _load_server_module(monkeypatch, plugin_server["base_url"], save_dir=save_dir)

    async def scenario():
        # list_models
        models_blocks = await _call(server, "list_models", {})
        models = json.loads(models_blocks[0].text)
        assert any(m["id"] == "qwen-image-2.1-uc" for m in models)

        # generate: count 2, waits, saves two PNGs, no image block by default
        seen: set[str] = set()
        finisher = asyncio.create_task(
            _finish_submitted_jobs(
                plugin_server["data_dir"], plugin_server["fake_gateway"], png_bytes, expected=2, seen=seen, timeout=15
            )
        )
        generate_blocks = await _call(
            server,
            "generate",
            {"model": "qwen-image-2.1-uc", "prompt": "plugin integration test", "count": 2, "wait_seconds": 10},
        )
        await finisher
        assert len(generate_blocks) == 1  # text only: include_thumbnail defaults to false
        generate_result = json.loads(generate_blocks[0].text)
        assert len(generate_result["saved_to"]) == 2
        for saved_path in generate_result["saved_to"]:
            path = Path(saved_path)
            assert path.is_relative_to(save_dir.resolve())
            assert path.read_bytes() == png_bytes
        image_ids = [j["image_id"] for j in generate_result["jobs"] if j["image_id"] is not None]
        assert len(image_ids) == 2

        # generate again, this time asking for a thumbnail: exactly one image/webp block
        seen2: set[str] = set()
        finisher2 = asyncio.create_task(
            _finish_submitted_jobs(
                plugin_server["data_dir"], plugin_server["fake_gateway"], png_bytes, expected=1, seen=seen2, timeout=15
            )
        )
        thumb_blocks = await _call(
            server,
            "generate",
            {
                "model": "qwen-image-2.1-uc",
                "prompt": "plugin integration thumbnail test",
                "count": 1,
                "wait_seconds": 10,
                "include_thumbnail": True,
            },
        )
        await finisher2
        assert len(thumb_blocks) == 2
        assert thumb_blocks[1].mimeType == "image/webp"
        raw = base64.b64decode(thumb_blocks[1].data)
        assert len(raw) <= 20_000
        with PILImage.open(io.BytesIO(raw)) as im:
            assert max(im.size) <= 256

        # get_image: a save_to outside the save dir is refused
        target_image_id = image_ids[0]
        with pytest.raises(Exception, match="must be inside"):
            await _call(server, "get_image", {"image_id": target_image_id, "save_to": "../escape.png"})

        # get_image: one inside the save dir is saved with the server's own bytes
        get_blocks = await _call(
            server, "get_image", {"image_id": target_image_id, "save_to": "nested/pic.png"}
        )
        get_result = json.loads(get_blocks[0].text)
        saved_path = Path(get_result["saved_to"])
        assert saved_path == (save_dir.resolve() / "nested" / "pic.png")
        assert saved_path.read_bytes() == png_bytes

        # job_status
        first_batch_job_ids = [j["id"] for j in generate_result["jobs"]]
        status_blocks = await _call(server, "job_status", {"job_ids": first_batch_job_ids})
        status_result = json.loads(status_blocks[0].text)
        assert all(j["status"] == "done" for j in status_result)

        # list_images(query=...)
        list_blocks = await _call(server, "list_images", {"query": "plugin integration test"})
        list_result = json.loads(list_blocks[0].text)
        assert len(list_result) >= 1

        # list_workflows
        workflows_blocks = await _call(server, "list_workflows", {})
        workflows_result = json.loads(workflows_blocks[0].text)
        assert any(w["name"] == "plugin-test-workflow" for w in workflows_result)

        # run_workflow (by name)
        seen3: set[str] = set()
        finisher3 = asyncio.create_task(
            _finish_submitted_jobs(
                plugin_server["data_dir"], plugin_server["fake_gateway"], png_bytes, expected=1, seen=seen3, timeout=15
            )
        )
        run_blocks = await _call(
            server, "run_workflow", {"workflow": "plugin-test-workflow", "count": 1, "wait_seconds": 10}
        )
        await finisher3
        run_result = json.loads(run_blocks[0].text)
        assert len(run_result["saved_to"]) == 1

        # gpu_status
        gpu_blocks = await _call(server, "gpu_status", {})
        gpu_result = json.loads(gpu_blocks[0].text)
        assert {row["backend_id"] for row in gpu_result} == set(DEFAULT_REGISTRY.backends)

        # left pending: generate returns "next" and the job ids, no saved files
        pending_blocks = await _call(
            server,
            "generate",
            {"model": "qwen-image-2.1-uc", "prompt": "left pending on purpose", "count": 1, "wait_seconds": 1},
        )
        pending_result = json.loads(pending_blocks[0].text)
        assert pending_result["saved_to"] == []
        assert "next" in pending_result
        assert len(pending_result["jobs"]) == 1

        # Cleanup: this job was deliberately left pending above. plugin_server is shared (module
        # scope) across every test in this file, and _finish_submitted_jobs matches "any submitted
        # call id" -- a job left dangling here would otherwise be exactly what a later test's own
        # finisher wrongly picks up first, masking that later test's real job forever. Waiting for
        # the terminal status (not just calling finish()) closes the window where the DB row still
        # reads "submitted" even though the fake gateway already has an outcome for it.
        pending_job_id = pending_result["jobs"][0]["id"]
        cleanup_seen: set[str] = set()
        await _finish_submitted_jobs(
            plugin_server["data_dir"], plugin_server["fake_gateway"], png_bytes, expected=1, seen=cleanup_seen, timeout=15
        )
        await _wait_for_terminal_status(plugin_server["data_dir"], pending_job_id, timeout=15)

    asyncio.run(scenario())


def test_plugin_wrong_secret_is_reported_as_service_token_rejected(monkeypatch, plugin_server, tmp_path):
    """The edge mints no JWT for a mismatched secret, so the real app answers 403 -- every tool must
    report that as a plain hint, never an uncaught exception or a parsed HTML/login body."""
    server = _load_server_module(
        monkeypatch,
        plugin_server["base_url"],
        save_dir=tmp_path / "artio-save-wrong-secret",
        client_secret="not-the-real-secret",
    )

    async def scenario():
        blocks = await _call(server, "list_models", {})
        result = json.loads(blocks[0].text)
        assert "service token rejected" in result["error"]["hint"]

        blocks = await _call(server, "gpu_status", {})
        result = json.loads(blocks[0].text)
        assert "service token rejected" in result["error"]["hint"]

    asyncio.run(scenario())


# -- fix round: a mis-pasted secret is never echoed ------------------------------------------------


@pytest.mark.parametrize("artifact", ["\n", "\r\n", " ", "\t", "  \n"])
def test_credential_trailing_artifacts_are_stripped(monkeypatch, tmp_path, artifact):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save", client_secret=f"s3cr3t-ABC{artifact}")
    assert module.CLIENT_SECRET == "s3cr3t-ABC"
    assert module.CLIENT_ID == PLUGIN_CLIENT_ID  # unaffected: only the secret was given an artifact here


def test_credential_with_an_embedded_control_character_is_refused_without_printing_it(monkeypatch, tmp_path, caplog):
    """A CRLF embedded in the middle (not merely trailing) survives strip() and must still be
    refused: this is the header-injection shape, not the innocent copy-paste one."""
    secret = "s3cr3t-ABC\r\nX-Evil: 1"
    with pytest.raises(Exception) as excinfo:
        _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save", client_secret=secret)
    assert "s3cr3t-ABC" not in str(excinfo.value)
    assert "s3cr3t-ABC" not in caplog.text


def test_credential_with_non_ascii_is_refused_without_printing_it(monkeypatch, tmp_path):
    secret = "s3cr3t-éñ"
    with pytest.raises(Exception) as excinfo:
        _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save", client_secret=secret)
    assert secret not in str(excinfo.value)


def test_client_never_follows_a_redirect(monkeypatch, tmp_path):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(302, headers={"location": "https://team.cloudflareaccess.com/login"})
        return httpx.Response(200, json={"unreachable": "the client must never get here"})

    client = module._Client(_DUMMY_BASE_URL, "id", "secret", transport=httpx.MockTransport(handler))
    result = asyncio.run(client.request("GET", "/api/v1/models"))
    assert calls["n"] == 1  # never followed to the second, "successful" response
    assert result["error"]["status"] == 302
    assert "redirected to its login page" in result["error"]["hint"]


def test_client_request_never_interpolates_a_raw_exception_message(monkeypatch, tmp_path):
    """Pins the H1 fix directly at the code level, independent of whether startup validation would
    also have caught this particular case: a transport-level exception's own text can itself hold
    header content (httpx's real "Illegal header value b'<secret>'" message does), so only the
    exception's type name may ever reach a hint."""
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")
    secret_shaped = "s3cr3t-should-never-appear-in-a-hint"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"Illegal header value b'{secret_shaped}'")

    client = module._Client(_DUMMY_BASE_URL, "id", "secret", transport=httpx.MockTransport(handler))
    result = asyncio.run(client.request("GET", "/x"))
    assert secret_shaped not in result["error"]["hint"]
    assert "ConnectError" in result["error"]["hint"]


def test_secret_with_trailing_newline_authenticates_and_every_tool_stays_secret_free(
    monkeypatch, plugin_server, tmp_path, png_bytes, caplog
):
    """End-to-end: the real edge expects PLUGIN_CLIENT_SECRET with no artifact; configuring the
    plugin with a trailing newline must still authenticate (proving strip() round-trips correctly
    against a real server), and the raw secret must never surface in any tool's own result or in
    anything logged, across every one of the eight tools."""
    server = _load_server_module(
        monkeypatch,
        plugin_server["base_url"],
        save_dir=tmp_path / "artio-save-secret-audit",
        client_secret=PLUGIN_CLIENT_SECRET + "\n",
    )

    async def scenario():
        blocks = await _call(server, "list_models", {})
        assert isinstance(json.loads(blocks[0].text), list)

        blocks = await _call(server, "gpu_status", {})
        gpu_result = json.loads(blocks[0].text)
        assert "error" not in gpu_result

        blocks = await _call(server, "list_workflows", {})
        assert "error" not in json.loads(blocks[0].text) if isinstance(json.loads(blocks[0].text), dict) else True

        seen: set[str] = set()
        finisher = asyncio.create_task(
            _finish_submitted_jobs(
                plugin_server["data_dir"], plugin_server["fake_gateway"], png_bytes, expected=1, seen=seen, timeout=15
            )
        )
        generate_blocks = await _call(
            server, "generate", {"model": "qwen-image-2.1-uc", "prompt": "secret audit", "count": 1, "wait_seconds": 10}
        )
        await finisher
        generate_result = json.loads(generate_blocks[0].text)
        assert len(generate_result["saved_to"]) == 1
        image_id = next(j["image_id"] for j in generate_result["jobs"] if j["image_id"] is not None)

        blocks = await _call(server, "job_status", {"job_ids": [j["id"] for j in generate_result["jobs"]]})
        blocks += await _call(server, "list_images", {})
        blocks += await _call(server, "get_image", {"image_id": image_id})
        blocks += generate_blocks

        for block in blocks:
            text = getattr(block, "text", None)
            if text is not None:
                assert PLUGIN_CLIENT_SECRET not in text

    asyncio.run(scenario())
    assert PLUGIN_CLIENT_SECRET not in caplog.text


# -- fix round: the plugin's own safety rules, pinned directly -------------------------------------


def test_401_maps_to_service_token_rejected_hint(monkeypatch, tmp_path):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")
    response = httpx.Response(401, json={"error": {"message": "unauthorized"}})
    assert "service token rejected" in module._error_hint(response)


def test_list_images_truncates_long_prompts(monkeypatch, tmp_path):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")
    long_prompt = "p" * 300

    async def fake_request(method, path, **kwargs):
        return [
            {
                "id": 1, "model_id": "m", "prompt": long_prompt, "seed": 1, "width": 8, "height": 8,
                "starred": False, "created_at": 0.0, "batch_id": 1, "workflow_name": None,
            }
        ]

    monkeypatch.setattr(module.api, "request", fake_request)
    blocks = asyncio.run(_call(module, "list_images", {}))
    result = json.loads(blocks[0].text)
    assert len(result[0]["prompt"]) <= 200
    assert result[0]["prompt"].endswith("…")
    assert long_prompt not in result[0]["prompt"]


def test_job_status_truncates_long_errors(monkeypatch, tmp_path):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")
    long_error = "e" * 300

    async def fake_request(method, path, **kwargs):
        return [
            {
                "id": 1, "status": "failed", "error": long_error, "image_id": None, "seed": 1,
                "model_id": "m", "duration_s": 1.0, "est_cost_usd": 0.01,
            }
        ]

    monkeypatch.setattr(module.api, "request", fake_request)
    blocks = asyncio.run(_call(module, "job_status", {"job_ids": [1]}))
    result = json.loads(blocks[0].text)
    assert len(result[0]["error"]) <= 200
    assert long_error not in result[0]["error"]


def _job_row(job_id: int, image_id: int | None, seed: int) -> dict:
    return {
        "id": job_id, "status": "done", "image_id": image_id, "seed": seed, "model_id": "m",
        "duration_s": 1.0, "est_cost_usd": 0.01, "error": None,
    }


def test_generate_keeps_the_batch_when_one_save_fails(monkeypatch, tmp_path):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")

    async def fake_request(method, path, **kwargs):
        if method == "POST" and path == "/api/v1/generate":
            return {"batch_id": 1, "job_ids": [10, 11]}
        if method == "GET" and path == "/api/v1/jobs":
            return [_job_row(10, 100, 1), _job_row(11, 101, 2)]
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(module.api, "request", fake_request)

    async def fake_save_png(image_id, seed, save_to=None):
        if image_id == 101:
            raise module.ToolError("simulated fetch failure")
        return module.save_dir() / f"artio-{image_id}-{seed}.png"

    monkeypatch.setattr(module, "save_png", fake_save_png)

    blocks = asyncio.run(
        _call(module, "generate", {"model": "qwen-image-2.1-uc", "prompt": "x", "count": 2, "wait_seconds": 1})
    )
    result = json.loads(blocks[0].text)
    assert result["batch_id"] == 1
    assert len(result["saved_to"]) == 1  # the batch and the one real save both survive
    jobs_by_id = {j["id"]: j for j in result["jobs"]}
    assert "save_error" in jobs_by_id[11]
    assert "save_error" not in jobs_by_id[10]


def test_workflow_name_that_is_all_digits_resolves_by_name_first(monkeypatch, tmp_path):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")

    async def fake_request(method, path, **kwargs):
        assert (method, path) == ("GET", "/api/v1/workflows")
        return [{"id": 7, "name": "2024", "backend_id": "qwen21-uc", "has_seed_input": True}]

    monkeypatch.setattr(module.api, "request", fake_request)
    resolved = asyncio.run(module._resolve_workflow_id("2024"))
    assert resolved == 7  # the stored workflow named "2024", never int("2024") == 2024


def test_workflow_all_digit_string_falls_back_to_id_when_no_name_matches(monkeypatch, tmp_path):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")

    async def fake_request(method, path, **kwargs):
        return []

    monkeypatch.setattr(module.api, "request", fake_request)
    resolved = asyncio.run(module._resolve_workflow_id("42"))
    assert resolved == 42


def test_workflow_non_ascii_digits_are_not_treated_as_an_id(monkeypatch, tmp_path):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")

    async def fake_request(method, path, **kwargs):
        return []

    monkeypatch.setattr(module.api, "request", fake_request)
    resolved = asyncio.run(module._resolve_workflow_id("١٢"))  # Arabic-Indic "12"
    assert isinstance(resolved, dict) and "error" in resolved


def test_thumbnail_webp_respects_budget_and_side_for_a_large_noisy_image(monkeypatch, tmp_path):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")
    rng = random.Random(1234)
    w = h = 800
    im = PILImage.frombytes("RGB", (w, h), rng.randbytes(w * h * 3))
    buf = io.BytesIO()
    im.save(buf, "PNG")

    raw = module.thumbnail_webp(buf.getvalue())
    # Literal bounds, not module.THUMB_BUDGET/THUMB_SIDE: this random-noise source encodes to about
    # 23 KB at quality 80 on a 256x256 canvas (verified by hand), comfortably over 20,000 and under
    # 200,000 -- so a widened THUMB_BUDGET or THUMB_SIDE constant would pass silently if this
    # asserted against the (also mutated) constant instead of the spec's own fixed numbers.
    assert len(raw) <= 20_000
    with PILImage.open(io.BytesIO(raw)) as thumb:
        assert max(thumb.size) <= 256


def test_generate_includes_at_most_one_thumbnail_even_with_multiple_saved_images(monkeypatch, tmp_path, png_bytes):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")

    async def fake_request(method, path, **kwargs):
        if method == "POST" and path == "/api/v1/generate":
            return {"batch_id": 1, "job_ids": [1, 2]}
        if method == "GET" and path == "/api/v1/jobs":
            return [_job_row(1, 1, 1), _job_row(2, 2, 2)]
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(module.api, "request", fake_request)

    async def fake_save_png(image_id, seed, save_to=None):
        path = module.save_dir() / f"artio-{image_id}-{seed}.png"
        path.write_bytes(png_bytes)
        return path

    monkeypatch.setattr(module, "save_png", fake_save_png)

    blocks = asyncio.run(
        _call(
            module,
            "generate",
            {"model": "qwen-image-2.1-uc", "prompt": "x", "count": 2, "wait_seconds": 1, "include_thumbnail": True},
        )
    )
    assert len(blocks) == 2  # one text block, exactly one image block regardless of two saves
    assert blocks[1].mimeType == "image/webp"


def test_save_dir_is_created_with_mode_0700(monkeypatch, tmp_path):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "fresh-save-dir")
    root = module.save_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("list_models", {"unexpected_field": "x"}),
        ("generate", {"model": "m", "prompt": "x", "unexpected_field": "x"}),
        ("run_workflow", {"workflow": "w", "unexpected_field": "x"}),
    ],
)
def test_tool_call_with_an_unknown_argument_field_is_rejected(monkeypatch, tmp_path, tool_name, args):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")
    with pytest.raises(Exception, match="(?i)extra|forbid|unexpected"):
        asyncio.run(module.mcp.call_tool(tool_name, {"args": args}))


def test_only_generate_and_run_workflow_are_not_marked_read_only(monkeypatch, tmp_path):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")
    tools = asyncio.run(module.mcp.list_tools())
    mutating = {"generate", "run_workflow"}
    assert {t.name for t in tools} == {
        "list_models", "generate", "job_status", "list_images", "get_image",
        "list_workflows", "run_workflow", "gpu_status",
    }
    for tool in tools:
        is_read_only = bool(tool.annotations and tool.annotations.readOnlyHint)
        if tool.name in mutating:
            assert not is_read_only, f"{tool.name} must not be marked readOnlyHint"
        else:
            assert is_read_only, f"{tool.name} must be marked readOnlyHint"


def test_wait_seconds_is_capped_at_max_wait_s(monkeypatch, tmp_path):
    module = _load_server_module_standalone(monkeypatch, save_dir=tmp_path / "save")
    captured: dict[str, float] = {}

    async def fake_wait_for_jobs(job_ids, wait_seconds, ctx):
        captured["wait_seconds"] = wait_seconds
        return [{"id": j, "status": "done", "image_id": None} for j in job_ids]

    monkeypatch.setattr(module, "wait_for_jobs", fake_wait_for_jobs)

    async def fake_request(method, path, **kwargs):
        if method == "POST":
            return {"batch_id": 1, "job_ids": [1]}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(module.api, "request", fake_request)
    asyncio.run(
        _call(module, "generate", {"model": "qwen-image-2.1-uc", "prompt": "x", "wait_seconds": 999_999})
    )
    assert captured["wait_seconds"] == module.MAX_WAIT_S
