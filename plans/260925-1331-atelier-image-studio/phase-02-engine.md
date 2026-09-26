---
phase: 2
title: "Engine"
status: completed
priority: P1
effort: "12h"
dependencies: [1]
---

# Phase 2: Engine

## Context Links

<!-- Updated: Red Team 2026-09-25 - F1 poll pending signal -->
<!-- Updated: Red Team 2026-09-25 - F4 error handling -->
<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->

- Contract: jobs, determinism, errors, models, batch and disk-guard constraints; criteria 2, 3, 4, 5, 7 and 12. See the [brainstorm report](../reports/brainstorm-260925-1507-atelier-image-studio-hetzner.md).
- Brief §3 (data model) and §4 (generate, dispatcher, poller, cancel, retry, disk guard): [architecture brief](./reports/architecture-brief.md)
- Modal job semantics: [research-02 §1](./research/researcher-02-modal-sdk-app-plugin.md). **Research-02 §1 is wrong on one point:** it names `modal.exception.TimeoutError` as the "still pending" signal. The SDK raises Python's builtin `TimeoutError` instead (see below).
- Red-team evidence: [failure modes](./reports/red-team-failure-mode-analyst.md) (Findings 1, 2, 3, 10), [assumptions](./reports/red-team-assumption-destroyer.md) (Findings 1, 2, 3, 8).
- Backend code: `modal/qwen21_uc_app.py`
  - `build_workflow` at :85-96.
  - `generate()` hides its seed at :143-145.
  - `_run` raises `RuntimeError("ComfyUI rejected workflow: …")` at :123-124 and the ComfyUI error messages at :130-131.
  - `timeout=1800`, `max_containers=1` and `max_inputs=4` at :99-100; its methods are synchronous (`def`, :140-150).
- Verified SDK facts (modal 1.5.5, installed CLI site-packages; re-checked 2026-09-25 with runtime `issubclass` checks):
  - `_functions.py:70-75` imports only `ExecutionError`, `InvalidError`, `NotFoundError` and `OutputExpiredError` from `.exception`. So `_functions.py:334` (`raise TimeoutError()`) raises the **builtin** `TimeoutError`, which is not a subclass of `modal.exception.TimeoutError`. That builtin is the pending signal.
  - `_functions.py:332` raises `OutputExpiredError` when there are no outputs and no unfinished inputs (the result is gone).
  - `exception.py:209` `FunctionTimeoutError` and `:229` `OutputExpiredError` subclass Modal's own `TimeoutError` (`:189`), not the builtin. `_utils/function_utils.py:534` raises `FunctionTimeoutError` on a container timeout.
  - `exception.py:66`: every RPC error class subclasses `_GRPCErrorWrapper(grpclib.GRPCError)`. `_grpc_client.py:27-43` maps each gRPC status to a class: `ServiceError` (`:173`) for UNAVAILABLE, DEADLINE_EXCEEDED, CANCELLED and UNKNOWN; `NotFoundError` (`:161`); `AuthError` (`:141`); `PermissionDeniedError` (`:165`); `InvalidError` (`:149`); `ConflictError` (`:153`, a subclass of `InvalidError`, for FAILED_PRECONDITION and ABORTED); `ResourceExhaustedError` (`:169`).
  - `_utils/grpc_utils.py:456-461`: after retries, transport `OSError` and `asyncio.TimeoutError` become `modal.exception.ConnectionError` (`exception.py:233`).
  - `_functions.py:2271` `FunctionCall.from_id` performs no I/O, and `.aio` on it is deprecated. `_functions.py:2130` `_invocation()` and `:225` `_Invocation.pop_function_call_outputs` are the single RPC under `get(timeout=0)`.
  - `_container_entrypoint.py:193-237`: cancelling a **synchronous** input while input concurrency is on (`max_inputs > 1`) sends SIGINT to the whole container, which "shuts down the container, causing concurrently running inputs to be rescheduled". `terminate_containers=False` does not prevent this.
  - `cli/app.py:87-93` and `runner.py:122-133`: after `modal app stop`, a redeploy creates a **new** app, while a hydrated `Cls` handle keeps the old IDs (`_object.py` hydrates only once).

## Overview

When this phase is done, Atelier has a headless, fully tested job engine:
- a model-neutral registry with the Qwen-Image 2.1 UC entry;
- a graph builder that is byte-identical to the backend's `build_workflow`;
- SQLite storage with migrations;
- a Modal gateway whose error mapping is proven against the real SDK poll path;
- a job service that handles batches, cancel and retry;
- a dispatcher and a poller that resume after a restart and isolate one bad job from the rest;
- PNG and thumbnail storage;
- a disk guard.

No HTTP routes exist yet. Priority P1. Unit tests replace Modal with a fake at the gateway boundary. Two narrow exceptions were accepted in the red-team review: the gateway's poll test stubs only the SDK's output RPC, and one opt-in live test performs real generations.

## Key Insights

<!-- Updated: Red Team 2026-09-25 - F1 poll pending signal -->
<!-- Updated: Red Team 2026-09-25 - F4 error handling -->
<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F6 locks -->

