---
phase: 6
title: "GPU status, warm-up & stop"
status: completed
priority: P2
effort: "8h"
dependencies: [4]
---

# Phase 6: GPU status, warm-up & stop

## Context Links

<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F15 GPU status model -->

- Contract: the "warm-up must fail safe" constraint and criterion 6 (status, warm up, stop). See the [brainstorm report](../reports/brainstorm-260925-1507-atelier-image-studio-hetzner.md).
- Brief §4: GPU status every 10 s, cached; warm-up with persisted `warm_until`; and stop with cancel-first confirmation. See the [architecture brief](./reports/architecture-brief.md).
- [research-02 §2–3](./research/researcher-02-modal-sdk-app-plugin.md): no public SDK API for app state or containers, so shell out to the CLI with `--json`; `ping()` resets the idle timer; `update_autoscaler` resets on deploy. Its §1 advice to cancel with `terminate_containers=True` does not fit this backend (see Key Insights).
- Red-team evidence: [failure modes](./reports/red-team-failure-mode-analyst.md) (Findings 2, 3, 4, 9), [assumptions](./reports/red-team-assumption-destroyer.md) (Findings 2, 3, 5), [scope](./reports/red-team-scope-complexity-critic.md) (Findings 4, 5, 8), [security](./reports/red-team-security-adversary.md) (Findings 4, 6).
- Verified SDK facts (modal 1.5.5):
  - `cls.py:90`: bound methods hydrate with the class's `service_function.object_id`, so one `get_current_stats()` covers `run_workflow`, `ping` and `generate`.
  - `_functions.py:2080`: `get_current_stats()` returns `FunctionStats(backlog, num_total_runners, num_running_inputs, input_headroom)`.
  - `cli/app.py:41-50`: the state texts, for example `deployed`, `stopped`, `stopping...`, `initializing...`, `disabled` and `ephemeral`. `cli/app.py:104`: the list holds only apps that are "running, deployed or recently stopped".
  - `cli/app.py:99-133` and `cli/utils.py:132`: `modal app list --json` returns the keys `app_id`, `description`, `state`, `tasks`, `created_at` and `stopped_at`.
  - `cli/app.py:87-93` and `runner.py:122-133`: a redeploy after `modal app stop` creates a new app ID. `cli/app.py:550`: `modal app stop` takes `--yes`.
  - `cli/container.py:40-77`: `modal container list --app-id <id> --json` returns `container_id`, `app_id`, `app_name` and `start_time`.
  - `cli/container.py:308-338`: `modal container stop` is non-graceful by default and exits with "Container '…' is already stopped." (`:332-333`) for a finished container.
  - `cli/utils.py:189-199`: without `-y/--yes` on a non-TTY stdin, the command aborts.
  - `_container_entrypoint.py:193-237`: cancelling a synchronous input under input concurrency SIGINTs the whole container, and its other inputs are rescheduled.
  - `_functions.py:1262`: deployments reset `update_autoscaler`.
- Backend config: `modal/qwen21_uc_app.py:99-100` (`scaledown_window=60`, `max_containers=1`, `max_inputs=4`), its synchronous methods (`:140-150`), and the `ping()` added in phase 1.

## Overview

<!-- Updated: Validation Session 1 - ping probes ComfyUI -->

When this phase is done, the header and a `/gpu` page show each Modal backend as warm, warming, running, scaled to zero, stopped, unhealthy or unknown. They also show deployment state, warm containers, running inputs and backlog. Status is computed when a page reads it, from short caches, so nothing polls Modal while no tab is open.

"Warm up" for 5, 15 or 30 minutes makes the backend warm within about 70 s and keeps it warm with a `ping()` about every 30 s, with at most one in flight. Each ping also checks that ComfyUI answers, so "warm" means ready, and a failed ping shows "unhealthy". The window survives Atelier restarts, and the backend always scales to zero within about 60 s once pings stop.

"Stop" asks for confirmation when jobs are active, then:
1. cancels them all at once, together with any ping in flight;
2. terminates the backend's containers;
3. keeps checking until no runner or backlog remains.

A circuit breaker recycles a backend whose ComfyUI stopped answering. Priority P2.

## Key Insights

<!-- Updated: Red Team 2026-09-25 - F1 poll pending signal -->
<!-- Updated: Red Team 2026-09-25 - F4 error handling -->
<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F6 locks -->
<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F15 GPU status model -->
<!-- Updated: Validation Session 1 - ping probes ComfyUI -->

- **Warm-up must never outlive Atelier.**
  - The contract rejects `min_containers=1`, and research-02 §3 confirms that `update_autoscaler` overrides reset on every `modal deploy`.
  - Pings are the only warm-up mechanism, so the GPU scales down within `scaledown_window` (60 s) whenever Atelier stops pinging: window over, crash or `deploy.sh stop`.
