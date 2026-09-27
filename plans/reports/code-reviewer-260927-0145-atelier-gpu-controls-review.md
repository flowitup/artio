# Code review: Atelier GPU status, warm-up, Stop and circuit breaker (uncommitted phase-6 work)

Date: 2026-09-27 (Europe/Paris). Reviewer: code-reviewer. This review was read-only. No repo file was edited, and nothing called Modal for real.

## Code Review Summary

### Scope
- **Files, modified:** `atelier/{modal_gateway,worker,jobs,main}.py`, `atelier/routes/pages.py`, `templates/base.html`, `templates/partials/header_status.html`, `static/app.css`, `tests/fakes.py`, `docs/deployment-guide.md` and the phase-6 plan (+546/−15).
- **Files, new:** `atelier/gpu.py`, `atelier/routes/gpu.py`, `templates/gpu.html`, `templates/partials/gpu_panel.html`, `templates/partials/gpu_stop_confirm.html` and `tests/test_gpu.py` (1,228 lines).
- **LOC:** about 1,770 new or changed.
- **Focus:** the uncommitted diff, checked against the spec (phase-06 Key Insights, Requirements and Architecture) and the plan's key decisions.
- **Checks:**
  - `uv run ruff check` is clean.
  - `uv run pytest -q` gives 354 passed, 1 deselected.
- **Evidence gathered:**
  - 16 probe tests, run against the real code with fakes.
  - A headless-Chrome harness that served the real rendered `gpu_panel.html` together with the repo's `htmx.min.js`.
  - 21 source mutants, each run through the full suite.
  - modal 1.5.5 CLI and SDK source, read with `inspect.getsource`.

### Overall Assessment
The engine core is sound:
- The lock discipline holds.
- The ping-first gather and the persisted ping id work.
- Pingers do not leak.
- The breaker cannot deadlock.
- User cancel is still DB-only.
- `_modal_cli` is safe.

The defects are in the edges:
- **Panel wiring:** the panel's HTMX wiring polls itself in a tight loop and drops POST responses, including Stop's confirmation.
- **Stop exceptions:** Stop is not exception-safe, so it returns 500 instead of the required 200.
- **Stale status:** several status paths show stale or wrong state.

The suite is green but pins only 9 of 21 targeted behaviours. Not ready to commit as is. The two High items are small, local fixes.

---

## Critical Issues
None found:
- No data loss.
- No trust-boundary defect.
- No money leak: pings are the only warm mechanism, nothing in `atelier/` or `modal/` uses `min_containers`, `update_autoscaler` or `keep_warm`, and every pinger exit path is fail-safe.

## High Priority

### H1. The GPU panel re-requests itself in a tight loop and drops POST responses (Stop outcome, and about half of Stop confirmations)
- **Where:** `atelier/templates/partials/gpu_panel.html:10` (`hx-trigger="load, every 10s" hx-swap="outerHTML"`), `:40` and `:47` (forms targeting `#gpu-panel`).
- **Mechanism, part 1: the loop.** Every outerHTML swap inserts a new `#gpu-panel` that carries `load`. htmx fires `load` again as soon as the new element is processed, so the panel polls continuously. htmx documents this as "load polling", which is normally paired with a `delay:`.
- **Mechanism, part 2: lost responses.** A form POST resolves its target (the old `#gpu-panel`) when it is sent. Any poll swap that lands first detaches that target, and the POST's response is then discarded.
- **Evidence (headless Chrome, real markup, repo htmx):**

  | Scenario | Result |
  |---|---|
  | Idle `/gpu` for 4.5 s | **188 × `GET /gpu/panel`**, about 40 req/s locally; in production the rate is 1/RTT, around 7–20 req/s per open tab, all through Cloudflare Access and JWT verification |
  | Stop (POST 3 s) | 274 GETs; the "Stopped." outcome is **absent** from the DOM 2 s after the POST returned |
  | Stop needing confirmation (fast POST, 80 ms GET latency), 6 trials | confirm partial **lost in 3/6**, and the loop kept running |
  | Without `load` (`every 10s` only), POST spanning a poll tick | outcome still lost, because a poll can still swap during a long POST |
  | Fix variant (below) | 0 idle GETs in 7 s; the poll is dropped during the in-flight POST; outcome shown |

- **Impact:**
  - Continuous load on the 1-CPU box and on Access for as long as `/gpu` is open.
  - Buttons are replaced every RTT, so clicks can miss.
  - Stop's result ("Stopped" or "Still N runners after 60s", the only not-converged signal) is effectively never visible.
  - Stop with active jobs fails silently about half the time.
