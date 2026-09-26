# Red team: Assumption Destroyer (verification role: Scope Auditor)

Date: 2026-09-25. Scope: `plan.md` and `phase-01` to `phase-08`.

**Evidence sources:**
- The installed Modal SDK 1.5.5. `SDK/` below means `/Users/sweet-home/.local/share/uv/tools/modal/lib/python3.12/site-packages/modal/`.
- `qwen21_uc_app.py` and `README.md` at the repo root.
- The Folio and LearnFlow repos.
- The plan's `reports/` and `research/` folders.
- One read-only `gh auth status`.
- Two upstream sources fetched: restic `cmd_ls.go` and ComfyUI `execution.py`.

No state-changing call was made to Modal, Cloudflare or GitHub, and no SSH session was opened.

## Finding 1: The TRANSIENT tuple swallows every Modal RPC error, so permanent failures never fail a job
- **Severity:** High
- **Location:** Phase 2, section "Architecture" (the gateway poll mapping, the `spawn_workflow` paragraph and the dispatcher sketch)
- **Flaw:**
  - `TRANSIENT` includes `grpclib.exceptions.GRPCError`. In modal 1.5.5, every Modal RPC error class inherits from `grpclib.GRPCError` through `_GRPCErrorWrapper`: `NotFoundError`, `AuthError`, `PermissionDeniedError`, `InvalidError`, `ConflictError`, `ResourceExhaustedError`, `InternalError` and `ServiceError`.
  - So `except TRANSIENT` treats an undeployed or unknown app, a revoked or rotated token, a spend-limit refusal or a bad request as "still pending".
  - The plan's own example of a non-transient spawn failure is "`NotFoundError` when the app is not deployed … marks the job failed". That error is in fact caught by the transient branch and retried forever.
- **Failure scenario:**
  - The owner runs `modal app stop qwen21-uc`, which README.md lists as a routine command. Hydrating `Cls.from_name` then raises `NotFoundError`, which is treated as transient. Every new job stays `queued`, and the dispatcher retries every 60 s indefinitely.
  - Queued jobs have no timeout, because the 1800 s limit counts from `submitted_at`. The queue therefore never returns 286, so it polls every 2 s forever.
  - After a token rotation, every poll of a submitted job raises `AuthError`. The job shows "running" for 30 minutes and then fails with a misleading "last transient error".
  - CI stays green. `test_modal_gateway.py` only exercises `OutputExpiredError` and `FunctionTimeoutError`, and the fake raises whatever the test picks.
- **Evidence:**
  - Plan: `phase-02-engine.md:179-180` (`TRANSIENT = (modal.exception.ConnectionError, grpclib.exceptions.GRPCError, …)`), `:192-193` (`except TRANSIENT` returns pending), `:199` (the `NotFoundError` example), `:216-217` (`except TRANSIENT_SPAWN_ERRORS: self.backoff(backend.id); break`), `:301` (test scope).
  - SDK: `exception.py:66` (`class _GRPCErrorWrapper(grpclib.GRPCError)`); `exception.py:141,149,153,161,165,169,173` (`AuthError`, `InvalidError`, `ConflictError`, `NotFoundError`, `PermissionDeniedError`, `ResourceExhaustedError` and `ServiceError` all subclass it).
  - SDK: `_grpc_client.py:27-43` maps every gRPC status (including NOT_FOUND, UNAUTHENTICATED, RESOURCE_EXHAUSTED and FAILED_PRECONDITION) onto those classes, and `_grpc_client.py:47-63` converts every `GRPCError`. `cls.py:705-711` re-raises a lookup failure as `NotFoundError`.
  - Repo: `README.md:12` (`modal app stop qwen21-uc  # take it offline`).
- **Suggested fix:**
  - Define transient errors by allowlist: `modal.exception.ConnectionError`, `modal.exception.ServiceError` (UNAVAILABLE, DEADLINE_EXCEEDED, CANCELLED and UNKNOWN) and `grpclib.exceptions.StreamTerminatedError`.
  - Put an explicit `except (NotFoundError, AuthError, PermissionDeniedError, InvalidError, ConflictError, ResourceExhaustedError)` that returns failed **before** the transient branch, in both `poll()` and spawn.
  - Show the last spawn error on queued rows, and add a maximum age for queued jobs.
  - Test with real `NotFoundError("x")` and `AuthError("x")` instances.

