# Red team: Failure Mode Analyst (verification role: Flow Tracer)

**Scope:** `plan.md` and phase files 01–08.

**Evidence sources:**
- Modal SDK 1.5.5 at `~/.local/share/uv/tools/modal/lib/python3.12/site-packages/modal/` (written `SDK/` below) and synchronicity 0.12.5 next to it.
- `qwen21_uc_app.py`.
- The contract, scout and research reports.
- restic docs.

**Local checks:**
- Two hermetic Python runs against the installed SDK, for Finding 1. Only `_Invocation.pop_function_call_outputs` and `_Client.from_env` were stubbed, so there was no network access and no Modal call.
- `issubclass` introspection of the exception classes.

No server, Cloudflare, GitHub or Modal state was touched.

---

## Finding 1: "Still pending" is Python's builtin `TimeoutError`, so the planned poll marks every running job failed

- **Severity:** Critical
- **Location:**
  - Phase 2: "Key Insights" (catch order), "Architecture" (the gateway poll mapping) and step 6 (the `classify_poll_exception` test).
  - Phase 6: `warm_tick`, which inherits the problem.
- **Flaw:**
  - The plan and research-02 §1 say the pending signal is `modal.exception.TimeoutError`.
  - The SDK actually raises the **builtin** `TimeoutError`, a subclass of `OSError` and not of Modal's class.
  - None of the plan's specific `except` clauses catch it, so it falls into `except Exception` and becomes `PollResult.failed("TimeoutError")`.
- **Failure scenario:**
  1. The owner submits 4 jobs.
  2. On the first `poll_once`, 2 s later, every job becomes `failed` with the text "TimeoutError".
  3. Modal keeps rendering and billing those jobs, and nobody reads the results.
  4. Retry spawns again and fails again 2 s later. Every production job fails.
  5. `deploy.sh`'s health check (version only) still passes.
  6. In phase 6, the ping in flight is classed as finished on its first poll. `ping_calls` is popped, the UI never shows "warming", and the "one ping in flight" rule is lost.

  The step-6 unit test uses a hand-built `modal.exception.TimeoutError()`, and the worker tests use the fake gateway, so both stay green. Only the opt-in, owner-gated live test (phase 2, step 14) or the live phase-4 step 17 on production would expose it. The plan has no pre-decided response for either.
- **Evidence:**
  - The plan and research:
    - `phase-02-engine.md:24`: "plain `TimeoutError` while pending".
    - `phase-02-engine.md:47`: "`modal.exception.TimeoutError`, which is also the 'still pending' signal".
    - `phase-02-engine.md:186-195`: the `except` chain.
    - `phase-02-engine.md:301`: the test uses exception instances built by hand.
    - `research/researcher-02-modal-sdk-app-plugin.md:20-24`: "raises plain `modal.exception.TimeoutError`".
  - Where the exception comes from:
    - `SDK/_functions.py:70-75` imports only `ExecutionError, InvalidError, NotFoundError, OutputExpiredError` from `.exception`.
    - `SDK/_functions.py:334` runs `raise TimeoutError()`, which therefore resolves to the builtin.
  - Introspection:
    - `'TimeoutError' in vars(modal._functions)` returns `False`.
    - `issubclass(builtins.TimeoutError, modal.exception.TimeoutError)` returns `False`.
    - The builtin's MRO is `TimeoutError → OSError → Exception`.
  - synchronicity:
    - `synchronicity/exceptions.py:43-48` wraps the builtin `TimeoutError` in `UserCodeException`.
    - `synchronicity/async_wrap.py:31-33` re-raises the original object to the `.aio` caller.
  - Hermetic trace, with the plan's `except` chain copied verbatim and an output response of `outputs=[]`, `num_unfinished_inputs=1`: `FAILED text='TimeoutError' type=builtins.TimeoutError`.
  - The corrected mapping (`except TimeoutError:` → pending) returns `pending` for a pending call and `failed: expired` for an expired one in the same harness.
- **Suggested fix:**
  - Catch the builtin `TimeoutError` as pending, after `OutputExpiredError` and `FunctionTimeoutError`. Neither of those subclasses the builtin.
  - Replace the hand-built-exception test with one that runs the real `poll_function` with only `pop_function_call_outputs` stubbed, covering the pending, expired and done cases.
  - Correct research-02 §1 and the phase 2 "Verified SDK facts".

## Finding 2: `TRANSIENT` includes `grpclib.GRPCError`, the base of every Modal server error, so permanent failures are retried forever and queued jobs stall silently