- **Fix, verified in the harness:**
  - On the panel div: `hx-trigger="every 10s" hx-sync="this:drop"`. The `/gpu` page already server-renders the panel, so `load` is not needed.
  - On both forms: `hx-sync="closest #gpu-panel:replace"`.
  - Better still, also keep the last `StopOutcome` per backend on the Worker, with a timestamp, and render it on every panel read for about 2 min. That survives reloads and also surfaces the breaker's automatic recycle outcome, which is currently discarded (L9).

### H2. Stop is not exception-safe: the "always 200" route returns 500 when any Modal step raises
- **Where:**
  - `atelier/routes/gpu.py:123`: `outcome = await worker.stop_backend(...)`, with no handling around it.
  - `atelier/worker.py:419-427`: `stop_containers()` and `stats()` are called bare, inside `try/finally` only.
- **Scenarios:**
  - A CLI timeout (30 s) or a real stop failure: `stop_containers` re-raises by design (`test_stop_containers_reraises_a_real_stop_failure`).
  - A transient stats RPC error.
  - Pressing Stop on a stopped app. `stats()` then hydrates `Cls.from_name` and gets `NotFoundError`. After `GpuStatus` invalidates the handle on the app-id change to `None`, this is deterministic.
  - In each case the jobs are already cancelled (committed) and the Modal cancels already sent, but the user gets a 500. htmx ignores it, so the button looks dead.
  - The breaker path fails the same way. The exception dies in an unobserved task and only shows as asyncio's "Task exception was never retrieved" at GC.
- **Evidence (probe P1):**
  - `StopCliFails`: `POST /gpu/qwen21-uc/stop -> 500 'Internal Server Error'`.
  - `StatsNotFound`: `-> 500`.
- **Spec it violates:** "Routes … always 200" and "A CLI failure or timeout never breaks a page".
- **Fix:**
  - Inside the loop, catch `Exception` around `stop_containers` and `stats`. Log it, keep the last error, and on deadline (or at once, for a stopped or undeployed app) return a new `StopOutcome.failed(error)` or `not_converged(stats, error)`.
  - Skip the stats read when `app_state` says the app is not deployed: nothing can be running.
  - The route renders the error line with 200.
  - Have the breaker's task log its outcome, or use `add_done_callback` to log exceptions.

## Medium Priority