## Finding 2: Cancelling one running job shuts down the whole backend container and reschedules its siblings
- **Severity:** High
- **Location:** Phase 2, "Implementation Steps" step 6 (`cancel()`). Phase 3, "Implementation Steps" step 5 (queue cancel). Phase 6, "Key Insights" ("Stop deliberately differs from research-02 §1") and "Warm pinger and stop (sketch)".
- **Flaw:**
  - The plan says that the default `terminate_containers=False` means sibling inputs are never rescheduled. That is false for this backend.
  - `Qwen21UC`'s methods are synchronous and the class uses `@modal.concurrent(max_inputs=4)`. Modal's container runtime handles a cancel of a synchronous input under input concurrency by sending SIGINT to the whole container process, and logs: "This shuts down the container, causing concurrently running inputs to be rescheduled in other containers."
  - `terminate_containers` does not change this path. The contract's premise that cancel-first avoids a reschedule inherits the same error.
- **Failure scenario:**
  - (a) The owner starts a batch of 4, so all 4 run concurrently (`max_inflight=4`), and cancels one. The container gets SIGINT, and the other 3 are rescheduled.
    - With `max_containers=1`, that means a fresh cold start of about 68 s. All renders restart from zero at an extra ~$0.04–0.07 per cancel, and an active warm window loses its container.
    - The poller's timeout "best-effort cancel" does the same.
    - Acceptance row 4 ("cancel … one running job") triggers this.
  - (b) Stop: cancelling the running jobs makes the container SIGINT itself. The cancel is delivered through the container heartbeat, so the SIGINT can land before or during `stop_containers()`.
    - If `container stop <id>` then runs against a finished container, the CLI exits 1 with "Container … is already stopped." That becomes `ModalCliError`, and `stop_backend` raises after the DB cancel has committed.
    - The Stop POST then returns 500, and `force_refresh` is skipped.
    - The ping is cancelled last, in a sequential loop. If the container dies first, the uncancelled ping is rescheduled, which is the very cold start the design tries to avoid.
- **Evidence:**
  - Plan: `phase-02-engine.md:299`, `:104`; `phase-03-web-ui-and-access-auth.md:215`; `phase-06-gpu-status-warm-stop.md:43-49`, `:129-134`. Contract: `plans/reports/brainstorm-260925-1507-atelier-image-studio-hetzner.md:44`.
  - Backend: `qwen21_uc_app.py:99-100` (`@modal.concurrent(max_inputs=4)`, `max_containers=1`) and `:140-150` (synchronous `def generate` and `def run_workflow`).
  - SDK: `_container_entrypoint.py:193` (the concurrency branch), `:204-217` (`cancel_callback_sync` calls `os.kill(os.getpid(), signal.SIGINT)`, with the warning text at `:214`) and `:236-237` (synchronous inputs get that callback).
  - SDK: `_runtime/container_io_manager.py:1103-1104` (`input_concurrency_enabled` means concurrency > 1), `:702-709` (a cancel is delivered through the heartbeat) and `cli/container.py:333` (`SystemExit("Container '…' is already stopped.")`).
- **Suggested fix:**
  - For a user cancel of a running job while other inputs share the container, mark the job cancelled in the DB and do **not** call Modal's cancel. The conditional `complete()` already discards the late result, so the cost is at most one ~16 s render. Call Modal's cancel only when the job is still in Modal's backlog or is the only running input.
  - For Stop, cancel the ping and every call concurrently (`asyncio.gather`), ping first.
  - Treat "already stopped" as success, and always set `force_refresh`.
  - Correct the rationale in phases 2 and 6, and flag the contract premise to the owner.

## Finding 3: The per-process `Cls` cache is hydrated once and never refreshed, so `modal app stop` plus a redeploy leaves Atelier broken until a restart
- **Severity:** High
- **Location:** Phase 2, "Architecture" (the `spawn_workflow` per-process cache). Phase 6, "Implementation Steps" step 1 (`stats` through the cached `Cls`) and step 7.6.
- **Flaw:**
  - `ModalSdkGateway` keeps one `modal.Cls.from_name(app, cls)()` per backend for the whole process lifetime. Modal hydrates it once, by app name, and never re-resolves a hydrated object; the only exception is a memory-snapshot restore.
  - `modal app stop` stops an app permanently. A later deploy under the same name creates a new app, and the CLI tracks the old one as `previous_app_id`.
  - The cached handle keeps the stopped app's class and service-function IDs.