- **Severity:** High
- **Location:**
  - Phase 2: "Architecture" (the `TRANSIENT` tuple, the spawn-failure paragraph and the dispatcher sketch).
  - Phase 6: step 7.6 (`modal app stop`, then redeploy).
- **Flaw:**
  - Every non-OK gRPC status maps to a `_GRPCErrorWrapper` subclass, and that class subclasses `grpclib.GRPCError`. This covers `NotFoundError`, `AuthError`, `PermissionDeniedError`, `InvalidError` and `ConflictError`.
  - The plan says "`NotFoundError` … marks the job failed". In fact it matches `TRANSIENT`, so the job stays queued and the dispatcher backs off, up to 60 s at a time, forever.
  - Queued jobs never time out; only submitted jobs do.
  - The only pause reason ever shown to the user is the disk guard.
  - The per-process `Cls.from_name(...)()` cache is never invalidated.
- **Failure scenarios:**
  - **(a) Token rotation.**
    1. The owner rotates the Modal server token, following the phase 8 guide, and revokes the old one before `.env` is updated.
    2. Every spawn raises `AuthError`, which matches `TRANSIENT`, so the dispatcher backs off forever.
    3. Jobs sit in "queued" with no reason shown.
    4. Polls of submitted jobs report "pending" until the 1800 s timeout.
  - **(b) Phase 6 step 7.6.**
    1. `modal app stop qwen21-uc` runs, then a redeploy through `deploy-modal.yml`.
    2. A name lookup returns `app_id` only for a deployed app. For a stopped app it returns `previous_app_id` instead, so the deploy creates a **new** app with new object IDs.
    3. Atelier's cached handle was bound once to the stopped app's service function. Spawn, ping and stats therefore fail with a `_GRPCErrorWrapper`, and dispatch stalls until Atelier restarts.
    4. The step checks only the badge. The badge's deployed or stopped state comes from a fresh CLI subprocess, so the step can "pass" while dispatch is dead.
- **Evidence:**
  - The plan:
    - `phase-02-engine.md:179-180`: `TRANSIENT`.
    - `phase-02-engine.md:199`: the claim that `NotFoundError` fails the job, and the per-process cache.
    - `phase-02-engine.md:216-217`: backoff and `break`.
    - `phase-02-engine.md:104`: the timeout applies only to submitted jobs.
    - `phase-02-engine.md:211`: only the disk guard sets `dispatch_paused`.
    - `phase-02-engine.md:359`: the risk row covers only the opposite case.
    - `phase-06-gpu-status-warm-stop.md:179`: stats use the cached `Cls`.
    - `phase-06-gpu-status-warm-stop.md:178`: the plan already expects several `app list` rows sharing the name.
    - `phase-06-gpu-status-warm-stop.md:200-201`: step 7.6.
  - Error mapping:
    - `SDK/_grpc_client.py:27-44`: all 16 non-OK statuses map to wrapper classes, for example `UNAUTHENTICATED: AuthError` and `NOT_FOUND: NotFoundError`.
    - `SDK/exception.py:66`: `class _GRPCErrorWrapper(grpclib.GRPCError)`.
    - Introspection: `NotFoundError`, `AuthError`, `PermissionDeniedError`, `InvalidError`, `ResourceExhaustedError`, `InternalError` and `ServiceError` are all subclasses of `GRPCError`.
  - App identity after a stop:
    - `SDK/cli/app.py:87-93`: `app_id` is set only for a deployed app; a stopped one gives `previous_app_id`.
    - `SDK/runner.py:113-133`: with no deployed app, the deploy calls `_init_local_app_new`.
    - `SDK/runner.py:156-167`: IDs are reused only from the existing layout.
    - `SDK/cls.py:77-90`: bound methods hydrate once from `service_function.object_id`.
- **Suggested fix:**
  - Limit `TRANSIENT` to `modal.exception.ConnectionError`, `grpclib.exceptions.StreamTerminatedError`, `ServiceError` and `ResourceExhaustedError`.
  - On spawn, map `NotFoundError`, `ConflictError`, `AuthError`, `PermissionDeniedError` and `InvalidError` either to "fail the job" or to "pause the backend with a reason the user can see".
  - Drop the cached `Cls` on those errors, and whenever `app_state()` reports a changed `app_id`.
  - Add an alarm on the age of queued jobs.
  - Extend step 7.6 to submit one job after the redeploy.

