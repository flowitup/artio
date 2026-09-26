---
phase: 8
title: "JSON API, Claude plugin & final acceptance"
status: pending
priority: P2
effort: "9h"
dependencies: [7]
---

# Phase 8: JSON API, Claude plugin & final acceptance

## Context Links

<!-- Updated: Red Team 2026-09-25 - F13 plugin install -->
<!-- Updated: Validation Session 1 - verified uv script lock and plugin marketplace -->

- Contract criterion 10, plus criteria 1–14 for the final acceptance run: [brainstorm report](../reports/brainstorm-260925-1507-atelier-image-studio-hetzner.md)
- Brief §2 (plugin layout, docs), §8 (plugin env, tools, `/api/v1`, `build.sh`) and §9 item 8: [architecture brief](./reports/architecture-brief.md)
- MCP output cap (`MAX_MCP_OUTPUT_TOKENS` 25k, images counted as base64 text), tool timeouts, `userConfig`: [research-02 §6](./research/researcher-02-modal-sdk-app-plugin.md)
- Red-team evidence: [scope](./reports/red-team-scope-complexity-critic.md) (Findings 1, 2, 3, 9), [security](./reports/red-team-security-adversary.md) (Findings 3, 4, 5), [assumptions](./reports/red-team-assumption-destroyer.md) (Finding 8, lifetime item 14).
- Verified external facts:
  - Claude Code plugin reference (fetched 2026-09-25):
    - `userConfig` options need `type`, `title` and `description`; `"sensitive": true` stores the value in the OS credential store; `directory` is a valid type.
    - `${user_config.KEY}` is substituted in MCP server config, including `env`, and `${CLAUDE_PLUGIN_ROOT}` in `command`, `args` and `env`.
    - The configuration dialog appears only when installing through `/plugin` in a session; otherwise use `/plugin configure <name>`. `claude plugin install` installs only from marketplaces.
  - mcp 1.30.0: `mcp/server/fastmcp/__init__.py` exports `FastMCP`, `Context` and `Image`. `Image(data=…, format="webp")` has MIME type `image/webp` (`utilities/types.py:28-31`). A list result is flattened into text and image blocks (`utilities/func_metadata.py:526-551`).
  - mcp 1.30.0 `server.py`: `Context.report_progress(progress, total=None, message=None)` at :1197, `FastMCP.call_tool(name, arguments)` at :359, and the `tool()` decorator returns the function unchanged (:520).
  - uv 0.9.26, from a hermetic run in Validation Session 1:
    - `uv lock --script s.py` writes `s.py.lock`;
    - `uv run --locked --script s.py` runs when the lock matches, and refuses ("needs to be updated, but `--locked` was provided") after an unrelocked metadata change;
    - `uv run --frozen --script` runs without the check.
  - The Claude desktop app runs Folio's plugin with the same `/Users/sweet-home/.local/bin/uv`, per the local process list.
  - Claude Code 2.1.282 and the marketplace docs (fetched 2026-09-25):
    - `claude plugin marketplace add <URL | path | GitHub repo>` registers a marketplace, and `claude plugin install <plugin>@<marketplace>` installs from it.
    - `claude plugin validate <dir>` checks a marketplace directory.
    - `.claude-plugin/marketplace.json` needs `name`, `owner` and `plugins`, and each entry needs `name` and a `source` path relative to the marketplace root, without `..`.
- Folio plugin patterns, re-verified:
  - `folio/folio-plugin/.claude-plugin/plugin.json` (name, version, description, author, keywords).
  - `.mcp.json` runs `uv run --script ${CLAUDE_PLUGIN_ROOT}/mcp_servers/folio_mcp/server.py` and passes its secrets from the environment.
  - The `server.py:1-12` PEP 723 header pins `mcp[cli]>=1.2,<2`.
  - `folio/folio-plugin/README.md:61` installs the `.plugin` zip in the Claude desktop app.
- Verify deploys without the live URL: `learnflow/.claude/skills/learnflow/SKILL.md:153-157`.

## Overview

