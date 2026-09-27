# Code review: JSON API (`/api/v1`) and the Claude plugin (uncommitted phase-8 work)

Date: 2026-09-27 (Europe/Paris). Reviewer: code-reviewer. This review was read-only. No repo file was edited, and there were no commits, no ssh, no calls to atelier.flowitup.com and no real Modal calls. The plugin zip was built from a scratch copy, so the repo's own `plugin/atelier.plugin` was only listed, never rewritten.

## Code Review Summary

### Scope
- **Changed:** `atelier/routes/api_v1.py` (new), `atelier/auth.py` (`SERVICE_ROUTES`), `atelier/main.py`, and `plugin/`:
  - `server.py` and its `.lock`;
  - `.mcp.json` and `plugin.json`;
  - `SKILL.md`, `README.md` and `build.sh`.
- **Also changed:** `.claude-plugin/marketplace.json`, `docs/system-architecture.md` (new), the `README.md` rewrite, deployment-guide additions, `tests/test_api_v1.py` (19 tests), `tests/test_plugin_integration.py` (2 tests) and `tests/test_auth.py`.
- **Checks:** `uv run ruff check` is clean; `uv run pytest -q` gives 543 passed, 1 deselected.
- **Evidence gathered:**
  - Raw-socket requests against a real in-process uvicorn, so there is no client-side path normalization.
  - TestClient API probes.
  - A local scripted HTTP server driving the real plugin module.
  - A scratch `build.sh` run with the zip unpacked.
  - `uv --locked` staleness checks.
  - 33 mutants (19 on the API and auth code, 14 on the plugin server), each run through the full suite.

### Overall Assessment
- **Held:**
  - The authorization boundary: the service identity reaches exactly the nine endpoints under every path trick tried, and never warm, stop or HTML. Owner POSTs to the API keep the same-origin (CSRF) check.
  - API input validation.
  - Save-folder confinement.
  - The locked supply chain.
  - A clean built zip.
- **Defect:** **one High**. The plugin echoes the Client Secret into tool results when the configured value carries a stray newline, space or CR — a common copy-paste artifact. That contradicts both the spec and the plugin README's own privacy claim.
- **Also:** several Lows and weak test coverage on the plugin's safety rules (14 of 32 behaviour mutants caught). Fix the High before the owner installs the plugin.

---

## Critical Issues
None.

## High Priority

### H1. A mis-pasted Client Secret is echoed verbatim into every tool result, and from there into the transcript and Anthropic
- **Where:** `plugin/mcp_servers/atelier_mcp/server.py:137-138`: `return {"error": {..., "hint": f"could not reach Atelier: {exc}"}}`.
- **Mechanism:**
  - The secret goes into the httpx client's default headers, stored unvalidated at `:49-51` and `:126-132`.
  - When the value holds a character that is illegal in a header (a trailing `\n` or space from pasting, or a CR), httpx raises `LocalProtocolError("Illegal header value b'<secret>'")` on the first request.
  - That is an `httpx.HTTPError`, so `str(exc)`, including the raw secret, becomes the tool's result.
- **Evidence:** the real plugin module, loaded with `ATELIER_CF_CLIENT_SECRET` set to a test value, called `list_models`:

  | Configured secret | Tool result |
  |---|---|
  | value + `\n` | `{"error": {"status": null, "hint": "could not reach Atelier: Illegal header value b's3cr3t-VALUE-ABC\\n'"}}` (**secret present**) |
  | value + `\r\nX-Evil: 1` | secret present |
  | value + a space | secret present |

  A non-ASCII value fails differently: the module's import crashes (`UnicodeEncodeError` at `:127`), so the server never starts. The traceback shows positions, not the value.
- **Impact:**
  - The Access service token lets its holder queue GPU renders (spend) and read every image. Once echoed, it sits in Claude's context and the local transcript under `~/.claude/projects`, and has been sent to Anthropic.
  - The spec says errors "never echo headers or secrets". `plugin/README.md:42-44` and `:66-69` claim the secret never appears "in … a tool's own result".
  - The deployment guide's own leak response ("revoke immediately") would then be required for an innocent configuration mistake.