- **The pending signal is the builtin `TimeoutError`.**
  - `get(timeout=0)` on a running call raises Python's builtin `TimeoutError`. A mapping that only knows `modal.exception.TimeoutError` would mark every running job failed on its first poll, while Modal keeps rendering and billing it.
  - The classification order is: `OutputExpiredError` → failed "result expired"; `FunctionTimeoutError` → failed "backend timeout"; builtin `TimeoutError`, and `modal.exception.TimeoutError` defensively → pending.
  - The test drives the real `get` path with only the output RPC stubbed, so a hand-built exception can never hide this again.
- **Transient errors are an allowlist.**
  - Every Modal RPC error subclasses `grpclib.GRPCError`, including `NotFoundError` and `AuthError`. Treating `GRPCError` as transient would keep jobs queued forever after an undeployed app or a revoked token.
  - Only `modal.exception.ConnectionError`, `modal.exception.ServiceError` and `grpclib.exceptions.StreamTerminatedError` are transient. `ResourceExhaustedError` backs off with its reason shown.
- **Permanent errors are visible.**
  - `NotFoundError`, `AuthError`, `PermissionDeniedError`, `InvalidError` and `ConflictError` fail the job with the error text, drop the cached `Cls` handle, and raise a backend banner with the reason.
  - The cache is also dropped whenever the app state reports a different `app_id` (added in phase 6), because a redeploy after `modal app stop` creates a new app.
- **Nothing waits silently.**
  - A queued job shows its last dispatch problem ("waiting: …").
  - It fails after 30 minutes in the queue with "not dispatched: <last reason>".
  - The paused reason is kept per backend and recomputed every tick.
- **Remote ComfyUI failures come back with their original exception type** (for example the `RuntimeError` from `_run`), or as `ExecutionError` when Atelier can't import the class. Both are job failures and are stored as text.
- **User cancel never calls Modal's cancel.**
  - The backend's methods are synchronous under `@modal.concurrent`, so a Modal cancel SIGINTs the container and reschedules every sibling render onto a cold container.
  - A queued job is cancelled locally. A submitted job is marked cancelled in the DB only; the GPU may finish the render, and the conditional `complete()` discards the late result.
  - The job timeout also marks the job failed without a Modal cancel. Only Stop (phase 6) cancels calls on Modal, and it does so for all of them at once.
- **The dispatcher holds its backend's lock** across check → spawn → record. Stop (phase 6) takes the same lock, so no job can slip onto Modal between Stop's cancel and its container stop.
- **One bad result can't block the poller.**
  - Each job is polled inside its own `try`/`except`. The result size is checked before decoding.
  - A result that can never be stored fails that job with its text, and its partial files are removed. The other jobs finish in the same tick.
- **The app never calls `generate()`.** That method picks a random seed and hides it (`:143-145`). The app always chooses the seed, builds the graph from the registry template and calls `run_workflow`.
- **The graph builder duplicates the backend's model file names.** This is deliberate, because the app image never contains `modal/`. A parity test against the live `build_workflow` catches any drift.
- **Dispatch is at least once.** If the process dies between `spawn()` returning and the `submitted` update committing, the job is re-spawned after restart. That costs one duplicate render of about 16 s, which is accepted; the orphan result is never read.
- **`max_inflight = 4` matches `max_inputs=4` (`:100`).** With `max_containers=1`, the dispatcher never creates a Modal backlog on its own.
- **Search is kept in sync by triggers.** The FTS5 index is maintained by triggers in the initial migration, so image insert and delete, and tag changes, need no service code.
- **The schema is complete from the start.** The initial migration creates the whole data model from brief §3, plus `backend_state.ping_call_id` for phase 6. Later phases add no migration.

## Requirements

<!-- Updated: Red Team 2026-09-25 - F2 backup pipeline -->
<!-- Updated: Red Team 2026-09-25 - F4 error handling -->
<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F6 locks -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->
<!-- Updated: Validation Session 1 - storage defaults 50/40/5 -->
<!-- Updated: Validation Session 1 - Qwen size presets -->

Functional:
- **Settings** (`config.py`): a frozen dataclass loaded from the environment and validated.
  - `ATELIER_ENV` (`production` by default, `development` or `test`).
  - `ATELIER_DATA_DIR` (`/data`).
  - `ATELIER_PUBLIC_ORIGIN` (`https://atelier.flowitup.com`).
  - `ATELIER_CF_TEAM_DOMAIN` (`https://flowitupteam.cloudflareaccess.com`).
  - `ATELIER_CF_AUD`, `ATELIER_OWNER_EMAIL` and `ATELIER_PLUGIN_CLIENT_ID`, all required in production.
  - `ATELIER_DEV_IDENTITY` (`owner` or `service`).
  - `ATELIER_DATA_CAP_GB` (40) and `ATELIER_MIN_FREE_GB` (5): the validated defaults for the 50 GB volume.
  - `ATELIER_JOB_TIMEOUT_S` (1800), `ATELIER_TIMEZONE` (`Europe/Paris`) and `ATELIER_VERSION` (`dev`).
  - If `ATELIER_DEV_IDENTITY` is set and `ATELIER_ENV` is not `development`, raise `ConfigError` at startup.
  - In production, require the volume sentinel `<data_dir>/.atelier-volume`.
- **Database** (`db.py`):
  - `DB_FILENAME = "atelier.db"` is the one definition of the file name. The backup CLIs import it.
  - Open a short-lived connection per unit of work with `foreign_keys=ON`, `busy_timeout=5000` and `sqlite3.Row`.
  - `migrate()` sets WAL mode and applies the numbered `migrations/*.sql` files that `schema_version` doesn't list yet, each in its own transaction.