When this phase is done:
- a versioned JSON API under `/api/v1` exposes exactly what criterion 10 needs: models, generate, job status, image list, search and fetch, stored workflows and their runs, and GPU status. The plugin's service identity can call nothing else;
- a Claude plugin (`plugin/`) lets Claude drive Atelier with the Access service token. It saves full PNGs only inside its configured folder and returns a thumbnail only when asked. It installs in the Claude desktop app from `atelier.plugin` (the primary route) or in Claude Code from a one-entry local marketplace;
- the docs are complete: `docs/deployment-guide.md`, `docs/system-architecture.md` and the rewritten `README.md`;
- a full acceptance run of criteria 1–14 on production is recorded in this file, with text-only evidence.

Priority P2.

## Key Insights

<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->
<!-- Updated: Red Team 2026-09-25 - F13 plugin install -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->
<!-- Updated: Validation Session 1 - verified uv script lock and plugin marketplace -->

- **The service token gets exactly criterion 10's endpoints.** `SERVICE_ROUTES` (phase 3) lists the nine `/api/v1` method-and-path pairs below, and nothing else; HTML routes stay owner-only. The plugin has no warm or stop tool and there is no `POST /api/v1/gpu/*`, because criterion 10 asks only to "see GPU status". The Modal spend limit (D2) caps GPU cost.
- **Thumbnails are off by default (D3).**
  - An inline image goes to Anthropic and stays in the local Claude transcript, outside Atelier's store, its deletes and its backups. So `generate`, `get_image` and `run_workflow` take `include_thumbnail: bool = False`.
  - When asked, a call returns at most **one** WebP of at most 256 px and about 20 KB, which is about 27k base64 characters and stays under the 25k-token cap.
- **Saved files stay inside one folder.**
  - `save_to` is a model-controlled argument, so it is resolved and must be `is_relative_to(save_dir)`; anything else is an error.
  - The default `save_dir` is `~/Atelier`, created with mode 0700, which is not a synced folder. It is configurable in `userConfig`.
- **The plugin's dependencies are locked.** `uv lock --script` produces a committed `server.py.lock`, and `.mcp.json` runs `uv run --locked --script`, which refuses to start when the script's metadata no longer matches its lock (verified with uv 0.9.26). A new release of a transitive dependency can't slip into a process that holds the service-token secret.
- **`generate` waits up to about 110 s by default, hard-capped at 120 s.**
  - `MCP_TOOL_TIMEOUT` is very long, and a main-conversation call past two minutes moves to a background task. The wait therefore stays just under that.
  - During the wait the tool reports progress through `ctx.report_progress`. It then returns job IDs for `job_status` if anything is still running. This reuses the web UI's job model; no second execution path exists.
- **The plugin never follows Access's login redirect.** It sends `CF-Access-Client-Id` and `CF-Access-Client-Secret`, and Access turns them into a JWT whose `common_name` the origin checks. httpx runs with `follow_redirects=False`, so a 302 to the login page is reported as "service token rejected", with a hint, instead of an HTML page being parsed.
- **Two install routes.**
  - **Primary, as for Folio:** `build.sh` builds `atelier.plugin`, whose consumer is the Claude desktop app.
  - **Secondary, Claude Code:** a one-entry local marketplace registered with `claude plugin marketplace add <repo path>`, then `claude plugin install atelier@atelier-local` (or `/plugin install` in a session), then `/plugin configure atelier` for the secrets. These commands were verified against Claude Code 2.1.282.
  - If the desktop route doesn't support `userConfig`, the fallback is Folio's pattern: environment passthrough from the owner's shell, still with no literal secret in any file.
- **The API reuses the HTML routes' services, with one addressing scheme.** Workflows are addressed by `{id}`; the plugin resolves a name through `list_workflows`. Pagination is `limit` (≤ 50) plus `offset`, newest first, the same order as the gallery. `seed_mode` (random, fixed or keep) matches phase 7. The only image-file path is `/api/v1/images/{id}/file`.
- **The plugin integration test uses real JWT verification.**
  - It runs the real app under uvicorn on a random local port, in its normal (non-dev) auth mode, with `FakeModalGateway` as the only fake.
  - A test-only ASGI wrapper plays Cloudflare's edge: it turns the plugin's service-token headers into a real RS256 JWT signed by the test key, which the app verifies through the patched JWKS fetch.
  - The dev identity is not used, as brief §5 requires.

## Requirements

<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->
<!-- Updated: Red Team 2026-09-25 - F13 plugin install -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->
<!-- Updated: Validation Session 1 - verified uv script lock and plugin marketplace -->
<!-- Updated: Validation Session 1 - service token 1 year with reminder -->

