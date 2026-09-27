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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import modal.exception

from artio import gpu, jobs, storage
from artio.config import Settings
from artio.db import session
from artio.gpu import GpuStatus
from artio.modal_gateway import PERMANENT, TRANSIENT, AppState, FunctionStats, ModalGateway
from artio.registry import Backend, Registry
from artio.storage import UnstorableResult

log = logging.getLogger(__name__)

QUEUED_MAX_AGE_S = 1800
MAX_RESULT_BYTES = 64 * 1024 * 1024
_DISPATCH_INTERVAL_S = 1.0
_POLL_INTERVAL_S = 2.0
_BACKOFF_INITIAL_S = 2.0
_BACKOFF_MAX_S = 60.0
_STORE_FAILURE_LIMIT = 3
_SPAWN_TRANSIENT = (*TRANSIENT, modal.exception.ResourceExhaustedError)

# -- warm-up and stop (see gpu.py for the on-read status caches) ---------------------------------

PING_INTERVAL_S = 30  # leaves 30s of margin before the 60s scaledown_window
CONVERGE_S = 60  # the whole Stop sequence's budget from just after the DB cancel onward: cancel calls
# and every convergence step share this one deadline, so the total (lock + DB + this) stays well
# under Cloudflare's roughly 100s edge timeout even in the worst case.
_PINGER_STEP_S = 5
_CONVERGE_POLL_S = 5.0
_STOP_STEP_TIMEOUT_S = 30.0  # a per-step ceiling applied in addition to the deadline's own remaining
# budget (whichever is smaller), so one step can never swallow the whole rest of the sequence
_CANCEL_TIMEOUT_S = 10.0  # per cancel attempt, also capped by whatever of CONVERGE_S remains
_STOP_OUTCOME_TTL_S = 120.0
_BREAKER_THRESHOLD = 3
# A job failing on its first poll (within one poll interval of submission) with one of these in its
# text means ComfyUI itself didn't answer in an otherwise-running container, the same signal a
# failed ping carries. A boot failure ("ComfyUI did not start") can never appear this early -- a
# cold boot takes about 68s -- and recycling a container that never finished booting doesn't help,
# so that text is deliberately not a marker here.
_COMFYUI_DOWN_MARKERS = ("Connection refused", "Max retries exceeded", "127.0.0.1:8188")


def _is_comfyui_down_signal(error: str | None) -> bool:
    return error is not None and any(marker in error for marker in _COMFYUI_DOWN_MARKERS)


def _log_recycle_result(task: asyncio.Task) -> None:
    """Attached to the breaker's scheduled recycle task: it runs unobserved (nothing awaits it), so
    without this its outcome -- or any exception, however unlikely now that stop_backend itself
    never raises -- would otherwise only ever show up as asyncio's own GC warning, if at all."""
    if task.cancelled():
        log.warning("scheduled backend recycle was cancelled (shutdown in progress)")
        return
    exc = task.exception()
    if exc is not None:
        log.error("scheduled backend recycle failed unexpectedly: %s", exc, exc_info=exc)
        return
    outcome = task.result()
    log.info("scheduled backend recycle finished: %s", outcome.kind)