- **Registry** (`registry.py`):
  - `Backend` has `id`, `label`, `modal_app`, `modal_class`, `usd_per_hour` and `max_inflight`.
  - `Model` has `id`, `label`, `backend_id`, `build_graph`, a `ParamSchema` (steps, cfg, size bounds, multiple of 16, default preset) and `presets`.
  - A `Registry` object is injected into the app, so tests can build one with a second model.
  - The v1 entries are backend `qwen21-uc` (`Qwen21UC`, L40S, $1.95/h, `max_inflight=4`) and model `qwen-image-2.1-uc`.
    - Its presets, as validated, are 9:16 1088×1920 (the benchmarked default), 16:9 1920×1088 and 1:1 1328×1328. Custom sizes in multiples of 16 remain available.
    - Defaults are 25 steps and cfg 1.0 (`:141-142`).
    - Bounds are steps 1–60, cfg 0–10, and sizes 512–2048 in multiples of 16.
- **Graph builder:** `workflows/qwen_image_21.build_graph(GenParams)` returns exactly what `build_workflow(prompt, width, height, steps, seed, cfg, negative)` returns.
- **Gateway** (`modal_gateway.py`): a `ModalGateway` protocol with a `ModalSdkGateway` implementation.
  - `spawn_workflow(backend, graph)` returns the call ID. On a permanent error it drops the backend's cached `Cls` handle before re-raising.
  - `poll(call_id)` returns a `PollResult` that is pending (with an optional transient reason), done (with bytes) or failed (with text).
  - `cancel(call_id)` exists for Stop only (phase 6).
  - `invalidate(backend)` drops the cached handle; phase 6 calls it when the `app_id` changes.
  - Phase 6 adds status and control methods.
- **Job service** (`jobs.py`):
  - `create_batch(conn, registry, request, rng)` validates against the model schema, checks the disk guard **before** inserting, and creates one batch plus N queued jobs (N from 1 to 8).
    - Random mode draws N distinct seeds with `SystemRandom().sample(range(1, 2**31), n)`.
    - Fixed mode uses s, s+1, …, s+N−1.
    - Each job stores `params_json` and the exact `graph_json`.
  - `cancel_job(conn, id)` moves a queued or submitted job to `cancelled` in the DB only, and returns the previous status so the route can explain that a running render may still finish on the GPU. It never returns a call ID to cancel.
  - `retry_job` works only on failed or cancelled jobs. It creates a new job in the same batch with the same params, seed and graph, sets `retry_of`, and increments `attempt`.
  - `note_waiting(conn, backend_id, reason)` writes the waiting reason into `error` on that backend's queued jobs, and `mark_submitted` clears it.
  - `fail_stale_queued(conn, backend_id, older_than)` fails queued jobs older than 30 minutes with "not dispatched: <last reason>".
  - Job states are `queued → submitted → done | failed | cancelled`. The UI labels `submitted` as "running".
- **Worker** (`worker.py`):
  - `dispatch_once()` runs every 1 s and `poll_once()` every 2 s. `run()` starts both loops and `stop()` cancels them.
  - `locks` is a `defaultdict(asyncio.Lock)` keyed by backend. The dispatcher holds a backend's lock across check → spawn → record.
  - `paused` holds each backend's paused reason, recomputed every tick. `alerts` holds each backend's banner for permanent Modal errors, cleared by the next successful spawn.
  - `last_dispatch_tick` and `last_poll_tick` record the end of each completed tick, for `/healthz` (phase 3).
  - Submitted jobs are resumed after a restart simply by querying `status='submitted'`.
  - On success the poller writes the image and marks the job `done`, with `duration_s = finished − submitted` and `est_cost_usd = duration_s × usd_per_hour / 3600`.
  - On a remote error the job becomes `failed` with the error text truncated to 2,000 characters.
  - When `now − submitted_at > job_timeout_s`, the poller marks the job failed with the last transient reason, and calls no Modal cancel.
- **Storage** (`storage.py`):
  - Write `images/YYYY/MM/job-<id>.png` and a 512 px long-side WebP thumbnail next to it, atomically (temporary file, then `os.replace`).
  - Record width, height, bytes and sha256.
  - Normalize 16-bit and other non-8-bit modes to 8-bit for the thumbnail only; the PNG is stored as received.
  - Raise `UnstorableResult` for results that can never be stored, such as an unidentified image or Pillow's decompression-bomb guard. The poller fails such a job at once.
  - `remove_partial_files(data_dir, job_id)` deletes leftover temporary files for a job.
  - `disk_status()` reports used bytes (`SUM(images.bytes)`), the cap, volume free and total bytes, and a refusal reason when free space is below the floor or used bytes reach the cap.

Non-functional:
- All Modal calls from async code use `.aio`, except `FunctionCall.from_id`, which is called plainly.
- No blocking I/O runs on the event loop for longer than a SQLite statement.
- One process and one `Worker` instance. The worker's in-memory state (locks, paused reasons, alerts, backoff, tick times) lives only as long as the process, which is correct with a single uvicorn worker.
- No secrets in logs. Error text comes from exceptions, never from the environment.
- A result over 64 MB is refused before decoding.