Functional, JSON API (`routes/api_v1.py`). Errors return `{"error": {"code", "message"}}`. Path parameters are named `image_id` and `workflow_id` in code, so the route-enumerating crawl can fill them; the table abbreviates them as `{id}`. These nine endpoints form `SERVICE_ROUTES`:

| Method and path | Input | Output |
|---|---|---|
| `GET /api/v1/models` | none | Models with presets, defaults and bounds |
| `POST /api/v1/generate` | `{model, prompt, negative?, size?, width?, height?, steps?, cfg?, seed?, count?}` | 201 `{batch_id, job_ids}`; 422 on validation; 507 on the disk guard |
| `GET /api/v1/jobs?ids=1,2` | none | `[{id, status, error, image_id, seed, model_id, duration_s, est_cost_usd}]` |
| `GET /api/v1/images?q=&tag=&starred=&model=&limit=&offset=` | `limit` ≤ 50 | A trimmed metadata list, newest first |
| `GET /api/v1/images/{id}` | none | Full metadata: every criterion-2 field plus tags and workflow |
| `GET /api/v1/images/{id}/file` | none | `image/png` |
| `GET /api/v1/workflows` | none | `[{id, name, backend_id, has_seed_input}]` |
| `POST /api/v1/workflows/{id}/run` | `{seed_mode?: random\|fixed\|keep, seed?, count?}` | 201 `{batch_id, job_ids}` |
| `GET /api/v1/gpu` | none | Per-backend status, computed on read (phase 6) |

Functional, plugin (`plugin/`):
- **Tools:** `list_models`, `generate`, `job_status`, `list_images`, `get_image`, `list_workflows`, `run_workflow` and `gpu_status`.
  - Each has strict pydantic input (`extra="forbid"`), and all eight are annotated `readOnlyHint` except `generate` and `run_workflow`.
  - `generate`, `run_workflow` and `get_image` take `include_thumbnail: bool = False`, and save PNGs as `atelier-<id>-<seed>.png` under `save_dir`, or at a `save_to` that resolves inside it.
  - `run_workflow` takes a workflow name or ID, and resolves a name through `list_workflows`.
  - `list_images` takes `query`, `tag`, `starred`, `model`, `limit` (default 20, at most 50) and `offset`.
- **`plugin.json`** declares `userConfig` options, each with `type`, `title` and `description`:
  - `base_url` (default `https://atelier.flowitup.com`);
  - `cf_client_id` (required);
  - `cf_client_secret` (required, `sensitive: true`);
  - `save_dir` (type `directory`, default `~/Atelier`).
- **`server.py.lock`**, generated by `uv lock --script` and committed. `.mcp.json` runs `uv run --locked --script`.
- **`skills/atelier/SKILL.md`**:
  - when to use the tools, and the flow: check `gpu_status` (a cold start takes about 70 s), generate and wait, `job_status` if needed, `get_image`;
  - ask for a thumbnail only when the user wants to see the image;
  - error cases.

  It never suggests warming the GPU.
- **`build.sh`** zips the plugin (including `server.py.lock`) into `atelier.plugin` for the Claude desktop app, and lists its contents.
- **`.claude-plugin/marketplace.json`** at the repo root is the one-entry local marketplace for the Claude Code route:

  ```json
  {"name": "atelier-local", "owner": {"name": "Manh Trung BUI"},
   "plugins": [{"name": "atelier", "source": "./plugin", "description": "Drive Atelier from Claude"}]}
  ```

  The entry name must equal the manifest name `atelier`, and `source` is relative to the repo root, the marketplace root, with no `..`. This follows the verified marketplace docs.

Functional, docs:
- `docs/system-architecture.md` covers components, data flows, auth and route authorization, the job lifecycle, GPU control, storage, backups and deploy.
- `docs/deployment-guide.md` gains:
  - plugin installation by both routes;
  - the service token's 1-year lifetime, its recorded expiry date and calendar reminder, rotation, and the leak response: revoke **immediately**, with no grace period;
  - a final review.
- `plugin/README.md` covers install, configure, the tools, and privacy: thumbnails are opt-in, transcripts are kept per `cleanupPeriodDays` in `~/.claude/settings.json`, and saves are confined to `save_dir`.
- `README.md` is rewritten: what Atelier is, local development, commands, and links to the docs.