- **Failure scenario:**
  - Phase 6 step 7.6 runs exactly this sequence: `modal app stop`, then a `deploy-modal.yml` redeploy. README.md presents the stop as routine.
  - Afterwards, `spawn_workflow`, `spawn_ping` and `stats` all target the dead IDs. Under Finding 1 the resulting errors count as "transient", so jobs stay `queued` and warm-up pings never land.
  - Meanwhile `app_state`, a fresh CLI call every 60 s, reports `deployed`. The badge then shows one of two things:
    - `unknown`, indefinitely. Step 7.6 fails with no pre-decided response.
    - "scaled to zero", if the stats RPC still answers for the old function. Step 7.6 passes while generation is broken.
  - Only a container restart heals it. In the plan's timeline the phase 7 deploy restarts the container and masks the defect, so it first appears in production.
- **Evidence:**
  - Plan: `phase-02-engine.md:199`; `phase-06-gpu-status-warm-stop.md:179`, `:199-201`.
  - Repo: `README.md:12`.
  - SDK: `cls.py:695-716` (a `ClassGet` by `app_name`, then `_hydrate(response.class_id, …)` once); `cls.py:85-90` (bound methods hydrate from `service_function.object_id`); `_object.py:352-383` (`hydrate()` does nothing once `_is_hydrated`, except after a snapshot restore).
  - SDK: `cli/app.py:53-96` (a stopped app stays `previous_app_id` "if no other App with that name has been deployed since"); `cli/app.py:559` ("Permanently stop an App and terminate its running containers").
- **Suggested fix:**
  - Tie the cache to the deployment. Record the `app_id` that `app_state()` returns, and drop the cached `Cls` when that ID changes or after any non-transient spawn or stats error. Recreate it on next use.
  - Add a fake-gateway test for this: after an app ID change, the next spawn uses a fresh handle.
  - Also, step 7.6 needs `--yes` if an agent shell (non-TTY) runs it (`cli/app.py:573-589`, `cli/utils.py:194-198`).

## Finding 4: The weekly verify never sees a PNG, because `restic ls latest /data/images` is not recursive
- **Severity:** Critical
- **Location:** Phase 5, "Architecture" (the verify pipeline in the diagram and `atelier-backup-verify.sh` step 4) and "Risk Assessment"
- **Flaw:**
  - Images are stored at `images/YYYY/MM/job-<id>.png`.
  - restic's `ls` directory filter lists only entries directly inside the named directory unless `--recursive` is given.
  - The pipeline passes `/data/images` without `--recursive`. The stream therefore holds only the year directory node and zero `.png` paths.
- **Failure scenario:**
  - At the first live verify (phase 5 step 13, after phase 4 has already produced live images), `verify` reports every referenced PNG as missing and exits 3.
  - The badge then turns red every Sunday, so criterion 11 cannot pass as written.
  - The unit tests stay green because they use hand-written, deep-path `restic ls --json` fixtures, so the test is a phantom.
  - The pre-decided response ("adjust the parser to the real format") points the executor at the wrong cause.
- **Evidence:**
  - Plan: `phase-05-backups-and-restore.md:89` and `:138` (`restic … ls --json latest /data/images`, no `--recursive`), `:57` (`images/2026/09/job-12.png`), `:202` (hand-written fixtures), `:289` (the risk is attributed only to the node format); `phase-02-engine.md:106` (`images/YYYY/MM/`).
  - restic `cmd/restic/cmd_ls.go` (fetched): "only files inside those directories will be listed. If the --recursive flag is used, then the filter will allow traversing into matching directories' subfolders". The `--recursive` flag defaults to `false`.
- **Suggested fix:**
  - Use `ls --json --recursive latest /data/images`, and keep `message_type=="node"`, `type=="file"` and the `.png` suffix.
  - Capture the fixture from a real run of the pinned restic image against a throwaway local repository (no R2 needed).
  - Fail with "listing contains no PNG nodes" rather than "N missing".