## Architecture

<!-- Updated: Red Team 2026-09-25 - F1 poll pending signal -->
<!-- Updated: Red Team 2026-09-25 - F4 error handling -->
<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F6 locks -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->

```
create_batch()  ──disk guard──►  batches + N jobs(status=queued, params_json, graph_json)
      ▲ (routes: HTML and /api/v1)
Worker.dispatch_once (1 s): per backend, holding locks[backend]:
      fail queued jobs older than 30 min ─► submitted < max_inflight and queued exist?
      disk guard ok? ──► gateway.spawn_workflow(graph)
           transient/resource ─► keep queued, back off, "waiting: <reason>" on rows, paused[backend]
           permanent ─► fail this job, drop cached Cls, alerts[backend]
           ok ─► call_id ─► UPDATE … SET status='submitted' WHERE status='queued'
Worker.poll_once (2 s): for each submitted job, inside its own try/except:
      timeout? ──► failed (no Modal cancel)       gateway.poll(call_id):
                                                   pending ──► keep     failed(text) ──► failed
                                                   done(png) ──► size check ──► storage.save ──► BEGIN;
                                                   UPDATE job done WHERE submitted; INSERT image; COMMIT
                                                   (rowcount 0 ⇒ cancelled meanwhile: delete files, discard)
user cancel ──► UPDATE job SET status='cancelled' WHERE status IN ('queued','submitted')   (never a Modal cancel)
```

The schema is `atelier/migrations/0001_init.sql`. Every column and table from brief §3 is included, plus `backend_state.ping_call_id`, and `retry_of` set to NULL on delete so a whole job can be deleted with its image:

```sql
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
CREATE TABLE batches (id INTEGER PRIMARY KEY, created_at REAL NOT NULL,
  model_id TEXT, kind TEXT NOT NULL CHECK (kind IN ('generate','workflow')),
  base_params_json TEXT NOT NULL, count INTEGER NOT NULL CHECK (count BETWEEN 1 AND 8));
CREATE TABLE workflows (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, backend_id TEXT NOT NULL,
  graph_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE jobs (id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
  model_id TEXT, backend_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('generate','workflow')),
  params_json TEXT NOT NULL, graph_json TEXT NOT NULL,
  workflow_id INTEGER REFERENCES workflows(id) ON DELETE SET NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','submitted','done','failed','cancelled')),
  call_id TEXT, error TEXT, attempt INTEGER NOT NULL DEFAULT 1,
  retry_of INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  created_at REAL NOT NULL, submitted_at REAL, finished_at REAL, duration_s REAL, est_cost_usd REAL,
  CHECK (kind = 'workflow' OR model_id IS NOT NULL));
CREATE INDEX jobs_status_backend ON jobs(status, backend_id);
CREATE INDEX jobs_batch ON jobs(batch_id);
CREATE TABLE images (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
  model_id TEXT, file_png TEXT NOT NULL, file_thumb TEXT NOT NULL, width INTEGER NOT NULL,
  height INTEGER NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, seed INTEGER,
  prompt TEXT NOT NULL DEFAULT '', negative TEXT NOT NULL DEFAULT '',
  starred INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE INDEX images_model ON images(model_id, id);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE image_tags (image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE, PRIMARY KEY (image_id, tag_id));
CREATE TABLE presets (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, model_id TEXT NOT NULL,
  params_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE backend_state (backend_id TEXT PRIMARY KEY, warm_until REAL, ping_call_id TEXT);
CREATE VIRTUAL TABLE images_fts USING fts5(prompt, negative, tags, tokenize = 'unicode61 remove_diacritics 2');
CREATE TRIGGER images_ai AFTER INSERT ON images BEGIN
  INSERT INTO images_fts(rowid, prompt, negative, tags) VALUES (new.id, new.prompt, new.negative, ''); END;
CREATE TRIGGER images_ad AFTER DELETE ON images BEGIN DELETE FROM images_fts WHERE rowid = old.id; END;
CREATE TRIGGER image_tags_ai AFTER INSERT ON image_tags BEGIN
  UPDATE images_fts SET tags = (SELECT coalesce(group_concat(t.name, ' '), '') FROM image_tags it
    JOIN tags t ON t.id = it.tag_id WHERE it.image_id = new.image_id) WHERE rowid = new.image_id; END;
CREATE TRIGGER image_tags_ad AFTER DELETE ON image_tags BEGIN
  UPDATE images_fts SET tags = (SELECT coalesce(group_concat(t.name, ' '), '') FROM image_tags it
    JOIN tags t ON t.id = it.tag_id WHERE it.image_id = old.image_id) WHERE rowid = old.image_id; END;
```

`model_id` is NULL only for custom-workflow jobs (phase 7), because a stored workflow is bound to a backend, not to a model.

Gateway error classification, where the order of the checks is the contract:

