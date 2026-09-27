# Phase 8 repo work: JSON API, Claude plugin, docs

Plan: `plans/260925-1331-atelier-image-studio/phase-08-api-plugin-and-acceptance.md`. Scope: Implementation Steps 1-8 up to and including local checks. Steps 9-10 (owner-gated live install and acceptance run) are explicitly out of scope and untouched. No commit or push was made; every change listed below is uncommitted in the working tree.

## Step 1 — JSON API

`atelier/routes/api_v1.py` (new) implements exactly the nine criterion-10 endpoints, calling the existing service layer only (`jobs.create_batch`/`create_workflow_batch`, `library.search`/`image_detail`, `custom_workflows.list_workflows`/`get_workflow`/`seed_targets`, `worker.status.get`, `storage.resolve_under`) — no duplicated business logic. Every error returns `{"error": {"code", "message"}}`; a dedicated `RequestValidationError` handler is registered but scoped to `/api/v1` paths only (`register()` in the module, called from `main.py`), so every existing HTML route's own bounded query/path parameters keep FastAPI's default `{"detail": ...}` shape unchanged.

Error mapping: `UnknownModel`/`InvalidParams`/`ValueError` (bad model, out-of-bounds params, bad `seed_mode`/`count`) → 422; `DiskGuardError` → 507; an unknown image or workflow id → 404; a pydantic body/query/path validation failure → 422 via the custom handler. `GET /api/v1/gpu` has no POST sibling anywhere in the code — there is no route object to answer a warm or stop call at all, proven by `test_gpu_status_is_read_only`.

`atelier/auth.py`'s `SERVICE_ROUTES` (previously empty) now holds exactly the nine `(method, regex)` pairs. `atelier/main.py` gained one import and two lines: `from atelier.routes import api_v1` and `api_v1.register(app)` after the other routers.

**Decision on owner access to `/api/v1` (spec asked to decide and test):** nothing in `access_guard` singles out `/api/v1` for the owner identity — it is already unrestricted on any GET, and a state-changing call already needs the same same-origin (CSRF) check every owner POST needs everywhere else. So the owner can use the API exactly as freely as HTML, with no code change; `tests/test_api_v1.py::test_owner_identity_can_also_call_api_routes` proves both the GET-works and the POST-needs-Origin halves of that.

## Step 2 — API tests

`tests/test_api_v1.py` (new, 19 tests, all using real RS256 JWTs from the phase-3 fixtures): every test named in the spec is present verbatim —
`test_service_token_identity_can_call_every_allowlisted_endpoint`, `test_service_allowlist_matches_the_api_routes`, `test_api_rejects_missing_or_foreign_tokens`, `test_generate_returns_job_ids_and_jobs_endpoint_tracks_them`, `test_generate_refused_by_disk_guard_returns_507`, `test_images_search_paginates_by_offset_newest_first`, `test_image_file_download`, `test_workflow_run_by_id_with_seed_mode`, `test_gpu_status_is_read_only` — plus 10 more covering the models payload, unknown-model/unknown-body-field 422s, images pagination bounds, image-detail 404, workflow list/run 404, owner access, and the GPU payload shape.

**Existing tests modified, with reasons:**
- `tests/test_auth.py::test_service_identity_is_refused_outside_the_api_allowlist` — this test crawls every GET route and asserted 403 for the service identity on all of them. Since `SERVICE_ROUTES` now legitimately allows the service identity on the nine `/api/v1` GET routes, the test now explicitly skips `/api/v1` paths (with a docstring explaining why) and keeps asserting 403 on every HTML route, static files and the delete POST. This is exactly the "SERVICE_ROUTES was empty" case the task told me to expect and fix.
- `tests/test_pages_hide_secrets.py` needed **no change** — it already branched `service_headers if path.startswith("/api/v1") else owner_headers` in anticipation of this phase, and passed unmodified once `GET /api/v1/jobs` was made tolerant of a missing `ids` (see below).