Non-functional:
- No tool result carries more than one image, or more than about 25 KB of base64 image data; text stays compact.
- Every plugin error names the HTTP status and a hint, following Folio's `_error_hint` pattern, and never echoes headers or secrets.

## Architecture

<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

```
Claude ─► atelier MCP (uv run --locked --script, stdio) ─► httpx (no redirects) + CF-Access-Client-Id/Secret
      ─► Cloudflare Access (Service Auth policy) ─► JWT common_name=<client id> ─► origin access_guard (service)
      ─► SERVICE_ROUTES only ─► /api/v1 routes ─► same services as HTML ─► jobs/worker/Modal
generate(wait≤120 s): POST /api/v1/generate ─► poll /api/v1/jobs every 3 s (report_progress)
      ─► done: GET /api/v1/images/{id}/file ─► save PNG under save_dir
      ─► include_thumbnail? [json, one WebP ≤256 px ≤20 KB] : [json]      | still running: json with job ids
```

`plugin/.mcp.json`:

```json
{
  "mcpServers": {
    "atelier": {
      "command": "uv",
      "args": ["run", "--locked", "--script", "${CLAUDE_PLUGIN_ROOT}/mcp_servers/atelier_mcp/server.py"],
      "env": {
        "ATELIER_BASE_URL": "${user_config.base_url}",
        "ATELIER_CF_CLIENT_ID": "${user_config.cf_client_id}",
        "ATELIER_CF_CLIENT_SECRET": "${user_config.cf_client_secret}",
        "ATELIER_SAVE_DIR": "${user_config.save_dir}"
      }
    }
  }
}
```

The core of the plugin server (sketch):

```python
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]>=1.2,<2", "httpx>=0.27", "pydantic>=2.6", "pillow>=10.4"]  # mcp 2.x drops fastmcp
# ///
MAX_WAIT_S, THUMB_SIDE, THUMB_BUDGET = 120, 256, 20_000

def save_dir() -> Path:
    root = Path(os.environ.get("ATELIER_SAVE_DIR") or "~/Atelier").expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root

def target_path(save_to: str | None, default_name: str) -> Path:
    root = save_dir()
    path = (root / (save_to or default_name)).expanduser().resolve()   # an absolute save_to replaces root here
    if not path.is_relative_to(root):
        raise ToolError(f"save_to must be inside the configured image folder ({root}).")
    return path

def thumbnail_webp(png: bytes) -> bytes:
    img = PILImage.open(io.BytesIO(png)); img.thumbnail((THUMB_SIDE, THUMB_SIDE))
    for quality in (80, 70, 60, 50, 40, 30):
        buf = io.BytesIO(); img.save(buf, "WEBP", quality=quality)
        if buf.tell() <= THUMB_BUDGET:
            return buf.getvalue()
    img.thumbnail((THUMB_SIDE // 2, THUMB_SIDE // 2)); buf = io.BytesIO(); img.save(buf, "WEBP", quality=40)
    return buf.getvalue()

@mcp.tool(structured_output=False)
async def generate(args: GenerateArgs, ctx: Context) -> list:
    created = await api("POST", "/api/v1/generate", json=args.request_body())
    jobs = await wait_for_jobs(created["job_ids"], min(args.wait_seconds, MAX_WAIT_S), ctx)
    saved = [await save_png(j["image_id"], None) for j in jobs if j["status"] == "done"]
    result = {"batch_id": created["batch_id"], "jobs": jobs, "saved_to": [str(s.path) for s in saved]}
    if any(j["status"] in ("queued", "submitted") for j in jobs):
        result["next"] = "Still running. Call job_status with these job ids."
    blocks: list = [json.dumps(result)]
    if args.include_thumbnail and saved:
        blocks.append(Image(data=thumbnail_webp(saved[0].png), format="webp"))   # one thumbnail per call at most
    return blocks
```

The integration test's edge stand-in (test code only):

```python
class AccessEdge:
    """Plays Cloudflare Access for the test: valid service-token headers become a signed JWT header."""

    def __init__(self, app, key, client_id: str, client_secret: str):
        self.app, self.key, self.client_id, self.client_secret = app, key, client_id, client_secret

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if headers.get(b"cf-access-client-id") == self.client_id.encode() and \
               headers.get(b"cf-access-client-secret") == self.client_secret.encode():
                token = mint(self.key, common_name=self.client_id).encode()
                scope = {**scope, "headers": [*scope["headers"], (b"cf-access-jwt-assertion", token)]}
        await self.app(scope, receive, send)
```