- **Fix:**
  - At startup, `strip()` the ID and secret, and refuse (without printing the value) anything outside visible ASCII.
  - In `_Client.request`, never interpolate `exc`. Use `type(exc).__name__` plus the base URL.
  - Add a test that configures `secret + "\n"` and asserts the secret is absent from every tool's output.

  Swapping in the exception type name alone (mutant `plug_hint_echoes_exc_type_only`) passes the whole suite, so the fix is cheap and currently unpinned.

## Medium Priority
None.

## Low Priority
1. **L1. Out-of-range integers give a 500 instead of the JSON envelope.**
   - **Where:** `api_v1.py:180-194` (`/api/v1/jobs?ids=` has no integer bound) and `:223-228` (`offset` has no upper bound).
   - **Evidence:** `ids=10**30` and `offset=10**20` each return **500** `Internal Server Error` (SQLite `OverflowError`).
   - **Fix:** bound `offset` (for example `le=10**12`), and reject ids outside `1..2^63-1` with a 422.
2. **L2. The error envelope is not uniform.**
   - **Where:** `auth.py:177/181/189` (`PlainTextResponse("Forbidden")`), `request_limits.py:22` (plain-text 413), and the owner's unknown `/api/v1/*` paths (FastAPI `{"detail": "Not Found"}`).
   - **Evidence:** no token, a forged key, a wrong `aud`, a foreign `common_name`, and any non-allowlisted path all return 403 `text/plain` "Forbidden". A 70 KB prompt returns 413 `text/plain`.
   - **Impact:** the spec's table and `docs/system-architecture.md:82` say "every error is `{"error": …}`" (the doc does say "plain 403" at `:52`). The plugin copes, because it maps on status, but other clients see mixed shapes.
   - **Fix:** in `access_guard` and the limiter, return the JSON envelope when the path starts with `/api/v1`.
3. **L3. `/api/v1/images/{id}/file` is confined to `data_dir`, not to the image store.**
   - **Where:** `api_v1.py:249`, via `resolve_under(settings.data_dir, …)`.
   - **Evidence:** an images row whose `file_png` is `atelier.db` made the endpoint serve **the whole SQLite database** as `image/png` to the service identity.
   - **Reach:** this needs a corrupted or malicious row, since only `save_result` writes these paths, and all of them live under `images/`. Defense in depth.
   - **Fix:** resolve under `data_dir / "images"`. Mutant `file_no_confinement` (dropping `resolve_under` entirely) also survives, so no test pins traversal refusal on this endpoint.
4. **L4. A 401 gets a generic hint.**
   - **Where:** `server.py:106-116`. A 401 reads "Atelier answered with HTTP 401." while a 403 or a redirect reads "service token rejected: …".
   - **Reach:** Access can answer 401 for a bad or expired service token, depending on the policy.
   - **Fix:** map 401 the same way as 403.
5. **L5. `list_images` can exceed the MCP output cap.**
   - **Where:** `api_v1.py:201-213` returns each image's **full** prompt; the plugin forwards it unchanged (`server.py:304-319`).
   - **Impact:** prompts are bounded only by the 64 KB body limit. With `limit=50` and prompts around 2,000 characters, the result is roughly 100k characters, past the 25k-token `MAX_MCP_OUTPUT_TOKENS` cap the spec designs for. `job_status` with 50 failed jobs carrying 2,000-character ComfyUI errors is similar.
   - **Fix:** truncate `prompt` (and `error` in the job list) to about 200 characters in the trimmed payloads. `get_image` already returns the full detail.
6. **L6. A save failure loses the batch.**
   - **Where:** `server.py:218-228`. `_generate_or_run` saves each finished PNG, and `save_png` raises `ToolError` on any fetch failure (a missing file, a 404, or a malformed name from the API).
   - **Impact:** the whole tool call then fails, and the `batch_id` and `job_ids` of an already-created, paid batch never reach Claude.
   - **Fix:** catch per image, record `"save_error"` on that job, and still return the batch.