```python
TRANSIENT = (modal.exception.ConnectionError, modal.exception.ServiceError,
             grpclib.exceptions.StreamTerminatedError)              # allowlist of retryable faults only
PERMANENT = (modal.exception.NotFoundError, modal.exception.AuthError, modal.exception.PermissionDeniedError,
             modal.exception.InvalidError, modal.exception.ConflictError)

def classify_poll_exception(exc: BaseException) -> PollResult:
    if isinstance(exc, modal.exception.OutputExpiredError):
        return PollResult.failed("Result expired on Modal (results are kept 7 days).")
    if isinstance(exc, modal.exception.FunctionTimeoutError):
        return PollResult.failed(f"Backend timed out: {exc}")
    if isinstance(exc, (TimeoutError, modal.exception.TimeoutError)):   # builtin TimeoutError means "still running"
        return PollResult.pending()
    if isinstance(exc, (*TRANSIENT, modal.exception.ResourceExhaustedError)):
        return PollResult.pending(transient=str(exc)[:300])
    if isinstance(exc, PERMANENT):
        return PollResult.failed(f"Modal refused the call: {exc}"[:2000])
    return PollResult.failed(str(exc)[-2000:] or type(exc).__name__)   # remote ComfyUI errors and the rest

async def poll(self, call_id: str) -> PollResult:
    call = modal.FunctionCall.from_id(call_id)          # no I/O; the SDK deprecates .aio here
    try:
        value = await call.get.aio(timeout=0)
    except Exception as exc:
        return classify_poll_exception(exc)
    return PollResult.done(value)
```

`spawn_workflow` uses a per-process cache of `modal.Cls.from_name(app, cls)()` keyed by backend ID, then calls `await obj.run_workflow.spawn.aio(graph)` and returns `call.object_id`. On a `PERMANENT` error it pops the cache entry and re-raises, so the next spawn re-resolves the app by name.

Dispatcher and poller (sketch):

```python
QUEUED_MAX_AGE_S = 1800
MAX_RESULT_BYTES = 64 * 1024 * 1024

async def dispatch_once(self) -> None:
    for backend in self.registry.backends.values():
        async with self.locks[backend.id]:             # held across check -> spawn -> record; Stop waits on it
            await self._dispatch_backend(backend)
    self.last_dispatch_tick = self.clock()

async def _dispatch_backend(self, backend: Backend) -> None:
    now = self.clock()
    self.paused[backend.id] = None                      # recomputed every tick
    with db.session(self.settings) as conn:
        jobs.fail_stale_queued(conn, backend.id, older_than=now - QUEUED_MAX_AGE_S)
        free = backend.max_inflight - jobs.count_submitted(conn, backend.id)
        if free <= 0 or not jobs.has_queued(conn, backend.id) or now < self.retry_at.get(backend.id, 0.0):
            return
        if (reason := storage.disk_status(conn, self.settings).refusal):
            self.paused[backend.id] = reason
            jobs.note_waiting(conn, backend.id, reason)
            return
        batch = jobs.next_queued(conn, backend.id, limit=free)
    for job in batch:
        try:
            call_id = await self.gateway.spawn_workflow(backend, json.loads(job["graph_json"]))
        except (*TRANSIENT, modal.exception.ResourceExhaustedError) as exc:
            reason = f"Modal unavailable, retrying: {exc}"[:300]
            self.back_off(backend.id); self.paused[backend.id] = reason
            with db.session(self.settings) as conn: jobs.note_waiting(conn, backend.id, reason)
            return
        except Exception as exc:                         # permanent errors also drop the cached handle
            if isinstance(exc, PERMANENT):
                self.alerts[backend.id] = f"Modal refused the backend: {exc}"[:300]
            with db.session(self.settings) as conn: jobs.fail_queued(conn, job["id"], f"Could not start: {exc}")
            continue
        self.alerts.pop(backend.id, None)
        with db.session(self.settings) as conn:
            jobs.mark_submitted(conn, job["id"], call_id, self.clock())   # a job cancelled meanwhile stays cancelled;
                                                                          # its render is never read, and no Modal cancel is sent

async def poll_once(self) -> None:
    with db.session(self.settings) as conn:
        submitted = jobs.list_submitted(conn)
    for job in submitted:
        try:
            await self._poll_job(job)
        except storage.UnstorableResult as exc:
            self._fail_and_clean(job, f"Could not store the result: {exc}")
        except Exception as exc:                          # one bad job never blocks the others
            log.exception("storing the result of job %s failed", job["id"])
            if self._store_failures.bump(job["id"]) >= 3:
                self._fail_and_clean(job, f"Could not store the result: {exc}")
    self.last_poll_tick = self.clock()

async def _poll_job(self, job) -> None:
    if self.clock() - job["submitted_at"] > self.settings.job_timeout_s:   # no Modal cancel: it would restart siblings
        with db.session(self.settings) as conn: jobs.fail_submitted(conn, job["id"], self.timeout_text(job), self.clock())
        return
    result = await self.gateway.poll(job["call_id"])
    if result.state == "pending":
        self.note_transient(job["id"], result.transient)
        return
    if result.state == "failed":
        with db.session(self.settings) as conn: jobs.fail_submitted(conn, job["id"], result.error, self.clock())
        return
    if len(result.value) > MAX_RESULT_BYTES:
        raise storage.UnstorableResult(f"result is {len(result.value) // 2**20} MB, over the 64 MB limit")
    saved = storage.save_result(self.settings.data_dir, job["id"], result.value)
    with db.session(self.settings) as conn:
        if not jobs.complete(conn, job, saved, self.clock(), self.registry):   # WHERE status='submitted'
            storage.delete_files(self.settings.data_dir, saved)                 # cancelled while rendering: discard
```

