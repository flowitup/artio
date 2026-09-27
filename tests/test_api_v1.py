"""The versioned JSON API (`/api/v1`): the service identity's exact allowlist, the nine endpoints
themselves, and the shared `{"error": {"code", "message"}}` envelope.

Every request here uses a real RS256 JWT (conftest.mint), the same fixtures the rest of the suite
uses for the owner and service identities -- no dev identity, no shortcut around AccessVerifier.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

from fastapi.routing import iter_route_contexts
from starlette.testclient import TestClient

from atelier import db
from atelier.auth import SERVICE_ROUTES, service_may_call
from atelier.main import create_app
from tests.conftest import AUD, OWNER_EMAIL, PLUGIN_CLIENT_ID, PUBLIC_ORIGIN, TEAM_DOMAIN, mint

# -- the allowlist itself -----------------------------------------------------------------------------


def _fill(path: str, route_ids: dict[str, int]) -> str:
    for name, value in route_ids.items():
        path = path.replace(f"{{{name}}}", str(value))
    return path


def _api_v1_routes(app) -> set[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    for ctx in iter_route_contexts(app.routes):
        if ctx.path is None or not ctx.path.startswith("/api/v1"):
            continue
        for method in ctx.methods or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            seen.add((method, ctx.path))
    return seen


def test_service_allowlist_matches_the_api_routes(app_client, route_ids):
    routes = _api_v1_routes(app_client.app)
    assert len(routes) == 9

    for method, raw_path in routes:
        filled = _fill(raw_path, route_ids)
        assert service_may_call(method, filled), f"SERVICE_ROUTES does not allow {method} {filled}"

    for method, pattern in SERVICE_ROUTES:
        assert any(
            m == method and pattern.fullmatch(_fill(p, route_ids)) for m, p in routes
        ), f"SERVICE_ROUTES entry {method} {pattern.pattern!r} matches no real route"


def test_gpu_status_is_read_only(app_client, owner_headers):
    """No POST route exists under /api/v1/gpu at all: warm and stop stay owner-only HTML."""
    assert not any(method == "POST" and path == "/api/v1/gpu" for method, path in _api_v1_routes(app_client.app))
    # Even the owner identity, which bypasses the service allowlist entirely, gets no such endpoint.
    response = app_client.post("/api/v1/gpu", headers=owner_headers)
    assert response.status_code in (404, 405)


# -- authentication and authorization -----------------------------------------------------------------


def test_api_rejects_missing_or_foreign_tokens(app_client, access_key):
    response = app_client.get("/api/v1/models")
    assert response.status_code == 403

    key, _ = access_key
    foreign_token = mint(key, common_name="not-the-plugin")
    response = app_client.get("/api/v1/models", headers={"Cf-Access-Jwt-Assertion": foreign_token})
    assert response.status_code == 403


def test_service_token_identity_can_call_every_allowlisted_endpoint(
    app_client, service_headers, registry, fake_gateway, png_bytes, conn, route_ids
):
    model = next(iter(registry.models.values()))
    worker = app_client.app.state.worker

    responses = [app_client.get("/api/v1/models", headers=service_headers)]

    generate_response = app_client.post(
        "/api/v1/generate",
        headers=service_headers,
        json={"model": model.id, "prompt": "service identity smoke test", "count": 1},
    )
    responses.append(generate_response)
    assert generate_response.status_code == 201
    job_ids = generate_response.json()["job_ids"]

    asyncio.run(worker.dispatch_once())
    for job_id in job_ids:
        call_id = conn.execute("SELECT call_id FROM jobs WHERE id = ?", (job_id,)).fetchone()["call_id"]
        fake_gateway.finish(call_id, png_bytes)
    asyncio.run(worker.poll_once())

    responses.append(
        app_client.get(f"/api/v1/jobs?ids={','.join(str(j) for j in job_ids)}", headers=service_headers)
    )
    responses.append(app_client.get("/api/v1/images", headers=service_headers))
    responses.append(app_client.get(f"/api/v1/images/{route_ids['image_id']}", headers=service_headers))
    responses.append(app_client.get(f"/api/v1/images/{route_ids['image_id']}/file", headers=service_headers))
    responses.append(app_client.get("/api/v1/workflows", headers=service_headers))
    responses.append(
        app_client.post(
            f"/api/v1/workflows/{route_ids['workflow_id']}/run", headers=service_headers, json={"count": 1}
        )
    )
    responses.append(app_client.get("/api/v1/gpu", headers=service_headers))

    for response in responses:
        assert response.status_code != 403, f"service identity was refused on an allowlisted endpoint: {response.request.url}"
        assert response.status_code < 500


def test_owner_identity_can_also_call_api_routes(app_client, owner_headers, registry):
    """Nothing in access_guard singles out /api/v1 for the owner identity: GET works exactly like any
    other owner GET, and a state-changing call still needs the same same-origin check as everywhere
    else (proven by the second half of this test, not a special case for the API)."""
    model = next(iter(registry.models.values()))
    response = app_client.get("/api/v1/models", headers=owner_headers)
    assert response.status_code == 200

    no_origin = {k: v for k, v in owner_headers.items() if k != "Origin"}
    refused = app_client.post("/api/v1/generate", headers=no_origin, json={"model": model.id, "prompt": "x"})
    assert refused.status_code == 403

    accepted = app_client.post(
        "/api/v1/generate", headers=owner_headers, json={"model": model.id, "prompt": "x", "count": 1}
    )
    assert accepted.status_code == 201


# -- models -------------------------------------------------------------------------------------------


def test_models_lists_the_registry(app_client, service_headers, registry):
    response = app_client.get("/api/v1/models", headers=service_headers)
    assert response.status_code == 200
    body = response.json()
    ids = {m["id"] for m in body}
    assert ids == set(registry.models)
    model = next(iter(registry.models.values()))
    payload = next(m for m in body if m["id"] == model.id)
    assert payload["presets"][0]["name"] == model.param_schema.presets[0].name
    assert payload["default_preset"] == model.param_schema.default_preset


# -- generate and jobs ----------------------------------------------------------------------------------


def test_generate_returns_job_ids_and_jobs_endpoint_tracks_them(
    app_client, service_headers, registry, fake_gateway, png_bytes, conn
):
    model = next(iter(registry.models.values()))
    response = app_client.post(
        "/api/v1/generate",
        headers=service_headers,
        json={"model": model.id, "prompt": "a red fox in snow", "count": 2},
    )
    assert response.status_code == 201
    body = response.json()
    assert "batch_id" in body
    job_ids = body["job_ids"]
    assert len(job_ids) == 2

    worker = app_client.app.state.worker
    asyncio.run(worker.dispatch_once())
    call_ids = {}
    for job_id in job_ids:
        call_ids[job_id] = conn.execute("SELECT call_id FROM jobs WHERE id = ?", (job_id,)).fetchone()["call_id"]
        fake_gateway.finish(call_ids[job_id], png_bytes)
    asyncio.run(worker.poll_once())

    response = app_client.get(f"/api/v1/jobs?ids={','.join(str(j) for j in job_ids)}", headers=service_headers)
    assert response.status_code == 200
    jobs_payload = response.json()
    assert len(jobs_payload) == 2
    seeds = set()
    for job in jobs_payload:
        assert job["status"] == "done"
        assert job["image_id"] is not None
        assert job["duration_s"] is not None
        assert job["est_cost_usd"] is not None
        assert job["model_id"] == model.id
        seeds.add(job["seed"])
    assert len(seeds) == 2  # distinct random seeds, same guarantee as the HTML form


def test_generate_unknown_model_is_a_422_with_the_error_envelope(app_client, service_headers):
    response = app_client.post(
        "/api/v1/generate", headers=service_headers, json={"model": "not-a-real-model", "prompt": "x"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_generate_rejects_an_unknown_body_field(app_client, service_headers, registry):
    """extra="forbid" on GenerateBody: a typo'd field is refused, not silently ignored, and still
    answers the API's own error envelope (the custom RequestValidationError handler), not FastAPI's
    default {"detail": ...} shape."""
    model = next(iter(registry.models.values()))
    response = app_client.post(
        "/api/v1/generate", headers=service_headers, json={"model": model.id, "prompt": "x", "seed_mode": "random"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_generate_refused_by_disk_guard_returns_507(registry, fake_gateway, settings, access_key):
    tiny_cap_settings = dataclasses.replace(
        settings,
        public_origin=PUBLIC_ORIGIN,
        cf_team_domain=TEAM_DOMAIN,
        cf_aud=AUD,
        owner_email=OWNER_EMAIL,
        plugin_client_id=PLUGIN_CLIENT_ID,
        data_cap_gb=0,
    )
    db.migrate(tiny_cap_settings)
    seed_conn = db.connect(tiny_cap_settings)
    seed_conn.execute(
        "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) VALUES (0, 'm', 'generate', '{}', 1)"
    )
    seed_conn.execute(
        "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, status, created_at) "
        "VALUES (1, 'm', 'qwen21-uc', 'generate', '{}', '{}', 'done', 0)"
    )
    seed_conn.execute(
        "INSERT INTO images (job_id, model_id, file_png, file_thumb, width, height, bytes, sha256, created_at) "
        "VALUES (1, 'm', 'a.png', 'a.webp', 8, 8, 1, 'sha', 0)"
    )
    seed_conn.commit()
    seed_conn.close()

    key, _ = access_key
    service_token = mint(key, common_name=PLUGIN_CLIENT_ID)
    headers = {"Cf-Access-Jwt-Assertion": service_token}

    app = create_app(tiny_cap_settings, registry=registry, gateway=fake_gateway, start_worker=False)
    model = next(iter(registry.models.values()))
    with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
        response = client.post("/api/v1/generate", headers=headers, json={"model": model.id, "prompt": "x"})
        assert response.status_code == 507
        assert response.json()["error"]["code"] == "disk_guard"

        job_count = db.connect(tiny_cap_settings).execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        assert job_count == 1  # only the pre-seeded row: the POST created nothing


# -- images ---------------------------------------------------------------------------------------------


def _make_image(app_client, service_headers, conn, fake_gateway, png_bytes, registry, prompt: str) -> int:
    model = next(iter(registry.models.values()))
    response = app_client.post(
        "/api/v1/generate", headers=service_headers, json={"model": model.id, "prompt": prompt, "count": 1}
    )
    job_id = response.json()["job_ids"][0]
    worker = app_client.app.state.worker
    asyncio.run(worker.dispatch_once())
    call_id = conn.execute("SELECT call_id FROM jobs WHERE id = ?", (job_id,)).fetchone()["call_id"]
    fake_gateway.finish(call_id, png_bytes)
    asyncio.run(worker.poll_once())
    return conn.execute("SELECT id FROM images WHERE job_id = ?", (job_id,)).fetchone()["id"]


def test_images_search_paginates_by_offset_newest_first(
    app_client, service_headers, conn, fake_gateway, png_bytes, registry
):
    ids = [
        _make_image(app_client, service_headers, conn, fake_gateway, png_bytes, registry, f"paginated image {i}")
        for i in range(3)
    ]

    first_page = app_client.get("/api/v1/images?limit=2&offset=0", headers=service_headers)
    assert first_page.status_code == 200
    first_ids = [row["id"] for row in first_page.json()]
    assert first_ids == [ids[2], ids[1]]  # newest first

    second_page = app_client.get("/api/v1/images?limit=2&offset=2", headers=service_headers)
    assert second_page.status_code == 200
    second_ids = [row["id"] for row in second_page.json()]
    assert second_ids == [ids[0]]

    filtered = app_client.get(
        "/api/v1/images?q=paginated+image+1&limit=50&offset=0", headers=service_headers
    )
    assert filtered.status_code == 200
    assert [row["id"] for row in filtered.json()] == [ids[1]]


def test_images_list_never_includes_a_file_path(app_client, service_headers, route_ids):
    response = app_client.get("/api/v1/images", headers=service_headers)
    assert response.status_code == 200
    body = response.json()
    assert body
    for row in body:
        assert "file_png" not in row
        assert "file_thumb" not in row


def test_images_limit_over_fifty_is_rejected(app_client, service_headers):
    response = app_client.get("/api/v1/images?limit=51", headers=service_headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_image_detail_matches_the_full_metadata(app_client, service_headers, route_ids):
    response = app_client.get(f"/api/v1/images/{route_ids['image_id']}", headers=service_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == route_ids["image_id"]
    assert body["batch_id"] == route_ids["batch_id"]
    assert "tags" in body
    assert "workflow_name" in body


def test_image_detail_unknown_id_is_a_404(app_client, service_headers):
    response = app_client.get("/api/v1/images/999999", headers=service_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_image_file_download(app_client, service_headers, route_ids, png_bytes):
    response = app_client.get(f"/api/v1/images/{route_ids['image_id']}/file", headers=service_headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == png_bytes


# -- workflows --------------------------------------------------------------------------------------------


def test_workflows_list_reports_seed_input(app_client, service_headers, route_ids):
    response = app_client.get("/api/v1/workflows", headers=service_headers)
    assert response.status_code == 200
    body = response.json()
    entry = next(w for w in body if w["id"] == route_ids["workflow_id"])
    assert entry["has_seed_input"] is True  # the fixture's own graph has a literal KSampler seed


def test_workflow_run_by_id_with_seed_mode(app_client, service_headers, route_ids, conn):
    response = app_client.post(
        f"/api/v1/workflows/{route_ids['workflow_id']}/run",
        headers=service_headers,
        json={"seed_mode": "fixed", "seed": 1000, "count": 2},
    )
    assert response.status_code == 201
    body = response.json()
    job_ids = body["job_ids"]
    assert len(job_ids) == 2

    seeds = sorted(
        json.loads(conn.execute("SELECT params_json FROM jobs WHERE id = ?", (jid,)).fetchone()["params_json"])[
            "seed"
        ]
        for jid in job_ids
    )
    assert seeds == [1000, 1001]


def test_workflow_run_default_seed_mode_is_random_not_keep(app_client, service_headers, route_ids, conn):
    """No seed_mode in the body at all: the default must be "random", which allows count > 1 -- a
    default of "keep" would reject this same request with a 422 ("'keep' seed mode is only allowed
    with count 1")."""
    response = app_client.post(
        f"/api/v1/workflows/{route_ids['workflow_id']}/run",
        headers=service_headers,
        json={"count": 2},
    )
    assert response.status_code == 201
    job_ids = response.json()["job_ids"]
    assert len(job_ids) == 2
    seeds = {
        json.loads(conn.execute("SELECT params_json FROM jobs WHERE id = ?", (jid,)).fetchone()["params_json"])[
            "seed"
        ]
        for jid in job_ids
    }
    assert len(seeds) == 2  # distinct random seeds


def test_workflow_run_unknown_id_is_a_404(app_client, service_headers):
    response = app_client.post("/api/v1/workflows/999999/run", headers=service_headers, json={})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# -- gpu --------------------------------------------------------------------------------------------------


def test_gpu_status_reports_every_backend(app_client, service_headers, registry):
    response = app_client.get("/api/v1/gpu", headers=service_headers)
    assert response.status_code == 200
    body = response.json()
    assert {row["backend_id"] for row in body} == set(registry.backends)
    for row in body:
        assert row["state"]  # a real label, not an empty string


# -- fix round: exact-match allowlisting and the method check, pinned directly ---------------------


def test_service_may_call_uses_fullmatch_not_prefix_match():
    assert service_may_call("GET", "/api/v1/models") is True
    assert service_may_call("GET", "/api/v1/models/extra") is False  # a real route, but longer
    assert service_may_call("GET", "/api/v1/model") is False  # a prefix of the pattern, not equal


def test_service_may_call_checks_method_not_just_path():
    assert service_may_call("POST", "/api/v1/models") is False
    assert service_may_call("DELETE", "/api/v1/images") is False
    assert service_may_call("GET", "/api/v1/generate") is False  # only POST is allowlisted here


def test_service_may_call_requires_a_numeric_id_segment():
    assert service_may_call("GET", "/api/v1/images/1") is True
    assert service_may_call("GET", "/api/v1/images/abc") is False
    assert service_may_call("GET", "/api/v1/images/1e5") is False
    assert service_may_call("GET", "/api/v1/images/-1") is False


# -- fix round: giant integers and negative offsets get the JSON envelope, never a 500 --------------


def test_jobs_ids_out_of_range_returns_422_not_500(app_client, service_headers):
    response = app_client.get(f"/api/v1/jobs?ids={10**30}", headers=service_headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_jobs_more_than_fifty_ids_is_rejected(app_client, service_headers):
    ids = ",".join(str(i) for i in range(1, 52))
    response = app_client.get(f"/api/v1/jobs?ids={ids}", headers=service_headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_images_offset_out_of_range_returns_422_not_500(app_client, service_headers):
    response = app_client.get(f"/api/v1/images?offset={10**20}", headers=service_headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_images_offset_negative_is_rejected(app_client, service_headers):
    response = app_client.get("/api/v1/images?offset=-1", headers=service_headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# -- fix round: the JSON error envelope covers 403 and 413 under /api/v1, HTML is unchanged ----------


def test_api_403_uses_the_json_error_envelope(app_client):
    response = app_client.get("/api/v1/models")
    assert response.status_code == 403
    assert response.json() == {"error": {"code": "forbidden", "message": "Forbidden"}}


def test_html_403_stays_plain_text(app_client):
    """Pins that the envelope change is scoped to /api/v1: an HTML route's 403 is unchanged, an
    owner decision this fix must not silently widen."""
    response = app_client.get("/gallery")
    assert response.status_code == 403
    assert response.text == "Forbidden"
    assert response.headers["content-type"].startswith("text/plain")


def test_api_413_uses_the_json_error_envelope(app_client, service_headers):
    oversized = {"model": "x", "prompt": "a" * 100_000}
    response = app_client.post("/api/v1/generate", headers=service_headers, json=oversized)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_api_413_for_a_chunked_json_body_without_content_length(app_client, service_headers):
    """A chunked body carries no Content-Length, so only the streamed byte count can stop it. FastAPI
    turns any error while reading a JSON body into its own 400; the limit's 413 must still win."""

    def chunks():
        yield b'{"model": "x", "prompt": "'
        for _ in range(20):
            yield b"a" * 8192
        yield b'"}'

    headers = {**service_headers, "content-type": "application/json"}
    response = app_client.post("/api/v1/generate", headers=headers, content=chunks())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_html_413_stays_plain_text(app_client, owner_headers):
    response = app_client.post("/generate", headers=owner_headers, content=b"x" * 100_000)
    assert response.status_code == 413
    assert response.headers["content-type"].startswith("text/plain")


# -- fix round: the image-file endpoint is confined to the image store, not just data_dir -----------


def test_image_file_is_confined_to_the_image_store(app_client, service_headers, conn):
    """A corrupted images row pointing outside images/ -- here, at the database file itself -- must
    never be served back as image/png: confinement to data_dir alone isn't enough."""
    conn.execute(
        "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) VALUES (0, 'm', 'generate', '{}', 1)"
    )
    conn.execute(
        "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, status, created_at) "
        "VALUES (1, 'm', 'qwen21-uc', 'generate', '{}', '{}', 'done', 0)"
    )
    conn.execute(
        "INSERT INTO images (job_id, model_id, file_png, file_thumb, width, height, bytes, sha256, created_at) "
        "VALUES (1, 'm', 'atelier.db', 'a.webp', 8, 8, 1, 'sha', 0)"
    )
    conn.commit()
    image_id = conn.execute("SELECT id FROM images WHERE file_png = 'atelier.db'").fetchone()["id"]

    response = app_client.get(f"/api/v1/images/{image_id}/file", headers=service_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_image_file_traversal_via_a_corrupted_row_is_refused(app_client, service_headers, conn):
    """A row escaping data_dir entirely (not just the image store) must still be refused -- the
    images/-confinement check is in addition to resolve_under's own traversal guard, not instead of it."""
    conn.execute(
        "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) VALUES (0, 'm', 'generate', '{}', 1)"
    )
    conn.execute(
        "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, status, created_at) "
        "VALUES (1, 'm', 'qwen21-uc', 'generate', '{}', '{}', 'done', 0)"
    )
    conn.execute(
        "INSERT INTO images (job_id, model_id, file_png, file_thumb, width, height, bytes, sha256, created_at) "
        "VALUES (1, 'm', '../outside.png', 'a.webp', 8, 8, 1, 'sha', 0)"
    )
    conn.commit()
    image_id = conn.execute("SELECT id FROM images WHERE file_png = '../outside.png'").fetchone()["id"]

    response = app_client.get(f"/api/v1/images/{image_id}/file", headers=service_headers)
    assert response.status_code == 404


# -- fix round: the workflow listing no longer parses every stored graph per row --------------------


def test_workflows_list_never_calls_get_workflow_per_row(app_client, service_headers, route_ids, monkeypatch):
    from atelier import custom_workflows

    def _boom(*args, **kwargs):
        raise AssertionError("GET /api/v1/workflows must not call get_workflow per row (that's the N+1)")

    monkeypatch.setattr(custom_workflows, "get_workflow", _boom)
    response = app_client.get("/api/v1/workflows", headers=service_headers)
    assert response.status_code == 200
    body = response.json()
    assert any(w["id"] == route_ids["workflow_id"] for w in body)