- **A cold ping is the boot.** A ping on a scaled-to-zero backend triggers `@modal.enter()`, about 68 s. The UI therefore shows "warming" until the ping completes, then forces a fresh status read.
- **A ping is a ComfyUI health probe (validated, contract amendment 8).**
  - `ping()` answers `"ok"` only when ComfyUI's `/system_stats` returns 200, and raises otherwise (phase 1).
  - So "warm" is shown only after a successful ping.
  - A failed ping marks the backend `unhealthy` and counts toward the circuit breaker.
- **The ping is polled with the job mapping.** The pinger calls the same `gateway.poll()` as jobs, so a running ping reads as pending through the builtin `TimeoutError` (phase 2), and "one ping in flight" really holds.
- **Keep, and persist, the ping's `FunctionCall`.** The pinger spawns `ping` (`spawn.aio`) and stores the call ID in memory and in `backend_state.ping_call_id`. Stop can then cancel a ping in flight even after an Atelier restart.
- **Stop cancels everything at once, then stops containers, then converges.**
  - The backend's methods are synchronous under `@modal.concurrent`, so **any** Modal cancel SIGINTs the whole container and reschedules its other inputs. `terminate_containers=False` doesn't prevent this. Research-02 §1's `terminate_containers=True` advice, and this plan's earlier "cancel one by one" order, were both wrong for this backend.
  - The sequence is:
    1. Take the backend lock.
    2. Clear warm.
    3. Mark every queued and submitted job cancelled.
    4. Send the Modal cancel of every tracked call in one `asyncio.gather`, ping first, so nothing live is left to reschedule.
    5. Run `modal container stop --yes` for each listed container, treating "already stopped" as success.
    6. Repeat stats plus container list for up to 60 s until runners and backlog are both 0.
    7. Always force a status refresh.
  - A user cancel of a single job never calls Modal (phase 2); only Stop does.
- **Stop cancels queued jobs too.** The dispatcher would otherwise start them seconds later and cold-start the GPU the user just stopped. The confirmation names both counts.
- **The lock is held, not just checked.** The dispatcher (phase 2) and the pinger each hold `locks[backend_id]` across check → spawn → record, and Stop holds it for its whole sequence. A spawn in flight therefore finishes and is recorded before Stop starts, and nothing new can start until Stop ends. A single process and a single event loop make an in-memory lock sufficient.
- **Status is computed on read.**
  - The contract's "refreshed automatically" is met by the header's 10 s HTMX poll. A background status loop would spawn about 1,440 CLI processes a day on Folio's shared host with nobody reading the result.
  - So the status is computed when `/gpu/panel`, the header, `/api/v1/gpu` or `start_warm` asks. Stats (an SDK call) are cached for 10 s and the app state (a CLI subprocess) for 60 s, with one refresh in flight per backend.
- **A missing app is a stopped app.** `modal app list` shows only recently stopped apps, so "no row for this app name" means stopped ("not deployed"), not unknown.
- **The handle follows the deployment.** When a fresh app state reports a different `app_id`, the gateway's cached `Cls` handle is dropped (phase 2's `invalidate`). A redeploy after `modal app stop` then works without restarting Atelier.
- **A circuit breaker covers a dead ComfyUI.**
  - Outside warm windows nothing pings, so a container whose ComfyUI died would still take jobs that all fail at once.
  - After 3 consecutive jobs on a backend fail on their first poll (within one 2 s poll interval) with ComfyUI-unreachable or connection-refused text, the backend becomes `unhealthy`. The breaker then clears warm and runs the Stop path to recycle the container, and shows a notice.
  - During a warm window the probe catches a dead ComfyUI directly: each failed ping counts toward the same breaker.
- **The GPU controls are owner-only HTML.** Warm and Stop are not in `/api/v1`; the plugin reads status only. Repeated warm requests can extend a window without limit, so total spend is bounded by the Modal workspace spend limit (D2), not by Atelier.

## Requirements

<!-- Updated: Red Team 2026-09-25 - F4 error handling -->
<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F6 locks -->
<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F12 Modal tokens -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->
<!-- Updated: Red Team 2026-09-25 - F15 GPU status model -->
<!-- Updated: Validation Session 1 - ping probes ComfyUI -->

Functional:
- **Gateway additions** (`modal_gateway.py`):
  - `stats(backend)` returns runners, running inputs and backlog.
  - `app_state(backend)` returns the state and app ID. It prefers a `deployed` row when several rows share the app name, and returns `stopped` with no app ID when no row matches.
  - `spawn_ping(backend)` returns the call ID.
  - `stop_containers(backend)` lists the app's containers and stops each with `--yes`, treating "already stopped" as success, and returns the number listed.
  - The CLI helper runs `sys.executable -m modal …` with `stdin=DEVNULL`, `NO_COLOR=1`, `TERM=dumb` and a 30 s timeout, and raises `ModalCliError` with at most 500 characters of stderr.