## Related Code Files

<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F13 plugin install -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

Create:
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/routes/api_v1.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/plugin/.claude-plugin/plugin.json`
- `/Users/sweet-home/Works/qwen21-uc-modal/plugin/.mcp.json`
- `/Users/sweet-home/Works/qwen21-uc-modal/plugin/mcp_servers/atelier_mcp/server.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/plugin/mcp_servers/atelier_mcp/server.py.lock` (generated by `uv lock --script`, committed)
- `/Users/sweet-home/Works/qwen21-uc-modal/plugin/skills/atelier/SKILL.md`
- `/Users/sweet-home/Works/qwen21-uc-modal/plugin/README.md`
- `/Users/sweet-home/Works/qwen21-uc-modal/plugin/build.sh`
- `/Users/sweet-home/Works/qwen21-uc-modal/.claude-plugin/marketplace.json` (the one-entry local marketplace)
- `/Users/sweet-home/Works/qwen21-uc-modal/docs/system-architecture.md`
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_api_v1.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_plugin_integration.py`

Modify:
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/auth.py`: fill `SERVICE_ROUTES` with the nine API pairs.
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/main.py`: include the API router.
- `/Users/sweet-home/Works/qwen21-uc-modal/pyproject.toml` and `uv.lock`: add the dev dependency `mcp[cli]>=1.2,<2` so tests can load the plugin server.
- `/Users/sweet-home/Works/qwen21-uc-modal/README.md`: rewrite.
- `/Users/sweet-home/Works/qwen21-uc-modal/docs/deployment-guide.md`: plugin setup by both routes, token rotation and immediate revocation on a leak, final review.
- `/Users/sweet-home/Works/qwen21-uc-modal/plans/260925-1331-atelier-image-studio/phase-08-api-plugin-and-acceptance.md`: fill in the Acceptance record.

Delete: none.

## Implementation Steps

<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->
<!-- Updated: Red Team 2026-09-25 - F13 plugin install -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->
<!-- Updated: Validation Session 1 - verified uv script lock and plugin marketplace -->

1. **JSON API.**
   - Write `routes/api_v1.py` with the nine endpoints. It uses pydantic request models, calls the existing services and maps their errors: `ValidationError` to 422, `DiskGuardError` to 507, unknown IDs to 404.
   - Fill `SERVICE_ROUTES` in `auth.py` with exactly these pairs, and include the router in `main.py`.
2. **API tests.** Write `tests/test_api_v1.py` with real RS256 service JWTs from the phase 3 fixtures:
   - `test_service_token_identity_can_call_every_allowlisted_endpoint`.
   - `test_service_allowlist_matches_the_api_routes`: every `/api/v1` route in `app.routes` is in `SERVICE_ROUTES`, and every entry matches a route.
   - `test_api_rejects_missing_or_foreign_tokens`: 403 for no JWT and for a `common_name` that is not the plugin's.
   - `test_generate_returns_job_ids_and_jobs_endpoint_tracks_them`.
   - `test_generate_refused_by_disk_guard_returns_507`.
   - `test_images_search_paginates_by_offset_newest_first`.
   - `test_image_file_download`.
   - `test_workflow_run_by_id_with_seed_mode`.
   - `test_gpu_status_is_read_only`: no POST route exists under `/api/v1/gpu`.
