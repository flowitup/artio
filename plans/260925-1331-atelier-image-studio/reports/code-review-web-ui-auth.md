# Code review: web UI and Access auth (uncommitted snapshot, 2026-09-26)

**Reviewer:** an independent code-reviewer subagent. It read the full code, probed the real `create_app()` in-process and against a real uvicorn on 127.0.0.1 (shut down afterwards), and ran 20 source mutations. No Modal calls.

**Baseline:** `uv run pytest -q` passed 216 tests (1 deselected), and `ruff` was clean. No Critical or High findings.

**What held:** the auth core fails closed:
- RS256 only;
- `alg:none` and HS256 refused;
- 30 s leeway;
- service identity refused on every non-allowlisted path;
- the Origin/Referer check correct under probes;
- body limits enforced before auth;
- autoescape on;
- vendored asset checksums match.

Coordinator dispositions are in the last column.

| ID | Sev | Finding | Location | Disposition |
|---|---|---|---|---|
| M1 | Medium | A 286 response never stops polling: each swapped-in panel re-arms `every 2s`. | `templates/partials/job_rows.html:1-5`, `routes/jobs.py:86` | **Accept.** Emit `hx-trigger` only while jobs are active, and test that it is absent when idle. |
| M2 | Medium | `/healthz` reports `"loops":"disabled"` with a 200 for a worker that started but never ticked. | `routes/health.py:23-25,51` | **Accept.** The worker records `started_at`; "disabled" only if never started, "stale" (503) if started without a tick in 30 s. `create_app(start_worker=False)` is refused unless `ATELIER_ENV` is `test` or `development`. |
| M3 | Medium | No anti-framing headers, so clickjacking bypasses the Origin CSRF check. | `auth.py`, `main.py` | **Accept.** Send `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'` and `X-Content-Type-Options: nosniff` on every response, with tests. The live Access cookie was set to SameSite=Lax on 2026-09-26, which already blocks the cookie in cross-site frames; this is defense in depth. |
| M4 | Medium | The secrets crawl passes even when every page answers 403. | `tests/test_pages_hide_secrets.py:60-63` | **Accept.** Assert 200 or 286 for every route and 200 for the static asset. |
| M5 | Medium | The path-containment test can't fail. | `tests/test_ui_image.py:90-98` | **Accept.** Use an existing file outside `data_dir` (both an absolute path and a symlink), and cover `/thumb` too. |
| M6 | Medium | The CSRF boundary is untested. | `auth.py:99-106`, `tests/test_auth.py` | **Accept.** Add a parametrized Origin/Referer boundary test. |
| L1 | Low | The guard compares `request.url.path` while the router uses `scope["path"]`. | `auth.py:112,119,122` | **Accept.** Use `request.scope["path"]` everywhere. |
| L2 | Low | Unexpected claim types or a non-JSON JWKS body give a 500. | `auth.py:84-94` | **Accept.** Check claim types, compare bytes, and also catch `ValueError`, all mapped to 403. |
| L3 | Low | JWKS verification shares the default thread pool with `save_result`. | `auth.py:59,83` | **Accept.** Use a dedicated 2-thread executor for verification and `PyJWKClient(..., timeout=5)`. |
| L4 | Low | The "GPU may still finish" note also shows on jobs cancelled while queued. | `job_rows.html:27-29` | **Accept.** |
| L5 | Low | A cancelled row's elapsed time keeps counting forever. | `routes/jobs.py:33-46` | **Accept.** Show "—". |
| L6 | Low | Remix blanks a seed of 0 and turns cfg 0.0 into 1.0. | `gen_params.html:28,39` | **Accept.** |
| L7 | Low | Oversized integer IDs or page numbers give a 500. | `routes/*` | **Accept.** Add bounds (`le=2**63-1`). |
| L8 | Low | Delete unlinks stored paths without the containment check. | `library.py:191-199`, `storage.py:123-126` | **Accept.** Move `_resolve_under` into `storage` and use it for deletes too. |
| L9 | Low | Retrying a missing job flashes only the bare ID. | `routes/jobs.py:110-111` | **Accept.** |
| L10 | Low | The dev-identity test can't fail, and the verifier trusts `dev_identity` in any env. | `tests/test_auth.py:196-201`, `auth.py:76-77` | **Accept.** Also require `env == "development"` in `identify()`, and make the test real. |
| L11 | Low | Other surviving mutations: config-side casefold, `require`, and the per-message byte counter (TestClient never streams). | tests | **Accept.** Add tests for these. A multi-message ASGI body test replaces the TestClient-only "chunked" case. The `common_name` positive test waits for the plugin API. |
