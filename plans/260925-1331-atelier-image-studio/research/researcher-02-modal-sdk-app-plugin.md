# Research: Modal SDK, app status, warm-up, auth, pricing, Claude plugin, HTMX, deploy

Date: 2026-09-25. Modal SDK verified against the installed CLI's Python (modal 1.5.5) at
`/Users/sweet-home/.local/share/uv/tools/modal/lib/python3.12/site-packages/modal/` (short: `SDK/`), read
directly with `Read`/`grep`, not from memory. No spawn/remote/deploy/stop call was made; only attribute
introspection and public docs.

## 1. Jobs

**`spawn()`.** `_Function.spawn()` (`SDK/_functions.py:2035`) calls the function asynchronously and
returns a `_FunctionCall[ReturnType]` hydrated with `invocation.function_call_id`. A bound class method
(e.g. `Qwen21UC().generate`) is itself a `modal.functions.Function` instance at runtime (confirmed live:
`type(Qwen21UC().generate)`), so `.spawn()`, `.spawn.aio`, `.remote`, `.local` apply to class methods
exactly as to plain functions. `FunctionCall.object_id` (`SDK/_object.py:307`) is the string to persist in
SQLite; `FunctionCall.from_id(id)` (`SDK/_functions.py:2121`) rehydrates a handle from it later, including
after a process restart.

**`get(timeout=0)` when not ready.** `_FunctionCall.get()` (`SDK/_functions.py:2179`) calls
`_Invocation.poll_function()` (`SDK/_functions.py:319`). If the server returns zero outputs and
`num_unfinished_inputs == 0`, it raises `OutputExpiredError` (the result is gone). If zero outputs but the
input is still in flight, it raises plain `modal.exception.TimeoutError`. Both are in `SDK/exception.py`;
`OutputExpiredError(TimeoutError)` (line 229) **subclasses** `TimeoutError` (line 189), so a bare
`except modal.exception.TimeoutError` catches both — catch `OutputExpiredError` first if expired-vs-still-
running needs distinct handling.

**Result retention.** Modal's job-queue guide (fetched directly) states results are retrievable via
`.get()` "for up to 7 days after completion. After this period, we return an expired response"
(`OutputExpiredError`). This is a server-side policy documented only in the guide, not in SDK code.

**Remote exceptions.** `_process_result()` (`SDK/_utils/function_utils.py:527`) is the single funnel for
every non-success result. A container-side `timeout=` breach (the function's own deadline, e.g.
`timeout=1800`) raises `FunctionTimeoutError` — distinct from the client poll `TimeoutError` above. A
normal exception raised inside `generate`/`run_workflow`/`_run` is pickled server-side and deserialized
back to the *same exception type* locally, with the remote traceback appended; if the class can't be
imported locally, the traceback text is wrapped into `ExecutionError` instead of being lost; with no
structured data at all it raises generic `RemoteError(result.exception)`. Catching `Exception` broadly
around `.get()` is safe — store `str(exc)` in SQLite rather than assume one exception class.

**`cancel()`.** `_FunctionCall.cancel(terminate_containers: bool = False)` (`SDK/_functions.py:2256`)
cancels the input "without retrying." With `terminate_containers=True` it also kills the container(s);
docstring: **"Any other inputs running concurrently on those containers will be rescheduled."** The
cancelled input itself isn't rescheduled, but sibling concurrent inputs on the same container (relevant
given `@modal.concurrent(max_inputs=4)`) get requeued — likely a fresh cold start given `max_containers=1`.
For "Stop": cancel tracked in-flight `FunctionCall`s with `terminate_containers=True` first, then sweep
any container left warm only by `ping()` traffic (nothing to cancel) via `modal container stop` (§2).

**Async variants / thread-safety.** Every public object is generated from a private async implementation
via `synchronize_api()` (`SDK/_utils/async_utils.py:330`, the `synchronicity` library). The blocking form
(`.spawn()`, `.get()`, `.cancel()`) runs the coroutine on a background thread's event loop and blocks the
caller; `.aio` runs the same coroutine on the caller's own running loop. Confirmed live:
`hasattr(gen.generate.spawn, "aio")` and `hasattr(fc.get, "aio")` are both `True`. Inside a FastAPI
`async def` route or a background asyncio task, always use `.spawn.aio`/`.get.aio`/`.cancel.aio` — the
blocking form there would still work but blocks the request-serving loop thread on Modal's RPC round-trip.
Exception: `FunctionCall.from_id()` carries `@deprecate_aio_usage((2025, 11, 14), ...)`
(`SDK/_functions.py:2121`); its own message: **"it doesn't perform any IO, and is safe in async
contexts"** — call it as plain sync even from async code, never `.from_id.aio(...)`. `Client.from_env()`
(`SDK/client.py:238`) is a process-wide singleton behind an `asyncio.Lock`, safe to reuse across
concurrent coroutines in one loop.