7. **L7. An all-digit workflow *name* is treated as an id.**
   - **Where:** `server.py:354-356`, via `workflow.isdigit()`.
   - **Impact:** a workflow named `2024` resolves to id 2024. `isdigit()` also accepts non-ASCII digits (`"١٢"` becomes 12).
   - **Fix:** try a name match first, or take a separate `workflow_id` argument.
8. **L8. `GET /api/v1/workflows` re-parses every stored graph (N+1).**
   - **Where:** `api_v1.py:259-276` calls `get_workflow` (a full `json.loads` of `graph_json`) for each workflow just to compute `has_seed_input`. That reintroduces the listing cost the phase-7 fix removed (about 100 ms for 10 × 2 MB graphs, on the event loop).
   - **Fix:** compute `has_seed_input` at upload time and store it, or query only what `seed_targets` needs.

## Edge Cases Found by Scout
- **Secret echo:** a secret with a trailing newline or space is echoed back (H1).
- **Path handling:**
  - Encoded slashes (`/api/v1/images%2F1`, `/api/v1/images/1%2Ffile`) are decoded **before** both auth and routing, so they reach allowlisted routes consistently, with no escalation.
  - Absolute-form request targets (`GET http://evil/api/v1/models`) are handled the same way.
- **Integer bounds:** giant integers in `ids`/`offset` return 500 (L1).
- **Payload sizes:**
  - A long-prompt `list_images` result can exceed the MCP output cap (L5).
  - The image-file endpoint's confinement boundary is `data_dir`, not the image store (L3).

## Positive Observations (what held, with evidence)
- **Service boundary.** Tested with raw request lines on a real uvicorn server:
  - All nine endpoints answer 200 or 201 for the service identity.
  - **403** for:
    - a trailing slash, `//`, upper-case `API`, `Models`, `/./`, `..` segments and `..%2F`;
    - `;x`, `%00`, `-1`;
    - `HEAD` and `OPTIONS`;
    - `POST` to GET routes and `GET /api/v1/generate`, `DELETE`;
    - `POST /api/v1/gpu` and `/api/v1/gpu/<id>/warm`;
    - `POST /gpu/<id>/warm|stop` and `GET /gpu`;
    - `/gallery`, `/images/1/file`, `/jobs/1/graph.json`, `POST /workflows` and `/static/app.css`.
  - A query string (`?x=/gpu`) and a fragment don't change routing.
  - `SERVICE_ROUTES` uses `fullmatch` and is pinned against the actual route table (`test_service_allowlist_matches_the_api_routes` caught the added-`/gallery`, dropped-generate and added-warm-route mutants).
  - No warm or stop route exists under `/api/v1`, and the plugin has no such tool.
- **Other identities.**
  - No JWT, a JWT signed by another key, a wrong `aud`, or a foreign `common_name` all get 403.
  - The owner can read the API; owner POSTs without an Origin or with a foreign one get **403**, and a same-origin POST gets 201.
  - A cookie-session `text/plain` POST cannot form a valid body (422), and no CORS headers are served.
  - The service identity is correctly exempt from the Origin check, since it is a non-browser client.
  - Service-identity matching uses `hmac.compare_digest`.
- **API validation:**
  - count 0/9/−1, steps and cfg (including `NaN`) out of range, width/height bounds, size-plus-width, width-only and an unknown preset all get 422.
  - Out-of-range fixed seeds (−1, 2^63, 2^70, and 2^63−1 ×2) get 422; an unknown model and an extra body field get 422.
  - A >64 KB body gets 413. `limit=51` and `offset=-1` get 422.
  - An unknown image or workflow gets 404, and path ids `0` and `2^64` get 422.
  - Workflow runs return 422 for keep×2, fixed without a seed, and a bogus mode; the disk guard returns 507 (tested).
  - No response in the probe set contained a traceback, file path, `MODAL_TOKEN` or `sqlite3` text.
  - Image detail and list payloads carry no file paths, and the file download is `image/png` with `nosniff`.