@dataclass(frozen=True, slots=True)
class StopOutcome:
    """What Stop did. `kind` drives the route: "needs_confirmation" renders the confirm partial,
    the other three render the panel with an outcome line -- "failed" included, since Stop must
    never surface an exception as a 500: every failure inside it becomes one of these instead."""

    kind: Literal["needs_confirmation", "stopped", "not_converged", "failed"]
    queued: int = 0
    running: int = 0
    cancelled: int = 0
    cancel_failures: int = 0
    stats: FunctionStats | None = None
    error: str | None = None

    @classmethod
    def needs_confirmation(cls, queued: int, running: int) -> StopOutcome:
        return cls(kind="needs_confirmation", queued=queued, running=running)

    @classmethod
    def stopped(cls, *, cancelled: int, cancel_failures: int = 0) -> StopOutcome:
        return cls(kind="stopped", cancelled=cancelled, cancel_failures=cancel_failures)

    @classmethod
    def not_converged(
        cls, stats: FunctionStats | None, *, cancel_failures: int = 0, error: str | None = None
    ) -> StopOutcome:
        return cls(kind="not_converged", stats=stats, cancel_failures=cancel_failures, error=error)

    @classmethod
    def failed(cls, error: str) -> StopOutcome:
        return cls(kind="failed", error=error)


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
        converge_poll_interval_s: float = _CONVERGE_POLL_S,
        pinger_step_s: float = _PINGER_STEP_S,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.gateway = gateway
        self.clock = clock
        self._dispatch_interval_s = dispatch_interval_s
        self._poll_interval_s = poll_interval_s
        self._converge_poll_interval_s = converge_poll_interval_s
        self._pinger_step_s = pinger_step_s

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

        # -- GPU status, warm-up and stop (see gpu.py and the pinger/stop_backend below) --------
        self.status = GpuStatus(gateway, clock=clock)
        self.ping_calls: dict[str, str] = {}
        self.last_ping: dict[str, float] = {}
        self.pingers: dict[str, asyncio.Task] = {}
        self.warm_since: dict[str, float] = {}
        self._breaker: dict[str, int] = defaultdict(int)
        self._recycle_tasks: list[asyncio.Task] = []
        # (recorded_at, outcome): so the panel can show Stop's (or a recycle's) result on every read
        # for a while, not just the one that triggered it -- the panel's own poll would otherwise
        # race the POST response, and a breaker-triggered recycle would be invisible entirely.
        self.last_stop_outcome: dict[str, tuple[float, StopOutcome]] = {}

    # -- dispatcher --------------------------------------------------------------------------------

    async def dispatch_once(self) -> None:
        for backend in self.registry.backends.values():
            if self.locks[backend.id].locked():
                # Stop (or the pinger) holds this backend's lock for its whole sequence. Waiting
                # here would stall every OTHER backend's dispatch behind a Stop that can run up to
                # about a minute, and would make last_dispatch_tick (and so /healthz) go stale.
                # Skipping is safe: Stop itself already cancelled this backend's queued jobs, so
                # there is nothing here for dispatch to spawn until the lock is free again.
                continue
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
            self._note_comfyui_signal(job, result.error)
            self._forget(job["id"])
            return None
        self._reset_breaker(job["backend_id"])
        self.status.clear_unhealthy(job["backend_id"])  # a finished render proves ComfyUI works
        return result.value

    def _note_comfyui_signal(self, job, error: str | None) -> None:
        """Feeds the circuit breaker: an instant failure (within one poll interval of submission)
        whose text matches a ComfyUI-down pattern counts toward it; anything else -- a later
        failure, or a non-matching one -- resets it, same as a success would."""
        first_poll = self.clock() - job["submitted_at"] <= self._poll_interval_s
        if first_poll and _is_comfyui_down_signal(error):
            self._record_breaker_signal(job["backend_id"], error or "")
        else:
            self._reset_breaker(job["backend_id"])

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

    # -- circuit breaker: a dead ComfyUI recycles its backend -----------------------------------------

    def _reset_breaker(self, backend_id: str) -> None:
        self._breaker[backend_id] = 0

    def _record_breaker_signal(self, backend_id: str, error: str) -> None:
        self._breaker[backend_id] += 1
        if self._breaker[backend_id] < _BREAKER_THRESHOLD:
            return
        self._breaker[backend_id] = 0
        self.status.set_unhealthy(backend_id, f"ComfyUI stopped answering: {error}"[:300])
        # Scheduled as a task, never awaited here: the pinger step already holds locks[backend_id]
        # when it calls this, and stop_backend needs that same lock -- awaiting it inline would
        # deadlock against the caller's own lock. The poller path holds no lock, but a task keeps
        # both call sites identical and never blocks the tick that triggered the recycle.
        backend = self.registry.backends[backend_id]
        task = asyncio.create_task(self.stop_backend(backend, confirm=True))
        task.add_done_callback(_log_recycle_result)
        self._recycle_tasks = [t for t in self._recycle_tasks if not t.done()]
        self._recycle_tasks.append(task)

    # -- warm-up: a pinger task runs only while a window is open ----------------------------------

    def ensure_pinger(self, backend_id: str) -> None:
        """Starts a pinger task for this backend unless one is already running. Called from the warm
        route and, at startup, for every backend with a still-open persisted window."""
        existing = self.pingers.get(backend_id)
        if existing is not None and not existing.done():
            return
        self.status.reset_ping(backend_id)  # a fresh window has no successful ping yet
        self.pingers[backend_id] = asyncio.create_task(self._pinger(self.registry.backends[backend_id]))

    def note_runners(self, backend_id: str, containers: int, now: float) -> None:
        """Tracks when a backend was first observed with at least one container, for the running-
        cost estimate. Cleared once it scales back to zero, so a later warm-up starts a fresh one."""
        if containers >= 1:
            self.warm_since.setdefault(backend_id, now)
        else:
            self.warm_since.pop(backend_id, None)

    async def _pinger(self, backend: Backend) -> None:
        try:
            while True:
                async with self.locks[backend.id]:  # held across check -> spawn -> record
                    try:
                        keep_going = await self._warm_step(backend)
                    except Exception:
                        # Never dies on an exception: logs and retries on the next step, until the
                        # window ends (a persisted spawn_ping/poll transient is already retried
                        # inside _warm_step without raising; this is the defensive backstop).
                        log.exception("warm-up step for %s failed unexpectedly, retrying", backend.id)
                        keep_going = True
                if not keep_going:
                    return
                await asyncio.sleep(self._pinger_step_s)
        finally:
            self.pingers.pop(backend.id, None)

    async def _warm_step(self, backend: Backend) -> bool:
        """One pinger tick: poll a ping already in flight (at most one), else spawn a new one every
        PING_INTERVAL_S. Returns False (the pinger exits) once the window has ended.

        The warm route writes a new warm_until without taking locks[backend_id] (see gpu.start_warm
        and clear_expired_warm), so both places this step ends the window use a compare-and-clear
        against the exact `until` this call observed, never a blind clear: a click landing in the
        gap must survive, not be silently wiped by a stale read."""
        now = self.clock()
        with session(self.settings) as conn:
            until, ping_id = gpu.warm_state(conn, backend.id)

        if until is None:
            return False  # nothing (left) to serve: another path already ended this window

        if ping_id:
            result = await self.gateway.poll(ping_id)  # a running ping reads as pending, like a job
            if result.state == "pending":
                return True  # one ping in flight at most (a cold boot takes about 68s)
            if result.state == "done":
                self.status.ping_ok(backend.id)  # "warm" is shown only after this
                self._reset_breaker(backend.id)
            else:  # ComfyUI did not answer /system_stats
                self.status.ping_failed(backend.id, result.error)
                self._record_breaker_signal(backend.id, result.error or "")
            with session(self.settings) as conn:
                gpu.clear_ping(conn, backend.id)
            self.ping_calls.pop(backend.id, None)
            self.status.force_stats(backend.id)  # a page open right now sees the new outcome immediately

        if now >= until:
            with session(self.settings) as conn:
                cleared = gpu.clear_expired_warm(conn, backend.id, until)
            return not cleared  # cleared: the window really ended. Not cleared: a fresh one landed first

        if now - self.last_ping.get(backend.id, 0.0) >= PING_INTERVAL_S:
            try:
                call_id = await self.gateway.spawn_ping(backend)
            except _SPAWN_TRANSIENT as exc:
                log.warning("spawn_ping transient failure for %s, retrying next step: %s", backend.id, exc)
                return True
            except PERMANENT as exc:
                self.alerts[backend.id] = f"Modal refused the warm-up ping: {exc}"[:300]
                with session(self.settings) as conn:
                    cleared = gpu.clear_expired_warm(conn, backend.id, until)
                return not cleared
            with session(self.settings) as conn:
                gpu.set_ping(conn, backend.id, call_id)  # persisted: Stop can cancel it after a restart
            self.ping_calls[backend.id] = call_id
            self.last_ping[backend.id] = now
        return True

    # -- stop: cancel everything at once, stop containers, converge ------------------------------

    async def stop_backend(self, backend: Backend, *, confirm: bool) -> StopOutcome:
        """Never raises: every Modal step is caught inside, and the whole sequence is wrapped so an
        unexpected failure becomes StopOutcome.failed(...) instead of a 500. The route always
        answers 200 either way. The outcome is also remembered (see recent_stop_outcome) so a panel
        read well after this call returns -- the next poll, or a page reload -- still shows it."""
        outcome = await self._stop_backend_locked(backend, confirm=confirm)
        if outcome.kind != "needs_confirmation":
            self.last_stop_outcome[backend.id] = (self.clock(), outcome)
        return outcome

    def recent_stop_outcome(self, backend_id: str) -> StopOutcome | None:
        """The last Stop (or breaker recycle) outcome for a backend, while it's still recent."""
        recorded = self.last_stop_outcome.get(backend_id)
        if recorded is None:
            return None
        recorded_at, outcome = recorded
        return outcome if self.clock() - recorded_at < _STOP_OUTCOME_TTL_S else None

    async def _stop_backend_locked(self, backend: Backend, *, confirm: bool) -> StopOutcome:
        async with self.locks[backend.id]:  # held for the whole sequence: nothing new can start meanwhile
            try:
                with session(self.settings) as conn:
                    queued, running = jobs.active_counts(conn, backend.id)
                    if (queued or running) and not confirm:
                        return StopOutcome.needs_confirmation(queued, running)
                    _, ping_id = gpu.warm_state(conn, backend.id)
                    gpu.clear_warm(conn, backend.id)  # clears warm_until and ping_call_id
                    call_ids = jobs.cancel_all_for_backend(conn, backend.id)
                self.ping_calls.pop(backend.id, None)

                # One deadline for the WHOLE rest of the sequence, cancels included: set before the
                # cancel phase, not after it, so a slow cancel can't push the total past Cloudflare's
                # roughly 100s edge timeout. Each step below gets only whatever of it remains.
                deadline = self.clock() + CONVERGE_S
                targets = ([ping_id] if ping_id else []) + call_ids  # ping first, then every call
                cancel_failures = await self._cancel_all(backend.id, targets, deadline)

                last_error: str | None = None
                app_state: AppState | None = None
                while True:
                    app_state, err = await self._bounded_step(
                        backend.id, deadline, "app_state", lambda: self.gateway.app_state(backend)
                    )
                    last_error = err or last_error
                    if app_state is not None and app_state.state != "deployed":
                        # Nothing can be running on an undeployed app: skip stop_containers/stats.
                        return StopOutcome.stopped(cancelled=len(call_ids), cancel_failures=cancel_failures)

                    # Reuses this same read inside stop_containers instead of it running its own
                    # app_state() (an extra `modal app list` per iteration) when this one succeeded.
                    _, err = await self._bounded_step(
                        backend.id,
                        deadline,
                        "stop_containers",
                        lambda state=app_state: self.gateway.stop_containers(backend, app_state=state),
                    )
                    last_error = err or last_error
                    stats, err = await self._bounded_step(
                        backend.id, deadline, "stats", lambda: self.gateway.stats(backend)
                    )
                    last_error = err or last_error

                    if stats is not None and stats.num_total_runners == 0 and stats.backlog == 0:
                        return StopOutcome.stopped(cancelled=len(call_ids), cancel_failures=cancel_failures)
                    if self.clock() >= deadline:
                        return StopOutcome.not_converged(stats, cancel_failures=cancel_failures, error=last_error)
                    await asyncio.sleep(self._converge_poll_interval_s)
            except Exception as exc:
                # Stop must never raise: anything unexpected becomes a failed outcome instead.
                log.exception("stop_backend failed unexpectedly for %s", backend.id)
                return StopOutcome.failed(str(exc)[:300])
            finally:
                self.status.force(backend.id)  # always refresh, whatever happened

    async def _bounded_step(
        self, backend_id: str, deadline: float, label: str, call: Callable[[], Awaitable[Any]]
    ) -> tuple[Any, str | None]:
        """Runs one Stop step (app_state/stop_containers/stats) under a timeout that is both capped
        at _STOP_STEP_TIMEOUT_S and never more than whatever of the overall deadline remains --
        whichever is smaller -- so neither one slow step nor a near-exhausted budget can make the
        whole sequence run long. Catches any failure, including the timeout itself, logs it with a
        readable reason (never an empty message: a bare TimeoutError's str() is ''), and returns
        (None, error_text) instead of letting it end the loop.

        Any: the three gateway methods this wraps return different types (AppState, int,
        FunctionStats); the caller already knows which one it asked for."""
        remaining = max(0.5, min(_STOP_STEP_TIMEOUT_S, deadline - self.clock()))
        try:
            async with asyncio.timeout(remaining):
                return await call(), None
        except TimeoutError:
            text = f"{label}: timed out after {remaining:.0f}s"
            log.warning("%s for %s during stop", text, backend_id)
            return None, text
        except Exception as exc:  # noqa: BLE001 -- one failed step is retried on the next iteration
            text = f"{label}: {str(exc) or type(exc).__name__}"
            log.warning("%s for %s during stop", text, backend_id)
            return None, text

    async def _cancel_all(self, backend_id: str, call_ids: list[str], deadline: float) -> int:
        """Sends every tracked call's cancel at once (the caller puts the ping first). Logs and
        retries each failure once; a cancel that still fails is reported in the outcome, not
        silently dropped -- modal 1.5.5's own container-stop docstring says an uncancelled running
        input gets rescheduled on a fresh container, which would cold-boot the very backend Stop is
        stopping. Each attempt is capped at _CANCEL_TIMEOUT_S or whatever of `deadline` remains,
        whichever is smaller, so two slow attempts can't by themselves exhaust the whole sequence's
        budget. Returns the number that failed even after the retry."""
        if not call_ids:
            return 0
        failed = await self._cancel_once(backend_id, call_ids, deadline)
        if failed:
            failed = await self._cancel_once(backend_id, failed, deadline)
        return len(failed)

    async def _cancel_once(self, backend_id: str, call_ids: list[str], deadline: float) -> list[str]:
        timeout = max(0.5, min(_CANCEL_TIMEOUT_S, deadline - self.clock()))

        async def cancel_one(call_id: str) -> str | None:
            try:
                async with asyncio.timeout(timeout):
                    await self.gateway.cancel(call_id)
            except Exception as exc:  # noqa: BLE001 -- one failed cancel must not abort the others
                log.warning("Modal cancel failed for call %s on %s: %s", call_id, backend_id, exc)
                return call_id
            return None

        results = await asyncio.gather(*(cancel_one(c) for c in call_ids))
        return [c for c in results if c is not None]

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

        # Pinger tasks (started on demand by ensure_pinger, not by run()) and any breaker-scheduled
        # recycle tasks: cancel whatever is still running so nothing is left pending at shutdown.
        # Pings stop with the process either way, so the backend still scales down within 60s.
        pending = [t for t in (*self.pingers.values(), *self._recycle_tasks) if not t.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.pingers.clear()
        self._recycle_tasks = []