## 2. Status

**`get_current_stats()` fields.** Returns `FunctionStats` (`SDK/types.py:135`: `backlog`,
`num_total_runners`, `num_running_inputs`, `input_headroom`), mapped straight from the RPC response;
neither the dataclass nor the proto `.pyi` carries field-level doc comments. Reading for Atelier:
`num_total_runners` = warm container count; `num_running_inputs` = inputs executing now (bounded by
`max_inputs=4` × runners); `backlog` = queued inputs with no runner yet; `input_headroom` = spare
concurrent-input capacity. `[UNVERIFIED]`: the exact `input_headroom` formula is undocumented — treat as
advisory, not a guaranteed `runners*4 - running` identity.

**App state programmatically.** No public `App.state`/`App.list()`. `modal app list`
(`SDK/cli/app.py:99`) itself calls `client.stub.AppList(...)` directly and maps state via
`APP_STATE_TO_MESSAGE` (line 41): `APP_STATE_DEPLOYED` → "deployed", `APP_STATE_STOPPED` → "stopped",
plus stopping/ephemeral/disabled/initializing. "Deployed" with 0 containers is normal scale-to-zero, not
an error; "stopped" means the app was undeployed and needs `modal deploy` again — don't conflate the two.
`App.lookup()` (`SDK/app.py:320`) only finds/creates a reference; it exposes no state.

**Listing/stopping containers.** Same pattern: no public SDK method. `modal container list`
(`SDK/cli/container.py`) calls `client.stub.TaskList(...)`; `modal container stop <id>` calls
`client.stub.ContainerStop(task_id=..., graceful=...)`. Both use `_Client.from_env()` plus the raw
generated protobuf stub — internal, undocumented, no compatibility guarantee. **Recommendation: shell out
to the `modal` CLI with `--json`**, whose JSON contract is documented and stable, rather than reimplement
these RPCs. `display_table()` (`SDK/cli/utils.py:141`) snake_cases each column header
(`_col_name_to_json_key`, line 132), so `modal app list --json` yields `[{"app_id", "description",
"state", "tasks", "created_at", "stopped_at"}]` and `modal container list --json` yields
`[{"container_id", "app_id", "app_name", "start_time"}]`. `container stop` defaults to non-graceful
(running inputs cancelled and rescheduled elsewhere); use `--graceful` only to let an in-flight job finish
first.

## 3. Warm-up

**Does `ping()` trigger `@modal.enter()`?** `_enter()` (`SDK/_partial_function.py:589`): "Decorator for
methods which should be executed when a new container is started" — once per new container, not per call.
On a warm container, `ping()` just resets the idle clock. On a cold backend, `ping()` **is** the cold
start and pays the full ~68 s ComfyUI boot exactly like `generate` would — a "warm-up" button's first ping
is not free or fast.

**Idle-timer mechanism.** `scaledown_window` is sent to the server as `task_idle_timeout_secs`
(`SDK/_functions.py:1065`); any completed input — real job or no-op `ping()` — resets that container's
idle countdown. With `scaledown_window=60`, a sub-60 s ping cadence keeps it from reaching zero; the
brainstorm's chosen 30–45 s cadence leaves 15–30 s margin against jitter or a missed tick.

**Per-container cost of warm idle time.** Modal's cold-start guide: "you will be billed for any resources
used while the container is idle (e.g., GPU reservation or residual memory occupancy)." No idle discount —
a warm-but-unused L40S bills the same $0.000542/s (~$1.95/h, §5) as one actively rendering. Keeping warm
for 30 minutes via pings costs about $0.98 regardless of images produced.

**Does `update_autoscaler` survive `modal deploy`?** No. Its own docstring (`SDK/_functions.py:1249`,
line 1262): **"Subsequent deployments of the App containing this Function will reset the autoscaler back
to its static configuration."** So a routine redeploy of `qwen21_uc_app.py` silently reverts any standing
`min_containers`/etc. override back to the decorator's static values — confirming the brainstorm's
rejection of `update_autoscaler(min_containers=1)` isn't only about crash risk but also deploy-time reset.

## 4. Server auth

`MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` set the API token pair, taking precedence over `~/.modal.toml`.
`MODAL_ENVIRONMENT` names the default Environment; each Environment has "its own set of Secrets and any
object lookups... by default look for objects in the same Environment" (`modal.com/docs/guide/
environments`). **Scoping**: Service Users are the mechanism, but exist only on shared (team) workspaces.
A new Service User starts with "No Access to every Environment" until assigned Viewer or Contributor on
specific Environments (`modal.com/docs/guide/service-users`), whose own guidance is to "assign the Viewer
or Contributor role only on the Environments they actually need, keeping production isolated from
development." **Recommendation**: if the workspace supports Service Users, create one scoped to
Contributor on a single Environment rather than reusing a personal owner token. `[UNVERIFIED]`: whether
the account backing `qwen21-uc` is a shared workspace (Service Users available) or solo (degrades to "a
second personal token").

## 5. Pricing

`modal.com/pricing` (fetched directly): L40S is **$0.000542/s** (≈$1.95/h), matching the brainstorm's
existing figure. Idle time inside `scaledown_window` bills at the same rate as active compute (§3);
"never pay for idle resources" refers to scale-to-zero between invocations, not the idle tail inside an
already-warm container. A third-party course site's claim of "10-second billing increments" could not be
confirmed against `modal.com/pricing` or the SDK — **`[UNVERIFIED]`**, treat billing as straight
per-second until seen on an invoice.

## 6. Claude plugin

**Folio plugin** at `/Users/sweet-home/Works/folio/folio-plugin/` (no secrets printed).
`.claude-plugin/plugin.json` is minimal (`name`, `version`, `description`, `author`, `keywords`, no
`userConfig`). `.mcp.json` runs `uv run --script ${CLAUDE_PLUGIN_ROOT}/mcp_servers/folio_mcp/server.py`
with `FOLIO_EMAIL`/`FOLIO_PASSWORD`/`FOLIO_BASE_URL` as raw `env` passthrough — predates the `userConfig`
mechanism. `server.py` (2775 lines) opens with PEP 723 (`# /// script … dependencies =
["mcp[cli]>=1.2,<2", "httpx>=0.27", "pydantic>=2.6"] … ///`), with a code comment explaining the `<2`
ceiling: `mcp` 2.x drops `mcp.server.fastmcp` for a different API, so unpinned would break on import — pin
the same way for Atelier. It never returns binary image data through a tool result: `_download()` (line
313) GETs the binary endpoint, writes it locally with `open(out, "wb")`, and returns JSON (`saved_to`,
`size_bytes`, `content_type`). This is exactly the pattern the brainstorm is evaluating for Atelier and is
already proven in this plugin family — **recommend following it: save the full PNG locally, return JSON
metadata (optionally a small inline thumbnail), never the full image bytes.**

**Manifest schema (current)**, verified against `code.claude.com/docs/en/plugins/manifest-reference`:
`name` is the only required field; `displayName`, `version`, `description`, `author`, `homepage`,
`repository`, `license`, `keywords`, `metadata`, `defaultEnabled`, `dependencies`, `settings`,
`userConfig`, `channels`, `skills`, `commands`, `agents`, `hooks`, `mcpServers`, `lspServers`,
`outputStyles`, `workflows`, `experimental` are optional; an unrecognized top-level key is silently
stripped. `userConfig` supports `"sensitive": true`, which "masks input and stores the value in secure
storage instead of `settings.json`" — the correct place for the Cloudflare Access service token, an
improvement over Folio's raw-env-passthrough. PEP 723 + `uv run --script` is unrelated to the manifest
itself; it's just how `.mcp.json`'s `command`/`args` invoke a self-contained pinned script.

**Returning images / size limits.** In Claude Code, tool results are capped by `MAX_MCP_OUTPUT_TOKENS`
(`code.claude.com/docs/en/env-vars`): default **25,000 tokens**, warning at 10,000; over-cap results spill
to a file with an error naming the path. Current GitHub issues against `anthropics/claude-code` report
that `ImageContent` isn't converted to a native image block the way a directly-attached image is — it's
measured as base64 text, costing roughly 15,000–25,000 tokens for one mid-size PNG, landing at or over the
default cap alone. This strongly supports the brainstorm's proposed approach: return a small WebP
thumbnail and save the full PNG locally via the Folio pattern above, not the full-resolution image inline.

**Tool-call timeouts.** Per `code.claude.com/docs/en/mcp` (fetched directly): a per-server `timeout` in
`.mcp.json` is "a hard wall-clock limit per tool call, and progress notifications from the server don't
extend it." Unset, it falls back to `MCP_TOOL_TIMEOUT` (ms), whose own default is "about 28 hours" — a
70–90 s cold start is not at meaningful risk of a client-side timeout kill even with no special handling.
The same page: "a main-conversation call that runs past two minutes moves to a background task first."
**Recommendation**: have the "generate" tool `spawn()` through Atelier's own HTTP API, then poll with
`ctx.report_progress()` every few seconds for transcript UX (not required to dodge a timeout), capping the
wait around the cold-start estimate plus margin (~100 s); if still running, return the job ID for a
separate "check status" tool — reusing the same job/poll model as the web UI rather than a second pattern.