- **Save confinement (real plugin module).** Each of these was refused with a `ToolError`, and nothing was written outside the folder:
  - an absolute `save_to`, `../outside`, a symlinked directory and a symlinked file pointing outside;
  - an API-supplied seed containing `../../../../escape`.

  `~/x.png` and `sub/d.png` stay inside. A new save folder is created with mode **0700**.
- **HTTP client.** It never follows redirects. A 302 to the Access login page maps to "service token rejected: … redirected to its login page", and a 403 to "service token rejected". A 500 HTML body is never echoed.
- **Built zip.** A scratch `build.sh` run produced exactly six files: `plugin.json`, `.mcp.json`, `README.md`, `server.py`, `server.py.lock` and `SKILL.md`. There is no `__pycache__`, `.pyc`, `.env` or `.DS_Store`, and no secret-shaped string. `.mcp.json` holds exactly four `${user_config.*}` references, with the secret marked `sensitive: true` in `plugin.json`. The repo's existing `atelier.plugin` matches that listing.
- **Supply chain.**
  - The lock pins `mcp` 1.30.0, `httpx` 0.28.1, `pydantic` 2.13.5 and `pillow` 12.3.0, with 656 sha256 hashes.
  - `uv run --locked --script` ran with the matching lock (exit 0).
  - It **refused** a changed version bound, and an added dependency (exit 2, "needs to be updated, but `--locked` was provided").
- **Integration test.** It uses real JWT verification: `AccessEdge` mints a real RS256 token only for the matching ID and secret, and the app's own `AccessVerifier` checks signature, `aud`, `iss` and expiry against the patched JWKS. A wrong secret reaches the app with no JWT and gets the real 403. The dev identity is never used.
- **Docs.** README, `system-architecture.md` and the guide all state there are no backups. Warm and stop are described only as owner-only web controls, and never as part of the API or plugin. Credentials appear by name only. The false statement in them is the plugin README's secret claim (H1).

## Test quality: mutation run (33 mutants, full suite each)
**14 killed, 19 survived.** One survivor, `plug_hint_echoes_exc_type_only`, is the H1 fix itself (a behaviour improvement), so the real score is **14 of 32**.
- **Killed:**
  - the allowlist adding `/gallery`, dropping generate, or gaining a warm route;
  - the owner-CSRF exemption for `/api/v1`;
  - the images `limit` cap;
  - generate returning 200, or the disk guard unmapped;
  - the file served as `text/html`;
  - the validation-envelope scope and generate `extra="ignore"`;
  - an unknown workflow returning 422;
  - the plugin's save-confinement check, thumbnail-on-by-default and the 403 hint.