- **`gpu.py`:**
  - `GpuStatus` is the on-read status: `get(backend, force=False)` refreshes stats after 10 s and the app state after 60 s, one refresh in flight per backend. It calls `gateway.invalidate(backend)` when the `app_id` changes.
  - `display_state(status)`, checked in this order:
    - `unknown` on error;
    - `stopped` if the app is not deployed;
    - `unhealthy` if the last ping failed or the breaker notice stands;
    - `warming` if a warm window is open and no ping has succeeded in it yet;
    - `warm` if a window is open, runners ≥ 1 and the last ping succeeded;
    - `running` if no window is open and runners ≥ 1;
    - `scaled to zero` otherwise.

    "Warm" is therefore shown only after a successful ping.
  - `warm_state(conn, backend_id)`, `set_ping`, `clear_ping`, `clear_warm` and `start_warm(minutes ∈ {5, 15, 30})` are the only readers and writers of the `backend_state` table. `start_warm` persists `warm_until = now + minutes`, is refused while the app is stopped, and starts the pinger.
  - Cost helpers: window estimate = minutes × $/h ÷ 60 (+ about $0.04 when cold); running estimate = time since first seen warm × $/h.
- **Worker additions** (`worker.py`):
  - `ensure_pinger(backend_id)` starts a pinger task only while a warm window is open, from `start_warm`, and at startup if a persisted window is still open. The task ends when the window ends or Stop clears it.
  - The pinger step runs every 5 s inside `async with locks[backend_id]`: first ping immediately, then every 30 s, at most one in flight. It polls the ping with `gateway.poll()`, forces a status refresh when the ping completes, and uses `ping_calls.pop(backend_id, None)`.
  - A ping that completes with a failure (ComfyUI didn't answer) sets `unhealthy` with the error text and counts toward the breaker. A successful ping clears `unhealthy` and resets the counter.
  - A transient error from `spawn_ping` or `poll` is retried on the next step. A permanent one (phase 2's `PERMANENT`) clears the window and sets the backend alert. The pinger task never dies on an exception; it logs and continues until the window ends.
  - `stop_backend(backend, confirm)` is a `Worker` method, because it needs the locks, the ping and the jobs, and it runs the sequence from Key Insights. Its whole sequence is bounded at about 70 s, well under Cloudflare's 125 s.
  - The circuit breaker counts two kinds of ComfyUI-down signal per backend:
    - failed pings;
    - jobs that fail on their first poll with text matching `Connection refused`, `Max retries exceeded`, `127.0.0.1:8188` or `ComfyUI did not start`.

    Any successful ping or job, or a non-matching failure, resets it. At 3 it sets the `unhealthy` notice and schedules `stop_backend(confirm=True)` as a task, so it never waits on a lock its caller holds.
- **Routes** (`routes/gpu.py`, owner-only HTML, always 200):
  - `GET /gpu` (page) and `GET /gpu/panel` (partial, polled every 10 s).
  - `POST /gpu/{backend}/warm` with `minutes`, an `hx-post` that returns the panel.
  - `POST /gpu/{backend}/stop` with `confirm`, an `hx-post`. Without `confirm` and with active jobs, it returns the confirmation partial listing queued and running counts. Otherwise it returns the panel with the outcome: stopped, or "still N runners after 60 s" with a runbook link.
- **Header:** a GPU badge in `header_status.html` linking to `/gpu` and showing the state, "warm until hh:mm", and the unhealthy notice.
- **Runbook:** a "Backend unhealthy" entry in `docs/deployment-guide.md` covering what it means (a failed ComfyUI probe, or instant ComfyUI failures), what the automatic recycle did, how to retry the cancelled jobs, and when to redeploy.

Non-functional:
- At most one ping in flight per backend. The ping interval of 30 s leaves at least 30 s of margin before the 60 s scaledown.
- A CLI failure or timeout never breaks a page. The status shows `unknown` with the error, and the next read retries.
- No credential is passed on a command line; the CLI reads `MODAL_TOKEN_*` from the inherited environment. That runtime token is workspace-wide (D2).
- `input_headroom` is not shown anywhere.

## Architecture

<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F6 locks -->
<!-- Updated: Red Team 2026-09-25 - F15 GPU status model -->
<!-- Updated: Validation Session 1 - ping probes ComfyUI -->

```
reader (/gpu/panel, header, /api/v1/gpu, start_warm) ─► GpuStatus.get(backend)
      stats older than 10 s? gateway.stats()  [SDK]     app state older than 60 s? gateway.app_state()  [CLI]
      app_id changed? gateway.invalidate(backend)       no row? state = stopped
POST warm ─► gpu.start_warm: warm_until = now+N ─► worker.ensure_pinger(backend)
pinger task (only while a window is open), every 5 s under locks[backend]:
      ping in flight? gateway.poll() ─► pending: keep | ok: ping ok, reset breaker | failed: unhealthy, feed breaker
                                        ─► then clear ping, force refresh
      window over? clear warm, exit      30 s since last ping? spawn_ping, persist ping_call_id
POST stop ─► worker.stop_backend: lock ─► confirm? ─► clear warm ─► cancel queued+submitted jobs (DB)
      ─► gather(cancel ping, cancel calls…) ─► container stop --yes each ─► converge ≤ 60 s ─► force refresh
poller (phase 2) and pinger ─► 3 consecutive ComfyUI-down signals (failed pings, instant job failures)
      ─► unhealthy notice ─► stop_backend(confirm=True) scheduled as a task
```

Pinger step and stop (sketch):

```python
PING_INTERVAL_S = 30
CONVERGE_S = 60

async def _pinger(self, backend: Backend) -> None:
    while True:
        async with self.locks[backend.id]:                 # held across check -> spawn -> record
            if not await self._warm_step(backend):
                return
        await asyncio.sleep(5)

async def _warm_step(self, backend: Backend) -> bool:
    now = self.clock()
    with db.session(self.settings) as conn:
        until, ping_id = gpu.warm_state(conn, backend.id)
    if ping_id:
        result = await self.gateway.poll(ping_id)          # a running ping reads as pending, like a running job
        if result.state == "pending":
            return True                                    # one ping in flight at most (a cold boot takes ~68 s)
        if result.state == "done":
            self.status.ping_ok(backend.id)                # "warm" is shown only after a successful probe
            self.breaker.reset(backend.id)
        else:                                              # ComfyUI did not answer /system_stats
            self.status.ping_failed(backend.id, result.error)
            self.breaker.record(backend.id, result.error)  # at 3 it schedules stop_backend as a task
        with db.session(self.settings) as conn:
            gpu.clear_ping(conn, backend.id)
        self.ping_calls.pop(backend.id, None)
        self.status.force(backend.id)
    if until is None or now >= until:
        with db.session(self.settings) as conn:
            gpu.clear_warm(conn, backend.id)
        return False
    if now - self.last_ping.get(backend.id, 0.0) >= PING_INTERVAL_S:
        call_id = await self.gateway.spawn_ping(backend)
        with db.session(self.settings) as conn:
            gpu.set_ping(conn, backend.id, call_id)        # persisted, so Stop can cancel it after a restart
        self.ping_calls[backend.id] = call_id
        self.last_ping[backend.id] = now
    return True

async def stop_backend(self, backend: Backend, *, confirm: bool) -> StopOutcome:
    async with self.locks[backend.id]:
        try:
            with db.session(self.settings) as conn:
                queued, running = jobs.active_counts(conn, backend.id)
                if (queued or running) and not confirm:
                    return StopOutcome.needs_confirmation(queued, running)
                _, ping_id = gpu.warm_state(conn, backend.id)
                gpu.clear_warm(conn, backend.id)           # clears warm_until and ping_call_id
                call_ids = jobs.cancel_all_for_backend(conn, backend.id)
            self.ping_calls.pop(backend.id, None)
            targets = ([ping_id] if ping_id else []) + call_ids
            await asyncio.gather(*(self.gateway.cancel(c) for c in targets), return_exceptions=True)
            deadline = self.clock() + CONVERGE_S
            while True:                                    # a cancel may have SIGINTed the container already
                await self.gateway.stop_containers(backend)
                stats = await self.gateway.stats(backend)
                if stats.runners == 0 and stats.backlog == 0:
                    return StopOutcome.stopped(cancelled=len(call_ids))
                if self.clock() >= deadline:
                    return StopOutcome.not_converged(stats)
                await asyncio.sleep(5)
        finally:
            self.status.force(backend.id)                  # always refresh, whatever happened
```

Gateway container stop:

```python
async def stop_containers(self, backend: Backend) -> int:
    state = await self.app_state(backend)
    if state.state != "deployed" or not state.app_id:
        return 0
    rows = json.loads(await _modal_cli("container", "list", "--app-id", state.app_id, "--json"))
    for row in rows:
        try:
            await _modal_cli("container", "stop", "--yes", row["container_id"])   # --yes: a non-TTY stdin aborts otherwise
        except ModalCliError as exc:
            if "already stopped" not in str(exc):          # a container that exited on its own is fine
                raise
    return len(rows)
```

The in-memory state is `status` (the `GpuStatus` caches), `ping_calls`, `last_ping`, `pingers`, `warm_since`, `locks` and the breaker counters. It lives on the one `Worker` per process, which is correct with a single uvicorn worker. `warm_until` and `ping_call_id` are persisted in `backend_state`, so a window and a ping in flight survive a restart.

## Related Code Files

<!-- Updated: Red Team 2026-09-25 - F15 GPU status model -->

Create:
- `/Users/sweet-home/Works/artio/atelier/gpu.py`
- `/Users/sweet-home/Works/artio/atelier/routes/gpu.py`
- `/Users/sweet-home/Works/artio/atelier/templates/gpu.html`
- `/Users/sweet-home/Works/artio/atelier/templates/partials/gpu_panel.html`
- `/Users/sweet-home/Works/artio/atelier/templates/partials/gpu_stop_confirm.html`
- `/Users/sweet-home/Works/artio/tests/test_gpu.py`

Modify:
- `/Users/sweet-home/Works/artio/atelier/modal_gateway.py`: `stats`, `app_state`, `spawn_ping`, `stop_containers`, the `_modal_cli` helper, and the pure parsers `parse_app_state` and `parse_container_ids`.
- `/Users/sweet-home/Works/artio/atelier/worker.py`: `status`, `ensure_pinger`, the pinger step, `stop_backend`, and the circuit breaker in the poller's failure path.
- `/Users/sweet-home/Works/artio/atelier/jobs.py`: `active_counts` and `cancel_all_for_backend`.
- `/Users/sweet-home/Works/artio/atelier/main.py`: include the GPU router, and start pingers for open windows at startup.
- `/Users/sweet-home/Works/artio/atelier/routes/pages.py`: the header-status handler adds the GPU summary from `worker.status`.
- `/Users/sweet-home/Works/artio/atelier/templates/partials/header_status.html`: the GPU badge.
- `/Users/sweet-home/Works/artio/atelier/templates/base.html`: a nav link to `/gpu`.
- `/Users/sweet-home/Works/artio/tests/fakes.py`: the fake gateway gains scripted stats and app state, `spawn_ping` with an optional test hook for slow spawns, `stop_containers` with an "already stopped" option, and entries in its ordered `calls` log.
- `/Users/sweet-home/Works/artio/docs/deployment-guide.md`: the "Backend unhealthy" runbook entry.

Delete: none.

## Implementation Steps

<!-- Updated: Red Team 2026-09-25 - F4 error handling -->
<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F6 locks -->
<!-- Updated: Red Team 2026-09-25 - F15 GPU status model -->
<!-- Updated: Validation Session 1 - ping probes ComfyUI -->

1. **Gateway.**
   - Add the pure parsers first, each with tests on literal JSON.
     - `parse_app_state(rows, app_name)` takes the rows with `description == app_name`, prefers `state == "deployed"`, otherwise takes the newest `created_at`, and returns `AppState("stopped", None)` when no row matches.
     - `parse_container_ids(rows)` returns the container IDs.
   - Then add `_modal_cli` and the four async methods. `stats` uses the cached `Cls` object and `await obj.ping.get_current_stats.aio()`; any method gives the same class-level stats (`cls.py:90`).
2. **Jobs.** Add `jobs.active_counts` and `jobs.cancel_all_for_backend`. The latter runs one transaction and returns the call IDs of the submitted jobs it cancelled.
3. **`gpu.py`.** Write `GpuStatus` with the two caches and a per-backend refresh lock, `display_state`, the `backend_state` helpers and the cost helpers.
4. **Worker.**
   - Add `ensure_pinger`, `_pinger` and `_warm_step` as sketched. At startup, `create_app`'s lifespan calls `ensure_pinger` for every backend with an open window.
   - Add `stop_backend` as sketched.
   - Add the breaker, fed from two places: the poller, when a job fails on its first poll with matching text, and the pinger, on every failed ping. A successful ping or job resets it, and at 3 it schedules `stop_backend` as a task.
5. **Routes and templates.**
   - `gpu_panel.html` renders one card per backend: display state, "deployed" or "stopped", containers, running inputs, backlog, "warm until hh:mm", the cost hint, the running cost, and the unhealthy notice.
   - It has warm buttons for 5, 15 and 30 minutes and a Stop button, all `hx-post`. The confirmation partial re-posts with `confirm=1`. Every response is 200.
   - The panel uses `hx-trigger="load, every 10s"`.
6. **Runbook.** Add the "Backend unhealthy" entry to the guide.
7. **Tests.** Write `tests/test_gpu.py` (listed in the Todo list) with the fake gateway and an injected clock. Then run `uv run ruff check` and `uv run pytest -q`.
8. **Commit and push.** Commit as `feat: GPU status on read, fail-safe warm-up pings and stop with convergence`. **[OWNER-GATED]** The push to `main` deploys.
9. **[OWNER-GATED] Live verification on production,** spending about $1 of GPU. Ask the owner before starting, and again before the `deploy.sh stop` and `modal app stop` sub-steps. Record each timing.
   1. **Idle status.** `/gpu` shows "scaled to zero · deployed · 0 containers".
   2. **Warm 5 min.** The badge goes from "warming" to "warm" within about 70 s, after the first successful ping, and `uv run modal container list --json` on the laptop shows 1 container.
   3. **Expiry.** About 60 s after the window ends, `modal container list --json` returns `[]` and the UI shows "scaled to zero".
   4. **Kill test.**
      - Warm for 15 min. Once warm, run `ssh folio-prod '/opt/atelier/deploy.sh stop'` and confirm `[]` within about 90 s.
      - Then run `ssh folio-prod '/opt/atelier/deploy.sh start'`.
      - The pinger resumes the remaining window, by design. Press Stop to end it, which also exercises Stop with no jobs.
   5. **Stop with jobs.** Submit a batch of 2. While they run, press Stop. The confirmation shows the counts. Confirm, and within about 10 s `modal container list --json` returns `[]`, both jobs show "cancelled", and no new container appears in the next 90 s.
   6. **Stopped state and redeploy.** Stopping the app spends no GPU; the job afterwards costs one cold render, about $0.07.
      - Run `uv run modal app stop --yes qwen21-uc`. The badge shows "stopped" within 60 s, and Warm is refused.
      - Redeploy with `gh workflow run deploy-modal.yml --ref main`, and the badge returns to "scaled to zero".
      - Then submit one job **without restarting Atelier**. It must complete, which proves the cached handle followed the new app ID.

## Todo List

- [x] Gateway: parsers tested on literal CLI JSON (including the missing-app case); `_modal_cli` with `--yes`, no-color and timeout; `stats`, `app_state`, `spawn_ping`, `stop_containers` with "already stopped" as success. Verified: `test_parse_app_state_prefers_the_deployed_row`, `test_app_missing_from_the_list_reads_as_stopped`, `test_parse_app_state_picks_the_newest_row_when_none_is_deployed`, `test_parse_container_ids_returns_the_ids`, `test_modal_cli_*` (3), `test_stop_treats_already_stopped_containers_as_success`, `test_stop_containers_reraises_a_real_stop_failure`, `test_stop_containers_returns_zero_when_the_app_is_not_deployed`, `test_app_state_parses_a_real_cli_response`, `test_stats_calls_get_current_stats_on_the_ping_bound_method`, `test_spawn_ping_returns_the_call_id`, `test_spawn_ping_permanent_error_drops_the_cached_handle` all pass, only stubbing `asyncio.create_subprocess_exec` / `modal.Cls.from_name`.
- [x] `gpu.py`: on-read status with 10 s and 60 s caches, single refresh in flight, handle invalidation on a new app ID; display state with "warm" only after a successful ping; `backend_state` helpers; cost helpers. Verified: `test_status_is_computed_on_read_with_ten_and_sixty_second_caches`, `test_concurrent_readers_share_one_refresh`, `test_status_shows_deployed_warm_scaled_to_zero_and_stopped`, `test_changed_app_id_drops_the_cached_handle` all pass.
- [x] Worker: a pinger task only while a window is open, holding the lock across spawn, with a persisted ping ID; failed pings mark unhealthy; Stop sequence with gather-cancel and convergence; circuit breaker fed by pings and jobs. Verified: all named pinger/Stop/breaker tests in `tests/test_gpu.py` pass (22 tests across the Warm-up, Stop and Breaker sections), including lock-contention proofs (`test_stop_waits_for_a_slow_ping_spawn_in_flight`, `test_dispatcher_spawns_nothing_while_stop_runs`) and restart resumption.
- [x] Routes, `/gpu` page, polled panel, stop confirmation, header badge, all 200. Verified: `test_gpu_page_and_panel_are_owner_only_and_200`, `test_warm_and_stop_routes_are_owner_only`, `test_warm_route_*`, `test_stop_route_requires_confirmation_then_stops`, `test_header_status_shows_the_gpu_badge` pass; `tests/test_auth.py::test_service_identity_is_refused_outside_the_api_allowlist` and `tests/test_pages_hide_secrets.py::test_pages_hide_secrets` still pass unmodified with the two new GET routes swept in.
- [x] "Backend unhealthy" runbook entry. Added to `docs/deployment-guide.md`'s Routine operations section, alongside a short GPU status/warm-up/stop operator note.
- [x] `tests/test_gpu.py` green. 66 tests, all passing (`uv run pytest -q tests/test_gpu.py`) after the independent review's fix round (see notes below); 42 before it.
- [x] Independent-review fix round: 2 High and 6 Medium defects fixed in place (panel HTMX self-poll and dropped POST responses; Stop's exception safety; dispatcher stalling and unbounded Stop steps; a warm-click/pinger-expiry race; a stopped app misreading as "unknown"; no negative caching on a failing gateway; swallowed cancel failures; a sticky unhealthy notice), plus the reviewer's 9 Low items that were cheap and safe, and its owner-decision defaults (extend-only warm clicks, job success clears unhealthy, the breaker marker set, trusting the app-state read for a stopped app). The phantom test was replaced with one that genuinely runs `stop_backend` concurrently with the dispatcher. See the fullstack-developer report's "Review fixes" section for the file:line detail and the mutation re-check.
- [x] Re-review fix round: the 6 remaining Low regressions (an orphaned CLI child on outer cancellation; a duplicate app-state read per convergence pass; the cancel phase running outside the overall deadline; an empty timed-out-step message; unused per-step timeout constant; a full 60s of "unknown" after one transient app-state failure) are all fixed, plus tests added for the reviewer's 13 previously-uncaught mutants (the hanging-step-vs-deadline, stats negative caching, pinger-exits-after-Stop and queued-jobs'-null-call-ids cases first). `tests/test_gpu.py` grew from 67 to 90 tests. See the fullstack-developer report's "Re-review fixes" section.
- [x] [OWNER-GATED] Live checks: warm within about 70 s, zero about 60 s after expiry, kill test, stop with jobs, stopped state plus a job after redeploy; timings recorded

### Live verification (2026-09-27, owner-approved, about $0.50 of Modal credits)

The GPU controls were pushed as `cd8497c`; the deploy was green, and the app log showed no errors.

1. **Idle:** `/gpu` showed "scaled to zero · deployed · 0 containers". The warm buttons show estimates of $0.20, $0.53 and $1.01.
2. **Warm 5 min**, clicked at 10:10:15:
   - after 34 s: "warming", 1 container and backlog 1 (the first ping, waiting for the boot);
   - by 54 s at the latest: **warm**. Modal's own listing showed exactly one `qwen21-uc` container.
3. **Expiry:** the window ended at 10:15:15, and Modal listed 0 containers at 10:17:09, 97–114 s after the end.
   - The plan's "about 60 s" left out two delays: the last ping can be up to 30 s old, and Modal takes a short while to remove the container after its 60 s idle window. The guide now says about 2 minutes.
   - The header badge refreshed slowly because the tab was hidden. `document.visibilityState` was `hidden`, and Chrome throttles timers in background tabs; it refreshes normally when visible. This is not an app defect.
4. **Kill test:**
   - Warm 15 min, clicked at 10:19:43, was warm by 65 s.
   - `deploy.sh stop` at 10:20:56: 0 containers 97–108 s later.
   - `deploy.sh start` at 10:22:54: the pinger resumed the open window, and a container was up by 10:23:05.
   - Stop with no jobs, at 10:23:21: the panel read "Stopped." within 20 s, and 0 containers held through 10:24:37 (no pinger came back).
5. **Stop with jobs** (a batch of 2):
   - The confirmation read "0 queued and 2 running job(s) will be cancelled".
   - After confirming (08:25:59Z), the POST finished at 08:26:15Z (16 s), and the panel read "Stopped. Cancelled 2 running job(s)."
   - Both jobs showed cancelled with Retry, and no container appeared in the next 110 s.
6. **Stopped state and redeploy:**
   - `modal app stop --yes qwen21-uc` at 10:35:23: the panel read **"stopped"** by 10:36:24, and Warm was refused ("is stopped: deploy it before warming it up.").
   - Deploy-modal run 36306756609 was green at 10:37:37 with a new app ID, and the panel read "scaled to zero" by 10:38:11.
   - One job submitted at 10:38:22 **without restarting Atelier** finished (done in 117 s, the first cold container of the new app), so the cached handle followed the new app ID.
7. **Defect found and fixed during the checks:**
   - A refusal (stopped app, unknown backend, bad minutes) replaced the whole panel with a bare message, which removed its buttons and its poll.
   - `_flash` now re-renders the panel with the message inside it. The new test fails without the fix, and the suite now has 403 tests.

## Success Criteria

<!-- Updated: Red Team 2026-09-25 - F4 error handling -->
<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F6 locks -->
<!-- Updated: Red Team 2026-09-25 - F15 GPU status model -->
<!-- Updated: Validation Session 1 - ping probes ComfyUI -->

**Criterion 6**, proven by these tests and the live steps above:
- **Status:**
  - `test_status_is_computed_on_read_with_ten_and_sixty_second_caches` and `test_concurrent_readers_share_one_refresh` pass.
  - `test_status_shows_deployed_warm_scaled_to_zero_and_stopped`, `test_parse_app_state_prefers_the_deployed_row` and `test_app_missing_from_the_list_reads_as_stopped` pass.
  - `test_changed_app_id_drops_the_cached_handle` passes.
  - The panel partial polls every 10 s. Live, the UI matches `modal container list` within one refresh, and a job succeeds after the redeploy without restarting Atelier.
- **Warm up:**
  - `test_pinger_runs_only_while_a_window_is_open` passes.
  - `test_warm_is_shown_only_after_a_successful_ping` and `test_running_is_shown_for_containers_outside_a_warm_window` pass.
  - `test_warm_pings_immediately_then_every_30_seconds_with_one_in_flight` passes.
  - `test_warm_window_and_ping_resume_after_worker_restart` passes.
  - `test_expired_window_stops_pings_and_clears_state` passes.
  - Live, the backend is warm within about 70 s and scales to zero about 60 s after the window, and after `deploy.sh stop`.
- **Stop:**
  - `test_stop_requires_confirmation_when_jobs_are_active` passes, with the confirmation partial returned as 200.
  - `test_stop_cancels_every_call_at_once_ping_first_then_stops_containers` passes, checking the order in the fake's `calls` log.
  - `test_stop_treats_already_stopped_containers_as_success` and `test_stop_converges_until_runners_and_backlog_are_zero` pass.
  - `test_stop_after_restart_cancels_the_persisted_ping` passes.
  - `test_stop_waits_for_a_slow_ping_spawn_in_flight` and `test_dispatcher_spawns_nothing_while_stop_runs` pass.
  - Live, the backend reaches 0 containers within about 10 s after confirmation, and no container reappears within 90 s.
- **Breaker:** `test_circuit_breaker_recycles_after_three_instant_comfyui_failures`, `test_failed_ping_marks_unhealthy_and_feeds_the_breaker` and `test_breaker_resets_on_success_or_other_failure` pass.

## Verification

```bash
cd /Users/sweet-home/Works/artio
uv run ruff check && uv run pytest -q tests/test_gpu.py -v && uv run pytest -q
# Free Modal API reads (no GPU time)
uv run modal app list --json
uv run modal container list --json
# [OWNER-GATED] live steps in Implementation Step 9 (spend GPU money)
ssh folio-prod 'docker exec atelier python -m modal app list --json >/dev/null && echo "CLI works inside the container"'
```

## Risk Assessment

<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F15 GPU status model -->
<!-- Updated: Validation Session 1 - ping probes ComfyUI -->

| Risk | Likelihood × impact | Observable signal | Pre-decided response |
|---|---|---|---|
| Stop doesn't converge within 60 s | Low × Medium | The panel shows "still N runners after 60 s" | Press Stop again, which re-runs the sequence. If it persists, stop and report to the owner with the container IDs and times. |
| CLI output isn't pure JSON (rich formatting) | Low × Medium | `json.JSONDecodeError`, shown as `unknown` | `NO_COLOR` and `TERM=dumb` are already set. Also strip anything before the first `[`. Adjust within the plan. |
| A CLI subprocess pushes the container over 768 MB | Low × Medium | `docker inspect atelier --format '{{.State.OOMKilled}}'` is `true` | Status-on-read already removed the background CLI calls. If it still happens, **[OWNER-GATED]** raise `mem_limit` to `1g` in the server's `compose.yaml`. |
| The 30 s ping interval is too slow under Modal scheduling jitter | Low × Low | Live, the container scales down during an active window | Drop `PING_INTERVAL_S` to 20. Still one in flight at most. |
| A ping fails on a healthy but busy container, showing "unhealthy" wrongly | Low × Low | Pings fail while jobs on the same container succeed | Raise the probe timeout in `ping()` (phase 1 risk table) and tell the owner. |
| The breaker trips on a real but transient ComfyUI error | Low × Low | An unexpected "Backend recycled" notice | The runbook explains how to retry. If it happens repeatedly, raise the threshold after telling the owner. |
| An undeployed app makes `spawn_ping` fail | Low × Low | Warm refused, or ping errors while the app is stopped | `start_warm` refuses while the state is `stopped`; a permanent ping error clears the window and shows the reason. |

**Rollback:** `git revert`, then **[OWNER-GATED]** push to redeploy. If a GPU is left warm for any reason, run `uv run modal container stop --yes <id>` from the laptop. Pings stop with the code, so the backend scales to zero within 60 s anyway.

## Security Considerations

<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F12 Modal tokens -->
<!-- Updated: Validation Session 1 - verified Modal spend limit -->

- The CLI subprocess inherits only the container environment, including the workspace-wide runtime token (D2). Arguments are fixed strings plus IDs parsed from the CLI's own JSON, and are never user input.
- Warm and Stop are owner-only HTML POSTs behind the Access check and the origin check. The plugin's service identity can read GPU status but cannot warm or stop.
- Each warm request is capped at 30 minutes, but repeated requests extend the window. Total GPU spend is bounded by the Modal workspace spend limit (D2), which stops billable workloads when reached, not by Atelier.

## Next Steps

Phase 7 adds the prompt library and custom workflows. Phase 8 exposes GPU **status** through `GET /api/v1/gpu` and the plugin's `gpu_status` tool.