**One API design choice worth flagging:** `GET /api/v1/jobs` with no `ids` query param returns `200 []` rather than a 422. The spec's table lists `ids=1,2` as the example but doesn't mandate it be required; making it optional (empty result on omission) let the generic secret-leak crawl exercise this route with no special-casing, the same as every other GET route in the app. `test_api_v1.py` still covers the malformed-`ids` and empty-`ids` cases explicitly via the plugin's own `job_status` tool call shape.

## Step 3 — Plugin skeleton

- `plugin/.claude-plugin/plugin.json`: name `atelier`, version `0.1.0`, description, author "Manh Trung BUI" (matches Folio's), keywords, and `userConfig` with `title`+`description` on all four options (`base_url`, `cf_client_id` required, `cf_client_secret` required+`sensitive: true`, `save_dir` type `directory`). No `mcpServers`/`skills` keys in the manifest — I checked Folio's real, installed plugin (`/Users/sweet-home/Works/folio/folio-plugin`) and confirmed those aren't manifest keys; `.mcp.json` and `skills/` are auto-discovered by location, same as Folio's.
- `plugin/.mcp.json`: `uv run --locked --script ${CLAUDE_PLUGIN_ROOT}/mcp_servers/atelier_mcp/server.py`, four `${user_config.*}` substitutions, exactly as sketched.
- `plugin/mcp_servers/atelier_mcp/server.py`: PEP 723 header (`mcp[cli]>=1.2,<2`, `httpx>=0.27`, `pydantic>=2.6`, `pillow>=10.4`), `_Client` (no-redirect httpx client, CF headers from env, 30 s timeout, never raises for an HTTP-level error — returns `{"error": {"status", "hint"}}` instead so a tool can report it inline), `_error_hint` per status (403/404/422/507 + generic, server message truncated to 500 chars), `save_dir()`/`target_path()` (path-traversal-safe, `ToolError` on escape), `thumbnail_webp()` (≤256 px, ≤~20 KB, quality ladder with a smaller fallback), `save_png()`, `wait_for_jobs()` (polls `/api/v1/jobs` every 3 s, reports progress best-effort — see deviation below), and the eight tools, each with its own strict pydantic `Args` model (`extra="forbid"`). All eight carry `readOnlyHint=True` except `generate` and `run_workflow`. `save_to` (path-confined) is only a `get_image` parameter, matching the architecture sketch's own `save_png(image_id, None)` calls inside `generate`/`run_workflow` — those two always use the default `atelier-<id>-<seed>.png` naming since a multi-image batch can't share one `save_to`.
- `plugin/mcp_servers/atelier_mcp/server.py.lock` generated by `uv lock --script` and committed (untracked, ready to `git add`). Verified by hand: `uv run --locked --script` runs; after editing a dependency line without relocking, it refuses with "needs to be updated, but `--locked` was provided" (then restored).

**One deviation from the sketch, disclosed:** FastMCP's tool schema wraps a single-`BaseModel`-parameter tool's fields under an `"args"` key in the generated JSON schema (verified against the installed mcp 1.30.0 directly) — so every tool call (from Claude, and from my own integration test) is shaped `{"args": {...fields...}}`, not the fields at the top level. This is exactly what the sketch's own `async def generate(args: GenerateArgs, ctx: Context)` signature produces; I did not fight it, since the client always calls whatever schema the server actually declares.

**A second deviation:** the sketch's `generate` returns `[json.dumps(result)]` or `[json.dumps(result), Image(...)]` for a `structured_output=False` tool. To keep every tool's return shape uniform (a `list` of content blocks, never a bare `str`/tuple), I set `structured_output=False` on all eight tools, not just `generate`/`run_workflow`/`get_image` — the four pure-read tools (`list_models`, `job_status`, `list_images`, `list_workflows`, `gpu_status`) now return `[json.dumps(...)]` too, instead of a bare `-> str`. Without this, `mcp.call_tool()` returns a `(blocks, structured_dict)` tuple for a bare-`str`-returning tool, which is an unnecessary shape difference for no functional benefit (Claude gets the same JSON text either way).

**A third, defensive addition not in the sketch:** `ctx.report_progress()` is wrapped in a best-effort try/except (`_report_progress`) — a client driving the tool outside a real MCP request (as the integration test does, and conceivably some MCP hosts) has no live request context, and `report_progress` raises `ValueError` in that case. Progress reporting is a courtesy; it must never fail the tool call itself.

## Step 4 — Plugin docs

`plugin/skills/atelier/SKILL.md`: frontmatter `name`/`description` with explicit trigger conditions, the check-status → list-models → generate-and-wait → job_status-if-needed → get_image flow, thumbnails-only-on-request guidance, and error cases (`service token rejected`, `disk_guard`, `not_found`, `validation_error`, and a hard tool-call failure). No warming advice anywhere, as required.

`plugin/README.md`: install by both routes (including the desktop-`userConfig`-missing fallback to environment passthrough, Folio's own pattern), the configuration table, the tool list, and the privacy section (thumbnails opt-in with exact size/format, transcript retention via `cleanupPeriodDays`, saves confined to `save_dir`, secret storage locations named — never values).

## Step 5 — Build script and marketplace

`plugin/build.sh`: `set -euo pipefail`, zips `.claude-plugin .mcp.json mcp_servers skills README.md` (excluding `__pycache__`/`.DS_Store`) into `atelier.plugin`, then lists contents. `.claude-plugin/marketplace.json` at the repo root: `atelier-local` / owner "Manh Trung BUI" / one entry `{"name": "atelier", "source": "./plugin", "description": "Drive Atelier from Claude"}`, plus a marketplace-level `description` (added after `claude plugin validate` flagged its absence as a warning — trivial fix, now clean with no warnings).

`bash plugin/build.sh` succeeds; `claude plugin validate` passes clean (no warnings) on both `plugin/` and the repo root. Zip-content checks (exact commands from Verification): 4 `${user_config.}` references, the literal-secret grep exits 1 (none found), `server.py.lock` is present, no `.env` in the archive, no secret value string found anywhere in the zip's contents.

## Step 6 — Plugin integration test

`tests/test_plugin_integration.py` (new, 2 tests): a real app (via `create_app`, `start_worker=True`, `FakeModalGateway`) runs under `uvicorn.Server` in a background thread on a free local port, wrapped in `AccessEdge` (the sketch's own emulated-edge class, adapted verbatim) so a matching `CF-Access-Client-Id`/`Secret` pair becomes a real RS256 JWT the app's own `AccessVerifier` checks — the dev identity is never used. The plugin server is loaded fresh with `importlib` per configuration (registered in `sys.modules` before `exec_module`, required because pydantic's eager schema-building at import time resolves `Literal`/forward refs through `sys.modules[cls.__module__]`).

`test_plugin_end_to_end` drives, on one shared server: `list_models`; `generate` with count 2 and `wait_seconds` 10 (no image block, two saved PNGs, byte-identical to the fake's PNG); a second `generate` with `include_thumbnail=true` (exactly one `image/webp` block, decoded ≤20,000 bytes, long side ≤256 px); `get_image` with a `save_to` outside `save_dir` (raises, message matches "must be inside") and one inside (`nested/pic.png`, saved with the server's bytes — this surfaced and fixed a real bug: `save_png` didn't create the parent directory for a nested `save_to`); `job_status`; `list_images(query=...)`; `list_workflows`; `run_workflow` by name; `gpu_status`; and a `generate` left pending (fake gateway never finished) returning `"next"` plus the job ids and no saved files. A concurrent asyncio task (`_finish_submitted_jobs`) plays Modal's side of a render by polling the database directly for newly `submitted` call ids and finishing them in the fake gateway — the real `Worker`'s dispatch/poll loops do the rest, exactly as production would. `test_plugin_wrong_secret_is_reported_as_service_token_rejected` reconfigures only the plugin's own secret (same running server) and proves every tool reports "service token rejected" from the real 403 the edge/app produce, never a crash or a parsed login page.

## Step 7 — Docs

`docs/system-architecture.md` (new): components, the auth/authorization model (including the explicit owner-may-also-use-`/api/v1` decision), the `/api/v1` endpoint table with its error mapping, the job lifecycle, GPU control, storage/disk-guard, the no-backups fact, and deploy — all cross-checked against the actual code, not aspirational.

`docs/deployment-guide.md`: added a leak-response bullet (immediate revocation, no grace period) next to the existing rotation section, and a new "Installing the Claude plugin" section (both routes, the desktop-`userConfig`-missing fallback, the Claude Code marketplace commands, and the lock/`--locked`/`--frozen` note) plus a short "Final review" section pointing at the plan's Acceptance record as the dated proof, not a second copy of this guide. No other section was touched.

`README.md`: fully rewritten — what Atelier is (including the no-backups fact), local development (`uv sync`, `uvicorn --factory --reload`), commands (ruff, pytest, `build.sh`, `uv lock --script`), and links to both docs files, `plugin/README.md` and the plan directory.

## Local checks (acceptance criteria)

1. `uv run ruff check` — clean.
2. `uv run pytest -q` — **543 passed, 1 deselected** (522 baseline + 19 in `test_api_v1.py` + 2 in `test_plugin_integration.py`). Every test named in the spec for both new files is present and green, and the route-enumerating crawl (`test_service_identity_is_refused_outside_the_api_allowlist`, `test_pages_hide_secrets`) passes with the new `/api/v1` routes swept in.
3. `bash plugin/build.sh` succeeds; all zip-content checks pass (see Step 5).
4. `claude plugin validate` (Claude Code 2.1.283, available locally): clean pass with no warnings on both `plugin/` and the repo root. Nothing here needed an interactive session.
5. This report.

## Files changed

Created: `atelier/routes/api_v1.py`; `plugin/.claude-plugin/plugin.json`, `plugin/.mcp.json`, `plugin/mcp_servers/atelier_mcp/server.py` (+`.lock`, untracked pending `git add`), `plugin/skills/atelier/SKILL.md`, `plugin/README.md`, `plugin/build.sh`; `.claude-plugin/marketplace.json`; `docs/system-architecture.md`; `tests/test_api_v1.py`, `tests/test_plugin_integration.py`.

Modified: `atelier/auth.py` (`SERVICE_ROUTES` filled), `atelier/main.py` (router registered), `pyproject.toml`/`uv.lock` (`mcp[cli]>=1.2,<2` dev dependency via `uv add --dev`; confirmed the Dockerfile's `uv sync --frozen --no-dev` never installs it in production), `README.md` (rewrite), `docs/deployment-guide.md` (plugin install, leak response, final review), `tests/test_auth.py` (one test scoped to exclude `/api/v1`, explained above), the phase-08 plan file (todos ticked with verification notes).

Not modified: `deploy/`, `.github/`, `Dockerfile`, `compose.yaml`, `modal/`, `atelier/migrations/`, any HTML route or template, `.gitignore` (`*.plugin` already covered it), `atelier/library.py`/`atelier/custom_workflows.py`/`atelier/jobs.py`/`atelier/gpu.py`/`atelier/worker.py`/`atelier/registry.py`/`atelier/storage.py` (read-only reuse only).

## Review fixes

Independent review (`plans/reports/code-reviewer-260927-1519-atelier-api-plugin-review.md`) found one High, eight Lows and 18 surviving behaviour mutants (of the review's own 32, after excluding the one mutant that *was* the H1 fix itself). All fixed in place, uncommitted, same working tree.

### H1 — secret echo (High)

`plugin/mcp_servers/atelier_mcp/server.py`: a Client ID/Secret carrying a stray newline, CR or space made httpx raise `LocalProtocolError("Illegal header value b'<secret>'")`, and the plugin put that exception's own text — the raw secret — into the tool result.

- Added `_clean_credential()`, run at import time on both `CLIENT_ID` and `CLIENT_SECRET`: `.strip()` removes a trailing/leading artifact (fixes the three reported cases outright, since the secret then matches Access's own stored value exactly); anything still containing a control character (an embedded CRLF, e.g. a header-injection attempt) or a non-ASCII byte raises `ConfigurationError` at startup, naming only the variable, never its value.
- `_Client.request`'s exception handler no longer interpolates `str(exc)` for any `httpx.HTTPError`: it now reports `type(exc).__name__` plus the configured base URL only. This is a second, independent line of defense — it holds even for a transport failure startup validation didn't anticipate.
- `plugin/README.md`'s existing "never in ... a tool's own result" claim is now true; strengthened its wording to describe the startup stripping/validation itself.
- Tests (`tests/test_plugin_integration.py`): trailing-artifact stripping (parametrized over `\n`, `\r\n`, space, tab, `  \n`); an embedded control character refused at import without the secret appearing in the exception message or `caplog`; non-ASCII refused the same way; a direct `_Client`-level test with a crafted `httpx.MockTransport` handler that raises an exception whose own text contains a secret-shaped string, proving the hint never contains it; and a full end-to-end test against the real server (module-scoped `plugin_server` fixture) configuring the secret with a trailing newline, driving `list_models`, `gpu_status`, `list_workflows`, `generate`, `job_status`, `list_images` and `get_image`, asserting the plain secret string appears in no block's text and nothing in `caplog`.
- Each test fails on the pre-fix code (verified: reverting `_clean_credential`'s strip or control-character check, or reintroducing `{exc}` in the hint, each independently fails its respective test — see the mutation results below) and passes after.

### Lows

- **L1** (`atelier/routes/api_v1.py`): `GET /api/v1/jobs?ids=` now rejects any id outside `1..2**63-1` with 422 before binding to sqlite (previously an unhandled `OverflowError` → 500 for a huge integer); `offset` on `GET /api/v1/images` gained an upper bound (`le=10**12`, matching the HTML routes' own pagination bound pattern). Tests: giant `ids`, giant `offset`, negative `offset`, and more-than-50 `ids`, all asserting 422 with the error envelope, never 500.
- **L2** (`atelier/auth.py`, `atelier/request_limits.py` — outside the original file list, authorized by this fix round): `access_guard`'s three 403 sites and the body-size limiter's 413 now answer the `{"error": {"code","message"}}` envelope when the path starts with `/api/v1`, and keep the existing plain-text body for every HTML route and static file, unchanged. `docs/system-architecture.md` corrected ("plain 403" → describes both shapes and why). Tests: API 403/413 envelope shape; HTML 403/413 explicitly pinned as still plain text (regression guard for the scoping itself).
- **L3** (`atelier/routes/api_v1.py`): `GET /api/v1/images/{id}/file` now confines the resolved path to `data_dir/images/`, not merely `data_dir` — a corrupted `images.file_png` row (tested with a row pointing at `atelier.db`) no longer serves the database back as `image/png`; a `../`-escaping row is still refused too (both `resolve_under`'s own guard and the new images-root check are exercised). Tests: both corruption shapes, each expecting 404.
- **L4** (`server.py`): `_error_hint` now maps 401 the same as 403 ("service token rejected: ..."). Test: `_error_hint` called directly on a bare `httpx.Response(401, ...)`.
- **L5** (`server.py`, plugin only — the API keeps full data, per the owner decision): `list_images` truncates each entry's `prompt`, and `job_status` truncates each entry's `error`, to 200 characters with an ellipsis via a shared `_truncate()` helper; `wait_for_jobs` applies the same truncation to the `error` field it surfaces inside `generate`/`run_workflow` results. Tests: a 300-character prompt and a 300-character error, each asserted truncated with the original long text absent from the result.
- **L6** (`server.py`): `_generate_or_run` now catches a per-image `save_png` failure, records `job["save_error"]` on that job, and keeps the batch/job ids and every other successfully saved image in the result — a single fetch failure no longer loses an already-paid-for batch. Test: two "done" jobs, one save monkeypatched to raise; asserts the batch id and the surviving save are both still returned, with `save_error` on only the failing job.
- **L7** (`server.py`): `_resolve_workflow_id` now tries an exact name match against the full listing first, and falls back to treating the string as a numeric id only if it's all ASCII digits (`[0-9]+`, not `str.isdigit()`, which also accepts non-ASCII digits) and no name matched. A workflow literally named `"2024"` now resolves to itself, not to id 2024. Tests: an all-digit name that has a matching stored workflow (resolves by name); an all-digit string with no name match (still falls back to the id); a non-ASCII-digit string (never treated as an id).
- **L8** (`atelier/routes/api_v1.py`): `GET /api/v1/workflows` now issues one query for every workflow's `id`/`name`/`backend_id`/`graph_json`, then parses each graph and computes `has_seed_input` in a single `asyncio.to_thread` call — one query total, off the event loop, instead of the previous `list_workflows()` query plus one extra `get_workflow()` query per row. Test: monkeypatches `custom_workflows.get_workflow` to raise, and asserts the endpoint still answers 200 (proving it's never called).

### Mutation re-run

Reconstructed the reviewer's 33 mutants (14 originally killed, 19 survived minus the one H1-fix mutant = 18 actionable survivors) against the now-fixed code, applying each one at a time (backed up and restored between mutants, never via `git checkout`) and running the relevant test files:

- **First pass, 18 previously-surviving mutants against the fixed code and the new tests above: 14 caught, 4 survived** (`run_default_keep`, `trimmed_leaks_path`, `plug_thumb_budget_10x`, `plug_extra_allowed`) — each a genuine gap in the *new* tests, not the fixes:
  - `run_default_keep`: no test called `run_workflow` without an explicit `seed_mode`, so a wrongly-defaulted `"keep"` went unnoticed. Added a test with no `seed_mode` in the body and `count=2` (only valid under the real "random" default).
  - `trimmed_leaks_path`: no test asserted the trimmed image-list payload's *shape* (only its ordering/filtering). Added a test asserting `file_png`/`file_thumb` are absent from every entry.
  - `plug_thumb_budget_10x`: the thumbnail test asserted against `module.THUMB_BUDGET`/`module.THUMB_SIDE` themselves, which are exactly what the mutant changes — self-defeating. Changed both assertions to the literal `20_000`/`256`.
  - `plug_extra_allowed`: the strict-argument test only exercised `list_models`, not the mutated `GenerateArgs`. Parametrized it over `list_models`, `generate` and `run_workflow`.
- **Second pass, the same 4 (now fixed) plus 10 new mutants targeting this round's own fixes directly (H1's strip/control-character check/exception-interpolation, L1's range check, L2's envelope branch, L3's images-root check, L4's 401 mapping, L5's truncation, L6's try/except, L8's single-query listing): all 14 caught.**
- **Combined result: 32/32 of the reconstructed mutants now caught** (the 33rd, `plug_hint_echoes_exc_type_only`, is the H1 fix's own correct behavior, not a defect to catch).

### Verification

1. `uv run ruff check` — clean.
2. `uv run pytest -q` — **584 passed, 1 deselected** (543 before this round + 41 new/modified assertions across `tests/test_api_v1.py` and `tests/test_plugin_integration.py`).
3. `bash plugin/build.sh` — succeeds; zip-content checks (4 `${user_config.}` refs, no literal secret, lock shipped, no `.env`) all pass.
4. `claude plugin validate` (Claude Code 2.1.283) — clean pass, no warnings, on both `plugin/` and the repo root.

### Files touched this round (in addition to the original phase-8 files)

Modified: `atelier/auth.py` (JSON 403 envelope for `/api/v1`), `atelier/routes/api_v1.py` (L1/L3/L8 fixes), `atelier/request_limits.py` (JSON 413 envelope for `/api/v1` — outside the original file list, authorized for this fix round), `plugin/mcp_servers/atelier_mcp/server.py` (H1, L4-L7), `plugin/README.md` (secret-claim wording), `docs/system-architecture.md` (403/413 envelope correction), `tests/test_api_v1.py`, `tests/test_plugin_integration.py`.

No plan IDs, phase numbers or finding codes were put in any code comment, test name or commit — only the invariant or behavior itself.

## Open questions for the owner

1. None block the repo work itself. The two remaining todo items (plugin install with the real service token, and the criteria 1-14 acceptance run) are owner-gated by design and untouched, per the task's scope.
2. `docs/deployment-guide.md`'s new plugin section references `/path/to/this/repo` for the marketplace commands generically, since the actual server-side checkout path wasn't part of this task's evidence; the owner should confirm the real path when running Step 9.
3. `atelier/request_limits.py` was modified this round to fix L2 (413 envelope) — not in the original phase-8 file-ownership list, but explicitly authorized by the fix-round instructions. Flagging it here in case the controller wants a second look given it's a shared, security-relevant middleware.