`_fail_and_clean` removes the job's partial files and marks it failed with the text.

## Related Code Files

<!-- Updated: Red Team 2026-09-25 - F1 poll pending signal -->
<!-- Updated: Red Team 2026-09-25 - F4 error handling -->

Create:
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/config.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/db.py` (with `DB_FILENAME`)
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/migrations/0001_init.sql`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/registry.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/workflows/__init__.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/workflows/qwen_image_21.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/modal_gateway.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/jobs.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/worker.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/storage.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/fakes.py` (`FakeModalGateway`)
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_config.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_db.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_registry.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_graph_parity.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_modal_gateway.py` (real SDK poll path plus classification with real exception instances)
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_jobs.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_worker.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_storage.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_live_generation.py` (marker `live`)

Modify:
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/conftest.py`: add fixtures `settings` (temporary data dir, `ATELIER_ENV=test`), `conn`, `registry`, `fake_gateway`, `png_bytes` (a real PNG made with Pillow) and `rng` (seeded `random.Random`).

Delete: none.

## Implementation Steps

<!-- Updated: Red Team 2026-09-25 - F1 poll pending signal -->
<!-- Updated: Red Team 2026-09-25 - F4 error handling -->
<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

1. **`config.py`.** Implement `Settings` and `load_settings(environ)` with the validation rules from Requirements. Tests cover:
   - production without AUD, owner or client ID fails;
   - production with a dev identity fails;
   - development with a dev identity loads;
   - production fails when the sentinel is missing.
2. **Migrations.**
   - Write `0001_init.sql` exactly as shown in Architecture.
   - Write `db.py`: `DB_FILENAME`, `connect()`, the `session()` context manager (which commits or rolls back and always closes), and `migrate()`. `migrate()` applies files in numeric order, records each in `schema_version`, and is idempotent.
3. **Registry.** Write `registry.py` with the dataclasses, `DEFAULT_REGISTRY`, `Registry.model(id)` (raises `UnknownModel`) and `Registry.backend_for(model)`.
4. **Graph builder.** Write `workflows/qwen_image_21.py` with the `UNET`, `CLIP` and `VAE` constants and `build_graph(p)`, mirroring `build_workflow` node for node.
5. **Parity test.** Write `tests/test_graph_parity.py`:

   ```python
   @pytest.mark.parametrize("p", [
       GenParams(prompt="a red fox in snow", negative="", width=1088, height=1920, steps=25, cfg=1.0, seed=1),
       GenParams(prompt="phố cổ Hội An về đêm", negative="blurry, text", width=1328, height=1328,
                 steps=8, cfg=2.5, seed=2**31 - 1),
   ])
   def test_graph_matches_backend_build_workflow(backend_script, p):
       expected = backend_script.build_workflow(p.prompt, p.width, p.height, p.steps, p.seed, p.cfg, p.negative)
       assert qwen_image_21.build_graph(p) == expected
   ```

6. **Gateway.**
   - Write `modal_gateway.py`: `PollResult`, `TRANSIENT`, `PERMANENT`, `classify_poll_exception`, the `ModalGateway` protocol and `ModalSdkGateway` (`spawn_workflow`, `poll`, `cancel`, `invalidate`).
   - Write `tests/test_modal_gateway.py` with two groups of tests.
     - **The real SDK poll path.** Stub only `modal._functions._Invocation.pop_function_call_outputs`, plus `_Client.from_env` so the handle has a client object without any network. This is the same two-stub harness the red team ran. Drive `ModalSdkGateway.poll()` through the real `FunctionCall.from_id(...).get.aio(timeout=0)`:
       - `FunctionGetOutputsResponse(outputs=[], num_unfinished_inputs=1)` → pending (`test_running_call_polls_as_pending`);
       - `outputs=[]` with `num_unfinished_inputs=0` → failed "result expired" (`test_expired_call_polls_as_failed`);
       - one output carrying a PNG serialized with the SDK's own serializer → done with those bytes (`test_finished_call_polls_as_done`).
     - **Classification with real exception instances.** `NotFoundError("x")`, `AuthError("x")`, `PermissionDeniedError("x")`, `InvalidError("x")` and `ConflictError("x")` are permanent. `ConnectionError("x")`, `ServiceError("x")`, `StreamTerminatedError()` and `ResourceExhaustedError("x")` are pending with a reason. Builtin `TimeoutError()` is pending. `OutputExpiredError()` and `FunctionTimeoutError("x")` are failed. `RuntimeError("ComfyUI rejected workflow: …")` is failed with its text.
   - `test_permanent_spawn_error_drops_the_cached_handle`: the next spawn re-resolves the app by name.
7. **Storage.** Write `storage.py`:
   - `save_result()`: Pillow `verify()` then reopen, dimensions, a WebP thumbnail with 16-bit input normalized to 8-bit, sha256, atomic writes. It raises `UnstorableResult` on decode errors that will never succeed.
   - `delete_files()`, `remove_partial_files()` and `disk_status()` (using `shutil.disk_usage(data_dir)` and `SUM(images.bytes)`).