## Finding 3: On this backend, `FunctionCall.cancel()` shuts down the whole container, so sibling jobs are rescheduled after a cold start

- **Severity:** High
- **Location:**
  - Phase 2: step 6 (the `cancel()` claim) and Requirements (the best-effort cancel on timeout).
  - Phase 3: step 5 (Cancel on the queue).
  - Phase 6: Key Insights "Stop deliberately differs", `stop_backend` and `stop_containers`.
- **Flaw:**
  - `Qwen21UC`'s methods are synchronous `def`s under `@modal.concurrent(max_inputs=4)`.
  - For a synchronous input under input concurrency, the container's cancel callback sends SIGINT to the container's own process. The SDK logs that this "shuts down the container, causing concurrently running inputs to be rescheduled in other containers."
  - `terminate_containers=False` does not prevent this.
  - Both plan claims are false:
    - "sibling inputs are never rescheduled";
    - "Cancel every tracked call without terminating containers, so nothing is rescheduled".
- **Failure scenario:**
  - **Single cancel.**
    1. A batch of 4 is running on the warm container, and the owner cancels one row (criterion 4, acceptance row 4).
    2. The next container heartbeat delivers the cancel, and SIGINT shuts the container down.
    3. The other 3 inputs, still `submitted` in Atelier, are rescheduled, and so is any ping in flight.
    4. A new L40S cold-boots (about 68 s), and all 3 renders restart from zero.
    5. Every poller timeout cancel does the same.
  - **Stop.**
    1. Stop issues its cancels one after another (`phase-06:131-132`).
    2. The first cancel to arrive sends SIGINT while later calls are still live. Those calls are rescheduled and can boot a new container, so the "no container reappears within 90 s" check can fail intermittently.
    3. `stop_containers` may then run `container stop` on a container that is already exiting. The CLI exits with `SystemExit("…already stopped.")`, which raises `ModalCliError`, so the Stop route errors even though the database already marks the jobs cancelled.
- **Evidence:**
  - The plan: `phase-02-engine.md:299` and `:104`; `phase-03-web-ui-and-access-auth.md:215`; `phase-06-gpu-status-warm-stop.md:43-49`, `:121-135`, `:141-148` and `:228`.
  - The backend: `qwen21_uc_app.py:99-100` (`@modal.concurrent(max_inputs=4)`) and `:140-150` (synchronous methods).
  - Container-side cancellation:
    - `SDK/_runtime/container_io_manager.py:583-586` and `:1103-1104`: concurrency is enabled when `max_concurrent_inputs` is greater than 1.
    - `SDK/_runtime/container_io_manager.py:686-708`: the heartbeat delivers the cancel to `current_inputs[id].cancel()`.
    - `SDK/_runtime/container_io_manager.py:182-190`: `IOContext.cancel`.
    - `SDK/_container_entrypoint.py:193`: the input-concurrency branch.
    - `SDK/_container_entrypoint.py:204-217`: `cancel_callback_sync` calls `os.kill(os.getpid(), signal.SIGINT)`, with the warning at `:211-215`.
    - `SDK/_container_entrypoint.py:233-237`: synchronous inputs get `cancel_callback_sync`.
  - The CLI:
    - `SDK/cli/container.py:321-323`: stop reschedules running inputs.
    - `SDK/cli/container.py:332-333`: an already stopped container ends in `SystemExit`.
  - The contract records the rescheduling behaviour at `brainstorm…:96`.
- **Suggested fix:**
  - **Single cancel.** When other inputs are in flight on the backend, mark the job cancelled in the database only and discard the late result; brief §4 already accepts "ComfyUI may finish rendering anyway". Call Modal's cancel only when the job is the sole input in flight. Apply the same rule to timeout cancels.
  - **Stop.** Send all cancels at once with `asyncio.gather`, and treat "already stopped" as success. Then repeat `stats()` and `container list`/`container stop` until runners and backlog are both 0, or a deadline passes.
  - **Owner decision.** Ask the owner whether the non-goal ("the only change is `ping()`") may be widened to an async `run_workflow`, which would allow cancelling one input without stopping the container.

## Finding 4: Stop's per-backend lock is only checked, never held, by the pinger and dispatcher, so spawns slip in across `await`

- **Severity:** High
- **Location:**
  - Phase 6: Key Insights "One lock per backend", and the `warm_tick` and `stop_backend` sketches.
  - Phase 2: the dispatcher sketch.