## Finding 5: The per-backend Stop lock is checked before awaits and not held, so a ping or job spawned during Stop cold-starts a new container
- **Severity:** Medium
- **Location:** Phase 6, "Key Insights" ("One lock per backend") and "Architecture → Warm pinger and stop (sketch)". Phase 2, "Architecture" (the dispatcher sketch).
- **Flaw:**
  - The dispatcher and the pinger test `locks[b].locked()` once at the top of a tick. They then await network calls (`gateway.poll`, `spawn_ping`, and `spawn_workflow` for each job in a batch read earlier) and never hold the lock.
  - Stop can take the lock between the check and the spawn.
  - The plan's claim that "a single process and a single event loop make an in-memory lock sufficient" does not hold for a check that spans awaits.
- **Failure scenario:**
  - During a warm window, `warm_tick` passes the check, reads `warm_until` and awaits `spawn_ping`. The owner presses Stop, which clears the window, finds `ping_calls` empty, cancels the jobs and stops the containers.
  - `spawn_ping` then returns, and the tick stores a new ping. Modal boots a fresh container (about 68 s plus 60 s idle, or ~$0.07), and the check "no container reappears within 90 s" fails.
  - The dispatcher does the same with its in-memory batch: it spawns jobs that the DB already shows as cancelled, and cancels them only after `stop_containers` has run. Through Finding 2, that late cancel can also SIGINT the freshly booted container.
  - `self.ping_calls.pop(backend.id)` with no default raises `KeyError` when Stop popped the entry first.
- **Evidence:**
  - Plan: `phase-06-gpu-status-warm-stop.md:51`, `:104-105` (`if self.locks[backend.id].locked(): continue`), `:106-119` (the awaits after the check), `:110` (`pop` without a default), `:122-133` (Stop holds the lock), `:182`.
  - Plan: `phase-02-engine.md:204-223` (per-job awaited spawns over a `batch` read before the loop).
- **Suggested fix:**
  - Hold the backend lock from the spawn through the `mark_submitted` or `ping_calls` store. Alternatively, keep a "stop epoch" per backend, re-check it after each await, and cancel anything spawned under an older epoch.
  - Use `pop(id, None)`.
  - Add a test where Stop runs inside a slow fake `spawn_ping` or `spawn_workflow`.

## Finding 6: The backup unit's `Nice=10` and I/O priority never reach restic, which runs unthrottled next to Folio
- **Severity:** Medium
- **Location:** Phase 5, "Requirements → Non-functional" and "Architecture" (`restic_run` and the service unit)
- **Flaw:**
  - The unit's `Nice=` and `IOScheduling*` settings apply only to the bash script and the `docker` CLI client.
  - `docker run` has dockerd/containerd start restic under containerd-shim in the container's own cgroup, so restic inherits neither setting.
  - `restic_run` passes none of `NICE`, `IONICE_CLASS` or `IONICE_PRIORITY`, which the research says the official image honours. It also sets no `--cpus`, `--memory` or blkio weight.
- **Failure scenario:**
  - The first full backup, and any night with many new images, runs restic at default priority with `GOMAXPROCS` equal to the host's 4 vCPUs. It hashes and compresses on every core and reads the volume heavily while Folio's API, Postgres and MinIO share the same 4 vCPUs.
  - The Sunday `check --read-data-subset=5%` behaves the same way.
  - The risk-table response ("Nice and I/O priority are already lowered") changes nothing, so the plan has no real mitigation.
- **Evidence:**
  - Plan: `phase-05-backups-and-restore.md:75`, `:110-113` (`restic_run` with no priority or limit flags), `:155`, `:290`.
  - Research: `research/researcher-01-cloudflare-access-tunnel-restic-r2.md:94` ("The image forwards `NICE`, `IONICE_CLASS`, `IONICE_PRIORITY` env vars to `nice`/`ionice`").
  - Scout: `reports/scout-02-folio-prod-1-live-state.md:7` (4 vCPU and 7.7 GB, shared with Folio).
- **Suggested fix:**
  - Add `-e NICE=10 -e IONICE_CLASS=2 -e IONICE_PRIORITY=7 --cpus 1 --memory 1g` to `restic_run`, and optionally `-e GOMAXPROCS=1`.
  - During the first backup, verify the priority with `ps -o ni,comm` and `ionice -p` on the restic PID.

## Finding 7: The rollback drill pipes `gh auth token`, which has no package scope, so it stops at `docker pull` before the rollback branch
- **Severity:** Medium
- **Location:** Phase 4, "Implementation Steps" step 18 (the rollback drill)
- **Flaw:**
  - The drill passes the owner's `gh` OAuth token to `deploy.sh`, which logs in to GHCR and pulls a private package.
  - The active token's scopes are `gist, read:org, repo, workflow`, with no `read:packages`.
  - `deploy.sh` runs with `set -euo pipefail`, so a denied login or pull exits before the up, health-check and rollback block.