8. **Job service.** Write `jobs.py`: `create_batch`, `cancel_job` (DB only), `retry_job`, `note_waiting`, `fail_stale_queued`, the conditional state transitions, and the queries used by the worker and the UI. Each state change is a single `UPDATE … WHERE status IN (…)` whose rowcount decides the outcome.
9. **Worker.**
   - Write `worker.py` with `Worker(settings, registry, gateway, clock=time.time)`, `dispatch_once()`, `poll_once()`, `run()` and `stop()`, plus `locks`, `paused`, `alerts`, the backoff and last-transient maps, and the two tick timestamps. These live for the process only; there is one `Worker` per process.
   - `run()` starts the two loops with `asyncio.create_task` and logs each loop exception without dying. Per-job errors never reach it.
10. **Fake gateway.** Write `tests/fakes.py` with `FakeModalGateway`.
    - It stands in for Modal at the gateway boundary: it records each spawned graph under a call ID and lets a test script the outcome per call with `finish(cid, png)`, `fail(cid, text)`, `expire(cid)` or `raise_on_spawn(exc)`. It also records cancels in an ordered `calls` log.
    - It holds no loop-bound asyncio primitives, and its state is guarded by a `threading.Lock`, so phase 8 can drive it from another thread.
11. **Engine tests.** Add the engine tests listed in the Todo list. They drive `dispatch_once()` and `poll_once()` directly and never sleep.
12. **Live test.** Add `tests/test_live_generation.py` with `pytestmark = pytest.mark.live`, skipped unless `ATELIER_LIVE_TESTS=1`. It uses `ModalSdkGateway` and a 1088×1920, 25-step graph from `build_graph`, the benchmarked settings, so a real render takes about 16 s.
    <!-- Updated: Implementation 2026-09-26 - fresh-container re-render -->
    - It renders A (seed 424242), then B (seed 424243), waiting for each result, then **waits until the backend scales to zero**, then renders A again on a fresh container.
    - **Sequential order alone is not enough.** A first owner-approved run rendered A → B → A on one warm container, and the second A came back in 2.3 s, served by ComfyUI's output cache even though B ran in between. Only a fresh container, whose ComfyUI has an empty cache, forces a real re-render.
    - It asserts that the two A renders are pixel-identical, that the second A took more than 5 s (it was really re-rendered), and that B differs from A (mean absolute pixel difference above 5.0).
13. **Lint and test.** Run `uv run ruff check` and `uv run pytest -q`. Commit as `feat: job engine with registry, Modal gateway, worker loops and storage`.
14. **[OWNER-GATED] Live test.** Ask the owner before running it; it spends about $0.20 (a cold start, two renders, a wait of about 2 minutes for scale-to-zero, then a second cold start and render). Record the result in this phase's Verification notes.

## Todo List

- [x] Settings with production and development guards (`tests/test_config.py`)
- [x] `db.py` with `DB_FILENAME` and `0001_init.sql`, with migrations applied twice as a no-op, WAL on and FTS5 present (`tests/test_db.py`)
- [x] Registry with the Qwen entry, plus a second-model registration test that needs no schema change (`tests/test_registry.py`)
- [x] Graph builder, and a parity test against `modal/qwen21_uc_app.py:build_workflow`
- [x] Gateway: the real SDK poll path (pending, expired, done) and classification of real exception instances; permanent errors drop the cached handle
- [x] Storage: atomic PNG and thumbnail writes, 16-bit thumbnail normalization, sha256, `UnstorableResult`, and disk-guard refusal at the cap and at the free floor (`tests/test_storage.py`)
- [x] Job service: N distinct seeds for random and fixed modes, validation errors, disk guard refusing before insert, DB-only cancel of queued and submitted jobs, retry keeping params and seed, waiting reasons and the 30-minute queue limit (`tests/test_jobs.py`)
- [x] Worker: at most 4 in flight per backend, lock held across spawn, done path with image metadata, failed path storing the ComfyUI text, late result discarded after cancel, transient and permanent spawn errors, timeout without a Modal cancel, dispatch paused per backend, one unstorable result next to a normal one, resume after restart (`tests/test_worker.py`)
- [x] Opt-in live test written (A → B, scale to zero, A on a fresh container), and skipped by default
- [x] [OWNER-GATED] Live test run once with the owner's go-ahead

## Success Criteria

<!-- Updated: Red Team 2026-09-25 - F1 poll pending signal -->
<!-- Updated: Red Team 2026-09-25 - F4 error handling -->
<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

- **Criterion 2** (engine side):
  - `test_running_call_polls_as_pending`, `test_expired_call_polls_as_failed` and `test_finished_call_polls_as_done` pass through the real SDK poll path.
  - `test_job_moves_from_queued_to_submitted_to_done` asserts that the image row keeps model, prompt, negative, seed, width, height, steps, cfg, `duration_s` and `est_cost_usd`.
  - `test_submitted_job_completes_after_worker_restart` builds a brand-new `Worker` on the same DB and the same fake backend state, and the job completes.
  - `test_unstorable_result_fails_only_its_own_job`: an oversized result and a 16-bit PNG sit next to a normal result. The normal job completes in the same tick, the oversized one fails with its size, and the 16-bit one completes with a normalized thumbnail.