## 7. HTMX

Verified against `htmx.org/docs/` and `htmx.org/attributes/hx-swap-oob/`. Polling:
`<div hx-get="/jobs/42" hx-trigger="every 2s"></div>` issues a GET every 2 s and swaps the response in.
**Stopping polling**: "If you want to stop polling from a server response you can respond with the HTTP
response code `286` and the element will cancel the polling" — have the job-status partial return 286 once
a job reaches a terminal state, no client-side JS needed. **OOB swap**: a response can include a second
element with `hx-swap-oob="true"` and a matching `id`; htmx swaps the primary target normally and also
swaps that element wherever its `id` already exists in the DOM — the documented pattern for updating a
persistent status badge from the same response that updates the job list.

**Project layout.** FastAPI's docs (`fastapi.tiangolo.com/advanced/templates/`): `templates/` for Jinja2
files, `static/` for assets, `app.mount("/static", StaticFiles(directory="static"), name="static")`,
`Jinja2Templates(directory="templates")` with `request` passed in every `TemplateResponse` context.
Nothing more elaborate is documented or needed here.

**Tailwind standalone CLI vs. classless CSS — recommend classless (Pico.css or similar) plus a small
hand-written `custom.css`, not a Tailwind Docker build stage.** Tailwind ships a real standalone binary
(`tailwindcss.com/docs/installation/tailwind-cli`, `.../blog/standalone-cli`) a multi-stage Dockerfile can
fetch and run with no Node — a viable, documented option, with one caveat from a maintainer GitHub issue:
the release binary doesn't run on Alpine, so the build stage needs a Debian-based image. But per the
brainstorm's own trade-off ("relies on the UI staying at the level of forms, a queue and a gallery"),
Pico.css ships under 10 KB, needs no build tool, styles plain semantic HTML by default, and has an
optional class-based mode for the few richer spots (image grid, badges, buttons) — covering this app's
actual UI with strictly less moving infrastructure (no build stage, no rebuild-on-template-change step)
than Tailwind, the simpler choice under KISS for a UI this bounded. Revisit Tailwind only if a later
feature (a canvas/mask editor, the brainstorm's own stated breaking point) needs finer control.

## 8. Deploy

**GHCR pull auth — recommend piping the workflow's own `GITHUB_TOKEN` to `docker login` over SSH on each
deploy, not a standing `read:packages` PAT on the server.** `GITHUB_TOKEN` can authenticate to `ghcr.io`
for the same repo's packages when the workflow declares `permissions: packages: read` (or the repo/org
default grants it); it's minted per run and expires quickly after the job ends. Piping it over SSH
(`ssh host "echo $GITHUB_TOKEN | docker login ghcr.io -u $ACTOR --password-stdin"`) still writes a
credential into the server's `~/.docker/config.json`, but a short-lived, auto-invalidated one — fitting
the brainstorm's carefulness about standing secrets on a shared production box (`folio-prod-1`) better
than a manually issued PAT, which is operationally simpler (server can pull anytime) but long-lived and
must be rotated/revoked by hand, with a larger blast radius if the box is compromised. Trade-off to accept
knowingly: the server can't pull outside a triggered deploy job — acceptable since deploys are the only
expected pull trigger.

**Rollback-capable `docker compose pull && up -d`.** General, widely-used pattern rather than one official
doc: tag images with an immutable identifier (git SHA/run number) alongside `:prod`; before retagging
`:prod` to the new build, record the currently-running image's tag/digest on the server (e.g. a
`PREVIOUS_TAG` file); a rollback job reads it, retags, and reruns `docker compose pull && up -d` against
that exact previous tag without rebuilding. This is convention assembled from Compose's own pull/up
semantics plus common CI recipes, not a built-in Compose feature. Concretely: have the deploy workflow
write the previous running tag to the server before pulling the new one, and provide a `rollback`
script/job that reads it back.

## Unresolved questions

- Whether the Modal workspace backing `qwen21-uc` is shared (Service Users available) or solo (least
  privilege degrades to a second personal token).
- The exact formula behind `FunctionStats.input_headroom`; undocumented beyond the field name.
- Whether GHCR pulls need an explicit `permissions: packages: read` block or inherit it from
  `flowitup/atelier`'s default `GITHUB_TOKEN` permissions — depends on that not-yet-created repo's
  Settings → Actions default.
- Whether Claude Code's ~28-hour default `MCP_TOOL_TIMEOUT` is bounded by any shorter idle-timeout layer
  for local stdio servers specifically (the docs' "first response byte" timer applies to HTTP/SSE servers,
  not stdio) — worth a short manual timing test once the plugin exists rather than trusting docs alone for
  a cold-start-sized wait.