- **Flaw:**
  - `warm_tick` checks `locks[id].locked()` once. It then awaits `gateway.poll` and `gateway.spawn_ping`, and stores the call ID afterwards.
  - `dispatch_once` fetches a batch, then awaits `spawn_workflow` for each job.
  - Neither ever acquires the lock. Checking and then acting across an `await` is not mutual exclusion, even on a single event loop.
- **Failure scenario:**
  - **Pinger.**
    1. A warm window is active. The pinger passes the lock check and awaits `spawn_ping`, an RPC of about 0.2–0.5 s.
    2. The owner presses Stop. Stop takes the lock, runs `clear_warm`, and pops `ping_calls`, which is still empty.
    3. Stop cancels the known calls, then runs `stop_containers`.
    4. The spawn returns, and the pinger stores the new `ping_calls[id]`: a live, uncancelled ping.
    5. That ping either boots a fresh L40S after Stop, or lands on the container being stopped and is rescheduled by `container stop`.
    6. The cost is about $0.07 of GPU, and the check "no container reappears within 90 s" fails.
  - **Pinger variant.** The pinger is awaiting `poll` on the ping that Stop pops. The poll returns done, and `self.ping_calls.pop(backend.id)` raises `KeyError`, aborting that tick for every backend.
  - **Dispatcher.** In the middle of its loop, it spawns jobs that Stop has already cancelled. Each becomes a spawn followed by a cancel against 0 containers, and each can trigger a cold boot.
- **Evidence:**
  - `phase-06-gpu-status-warm-stop.md:51`: the claim.
  - `:104`: the check.
  - `:107` and `:118`: the awaits.
  - `:110`: the unguarded `pop`.
  - `:122-133`: Stop pops at `:129`, then awaits at `:132-133`.
  - `:182`: "the lock check in `dispatch_once`".
  - `:198` and `:228`: the acceptance checks.
  - `phase-02-engine.md:212-223`: the batch is fetched, then spawns are awaited; only `mark_submitted`'s row count guards them.
  - `SDK/cli/container.py:321-323`: stop reschedules running inputs.
- **Suggested fix:**
  - The pinger and dispatcher hold `async with locks[id]` across check, spawn and record, so Stop waits for spawns in flight. An alternative is a per-backend "stop generation" number, re-checked after every `await`, with anything spawned under an older generation cancelled.
  - Use `ping_calls.pop(id, None)`.
  - Add a test that interleaves Stop with a suspended `spawn_ping` in the fake gateway.

## Finding 5: The tunnel cutover block has no stop between restarting the live connector and stopping the replica, and its health check reads log lines from before the restart

- **Severity:** High
- **Location:** Phase 4, step 16 (the replica cutover block) and the risk row "The restarted service fails on the new config".
- **Flaw:**
  - Step 16 is one continuous bash block, with no `set -e` and no conditionals.
  - `systemctl restart cloudflared` (sub-step 5) is followed unconditionally by `systemctl stop cloudflared-atelier-cutover` (sub-step 6).
  - The only check is `journalctl -u cloudflared -n 30 | grep -c "Registered tunnel connection"`. It reads the unit's last 30 lines, which include the previous process's lines, so it can report ≥ 1 after a failed restart.
- **Failure scenario:**
  1. The restarted `cloudflared` fails. For example, the unit runs as another user or with flags that differ from the replica's `systemd-run` line; that line is written by hand, runs as root, and "mirrors" `ExecStart` only by manual copying.
  2. `systemctl restart` reports an error, but the block continues.
  3. The check still finds old "Registered tunnel connection" lines from the previous process, and the next line stops the replica.
  4. The replica was the only healthy connector for folio, cdn and learn, so the shared tunnel goes dark. This is the contract's named disaster: a broken config takes folio.flowitup.com offline.
  - The risk row "Folio stays up on the replica throughout" is false for the block as written.
  - An agent running it through `ssh folio-prod '…'`, the plan's usual style, has no pause point.
  - "Ask before each step" gates step 16 as a whole, not its sub-steps.
- **Evidence:**
  - `phase-04-container-ci-cd-and-rollout.md:352-375`: the block. `:372` restarts, `:373` is the check, `:374` stops the replica unconditionally.
  - `:368-369`: the replica command is written by hand, and its flag placement is marked [UNVERIFIED].
  - `:461`: the risk row.
  - `:305`: the approval gate is per step.
  - `brainstorm-260925-1507-…md:36`: the contract constraint.
  - `reports/scout-02-folio-prod-1-live-state.md:26-33`: folio, cdn and learn all run on this tunnel.
  - `research/researcher-01-…md:78`: the drop window during a restart is undocumented.