3. **Plugin skeleton.**
   - `plugin.json`: name `atelier`, version `0.1.0`, the description, author `Manh Trung BUI` (as in Folio's manifest), keywords, and `userConfig` with `title` and `description` on every option.
   - `.mcp.json` as sketched.
   - `server.py` with the PEP 723 header, the `_Client` (base URL and CF headers from env, `follow_redirects=False`, a 30 s timeout), `_error_hint` per status, `save_dir`, `target_path`, `thumbnail_webp`, `save_png`, `wait_for_jobs` and the eight tools.
   - Run `uv lock --script plugin/mcp_servers/atelier_mcp/server.py` and commit the lock.
4. **Plugin docs.** Write `SKILL.md` (frontmatter `name` and `description`, then workflow steps and error cases, with no warming advice) and `plugin/README.md` (install by both routes, configure, the tools, and the privacy notes).
5. **Build script and marketplace.**
   - Write `build.sh`: `set -euo pipefail; cd "$(dirname "$0")"; rm -f atelier.plugin`, then `zip -r -X atelier.plugin .claude-plugin .mcp.json mcp_servers skills README.md -x '*/__pycache__/*' '*.DS_Store'`, then `unzip -l atelier.plugin`.
   - Write `.claude-plugin/marketplace.json` as in Requirements, then run `claude plugin validate /Users/sweet-home/Works/qwen21-uc-modal`, which checks a marketplace directory.
6. **Plugin integration test.** Write `tests/test_plugin_integration.py`:
   - Start the real app, wrapped in `AccessEdge`, under `uvicorn.Server` in a thread on 127.0.0.1 with a free port. Use a test `ATELIER_ENV=test` configuration with a known AUD, owner and client ID, a temporary data dir, and `FakeModalGateway`.
     - The fake is thread-safe, with a `threading.Lock` and no loop-bound asyncio primitive, because the app's loop runs on the server thread.
     - A helper completes spawned calls with real PNG bytes.
   - Set every environment variable with `monkeypatch`. Load `plugin/mcp_servers/atelier_mcp/server.py` with importlib, pointing `ATELIER_BASE_URL` at the server, `ATELIER_CF_CLIENT_ID`/`ATELIER_CF_CLIENT_SECRET` at the test token, and `ATELIER_SAVE_DIR` at a temporary dir.
   - Drive the tools through `await server.mcp.call_tool(...)`. Assert:
     - `list_models` lists the registry.
     - `generate` with count 2 and `wait_seconds` 10 saves two PNGs under the save dir and returns **no** image block by default. With `include_thumbnail=true` it returns exactly one `image/webp` block, which decodes to at most 20,000 bytes with a long side of at most 256 px.
     - `get_image` with a `save_to` outside the save dir is refused, and one inside is saved with the server's bytes.
     - `job_status`, `list_images(query=…)`, `list_workflows`, `run_workflow` (by name) and `gpu_status` all work.
     - With the fake left pending, `generate` returns `next` and the job IDs.
     - With a wrong secret configured, every tool reports "service token rejected" (the edge adds no JWT and the app answers 403).
7. **Docs.** Write `docs/system-architecture.md`, finish `docs/deployment-guide.md`, and rewrite `README.md`.
8. **Local checks.** Run `uv run ruff check`, `uv run pytest -q`, `bash plugin/build.sh`, and the zip-content checks from Verification. Commit as `feat: JSON API and Claude plugin for Atelier`. **[OWNER-GATED]** The push to `main` deploys.
9. **[OWNER-GATED] Install the plugin on the owner's machine.**
   - **Primary route:** install `plugin/atelier.plugin` in the Claude desktop app (Settings → Capabilities, or by opening the file), and enter `cf_client_id` and `cf_client_secret` when prompted. The secret comes from the password manager.
   - **If the desktop route shows no configuration for `userConfig`:** switch `.mcp.json` to environment passthrough (`"${ATELIER_CF_CLIENT_SECRET}"` read from the owner's shell environment, as Folio does). Never put a literal value in a file.
   - **Claude Code route:**
     - run `claude plugin marketplace add /Users/sweet-home/Works/qwen21-uc-modal`;
     - install with `claude plugin install atelier@atelier-local` in the shell, or `/plugin install atelier@atelier-local` in a session;
     - then run `/plugin configure atelier` to enter the secrets. A shell install shows no configuration dialog.
   - Run `claude plugin validate /Users/sweet-home/Works/qwen21-uc-modal/plugin`.
10. **[OWNER-GATED] Final acceptance run on production,** spending about $1.50 of GPU. Ask the owner before starting, and before the row 12 `.env` change. Execute every row of the Acceptance record in order, and fill in the evidence and result. Stop at the first failure and fix it in its owning phase before continuing.

## Todo List

- [ ] `/api/v1` routes over the shared services, with error mapping; `SERVICE_ROUTES` equals the nine criterion-10 endpoints; API tests with real service JWTs
- [ ] Plugin manifest with titled and described `userConfig` (secret marked sensitive), and `.mcp.json` using `${user_config.*}` and `uv run --locked --script`
- [ ] Plugin server: eight tools, no-redirect client, error hints, saves confined to `save_dir`, opt-in single WebP thumbnail within budget; `server.py.lock` committed
- [ ] `SKILL.md` (no warming advice), plugin README (privacy notes), `build.sh`, and the local marketplace file; the zip holds no secrets
- [ ] Plugin integration test through the emulated Access edge with real JWTs and the thread-safe fake gateway
- [ ] `docs/system-architecture.md`, the completed `docs/deployment-guide.md`, the rewritten README
- [ ] [OWNER-GATED] Plugin installed (desktop route, or Claude Code route) with the service token; `claude plugin validate` clean
- [ ] [OWNER-GATED] Acceptance record for criteria 1–14 filled with text-only evidence

## Success Criteria

<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

- **Criterion 10:**
  - From Claude, the owner lists models, generates and waits (getting a saved PNG, and a thumbnail when asked), checks status, lists and searches images, fetches an image to a local file, lists and runs a stored workflow, and sees GPU status.
  - `curl` to `https://atelier.flowitup.com/api/v1/models` without the service-token headers is rejected by Access (302 or 403).
  - At the origin, `test_api_rejects_missing_or_foreign_tokens` and phase 3's `test_service_identity_is_refused_outside_the_api_allowlist` prove the 403 paths.
  - `test_plugin_integration` passes through real JWT verification.
- **Criteria 1–14:** every row of the Acceptance record below shows "pass" with text evidence.

## Acceptance record

<!-- Updated: Red Team 2026-09-25 - F11 image data control -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

The executor fills this in during step 10. Every live step is **[OWNER-GATED]**. Evidence is **text only**: command output, timings, IDs and HTTP codes. No screenshots or images; `.gitignore` excludes image files under `plans/`.

| # | Check (method) | Evidence (text) | Result |
|---|---|---|---|
| 1 | Private browser session: `atelier.flowitup.com` redirects to the `flowitupteam` login. On the server, `curl` to 127.0.0.1:8090 with no or garbage JWT returns 403. | | |
| 2 | Cold backend: submit a job, see queued → running → done with no 524; the image page shows all settings; reload; `deploy.sh stop` and `start` mid-job and the job completes. | | |
| 3 | Wait until the backend has scaled to zero (ComfyUI serves an identical graph from its cache on a warm container), then remix an earlier image with the same seed and settings; the two PNGs are pixel-identical; the fresh-container live test from phase 2 has passed. | | |
| 4 | Upload a broken workflow, see its error, and retry a failed job. Cancel one queued job and one running job; the running one shows the "GPU may still finish" note. | | |
| 5 | The model picker lists the registry; the gallery model filter works; the second-model test is green in CI. | | |
| 6 | Status matches `modal container list`; warm 5 min is warm within about 70 s; zero about 60 s after expiry and after `deploy.sh stop`; Stop with jobs reaches 0 containers within about 10 s; a job succeeds after `modal app stop --yes` and a redeploy. | | |
| 7 | Batch of 4 with a preset: 4 distinct seeds, all done, grouped in the gallery; Remix prefills the form. | | |
| 8 | Save, load and delete a preset; star and tag an image; find it by prompt text and by tag. | | |
| 9 | Upload an API-format JSON for `qwen21-uc`, run it, see the result with its workflow attached; an invalid graph shows ComfyUI's error. | | |
| 10 | Every plugin tool from Claude; a call without the service token is rejected; the service token gets 403 on an HTML route. | | |
| 11 | `restic snapshots` shows last night's snapshot; the last weekly verify is ok; the restore rehearsal date is in the guide; a failure shows in the UI (phase 5's failure-display step). | | |
| 12 | Temporarily set `ATELIER_DATA_CAP_GB` below usage **[OWNER-GATED]**: a new job is refused with a clear message; usage is visible; restore the value. | | |
| 13 | The last push deployed its digest through the SSH health check (`gh run list`); folio, cdn and learn match their baselines; a `deploy-modal` run succeeds; the hermetic deploy-script tests are green. | | |
| 14 | gitleaks clean in CI; `docker image inspect` env and labels show no credential; the route-enumerating crawl test is green; the plugin zip holds no secret; `git ls-files` has nothing under `logs/` or `out/` and no image under `plans/`. | | |

## Verification

```bash
cd /Users/sweet-home/Works/qwen21-uc-modal
uv run ruff check && uv run pytest -q
uv run pytest -q tests/test_api_v1.py tests/test_plugin_integration.py -v
bash plugin/build.sh
unzip -p plugin/atelier.plugin .mcp.json | grep -c '\${user_config\.'          # 4 references
unzip -p plugin/atelier.plugin .mcp.json | grep -Ei 'secret"\s*:\s*"[^$]' ; test $? -eq 1 && echo "no literal secret"
unzip -l plugin/atelier.plugin | grep -q 'server.py.lock' && echo "lock shipped"
git ls-files 'plans/**/*.png' 'plans/**/*.jpg' 'plans/**/*.jpeg' 'plans/**/*.webp' | wc -l                  # 0
# [OWNER-GATED] live
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://atelier.flowitup.com/api/v1/models   # 302 or 403 without the token
claude plugin validate /Users/sweet-home/Works/qwen21-uc-modal/plugin
gh run list --repo flowitup/atelier --limit 3
```

## Risk Assessment

<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F13 plugin install -->
<!-- Updated: Validation Session 1 - verified uv script lock and plugin marketplace -->

| Risk | Likelihood × impact | Observable signal | Pre-decided response |
|---|---|---|---|
| The configuration dialog isn't shown after install | Medium × Medium | The plugin reports "service token rejected" right after install, with empty client ID and secret | Run `/plugin configure atelier` (Claude Code). On the desktop route, fall back to environment passthrough as Folio does; never a literal secret in a file. |
| The desktop app's install route doesn't support `userConfig` | Medium × Medium | No configuration prompt, and `${user_config.*}` stays unexpanded | Use the environment passthrough fallback from step 9, and record the choice in the plugin README. |
| The local marketplace is rejected | Low × Low | `claude plugin validate` or `marketplace add` reports an error | Fix the file to match the message. The schema and commands were verified against Claude Code 2.1.282 and its docs, and the desktop route is primary. |
| An older uv elsewhere rejects `--locked --script` | Low × Low | The plugin fails to start with a uv flag error | Upgrade uv to 0.9.26 or newer (verified). Otherwise use `--frozen`, which still installs only the locked versions. |
| A thumbnail still overflows the token cap in practice | Low × Medium | Claude Code warns that the result spilled to a file | Lower `THUMB_BUDGET` to 12 KB and `THUMB_SIDE` to 192. The one-thumbnail rule stays. |
| A wait close to 120 s is moved to a background task | Medium × Low | Claude reports the call as backgrounded | Lower the default `wait_seconds` to 90. The job-ID fallback already covers the rest. |
| Access answers service-token API calls with a 302 even with valid headers | Low × High | The plugin reports "service token rejected" live | **[OWNER-GATED]** Check the Service Auth policy includes `atelier-plugin` and that the token isn't expired. Do not weaken origin checks. |
| An acceptance row fails | Medium × Medium | A row reads "fail" | Stop the run, fix it in the owning phase, redeploy **[OWNER-GATED]**, and rerun that row and every row after it. |

**Rollback:**
- `git revert` the API commit and **[OWNER-GATED]** push. The HTML app is unaffected.
- Uninstall the plugin locally.
- If the token leaked, **[OWNER-GATED]** revoke it **immediately** in Zero Trust, with no grace period. Then create a new token, bind it to the Service Auth policy, and reconfigure the plugin.

## Security Considerations

<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->
<!-- Updated: Validation Session 1 - service token 1 year with reminder -->

- **The service token's powers are exactly the nine allowlisted API endpoints:** read models, images, workflows and GPU status; create generations and workflow runs. It has no delete, upload, warm, stop or HTML access. A leak therefore exposes image content and GPU spend, capped by the Modal spend limit (D2), and the response is immediate revocation.
- The client secret lives only in the OS credential store (sensitive `userConfig`) and the owner's password manager, never in the repo, the zip, the logs or a tool result. The token lasts 1 year, with its expiry recorded and a rotation reminder set (phase 4).
- The plugin runs from a committed lockfile. It never follows redirects, never prints headers, and trims error bodies to 500 characters.
- **Image data stays in controlled places.** Full PNGs go only under `save_dir` (`~/Atelier`, 0700 by default), which is enforced. Thumbnails reach Anthropic and the local transcript only when a call asks for one, and the README documents transcript retention.
- API POSTs from the owner's browser still require the origin check. Service-token calls come from the plugin process, not a browser, so they are not a CSRF vector.

## Next Steps

- With every acceptance row passing, set the plan status to completed through the `ak` CLI.
- Future models are added as a registry entry plus a Modal backend, with no migration, as the contract requires.
