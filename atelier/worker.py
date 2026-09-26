"""The background engine: one dispatcher loop and one poller loop, run from a single Worker per process.

Worker's in-memory state (locks, pause/alert text, backoff, tick times) lives only as long as the
process. That is correct with a single uvicorn worker: a restart just means dispatch_once() re-derives
`paused` from scratch and poll_once() resumes every 'submitted' job by querying the database, with no
state to recover.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable

import modal.exception

from atelier import jobs, storage
from atelier.config import Settings
from atelier.db import session
from atelier.modal_gateway import PERMANENT, TRANSIENT, ModalGateway
from atelier.registry import Backend, Registry
from atelier.storage import UnstorableResult

log = logging.getLogger(__name__)

QUEUED_MAX_AGE_S = 1800
MAX_RESULT_BYTES = 64 * 1024 * 1024
_DISPATCH_INTERVAL_S = 1.0
_POLL_INTERVAL_S = 2.0
_BACKOFF_INITIAL_S = 2.0
_BACKOFF_MAX_S = 60.0
_STORE_FAILURE_LIMIT = 3
_SPAWN_TRANSIENT = (*TRANSIENT, modal.exception.ResourceExhaustedError)


class Worker:
    """Owns two loops (dispatch_once, poll_once) plus the per-backend state they share."""

    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        gateway: ModalGateway,
        clock: Callable[[], float] = time.time,
        dispatch_interval_s: float = _DISPATCH_INTERVAL_S,
        poll_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.gateway = gateway
        self.clock = clock
        self._dispatch_interval_s = dispatch_interval_s
        self._poll_interval_s = poll_interval_s

        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.paused: dict[str, str | None] = {}
        self.alerts: dict[str, str] = {}
        self.started_at: float | None = None
        self.last_dispatch_tick: float | None = None
        self.last_poll_tick: float | None = None

        self._retry_at: dict[str, float] = {}
        self._backoff_s: dict[str, float] = {}
        self._backoff_reason: dict[str, str] = {}
        self._last_transient: dict[int, str] = {}
        self._store_failures: dict[int, int] = {}
        self._tasks: list[asyncio.Task] = []

    # -- dispatcher --------------------------------------------------------------------------------

    async def dispatch_once(self) -> None:
        for backend in self.registry.backends.values():
            async with self.locks[backend.id]:  # held across check -> spawn -> record
                await self._dispatch_backend(backend)
        self.last_dispatch_tick = self.clock()  # only set once every backend's tick has completed

    async def _dispatch_backend(self, backend: Backend) -> None:
        now = self.clock()
        self.paused[backend.id] = None  # recomputed every tick
        with session(self.settings) as conn:
            jobs.fail_stale_queued(conn, backend.id, older_than=now - QUEUED_MAX_AGE_S)

            if now < self._retry_at.get(backend.id, 0.0):
                # Still inside a spawn backoff window: keep showing why, until the next spawn attempt.
                self.paused[backend.id] = self._backoff_reason.get(backend.id)
                return

            free = backend.max_inflight - jobs.count_submitted(conn, backend.id)
            if free <= 0:
                if jobs.has_queued(conn, backend.id):
                    reason = f"all {backend.max_inflight} slots busy"
                    self.paused[backend.id] = reason
                    jobs.note_waiting(conn, backend.id, reason)
                return
            if not jobs.has_queued(conn, backend.id):
                return

            status = storage.disk_status(conn, self.settings)
            if status.refusal:
                self.paused[backend.id] = status.refusal
                jobs.note_waiting(conn, backend.id, status.refusal)
                return
            batch = jobs.next_queued(conn, backend.id, free)

        for job in batch:
            try:
                call_id = await self.gateway.spawn_workflow(backend, json.loads(job["graph_json"]))
            except _SPAWN_TRANSIENT as exc:
                reason = f"Modal unavailable, retrying: {exc}"[:300]
                self._back_off(backend.id, reason)
                self.paused[backend.id] = reason
                with session(self.settings) as conn:
                    jobs.note_waiting(conn, backend.id, reason)
                return
            except Exception as exc:  # noqa: BLE001 -- permanent Modal errors, and anything else spawn_workflow raised
                if isinstance(exc, PERMANENT):
                    self.alerts[backend.id] = f"Modal refused the backend: {exc}"[:300]
                with session(self.settings) as conn:
                    jobs.fail_queued(conn, job["id"], f"Could not start: {exc}")
                continue
            self.alerts.pop(backend.id, None)
            self._backoff_s.pop(backend.id, None)
            self._backoff_reason.pop(backend.id, None)
            with session(self.settings) as conn:
                # A job cancelled meanwhile stays cancelled; its render is never read, and no Modal
                # cancel is sent (mark_submitted's WHERE status='queued' makes this a no-op for it).
                jobs.mark_submitted(conn, job["id"], call_id, self.clock())

    def _back_off(self, backend_id: str, reason: str) -> None:
        current = self._backoff_s.get(backend_id, _BACKOFF_INITIAL_S)
        self._retry_at[backend_id] = self.clock() + current
        self._backoff_s[backend_id] = min(current * 2, _BACKOFF_MAX_S)
        self._backoff_reason[backend_id] = reason

    # -- poller --------------------------------------------------------------------------------------

    async def poll_once(self) -> None:
        with session(self.settings) as conn:
            submitted = jobs.list_submitted(conn)
        for job in submitted:
            try:
                value = await self._poll_job(job)
            except Exception:
                # An infrastructure error here (the poll call itself, or writing a timeout/failure) is
                # not a storage failure: log it and move on, without touching the store-failure count
                # or overwriting whatever error text the job may already carry.
                log.exception("polling job %s failed", job["id"])
                continue

            if value is None:
                continue  # pending, timed out, or already failed/completed inside _poll_job

            try:
                await self._store_result(job, value)
            except UnstorableResult as exc:
                await self._fail_and_clean(job, f"Could not store the result: {exc}")
            except Exception as exc:
                log.exception("storing the result of job %s failed", job["id"])
                count = self._store_failures.get(job["id"], 0) + 1
                self._store_failures[job["id"]] = count
                if count >= _STORE_FAILURE_LIMIT:
                    await self._fail_and_clean(job, f"Could not store the result: {exc}")
        self.last_poll_tick = self.clock()  # only set once every submitted job's tick has completed

    async def _poll_job(self, job) -> bytes | None:
        """Polls one job. Returns the finished result's bytes when there is one to store, else None:
        pending, timed out, or already failed and recorded by this call."""
        result = await self.gateway.poll(job["call_id"])
        if result.state == "pending":
            if result.transient:
                self._last_transient[job["id"]] = result.transient
            # The timeout applies only while the call is still pending. Checking it here, not before
            # the poll, means a result that finished during a long outage is still read and stored
            # instead of being thrown away just because nobody noticed it in time. No Modal cancel
            # either way: it would SIGINT the container and reschedule every sibling render.
            if self.clock() - job["submitted_at"] > self.settings.job_timeout_s:
                with session(self.settings) as conn:
                    jobs.fail_submitted(conn, job["id"], self._timeout_text(job), self.clock())
                self._forget(job["id"])
            return None
        if result.state == "failed":
            with session(self.settings) as conn:
                jobs.fail_submitted(conn, job["id"], result.error, self.clock())
            self._forget(job["id"])
            return None
        return result.value

    async def _store_result(self, job, value: bytes) -> None:
        if len(value) > MAX_RESULT_BYTES:
            raise UnstorableResult(f"result is {len(value) // 2**20} MB, over the 64 MB limit")

        # Pillow decode/encode, hashing and the atomic file writes are blocking I/O: run them off the
        # event loop. Only the (fast) SQLite calls stay on the loop.
        saved = await asyncio.to_thread(storage.save_result, self.settings.data_dir, job["id"], value)
        try:
            with session(self.settings) as conn:
                completed = jobs.complete(conn, job, saved, self.clock(), self.registry)
            if not completed:  # cancelled while rendering: discard the late result
                await asyncio.to_thread(storage.delete_files, self.settings.data_dir, saved)
        except Exception:
            # Anything after the save fails: the files are already on disk but unrecorded, so remove
            # them rather than leaving them as orphans the disk guard can't see. The job's failure
            # count is kept, so a failure that never goes away still ends the job after a few ticks.
            await asyncio.to_thread(storage.delete_files, self.settings.data_dir, saved)
            raise
        self._forget(job["id"])

    def _timeout_text(self, job) -> str:
        reason = self._last_transient.get(job["id"])
        base = f"timed out after {self.settings.job_timeout_s:.0f}s"
        return f"{base}: {reason}" if reason else base

    async def _fail_and_clean(self, job, text: str) -> None:
        """Marks a job failed and removes its partial files. Never raises itself: a cleanup problem for
        one job must not abort the tick or starve the other jobs in it."""
        try:
            await asyncio.to_thread(storage.remove_partial_files, self.settings.data_dir, job["id"])
        except Exception:
            log.exception("removing partial files for job %s failed", job["id"])
        try:
            with session(self.settings) as conn:
                jobs.fail_submitted(conn, job["id"], text, self.clock())
        except Exception:
            log.exception("marking job %s failed did not complete", job["id"])
        self._forget(job["id"])

    def _forget(self, job_id: int) -> None:
        self._last_transient.pop(job_id, None)
        self._store_failures.pop(job_id, None)

    # -- lifecycle -----------------------------------------------------------------------------------

    async def run(self) -> None:
        """Starts both loops as background tasks. A loop exception is logged, never fatal; per-job
        errors are already isolated inside poll_once/dispatch_once and never reach this level.

        Records started_at before creating the tasks, so a caller (e.g. /healthz) can tell "never
        started" (started_at is None) apart from "started but hasn't completed a tick yet"."""
        self.started_at = self.clock()

        async def loop(fn: Callable, interval: float) -> None:
            while True:
                try:
                    await fn()
                except Exception:
                    log.exception("%s failed", getattr(fn, "__name__", fn))
                await asyncio.sleep(interval)

        self._tasks = [
            asyncio.create_task(loop(self.dispatch_once, self._dispatch_interval_s)),
            asyncio.create_task(loop(self.poll_once, self._poll_interval_s)),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