- **Suggested fix:**
  - Split step 16 into sub-steps, each approved and checked on its own.
  - Check only the new process. Use `systemctl is-active cloudflared`, and read the journal filtered to the new invocation (`_SYSTEMD_INVOCATION_ID` of the new process) or with `--since "$T_RESTART"`. Also probe the metrics `/ready` endpoint on 127.0.0.1:20241.
  - Stop the replica only after the external curls for folio, cdn and learn match their baselines.
  - Build the replica command from `systemctl show -p ExecStart cloudflared`, not by hand.

## Finding 6: Deploy and rollback: manual rollback leaves `current-tag` on the bad SHA, health cannot see engine regressions, and a dropped SSH connection skips the rollback

- **Severity:** High
- **Location:**
  - Phase 4: `deploy.sh`, step 6 (the guide's manual rollback and routine operations) and the Rollback section.
  - The files that read `current-tag`: phase 5 (verify and restore test) and phase 6 step 7.4.
- **Flaw:**
  1. **Tags.** The documented manual rollback runs `compose up` with `previous-tag` but never rewrites `current-tag` or `previous-tag`. Yet `current-tag` is treated as the last known good image by `deploy.sh`, the backup verify, the restore test, the restart runbook and "any manual compose command".
  2. **Health.** Health means only that `/healthz` reports the SHA, which proves a DB `SELECT 1` and the sentinel. The worker loops "log each loop exception without dying", so a broken engine passes health. The releases that need rolling back are therefore exactly the ones left to the manual path.
  3. **SSH drop.** `deploy.sh` output is tied to the SSH session.
     - `up()` redirects only stdout, and compose writes its progress to stderr.
     - If the CI connection drops (the run is cancelled or the runner is lost), the next write to stderr fails with EPIPE, and compose, a Go program, dies with SIGPIPE in the middle of recreating the container.
     - Under `set -euo pipefail`, the failure branch starts by writing to stderr (`logger -s`), so the rollback never runs.
- **Failure scenario:**
  1. Release B passes health, but every job fails (for example, Finding 1).
  2. The owner rolls back by hand to A; `current-tag` still says B.
  3. From then on:
     - The phase 6 command `ATELIER_TAG=$(cat current-tag) compose up`, or any manual compose command the guide prescribes, silently redeploys B.
     - The Sunday verify and the restore test run B's `backup_db`.
     - The next CI deploy, C, records `previous-tag = B`. If C fails, `deploy.sh` rolls back to B.
     - If C succeeds, A is neither current nor previous, so pruning (keep 3) can delete it. The server cannot pull outside a deploy job (research-02 §8), so there is no local way back to A.
- **Evidence:**
  - `phase-04-container-ci-cd-and-rollout.md`:
    - `:194`: `prev=$(cat current-tag)`.
    - `:198-207`: the success and failure branches.
    - `:209-212`: pruning keeps only the current, the previous and one other tag.
    - `:301` and `:469`: manual rollback without writing the tags.
    - `:302`: routine operations read `current-tag`.
    - `:55`: health is the version only.
    - `:172-175`: `set -euo pipefail` and `logger -s`.
    - `:195`: only stdout is redirected.
    - `:203`: the failure branch logs before rolling back.
  - `phase-03-web-ui-and-access-auth.md:71`: what `/healthz` checks.
  - `phase-02-engine.md:306`: "logs each loop exception without dying".
  - `phase-05-backups-and-restore.md:138` and `:232`: the verify and restore test use `current-tag`.
  - `phase-06-gpu-status-warm-stop.md:196`: the restart command uses `current-tag`.
  - `research/researcher-02-…md:217-219`: no pulls outside a deploy job.
- **Suggested fix:**
  - Add a `rollback` command to `deploy.sh`, or a root-only `rollback.sh`, that swaps `current-tag` and `previous-tag` atomically. Make it the only documented manual path.
  - Make `/healthz` report whether the worker loops are alive: the time of the last successful tick and the count of consecutive errors, returning 503 when stale.
  - Detach the deploy from the SSH session: use `trap '' HUP PIPE` with `exec > >(logger -t atelier-deploy) 2>&1` after validation, or run it with `systemd-run --wait --pipe`.
  - Set `cancel-in-progress: false` explicitly.

## Finding 7: The weekly verify lists `/data/images` without `--recursive`, sees zero PNGs and reports every referenced image missing

- **Severity:** High
- **Location:** Phase 5: Architecture (the verify pipeline), step 4 of `atelier-backup-verify.sh`, and the risk row about the node format.
- **Flaw:**
  - With a directory filter, `restic ls` lists only that directory's direct children unless `--recursive` is given.
  - Images live at `images/YYYY/MM/job-<id>.png`, so `restic ls --json latest /data/images` returns only `/data/images/2026`, a directory node.
  - Verify therefore finds 0 PNGs, marks every referenced image missing, and exits 3.
  - The unit tests feed hand-written flat PNG lines, so they pass.
  - The risk table expects a "parse error", not a false data-loss report.
  - **A second false failure:** the check tolerates images **added** between the DB snapshot and restic's walk, but not images **deleted** in that window. Delete removes the row first, then the files, so a deleted image is still in the snapshot DB but missing from the tree.
- **Failure scenario:**
  1. On the first Sunday verify (step 13), the badge turns red with "N referenced PNGs missing", although the backup is complete.
  2. The symptom reads as backup data loss.
  3. The pre-decided response, "adjust the parser", invites loosening the matcher, for example by accepting directory nodes. That would later hide files that really are missing.
  4. Criterion 11 cannot pass until someone finds the missing flag.
- **Evidence:**
  - `phase-05-backups-and-restore.md:89` and `:138`: the command.
  - `phase-05-backups-and-restore.md:39-42`: only added images are tolerated.
  - `phase-05-backups-and-restore.md:202`: hand-written fixtures.
  - `phase-05-backups-and-restore.md:289`: the risk row.
  - `phase-02-engine.md:106`: the `images/YYYY/MM/` layout.
  - `phase-03-web-ui-and-access-auth.md:69`: delete removes the row, then the files.
  - The restic docs (`restic.readthedocs.io/en/stable/045_working_with_repos.html`): "Files in subdirectories are not listed when filtering by directories. If the `--recursive` flag is used, then subdirectories are also included."
- **Suggested fix:**
  - Use `restic ls --json --recursive latest /data/images`, and capture the test fixtures from real nested output.
  - Check each missing path against the live DB. If its row is gone, report "deleted after snapshot" as a warning, not a failure; running verify through `docker exec` gives access to the live DB.

## Finding 8: The disaster-restore runbook needs twice the data size on the same volume, its move nests `images/images`, and its compose commands fail

- **Severity:** Medium
- **Location:** Phase 5, step 16 (the disaster procedure); phase 4 Rollback, "Full removal".
- **Flaw:**
  - The procedure restores the **whole** snapshot into `.restore` on `/mnt/atelier-data`, then moves the trees into place.
  - The volume is 50 GB, the image cap is 40 GB, and `/` belongs to Folio with 21 GB free.
  - For an in-place disaster, such as a corrupt DB or a partial deletion, the full restore fits only while images take up to about 22 GB.
  - `mv .restore/data/images` into an existing `images/` creates `images/images`.
  - Plain `docker compose down` and `up -d --wait` in `/opt/atelier` abort, because the compose file requires `${ATELIER_TAG:?required}`.
- **Failure scenario:**
  1. After months of use, with 30 GB of images, `atelier.db` is corrupted.
  2. The runbook's first command, `compose down`, errors.
  3. Once past that, `restic restore latest --target .restore` runs out of space partway through: 30 GB into about 18 GB free.
  4. Restoring without the images is not documented, so the owner improvises on Folio's production host.
- **Evidence:**
  - `phase-05-backups-and-restore.md:238-244`: the procedure.
  - `phase-05-backups-and-restore.md:291`: the plan admits a full restore fits only "while data is small".
  - `phase-02-engine.md:71`: the 40 GB cap.
  - `phase-04-container-ci-cd-and-rollout.md:35` and `:315`: the 50 GB volume.
  - `phase-04-container-ci-cd-and-rollout.md:108`: `${ATELIER_TAG:?required}`.
  - `phase-04-container-ci-cd-and-rollout.md:302`: the guide itself says every manual compose command needs `ATELIER_TAG`.
  - `phase-04-container-ci-cd-and-rollout.md:472`: the full-removal steps use plain `docker compose down`.
  - `reports/scout-02-…md:8`: 21 GB free on `/`.
- **Suggested fix:** Document two paths, prefix every compose command with `ATELIER_TAG=$(cat /opt/atelier/current-tag)`, and rehearse path (a) during the restore test.
  - **(a) DB only.**
    1. Run `restore latest --include /data/backup/atelier.db`.
    2. Run `verify-restore` against the live images.
    3. Restore only the missing PNGs, each with `--include`.
  - **(b) Full restore.** Restore only onto a fresh volume, and move trees with `rsync -a` or `mv -T` semantics so nothing nests.

## Finding 9: Warm-up pings don't check ComfyUI, so a container whose ComfyUI died stays "warm" and fails every job for the whole window

- **Severity:** Medium
- **Location:** Phase 1 Requirements (`ping()` "never touches ComfyUI"); phase 6 Key Insights (pings are the only warm-up mechanism) and `display_state`.
- **Flaw:**
  - ComfyUI starts once per container in `@modal.enter()` and nothing watches it afterwards.
  - Every input depends on ComfyUI at `127.0.0.1:8188`, but `ping()` succeeds regardless, and `display_state` shows "warm" whenever at least one runner is up.
  - If the ComfyUI subprocess exits, Modal still sees a healthy container, and pings reset its idle timer every 30 s.
  - Every job then fails at once with a `requests` connection error. Atelier can't even rebuild that exception, because `requests` is not in its image, so it arrives as `ExecutionError`.
  - Nothing detects this, the runbook has no recovery step, and the plan's fail-safe analysis covers only Atelier dying, not the backend.
- **Failure scenario:**
  1. During a 30-minute warm window, a phase 7 custom workflow crashes ComfyUI.
  2. The UI still says "warm", and every retry fails in under a second.
  3. The owner pays up to about $0.98 for a dead GPU until the window ends or they happen to press Stop.
- **Evidence:**
  - `qwen21_uc_app.py:102-118`: ComfyUI is started once with `Popen` and never monitored.
  - `qwen21_uc_app.py:120-138`: `_run` calls `requests.post`/`get` on 127.0.0.1:8188.
  - `phase-01-start.md:42`: the `ping()` requirement.
  - `phase-01-start.md:41`: the image's dependencies don't include `requests`.
  - `phase-06-gpu-status-warm-stop.md:38-40` and `:69`: pings as the only mechanism, and `display_state`.
  - `SDK/_utils/function_utils.py:544-560`: a remote exception that can't be rebuilt locally becomes `ExecutionError`.
- **Suggested fix:**
  - Add a circuit breaker in Atelier, which stays within the non-goal: after N consecutive instant failures with connection-refused text, clear the warm window and run the Stop path, so the next job gets a fresh container.
  - Show "backend unhealthy" in the GPU badge, and add "press Stop to recycle" to the runbook.
  - Ask the owner whether `ping()` may probe `/system_stats`. That is a one-line change, but it widens the contract's single allowed change to the Modal script.

## Finding 10: One unsaveable result blocks the poller for up to 30 minutes and downloads its data again every 2 s

- **Severity:** Medium
- **Location:** Phase 2: Architecture (the `poll_once` sketch) and step 9 (how `run()` handles loop errors). Phase 7: arbitrary uploaded graphs use the same path.
- **Flaw:**
  - `poll_once` loops over submitted jobs without catching errors per job.
  - An exception from `storage.save_result` or `jobs.complete` aborts the whole tick, and the next tick hits the same job first.
  - `get(timeout=0)` leaves the output in place (`clear_on_success=False`), and results over 2 MiB come from blob storage, so each retry downloads the whole result again.
  - The loop is freed only when that job's 1800 s timeout marks it failed.
- **Failure scenario:**
  1. A phase 7 upscaler workflow returns an image Pillow refuses. Examples: a 16k × 16k PNG, which exceeds twice `MAX_IMAGE_PIXELS` and raises `DecompressionBombError` (a guard the plan deliberately keeps), or a 16-bit image that can't be written as WebP.
  2. Every 2 s the poller downloads about 100 MB, raises, and skips all later jobs.
  3. Three other jobs that finished on Modal show "running" for 30 minutes.
  4. Temporary files from failed atomic writes may pile up on the volume.
- **Evidence:**
  - `phase-02-engine.md:225-238`: no per-job `try`.
  - `phase-02-engine.md:306`: "logs each loop exception without dying".
  - `phase-02-engine.md:370`: the decompression-bomb guard stays on.
  - `phase-07-prompt-library-and-workflows.md:82-85`: custom workflow results go through the same poller.
  - `SDK/_functions.py:319-329`: `poll_function` passes `clear_on_success=False` at `:328`.
  - `SDK/_utils/function_utils.py:528-529`: a blob result goes through `blob_download`.
  - `SDK/_utils/blob_utils.py:36`: the 2 MiB inline limit.
  - `SDK/_utils/blob_utils.py:402-414`: `blob_download`.
- **Suggested fix:**
  - Wrap each job in `try`/`except`.
  - On a save error that won't go away, mark the job failed with its text and remove any temporary files, instead of retrying forever.
  - Check the result size before decoding.
  - Test with a fake gateway that returns an oversized or 16-bit PNG next to a normal one.

---

## Flow traces

1. **Poll of a pending call: FAILED against the plan.**
   1. `FunctionCall.get.aio(timeout=0)` enters `SDK/_functions.py:2180-2199`.
   2. That calls `_Invocation.poll_function` (`:319-334`), which calls `pop_function_call_outputs` (`:225-265`): a single RPC when the timeout is 0.
   3. There are no outputs and at least one unfinished input, so the SDK runs `raise TimeoutError()` (`:334`). This is the builtin class, given the imports at `:70-75`.
   4. synchronicity wraps it (`exceptions.py:43-48`) and unwraps it again (`async_wrap.py:31-33`), so the caller receives `builtins.TimeoutError`.
   5. In the plan's `except` chain, it reaches `except Exception`, and the job is marked failed. The hermetic run confirms this.
2. **Classifying server errors: FAILED against the plan.**
   1. An RPC error is mapped from its status to an exception class in `SDK/_grpc_client.py:27-44`; every class is a wrapper.
   2. The wrapper is `_GRPCErrorWrapper(grpclib.GRPCError)` (`SDK/exception.py:66`).
   3. The plan's `except TRANSIENT` therefore matches NotFound, Auth, Invalid and Conflict errors. The job is kept pending or backed off, and never failed.
3. **Cancelling a running input: FAILED against the plan.**
   1. `FunctionCall.cancel` (`SDK/_functions.py:2257-2271`) reaches the server.
   2. The container's heartbeat receives it (`container_io_manager.py:686-708`) and calls `IOContext.cancel` (`:182-190`).
   3. Synchronous inputs under input concurrency use `cancel_callback_sync` (`_container_entrypoint.py:233-237`, `:204-217`).
   4. That sends SIGINT, the container exits, and sibling inputs are rescheduled.
4. **App stop, then redeploy: the cached handle goes stale.**
   1. `modal deploy` looks the app up by name (`runner.py:113-133`). For a stopped app, `app_id` is empty (`cli/app.py:87-93`).
   2. The deploy therefore calls `_init_local_app_new` and gets new IDs; IDs are reused only from an existing layout (`runner.py:156-167`).
   3. Atelier's handle was hydrated once (`cls.py:77-90`), so it keeps pointing at the dead app until the process restarts.
5. **Stop versus the pinger: FAILED.**
   1. The pinger checks the lock (`phase-06:104`), then awaits the spawn (`:118`).
   2. Meanwhile Stop runs (`:122-133`) and pops `ping_calls` (`:129`).
   3. The pinger then stores the new call (`:118`), leaving an uncancelled ping.
6. **Deploy and rollback: PARTIAL.**
   - The automatic path (`phase-04:194-207`) restores both the container and the tags.
   - The manual path (`:301`, `:469`) restores the container only. The readers of `current-tag` (`phase-04:194` and `:302`; `phase-05:138` and `:232`; `phase-06:196`) keep the bad SHA.
   - On an SSH drop, a write to stderr fails with EPIPE before the rollback runs (`:195`, `:203`).
7. **Tunnel cutover: FAILED gating.**
   1. `phase-04:372` restarts the live connector.
   2. `:373` greps a journal window that mixes the old and new processes.
   3. `:374` stops the replica unconditionally.
8. **Weekly verify: FAILED.** `phase-05:138` runs `restic ls --json latest /data/images` without `--recursive`. With the layout at `phase-02:106`, this yields 0 PNG nodes, so every row is "missing" and verify exits 3.
9. **Resume after restart: OK, once Finding 1 is fixed.** `list_submitted` feeds `FunctionCall.from_id`, which does no I/O (`SDK/_functions.py:2271-2312`). `get(timeout=0)` then works, because results are kept (`clear_on_success=False`, `:328`).

## Unresolved questions

1. Which exact status does the server return when a job is spawned against a stopped app's cached function ID (NotFound or FailedPrecondition)? Every non-OK status lands in `TRANSIENT`, so Finding 2 holds either way.
2. Does sshd send SIGHUP to a forced command without a PTY when the connection drops? Finding 6, point 3, relies only on EPIPE on stderr.