- **Criterion 3:** the parity test is green, and the owner-approved live test shows the two same-seed renders pixel-identical, with the second rendered on a fresh container and taking more than 5 s.
- **Criterion 4** (engine side):
  - A failed poll stores the exact ComfyUI text, and `retry_job` creates a job with the same params, seed and graph.
  - `test_cancelling_a_running_job_discards_its_late_result_without_a_modal_cancel` passes; the fake's `calls` log shows no cancel.
  - `test_timed_out_job_fails_without_a_modal_cancel` passes.
  - `test_permanent_spawn_error_fails_the_job_with_its_text` and `test_transient_spawn_error_keeps_the_job_queued_with_a_visible_reason` pass.
  - `test_queued_job_fails_after_thirty_minutes_with_its_last_reason` passes.
- **Criterion 5:** `test_second_model_registers_without_schema_change` adds a model with its own graph builder, creates and completes a batch through the fake gateway, and filters images by `model_id`. `schema_version` stays at 1.
- **Criterion 7** (engine side): one `create_batch` with N=4 creates 4 jobs with 4 distinct seeds in one batch, and all reach `done`.
- **Criterion 12** (engine side): `create_batch` raises `DiskGuardError` with a clear message at the cap or at the free floor, and inserts no row. The dispatcher leaves queued jobs queued, with the reason, while the guard is tripped.
- `uv run ruff check` and `uv run pytest -q` are green.

## Verification

```bash
cd /Users/sweet-home/Works/qwen21-uc-modal
uv run ruff check
uv run pytest -q
uv run pytest -q tests/test_modal_gateway.py tests/test_graph_parity.py tests/test_worker.py -v
uv run python -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('create virtual table t using fts5(x)'); print('fts5 ok')"
# [OWNER-GATED] real generations; spends about $0.20
ATELIER_LIVE_TESTS=1 uv run pytest -m live -q -s
```

### Verification notes (2026-09-26, owner-approved live runs)
- **First run (A → B → A on one warm container): the timing assertion failed.** The second A came back in 2.3 s, pixel-identical but served by ComfyUI's output cache even though B ran in between. The test did its job: it showed that the sequential-order assumption was wrong. It was not loosened.
- **Second run (A → B, wait for scale-to-zero, then A on a fresh container): passed in 262 s.** The two same-seed renders are pixel-identical, the fresh-container render took more than 5 s, and B differs from A by a mean absolute pixel difference above 5.0. Criterion 3 is proven for a real re-render, and the backend scaled itself to zero in between.
- **Independent code review:** 15 findings (1 High, 7 Medium, 7 Low), all accepted and fixed before the engine commit. See [code-review-engine.md](./reports/code-review-engine.md).

## Risk Assessment

<!-- Updated: Red Team 2026-09-25 - F1 poll pending signal -->
<!-- Updated: Red Team 2026-09-25 - F4 error handling -->
<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->

| Risk | Likelihood × impact | Observable signal | Pre-decided response |
|---|---|---|---|
| A future SDK version changes the pending signal again | Low × High | `test_running_call_polls_as_pending` fails after a `modal` upgrade | The `<1.6` pin prevents silent upgrades. Fix the mapping before bumping the pin. |
| FTS5 missing from the SQLite build (uv Python or `python:3.12-slim`) | Low × Medium | `sqlite3.OperationalError: no such module: fts5` in `test_db.py`, or at container start in phase 4 | Move the FTS table and triggers into a later migration that is skipped when FTS5 is missing, and make phase 7 search fall back to `LIKE` on prompt, negative and tag names. Adjust within the plan. |
| A server-side hiccup maps to a class outside the transient allowlist | Low × Low | A job fails with Modal error text while Modal later shows the call succeeded | Add that class to `TRANSIENT` together with a test on a real instance. The user can retry the failed job meanwhile, per criterion 4. |
| A builtin `TimeoutError` raised inside the container reads as "pending" | Low × Low | A job stays "running" until the 1800 s timeout although the backend failed | The job timeout is the backstop. The text shown then includes the last poll reason. |
| A running job cancelled by the user still renders on the GPU | Certain × Low | The render finishes on Modal after the row shows "cancelled" | Intended: at most one ~16 s render of cost, instead of a SIGINT that would restart every sibling render on a cold container. |
| Duplicate render after a crash between spawn and update | Low × Low | Two renders for one job in the Modal logs | Accepted, since dispatch is at least once. No action. |
| GPU nondeterminism makes the two same-seed renders differ | Low × Medium | The live test reports a nonzero pixel difference | Stop and report the measured mean difference to the owner. Do not loosen the assertion; criterion 3 says "visually identical", which the owner judges. |
| Loading the backend script offline fails in CI | Low × Medium | The parity test errors on import | Switch to a checked-in fixture JSON captured from `build_workflow`, as in the phase 1 risk table. |

**Rollback:** the phase is local code only, so `git revert` its commits. Nothing is deployed, and the schema has no consumers yet.

## Security Considerations

- Job and image file names come from job IDs and dates, never from user input, so there is no path traversal.
- Pillow runs `verify()` on bytes that come from our own backend. The default decompression-bomb guard stays on, and a result over 64 MB is refused before decoding.
- Remote error text is truncated to 2,000 characters and rendered escaped in phase 3. It never includes environment values.
- Modal credentials are read only by the Modal SDK from `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`. `Settings` never loads or logs them.

## Next Steps

Phase 3 builds `create_app()`, the auth middleware and the HTML routes on these services, and starts `Worker.run()` in the lifespan. Phase 6 extends the gateway protocol and the fake with stats, app state, ping and container stop, and adds Stop on top of the locks created here.