- **Failure scenario:**
  - The drill exits 1, and `current-tag` and `/healthz` stay on the good SHA, so two of its three expected signals match by coincidence. No "rolled back" line is logged, because the rollback path never ran.
  - An operator who checks only the exit code and the tag records a false pass for the rollback evidence in criterion 13.
  - The drill also sends a long-lived, account-wide `repo`-scoped token into a root script on Folio's production host. That contradicts the phase's "short-lived registry token" posture.
- **Evidence:**
  - Plan: `phase-04-container-ci-cd-and-rollout.md:388-392`, `:172` (`set -euo pipefail`), `:185-190` (login, then pull), `:198-206` (rollback is reachable only after a successful pull), `:481`.
  - `gh auth status` (read-only, 2026-09-25): active account `trungbui2307`, token scopes `'gist', 'read:org', 'repo', 'workflow'`.
- **Suggested fix:**
  - Run the drill through `gh workflow run deploy.yml -f sha=<FAKE>`, which exercises the real `GITHUB_TOKEN` path. Alternatively, use a one-day token with only `read:packages`, and revoke it after the drill.
  - Make the pass condition require the `rolled back to <sha>` journal line.

## Finding 8: The same-seed determinism proof can be satisfied by ComfyUI's output cache instead of a second render
- **Severity:** Medium
- **Location:** Phase 2, "Implementation Steps" step 12 (the live test) and "Success Criteria" (criterion 3). Phase 8, "Acceptance record" row 3.
- **Flaw:**
  - The live test spawns seed 424242 twice and seed 424243 once, all at once. With one container and `max_inputs=4`, all three prompts go into the same ComfyUI queue in roughly arrival order.
  - When two byte-identical graphs run back to back, ComfyUI serves every node from its output cache and reports the cached SaveImage output, which is the same file, in history.
  - `_run` downloads that same file, so the pixel difference is 0 by construction and GPU (non)determinism is never measured.
  - Acceptance row 3 ("Remix … same seed and settings") has the same gap when it runs straight after the original on a warm container.
- **Failure scenario:** Criterion 3 is recorded as a pass, while a real re-render (a later remix on a fresh container, or after other prompts have evicted the cache) may differ. The GPU-nondeterminism risk row (`phase-02:362`) can never fire.
- **Evidence:**
  - Plan: `phase-02-engine.md:309-313`, `:335`, `:362`; `phase-08-api-plugin-and-acceptance.md:230`.
  - Backend: `qwen21_uc_app.py:95` (fixed `filename_prefix`), `:99-100` (one container, 4 concurrent inputs), `:122-137` (`_run` returns the first image listed in `history[pid]["outputs"]`).
  - ComfyUI `execution.py` (master, fetched): cached nodes are found through `self.caches.outputs.get(...)`, and for cached output nodes `emit_cached_output(...)` fills `ui_outputs`, which becomes `history_result["outputs"]`. This was not verified against the pinned v0.37.2 tag.
- **Suggested fix:**
  - Run the renders one at a time, each awaited, in the order 424242 → 424243 → 424242, so the second same-seed render must re-run the KSampler.
  - Assert that the second render took real time (more than 5 s) rather than returning at once.
  - For acceptance row 3, remix only after a different prompt has run, or after the backend has scaled to zero.

## Probes cleared (no finding)
- **Modal CLI in the read-only, non-root container works.**
  - `config.py:162-181` reads `~/.modal.toml` only if the file exists, and tokens come from the environment (`config.py:390-391`). The CLI writes no files.
  - A local measurement of `import modal.cli.entry_point` gave about 0.2 s and 67 MB, well under the plan's 1 s and 150 MB.