### M1. Stop holds `locks[backend]` for up to 60 s or more, so `dispatch_once` stalls and `/healthz` reports 503 `loops: stale`
- **Where:**
  - `worker.py:131` (the dispatcher awaits each backend's lock).
  - `worker.py:405` (Stop holds the lock for the whole sequence).
  - `routes/health.py:20` (`STALE_AFTER_S = 30`).
- **Mechanism:** `last_dispatch_tick` is set only after every backend's lock is acquired. A Stop that does not converge (at least 60 s) makes `/healthz` 503 for at least 30 s. That can flip Docker's healthcheck (15 s × 3 retries) to unhealthy. It also blocks dispatch for every backend, not just the stopped one.
- **The ~70 s bound doesn't hold:** the deadline is checked only between iterations, and one iteration can take 30 s × (2 + N containers) of CLI timeouts plus a 10 s stats retry budget. So "bounded at about 70 s" holds only when Modal is healthy, and a slow Modal can push past Cloudflare's timeout.
- **Evidence (probe P2, scaled with `CONVERGE_S=4`, `STALE_AFTER_S=2`, real loops):**
  - before: `{'loops': 'ok'}`
  - during Stop: `(503, {'status': 'degraded', 'loops': 'stale'})`
  - after: `ok`
- **Deploy risk is low:** `deploy.sh` recreates the container and checks the new one.
- **Fix:**
  - In `dispatch_once`, `if self.locks[backend.id].locked(): continue`. Skipping still guarantees nothing starts during Stop, and the tick completes.
  - Wrap each CLI and stats step in `asyncio.timeout(remaining)`, so the whole sequence really is about 70 s.

### M2. Warm clicked just as a window ends can be wiped by the pinger's stale `until`
- **Where:**
  - `worker.py:361-363`: `until` is read.
  - `:366`: `await gateway.poll(ping_id)`.
  - `:380-383`: `clear_warm` is decided on the stale `until`.
  - `routes/gpu.py:98-108`: the route writes `warm_until` without the lock, then `ensure_pinger` sees the still-running pinger and returns.
- **Evidence (probe P3, through the real route):**
  - The warm POST returns 200 and the panel says "warm until".
  - `warm_until` is 899 s ahead right after the click.
  - After the pinger's poll resumes: `warm_until=None ping_call_id=None pinger_alive=False`.
  - Net effect: the 15-min window vanished silently.
- **Reach:** a narrow window, about one poll RTT at the first step after expiry. It is fail-cheap, but the user was told it's warm.
- **Fix:** a compare-and-clear, `UPDATE backend_state SET warm_until=NULL, ping_call_id=NULL WHERE backend_id=? AND (warm_until IS NULL OR warm_until <= ?)`. If rowcount is 0, return True so the pinger keeps going. Apply the same guard on the PERMANENT-spawn clear.

### M3. A stopped app reads "unknown", never "stopped"
- **Where:**
  - `gpu.py:73-76`: `error` is checked before `app_state`.
  - `gpu.py:149-154`: a stats failure sets `error` even when `app_state` succeeded.
- **Mechanism:** for a not-deployed app, the `stats()` lookup fails (`NotFoundError` once the handle is invalidated or never hydrated), so the badge shows "unknown".
- **Spec and plan impact:** this contradicts Requirements ("stopped if the app is not deployed") and live step 6 ("The badge shows 'stopped' within 60 s").
- **Evidence (probe P5):** `app_state=AppState(state='stopped', app_id=None) error="Lookup failed…" -> display_state='unknown'`.
- **Fix:** refresh `app_state` first. When it is known and not deployed, skip `stats` (treat it as 0) and don't report an error. Alternatively, return "stopped" before "unknown" whenever `app_state` itself was read successfully.

### M4. No negative caching: every reader re-runs a failing SDK or CLI call, and waiting readers run them serially
- **Where:** `gpu.py:149-164`. On failure, `_stats_at` and `_app_state_at` are not advanced.
- **Evidence:**
  - P4: 5 concurrent readers against a failing CLI produce 5 CLI runs, serialized, in 1.01 s at 0.2 s each. With the 30 s production timeout, the last reader waits about 150 s, well past Cloudflare's timeout. Header polls from 2+ tabs queue like this.
  - P12: stats failing (for example, a stopped app, as in M3) means 20 reads in 1 s make 20 Modal RPCs. Combined with H1's load loop, that is a continuous Modal RPC stream while `/gpu` is open.
- **Spec note:** "the next read retries" is honoured literally, but single-flight collapses only on success.
- **Fix:**
  - On failure, record the attempt time and the error, and serve the cached value plus the error until a short backoff (10 s) passes.
  - Readers that were waiting on the lock should reuse the result of the refresh they waited for.

### M5. Failed Modal cancels are swallowed with no log and no retry
- **Where:** `worker.py:417`: the `return_exceptions=True` results are never inspected.
- **Why it matters:** the modal 1.5.5 `container stop` docstring says running inputs "will be cancelled and rescheduled on other containers". An input whose cancel failed is therefore rescheduled on a fresh container, which means a cold boot for a job the DB already marked cancelled. Convergence then keeps killing boots for 60 s, after which the input can run up to the 1800 s timeout unobserved.
- **Evidence (probe P8):** 2 cancel attempts, both raised; `outcome=stopped cancelled=2`; 0 log records.
- **Fix:**
  - Log each failed call id.
  - Retry the failed cancels at the top of each convergence iteration, before `stop_containers`.
  - Surface "N cancels failed" in the outcome.
- **Likelihood:** low (it needs a Modal API fault), but the impact is paid GPU time on the Stop button's own path.

### M6. After a breaker recycle, "unhealthy" never clears when jobs succeed
- **Where:**
  - `gpu.py:102-106`: only `ping_ok()` clears `_unhealthy`.
  - `worker.py:245`: a job success resets only the counter.
  - `display_state` ranks `unhealthy` above everything except unknown and stopped.
- **Evidence (probe P6):** after the recycle, 2 renders succeeded (breaker=0), yet `display_state='unhealthy'` still shows the old ComfyUI error.
- **Impact:** the red badge stays until a warm-up ping succeeds or Atelier restarts, which trains the owner to ignore it.
- **Fix:** clear `_unhealthy` on a successful job poll too. Also consider clearing it once a recycle converges and a later job succeeds.

## Low Priority
- **L1. A dead window still shows "warm until".** `header_status.html:16` and `gpu_panel.html:27` render "warm until hh:mm" whenever `warm_until` is set. A window that expired while Atelier was down is never cleared: `main.py:78-81` resumes only open windows. P7 shows "warm until" for a window that ended 1 h earlier. Fix: gate on `view.window_open`, and clear expired rows at startup.
- **L2. The breaker trips only by luck of timing.** `worker.py:248-256`. A ComfyUI-down failure first noticed on the second poll tick (more than 2 s after submit) resets the counter (P9: `[1, 2, 0]`). "ComfyUI did not start" appears only after `@modal.enter`'s long boot loop, so it can never match within 2 s: that marker is dead, and boot failures, the expensive case, never trip. Consider counting any matching failure within a few poll ticks, and not resetting on a late match. This depends on what the spec means by "first poll".
- **L3. A shorter warm click shortens the window.** `gpu.py:225-237` sets `warm_until = now + N`, so a 5-min click during a 30-min window cuts it to about 6 min (P14). The docstring and the spec say "extend". Fix one or the other; see the Unresolved Questions.
- **L4. Warm trusts stale or unknown state.** `routes/gpu.py:101-107`. If the forced refresh fails, `start_warm` trusts the stale cached `deployed`. If nothing was ever cached, it tells the user "X is stopped" when the state is merely unknown. Refuse with "status unavailable: <error>" whenever `status.error` is set.
- **L5. The error text is never shown.** The panel never renders `status.error`, although the spec says it "shows unknown with the error".
- **L6. CLI errors keep the wrong end.** `modal_gateway.py:175` keeps the first 500 chars of stderr, but tracebacks carry the useful part at the end. Use the tail, as `classify_poll_exception` already does (`[-2000:]`).
- **L7. More CLI runs than needed.**
  - Each convergence iteration runs `modal app list`, `modal container list` and one `modal container stop` per container, which is 2+N processes (`modal_gateway.py:266-278`). Reuse the app id: fetch it once per Stop, or pass it from `GpuStatus`.
  - `status.force()` after every ping (`worker.py:378`) also forces the app-list CLI, doubling CLI runs during windows. Only stats needs forcing.
- **L8. `cancel_all_for_backend` depends on its caller.** It is atomic today (P10: `in_transaction=True` before its SELECT, because `clear_warm`'s UPDATE opened the implicit transaction), and the standalone path has no await between SELECT and UPDATE. Still, the docstring couples it to caller context. A single `UPDATE … WHERE backend_id=? AND status IN ('queued','submitted') RETURNING status, call_id` is atomic in any context.
- **L9. Recycle results are thrown away.** `worker.py:316-319`: the recycle's `StopOutcome` and any exception are discarded when `_recycle_tasks` is pruned, so a recycle that does not converge is invisible (see H1 and H2).

## Edge Cases Found by Scout
- **Stop on a stopped app** always returns 500 (H2), and the badge shows "unknown", not "stopped" (M3). Owner live step 6 will hit both.
- **After Stop gives up:**
  - At the 60 s convergence deadline, inputs whose cancels failed are rescheduled by `modal container stop` (M5).
  - A window expiring while Atelier is down leaves a stale "warm until" (L1).
- **Clicks and timing:**
  - A Warm click at window expiry while a ping is being polled (M2).
  - About 50% of Stop confirmations are dropped by the self-poll loop (H1).
  - 2+ tabs during a Modal/CLI outage queue header requests behind serial 30 s CLI timeouts (M4).

## Positive Observations (what held, with evidence)
- **No event-loop blocking in the new code.**
  - `_modal_cli` uses `asyncio.create_subprocess_exec` with `wait_for`, then kill and wait on timeout.
  - SDK calls go through `.aio`, and `Cls.from_name()()` and `FunctionCall.from_id` are lazy.
  - The image precompiles bytecode (`UV_COMPILE_BYTECODE=1`), so CLI startup is cheap.
  - The `/healthz` risk is lock contention (M1), not blocking.
- **CLI concurrency is bounded.** Status reads run at most one CLI per backend (the refresh lock; `test_concurrent_readers_share_one_refresh`), and Stop and recycle run at most one, serialized by the worker lock. Under failure the problem is serial re-runs (M4), not unbounded concurrency.
- **User cancel is still DB-only.** The only `gateway.cancel` call site is `stop_backend`'s gather, with `terminate_containers` defaulting to False.
- **Stop's order matches the spec:**
  1. lock;
  2. confirm (counts);
  3. clear warm;
  4. DB cancel of queued and submitted jobs in one transaction (P10);
  5. one gather with the ping first;
  6. container stop;
  7. converge;
  8. forced refresh in `finally`.

  It is idempotent on repeat, and the lock is really held: the `stop_no_lock` and `stop_ping_last` mutants were killed.
- **Pinger:**
  - It holds the lock across check → spawn → record.
  - It persists `ping_call_id`.
  - "Warm" requires a successful ping and at least 1 runner.
  - A failed ping gives unhealthy plus a breaker count.
  - Its exits are fail-safe.
  - One task per backend: P13 shows 1 live task after 5 `ensure_pinger` calls.
  - A breaker trip from inside the pinger's lock doesn't deadlock: P15 shows the recycle completes and cancels the fresh ping.
- **Routes:**
  - All four routes are owner-only through the global guard; `SERVICE_ROUTES` is untouched.
  - The POSTs enforce same-origin. P11: warm with no Origin, warm or stop from an evil Origin, and stop by the service identity all return 403, and `warm_until` stays unset.
  - Jinja autoescape is on (`select_autoescape`), so remote error text is escaped.
- **`_modal_cli`:**
  - Arguments go as an argv list, with no shell.
  - Stop passes `--yes`.
  - `NO_COLOR`/`TERM=dumb`, and stdin is DEVNULL.
  - No env or token appears in any message.
- **CLI output parsing,** checked against the modal 1.5.5 source:
  - `container list` keys are `container_id`, `app_id`, `app_name` and `start_time`.
  - `app list` keys include `description` and `state`.
  - JSON timestamps are timezone-aware ISO strings.
  - "already stopped" is a `SystemExit` message written to stderr with exit code 1.
  - `print_json` uses `soft_wrap=True`, so the JSON is not hard-wrapped.
  - An app missing from the list reads as stopped.

## Test quality (42 tests)
- **All 42 tests assert outcomes,** with no bare no-exception tests.
- **One phantom test.** `test_dispatcher_spawns_nothing_while_stop_runs` (`tests/test_gpu.py:618`) never calls `stop_backend`: it holds a stand-in lock, then asserts the queued job ends up **submitted**, which is the opposite of what a real Stop does.
- **One half-tested name.** `test_breaker_resets_on_success_or_other_failure` covers only "other failure".
- **Mutation results, full suite per mutant: 9 killed / 21.**
  - Killed:
    - `stop_no_lock`, `stop_ping_last`
    - `breaker_threshold_4`
    - `pinger_spawns_every_step`
    - `status_no_invalidate`, `status_stats_ttl_30`
    - `cli_stop_without_yes`, `cli_already_stopped_reraised`
    - `stop_route_always_confirm`
  - **Survived (behaviour unpinned):**
    - `cancel_all_skips_queued`: Stop leaving queued jobs, so the GPU cold-starts right after Stop. This is the spec's own reason for cancelling queued jobs.
    - `stop_confirm_ignores_queued`
    - `stop_gather_raises`: cancel failures.
    - `breaker_threshold_2`
    - `breaker_no_reset_on_job_success`, `breaker_no_reset_on_ping_ok`
    - `breaker_ignores_first_poll_window`
    - `pinger_no_reset_ping`: a new window reads instantly warm.
    - `status_unhealthy_before_stopped`
    - `status_stats_errors_propagate`: nothing drives `GpuStatus.get` with a failing gateway.
    - `startup_no_pinger_resume`: the lifespan resume is untested, because the "resume after restart" test calls `_warm_step` directly.
    - `warm_route_no_force`
- **Add tests for:** each survivor above; Stop and the Stop route when `stop_containers`, `stats` or `cancel` raise; a template assertion that `#gpu-panel` has no self-`load` trigger and carries `hx-sync`; the M2 race (P3 is a ready template); and the stale-window display.

## Recommended Actions
1. **H1:** remove `load` from the self-replacing panel, and add `hx-sync` (`this:drop` on the panel, `closest #gpu-panel:replace` on the forms). Persist the last Stop outcome server-side.
2. **H2:** make `stop_backend` exception-safe, with a failure outcome and a 200 render. Skip stats when the app is not deployed, and log the breaker task's outcome.
3. **M1:** have the dispatcher skip a locked backend, and cap each Stop step with `asyncio.timeout`.
4. **M3 and M4:** order the refresh app-state-first, skip stats for undeployed apps, and add failure backoff.
5. **M5, M2 and M6:** log and retry failed cancels; clear the window with a compare-and-clear; clear unhealthy on job success.
6. Fill the mutation-survivor test gaps and replace the phantom test.
7. **Low items:** L1, L4 and L6 are one-liners; L2, L3 and L7 need an owner or spec decision, or are optional.

### Plan follow-ups (no plan edits made)
Phase-06 Todo items 1–6 are ticked. My read:
- **Gateway:** complete.
- **Runbook:** complete.
- **Items to re-open:**
  - **Status:** M3 and M4 contradict the stated display and error contract.
  - **Worker/Stop:** H2, M1 and M5.
  - **Routes/panel:** H1 violates the polled-panel behaviour, and H2 breaks "all 200".
  - **Tests green:** true, but see the mutation gaps.

The owner-gated live step 6 will currently show "unknown", not "stopped", and pressing Stop there returns 500.

### Metrics
- **Type coverage:** not measured (no type checker configured). The new code is annotated. `Any` is used only for the pre-existing Modal handle.
- **Test coverage:** not measured (no coverage tool in the repo). Mutation score on the targeted new behaviours: 9/21 (43%).
- **Linting:** 0 issues (`ruff check`). CI does not run `ruff format`.
- **Suite:** 354 passed, 1 deselected, in about 27 s.

### Unresolved Questions
1. L3: should a warm click extend the window, `max(existing, now+N)`, or set it, which can shorten it? The docstring and the spec say "extend".
2. M6: should a successful job clear the breaker's unhealthy notice? The spec says only that a ping clears it.
3. L2: is the 2 s "first poll" window meant to exclude boot-time failures? If so, the "ComfyUI did not start" marker is dead.
4. M3: before the handle is invalidated, does Modal's `FunctionGetCurrentStats` return zeros or an error for a stopped app's function? After invalidation the lookup fails for certain.

Status: DONE_WITH_CONCERNS
Summary: Two High defects need fixing before commit. The GPU panel re-requests itself in a tight loop and drops Stop outcomes and about half of Stop confirmations, and Stop returns 500 whenever a Modal step raises. Six Medium issues cover a `/healthz` false-stale during long Stops, a warm-window race, a stopped app shown as "unknown", no failure backoff, silently swallowed cancel failures, and a sticky unhealthy badge. The suite pins 9 of 21 targeted behaviours.

---

## Re-review (2026-09-27, after the implementer's "Review fixes" round)

**Method.** Same as the first pass: read-only, with scratch probes run against the working tree.
- Re-ran 22 probes (the P-series, updated to assert fixed behaviour, plus regression probes).
- Built a new end-to-end htmx harness: the **real app** (real routes, templates and `Worker.stop_backend`, fake gateway) under in-process uvicorn, driven by headless Chrome. A scratch ASGI wrapper injected the Access JWT.
- Ran 54 source/template mutants: my 21 re-anchored on the new code, plus 33 aimed at the fix code.
- Checks: `uv run ruff check` is clean; `uv run pytest -q` gives 379 passed, 1 deselected. Tree state was confirmed unchanged during the review: every touched file's mtime predates it.

### Verdicts on the original findings

| Finding | Verdict | Evidence |
|---|---|---|
| **H1** panel self-poll loop, lost POST responses | **Fixed** | Real-app harness: idle `/gpu` for 13 s gave **1** panel GET (at 10.0 s). A 3 s Stop spanning the poll tick: the poll was dropped (`this:drop`) and "Stopped." rendered. Confirm flow ended in "Stopped. Cancelled 2 running job(s).", with both jobs `cancelled` in the DB. **9/9** confirm trials at click offsets 9.8–10.6 s showed the confirm partial, including 5 where the poll GET was in flight at click time and was aborted by `replace`. The outcome is also persisted server-side (`recent_stop_outcome`, 120 s). |
| **H2** Stop could 500 | **Fixed** | Stop now returns 200 with a rendered outcome in each case below. The breaker's recycle logs `scheduled backend recycle finished: not_converged` and its outcome shows on the panel. |
| **M1** Stop stalls dispatch and `/healthz` | **Fixed**, one caveat (R3) | Scaled probe (`CONVERGE_S=4`): `/healthz` stayed **200 `ok`** during Stop. A job queued mid-Stop stayed `queued`, then was `submitted` right after. A step that hangs forever is cut at the deadline (2.0 s with `CONVERGE_S=2`). |
| **M2** warm click wiped by a stale `until` | **Fixed** | The same race through the real route: the fresh window survived (899 s ahead) and the pinger stayed alive. |
| **M3** stopped app shown "unknown" | **Fixed** | `AppState('stopped')`, `error=None`, **0** stats calls, displays `stopped`. |
| **M4** no negative caching | **Fixed** | 5 concurrent readers against a failing CLI made **1** CLI attempt (0.20 s). 20 reads in 1 s with stats failing made **1** RPC. See R6 for a side effect. |
| **M5** cancel failures swallowed | **Fixed** | 2 calls produced 4 attempts (one retry each) and 4 warnings naming the call ids. `cancel_failures=2` is rendered as "2 Modal cancel(s) failed even after a retry". |
| **M6** sticky unhealthy | **Fixed** | `unhealthy` after the recycle, then `scaled to zero` after one successful render. |

H2 outcomes, one per failure injected:

| Injected failure | Rendered outcome |
|---|---|
| `stop_containers` raises | "Stopped." (stats reported 0/0) |
| `stop_containers` and stats both raise | "Still ? runner(s) after 60s. Last error: stats: …" |
| app stopped | "Stopped.", with 0 stats calls |
| cancels raise | "Stopped. … 2 Modal cancel(s) failed" |
| unexpected DB error | "Stop failed: …" |

Low items:
- **L1:** fixed. The header and panel gate on `window_open`, and a real `start_worker=True` startup cleared an expired row to `(None, None)`.
- **L2:** partly resolved. The dead boot marker is gone. The late-match reset remains, now a deliberate choice pinned by `test_breaker_ignores_a_late_comfyui_down_failure`. Acceptable.
- **L3:** resolved (extend-only).
- **L4 / L5:** fixed. Warm with a failing status is refused ("Status unavailable: …") and the window stays unset; the panel renders the error.
- **L6:** fixed. The error keeps the stderr tail, capped at 500 characters.
- **L8:** fixed.
  - A standalone `UPDATE … RETURNING` returns both call ids and cancels all 3 jobs (2 submitted, 1 queued).
  - Local SQLite is 3.50.4.
  - Production is `python:3.12-slim@sha256:f77ac9e4…`. Its amd64 config, fetched from the registry, shows `PYTHON_VERSION=3.12.14` on Debian `trixie`, whose SQLite is 3.46.x, comfortably above RETURNING's 3.35 minimum.
- **L9:** fixed (via `last_stop_outcome`).

### L7 deferral: acceptable, but the fix round made it slightly worse
The remaining cost only occurs during a Stop's convergence: a few short CLI processes per 5–8 s iteration, for at most about 60 s. That is fine for a rare, user-initiated action.

However, `_stop_backend_locked` now runs its own `app_state()` (`worker.py:491`) in front of `stop_containers()`, which runs `app list` again (`modal_gateway.py:269`). Probe X2: two iterations issued **8** CLI calls (`app list` ×4, `container list` ×2, `container stop` ×2). That is 3+N per iteration, up from 2+N.

A cheap follow-up needs no protocol break: an optional `app_state: AppState | None = None` parameter on `stop_containers`, passed through from the pre-check. Not a blocker.

### New findings introduced by the fixes (none above Low)
1. **R1 (Low): an outer timeout leaves the `modal` CLI child running.**
   - **Where:** `worker.py:526-532`, with `modal_gateway.py:169-170`.
   - **Mechanism:** `_bounded_step` wraps each step in `asyncio.timeout(remaining)`. When the budget runs out mid-CLI, `_modal_cli` is cancelled at `await wait_for(...)`. Only `TimeoutError` kills the process, and the transport stays referenced by the child watcher, so GC does not kill it either.
   - **Evidence (probe X1):** `_bounded_step -> (None, 'app_state: ')`, and the child was **still alive** both inside the loop after `gc.collect()` and after the loop closed, under the default loop and under **uvloop** (production).
   - **Reach:** almost every non-converging Stop cuts one or two CLI calls this way; in its final pass each step gets only the 0.5 s floor.
   - **Impact:** the orphans exit on their own once Modal answers. During a Modal hang they can pile up at roughly 70–100 MB each in the 768 MiB container.
   - **Fix, validated in scratch on both loops:** in `_modal_cli`, add `except BaseException: proc.kill(); raise` after the `TimeoutError` branch.
2. **R2 (Low): two `app list` calls per convergence iteration.** See L7 above.
3. **R3 (Low): the cancel phase runs before the deadline exists.**
   - **Where:** `worker.py:486-488`. `_cancel_all` (30 s timeout, retried once) runs before `deadline = clock() + CONVERGE_S` is set.
   - **Worst case:** about 60 + 60 + 5 + 1.5 ≈ **127 s** (scaled probe: 4.0 s = 2×1 s of cancels + 2 s of convergence). The ~70 s bound holds only for convergence, and the worst case exceeds Cloudflare's roughly 100 s edge timeout (524).
   - **Mitigation already in place:** the outcome is persisted, so the next poll shows it.
   - **Fix:** set the deadline before cancelling and cap each cancel at `min(_CANCEL_TIMEOUT_S, remaining)`, or use about 10 s cancel timeouts.
4. **R4 (Low): a timed-out step shows an empty error.** `str(TimeoutError())` is `""`, so the panel shows "Last error: stop_containers: ." (probe error text: `'stop_containers: '`). Fix: `f"{label}: {exc or type(exc).__name__}"` at `worker.py:532`.
5. **R5 (Low): `_STOP_STEP_TIMEOUT_S` is dead code.** It is defined at `worker.py:47` but never used, and its comment claims a per-step ceiling that `_bounded_step` does not apply. Either use it, as `min(_STOP_STEP_TIMEOUT_S, remaining)`, or delete it.
6. **R6 (Low, UX): one transient failure shows "unknown" for a full minute.**
   - **Where:** `gpu.py:174-185`.
   - **Mechanism:** a failed app-state read is negatively cached for the full 60 s success TTL.
   - **Evidence:** a CLI that fails once at t=0 and would succeed thereafter still reads `unknown` at t=1, 30 and 59 s, and `scaled to zero` only at 61 s.
   - Warm and Stop force a fresh read, so only the badge lingers. A shorter error TTL (about 10 s) would keep the thundering-herd protection.

Informational, no action required:
- **Extend-only (owner decision 1):** the only way to end a warm window early is now Stop, which also cancels running jobs.
- **Stale `paused` reason:** `paused[backend]` is not recomputed while the dispatcher skips a locked backend, so a previous "slots busy" reason can linger in the header during a Stop.
- **Outcome after a Warm:** the persisted Stop outcome stays visible for up to 2 minutes even after a later Warm click.

### Regression sweep: held
- **No new event-loop blocking.** New code adds only SQL, dict updates and logging.
- **No deadlock paths.**
  - `locks[backend]` holders (dispatcher, pinger step, Stop) never wait on `_refresh_locks`, and `GpuStatus.get` never takes `locks[backend]`.
  - The dispatcher no longer waits at all.
  - The breaker still schedules Stop as a task.
  - Shutdown cancellation passes through `asyncio.timeout`/`except Exception` untouched, because `CancelledError` is not an `Exception`. `_log_recycle_result` then logs "cancelled".
- **Extend-only warm logic is sound.**
  - `max(existing, now+N)` with an expired `existing` yields `now+N`, so the pinger's compare-and-clear misses and the fresh window survives (M2).
  - The read-then-write in `start_warm` has no await in between.
  - REAL values round-trip exactly, so the `warm_until = ?` equality is reliable.
- **Deploy `/healthz` is safer than before.** The startup `clear_expired_warm` is a single short write, a resumed pinger or a breaker recycle at startup can no longer stall the first dispatch tick, and the poller is unchanged.

### Mutation re-check (full suite per mutant, 4 in parallel)
- **Original 21: 21/21 killed**, confirming the implementer's claim. `stop_gather_raises` was re-anchored as `stop_cancel_failures_swallowed`, because the gather no longer exists.
- **New 33, aimed at the fix code: 20 killed, 13 survived.**
  - **Killed:**
    - dispatcher locked-skip;
    - compare-and-clear, both the SQL and the ignored result;
    - app-state negative cache and the cached error being served;
    - stats skipped when stopped;
    - retry-once, both removed and resend-all;
    - `_bounded_step` re-raising, and the whole-body guard;
    - outcome recording and its TTL;
    - stopped-app shortcut;
    - clearing unhealthy on job success;
    - extend-only;
    - startup expired-clear;
    - all four `hx-trigger`/`hx-sync` template mutants.
  - **Survived (behaviour correct today, per the probes, but unpinned):**
    - `bounded_step_unbounded`: no test hangs a step against the deadline, so M1's bound is unpinned.
    - `neg_cache_stats_off`: stats negative caching (only app-state's is tested).
    - `warm_step_none_keeps_going`: nothing checks that the pinger **exits** after Stop clears the window. A regression would leak a spinning pinger.
    - `returning_keeps_null_ids`: queued jobs' NULL ids sent to cancel. The fake accepts them; in production that would show bogus "cancel failed" counts.
    - `cas_permanent_path_blind`: the PERMANENT-spawn clear.
    - `recycle_no_log_callback`: the recycle outcome log.
    - `ping_forces_nothing`.
    - `warm_route_trusts_error`: L4.
    - `stderr_head_again`: L6.
    - `tpl_panel_stale_window`, `tpl_header_stale_window`: L1 gating.
    - `tpl_panel_hides_error`: L5.
    - `tpl_panel_hides_cancel_failures`: the M5 message.
  - **Recommendation:** add tests for at least the first four.

### Re-review recommended actions
1. **Before commit** (two lines): kill the CLI child on any cancellation in `_modal_cli` (R1).
2. **Optional:** bound the cancel phase by the same deadline (R3); fix the empty timeout text (R4); remove or use `_STOP_STEP_TIMEOUT_S` (R5); shorten the negative-cache TTL for errors (R6).
3. **Follow-ups:** pass the app state into `stop_containers` (R2 / L7); add tests for the four highest-value survivors above.

Status: DONE_WITH_CONCERNS
Summary: All 2 High and 6 Medium findings are fixed, verified by the real-app headless-Chrome harness (no poll loop, 9/9 confirmations kept, outcomes shown) and by the probes. All 21 original mutants are now caught, but 13 of 33 new mutants on the fix code survive. No new blocker: six Low regressions remain, the most useful to fix before commit being the orphaned `modal` CLI child left running when Stop's per-step timeout fires (a two-line fix).