- **Survived, so the safety rule is unpinned:**
  - **Auth matching:** `svc_prefix_match` (`match` instead of `fullmatch`), `svc_method_ignored`, `svc_id_any_segment`. None escalates with today's routes, but nothing stops a future regression.
  - **API:** `file_no_confinement`, `offset_negative_ok`, `jobs_cap_off`, `run_default_keep`, and `trimmed_leaks_path` (a stored file path added to the image-list payload).
  - **Plugin:** `plug_follow_redirects` and `plug_redirect_hint_off` (no test exercises a redirect), `plug_save_dir_0755`, `plug_thumb_budget_10x` and `plug_thumb_side_4x` (the test PNG is too small to exercise the budget), `plug_all_thumbnails` (the "one image per call" rule), `plug_wait_uncapped`, `plug_generate_readonly` (annotations), `plug_extra_allowed` (the plugin's `extra="forbid"`), and `plug_name_resolution_first`.

## Recommended Actions
1. **H1:** strip and validate the configured credentials at startup; never interpolate exception text into hints; add a leak test.
2. **L1–L3:** bound `ids`/`offset`; return the JSON envelope for 403/413 under `/api/v1`; confine file serving to `images/`.
3. **L4–L8:** map 401 like 403; truncate prompt and error text in list payloads; keep the batch result when a save fails; resolve workflow names before ids; drop the N+1 graph parse.
4. **Tests:** pin exact-match allowlisting (a trailing slash, a prefix and a method mismatch get 403), a redirect answer, the thumbnail budget (with a large noisy PNG), the one-image rule, save-folder mode 0700, `extra="forbid"` on plugin args, and file-endpoint confinement.

### Plan follow-ups (no plan edits made)
- **Todo items that hold:** the API, `SERVICE_ROUTES`, the plugin skeleton, the lock, the build and marketplace, and the docs.
- **Correction:** the "every plugin error … never echoes headers or secrets" requirement is **not met** until H1 is fixed.
- **Still open:** the owner-gated install and acceptance steps.

### Metrics
- **Type coverage:** not measured.
- **Linting:** 0 issues (`ruff check`).
- **Suite:** 543 passed, 1 deselected.
- **Mutation score:** 14/32 behaviour mutants (44%).

### Unresolved Questions
1. **L2:** should 403 and 413 under `/api/v1` return the JSON envelope, or is plain text acceptable given `system-architecture.md:52` already documents a "plain 403"?
2. **L5:** is a server-side prompt truncation in list payloads acceptable, or should the plugin truncate instead?

Status: DONE_WITH_CONCERNS
Summary: The authorization boundary, API validation, save confinement, locked dependencies and clean zip all held under probes. One High must be fixed before the owner installs the plugin: a mis-pasted Client Secret (trailing newline or space) is echoed verbatim into every tool result. There are eight Lows, including 500s on giant integers, an inconsistent error envelope and file serving confined only to `data_dir`, and the tests catch 14 of 32 behaviour mutants.

---

## Re-review (2026-09-27, after the implementer's "Review fixes" round)

**Method.** Read-only, with scratch probes (a subprocess startup run, the plugin module loaded in process against a scripted local HTTP server, and a real uvicorn server with raw request lines) and 45 mutants, each run through the full suite. Checks: `uv run ruff check` is clean; `uv run pytest -q` gives 584 passed, 1 deselected. The rebuilt zip (from a scratch copy) still holds exactly six files.

**Owner decisions applied:** every 403 and 413 under `/api/v1` uses the JSON envelope; the plugin, not the server, shortens prompts in list output.

### Verdicts

| Finding | Verdict | Evidence |
|---|---|---|
| **H1** secret echo | **Closed** | See below. |
| **L1** 500 on giant ints | **Fixed** | `ids=10**30`, `0` and `-5`, and `offset=10**20`, all return 422 with the envelope; `offset=10**12` returns 200. |
| **L2** envelope | **Fixed, one gap** (R1) | 403 under `/api/v1` (no token, a non-allowlisted path, owner cross-origin POST) returns JSON `{"error":{"code":"forbidden",…}}`; HTML 403 stays `text/plain`. A 413 with a declared length returns JSON under `/api/v1` and plain text on HTML (`/presets`). |
| **L3** file confinement | **Fixed** | Rows pointing at `atelier.db`, `images/../atelier.db` and `../../etc/hosts` all return 404 `not_found`. |
| **L4** 401 hint | **Fixed** | A 401 now reads "service token rejected: …". |
| **L5** list size | **Fixed (plugin side, per owner)** | A 3,000-char prompt comes back as 200 chars in `list_images`, and a 3,000-char error as 200 chars in `job_status`. The full text is absent from the result. |
| **L6** lost batch | **Fixed** | With one of two saves failing, the result keeps `batch_id` 9, one saved file, and `save_error` only on the failing job. |
| **L7** digit names | **Fixed** | `"2024"` resolves to the workflow *named* 2024 (id 3); `"7"` with no such name falls back to id 7; `"١٢"` is never treated as an id. |
| **L8** N+1 | **Fixed** | One query, parsed off the event loop via `to_thread`. |

**H1 in detail.**
- **Strippable variants:** a secret with a trailing `\n`, a trailing `\r\n`, a trailing space or a leading tab is stripped at startup, and the server starts. Across **192 tool calls** (8 tools × 6 answers: 200, 403, 401, 302, a 500 HTML page, and a refused connection), the secret appeared in **no** result.
- **Refused variants:** an interior CRLF, an interior tab, a non-ASCII character or DEL is refused at startup with `ConfigurationError: ATELIER_CF_CLIENT_SECRET contains …`. The value itself is absent from the subprocess's stdout and stderr (exit 1).
- **Logs:** 140 KB of DEBUG logging captured from every run contained no secret.
- **Hints:** a transport error now reports only `type(exc).__name__` and the base URL.
- The plugin README's claim is now true.

**Limits are still enforced before parsing.**

| Request | Result |
|---|---|
| API, declared Content-Length 200 KB | 413 JSON, and the parser **never started** |
| HTML `/presets`, declared length or chunked | 413 `text/plain`, and the parser never started |
| `/workflows`, 4 MB multipart, chunked | 413, parse cut off at 3 MB (unchanged) |
| API, lying Content-Length (50) | 422 envelope: only 50 bytes read, the rest refused by the HTTP layer |

The thumbnail budget holds on a noisy 2048 px PNG: one WebP, 12,128 bytes, 256×256.

### New finding
- **R1 (Low; the mechanism predates this round and surfaces under owner decision 1): an over-limit *chunked* JSON body under `/api/v1` gets a 400 in FastAPI's shape, not the 413 envelope.**
  - **Evidence:** a chunked 200 KB `POST /api/v1/generate` returned **400 `{"detail":"There was an error parsing the body"}`**.
  - **Mechanism:** `_BodyTooLarge` (`atelier/request_limits.py:26`) is an `Exception`. FastAPI's JSON body reader wraps any `Exception` raised while reading into `HTTPException(400)`, as confirmed in the `fastapi.routing` source. So the limiter's own `except*` never sees it for JSON routes.
  - **Impact:** the limit is still enforced (the read stops just past 64 KB), but the owner's "every 413 under `/api/v1` uses the envelope" decision is not met for chunked uploads.
  - **Fix:** make `_BodyTooLarge` subclass `BaseException`. `except* _BodyTooLarge` matches it either way, and FastAPI's `except Exception` then lets it through.

### Mutation re-run (45 mutants: the original 33, re-anchored, plus 12 on this round's code)
**42 killed, 3 survived.**
- **Killed:**
  - all of the first round's actionable survivors, including exact-match allowlisting, the method check, id shape, redirect following and its hint, save-folder mode 0700, the thumbnail budget and side, the one-thumbnail rule, the wait cap, the `generate` annotation, and plugin `extra="forbid"`;
  - every new-code mutant: strip, control-character check, `{exc}` re-interpolation, the ids range check, the offset cap, the images-root check, both auth envelope directions, both limiter envelope directions, the limiter's declared-length check, prompt truncation, the per-image save guard, and `isdigit()`.
- **Survived (low value):**
  - `plug_no_ascii_check`: without it, httpx still refuses a non-ASCII secret at import with a position-only error, so nothing leaks. The test doesn't assert the error type.
  - `plug_name_resolution_first`: no test has two workflows with the target listed second.
  - `workflows_listing_on_loop_n1`: performance only.

### Regression sweep: held
- **Service boundary:** exact-match 403s re-checked on raw requests: trailing slash, `HTML`, and owner cross-origin POSTs.
- **HTML shapes** are unchanged, both 403 and 413.
- **Zip contents** are unchanged.
- **Save errors:** the new `save_error` text comes only from `ToolError` hints or OS path errors, and never from headers.

Status: DONE
Summary: H1 is closed: 192 tool calls, the startup path, stderr and logs never contained the secret, and header-illegal values are either stripped or refused without echo. All eight Lows are fixed as decided, and 42 of 45 mutants are caught. One new Low remains: a chunked over-limit JSON body under `/api/v1` returns FastAPI's 400 `{"detail"}` instead of the 413 envelope, fixed by making the limiter's exception a `BaseException`.