- **`.aio` is safe across callers' event loops.** Synchronicity runs the coroutine on its own loop thread (`synchronicity/synchronizer.py:499-514`, `run_coroutine_threadsafe` plus `wrap_future`). research-02 §1's claim that `.aio` uses the caller's loop is wrong but harmless.
- **`get(timeout=0)` is a single, non-destructive poll** (`_functions.py:225-264`; `poll_function` passes `clear_on_success=False` at `:325-329`). A crash between the poll and the DB commit re-reads the result after a restart.
- **`modal/` without `__init__.py` does not shadow the installed `modal` package**, because a regular package beats a namespace portion. The image contains no `modal/` directory.
- **Timers use explicit `UTC`.** `ATELIER_TIMEZONE` is used for display only, and `tzdata` is a dependency.
- **The single uvicorn worker is enforced by `CMD`.** The `docker exec` helpers are separate, short-lived processes.
- **The tunnel config pins `origincert`, so `route dns` should work from the box.** In Folio's repo copy the tunnel is referenced by name with `origincert` pinned (`folio/infra/cloudflare/cloudflared-config.yml:9-14`), which is what `route dns` needs. Scout-02 read only the live file's hostname and service lines, so check `origincert` in the live file before step 16.

## Lifetime audit
**Result: FAILED.** Two process-global states outlive the boundary they depend on (items 4 and 5), and a host resource shared with Folio is left unthrottled (item 13).

1. **`Settings`:** created in `create_app` (`phase-03:198`). Process-global and immutable. OK.
2. **`AccessVerifier` and the `PyJWKClient` JWK-set cache:** stored on `app.state.verifier` (`phase-03:95-99`). Process-global, and used from `asyncio.to_thread` worker threads. The cache is unlocked, but the worst case is a harmless duplicate fetch. OK.
3. **Modal `Client.from_env()`:** an SDK singleton on synchronicity's loop thread. Process-global and safe from any caller loop, including TestClient's. OK.
4. **`ModalSdkGateway` `Cls` cache:** `phase-02:199`. Process-global and bound to one Modal deployment, with no invalidation. **FAILED** (Finding 3).
5. **`Worker.locks` (`defaultdict(asyncio.Lock)`):** `phase-06:182`. Process-global, but checked and not held across awaits. **FAILED** (Finding 5).
6. **`Worker.ping_calls`, `last_ping` and `warm_since`:** `phase-06:151`. Live only as long as the process. **RISK:**
   - A ping still in flight at a restart or deploy is orphaned. Stop cannot cancel it, and `container stop` then reschedules it.
   - `warm_since` resets, so the running-cost estimate under-reports after a restart.
7. **`Worker.dispatch_paused`:** `phase-02:211`. Live only as long as the process, and one field is shared by all backends. The sketch sets it but never clears it, so the header can keep showing "paused" after space is freed. **RISK:** reset it on every tick, per backend.
8. **`Worker.backoff` and `last_transient`:** `phase-02:305`. Live only as long as the process. Losing the transient text on a restart is accepted by the plan. OK.
9. **Persisted state on the volume:** `backend_state.warm_until`, SQLite in WAL mode on the ext4 volume (the app and a `docker exec` process in the same container share memory on one host), `/data/backup-status.json` (a single writer module, uid 10001) and `/data/images`. OK.
10. **Atelier-only host state:** `/opt/atelier/{current,previous}-tag` and `.deploy.lock` (root, 0600 through `umask 077`), `/run/lock/atelier-backup.lock` and the `atelier-backup*` units. OK.
11. **`/etc/cloudflared/config.yml` (shared with Folio):** replaced by replica cutover, with a timestamped backup. OK, as long as a restore from that backup happens in the same session only; restoring it later would revert any Folio edit made since.
12. **Other host files shared with Folio:**
    - Root's `authorized_keys`: the full-removal step's wording, "remove the `authorized_keys` line from its backup" (`phase-04:473`), is ambiguous. Restoring the old file would drop keys added since, including Folio or LearnFlow deploy keys, so delete the single line instead. **RISK.**
    - `/etc/fstab`: mounted with `nofail`, and backed up. OK.
13. **Host CPU and I/O (shared with Folio):** the restic container runs outside the unit's nice and ionice settings. **FAILED** (Finding 6).
14. **Plugin and app in the phase 8 integration test** (`phase-08:189-195`). **RISK:**
    - The worker loops run on the uvicorn thread's loop, while a "helper task" in the test's loop completes calls on the same `FakeModalGateway`. The fake must therefore be thread-safe and must not hold asyncio primitives, such as an `Event`, `Queue` or `Lock` bound to one loop.
    - Set `ATELIER_ENV` and `ATELIER_DEV_IDENTITY` through `monkeypatch`, not `os.environ`, so the dev identity cannot leak into other tests.
