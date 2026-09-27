"""On-read GPU status, the `backend_state` table (warm windows and the persisted ping call), and
the cost estimate helpers.

Status is never polled in the background: `GpuStatus.get()` refreshes Modal's stats (an SDK call)
after 10 s and the app's deployment state (a CLI subprocess) after 60 s, with at most one real
refresh in flight per backend. A page that never opens therefore costs nothing -- see worker.py for
the pinger, which is the only thing that ever calls Modal without a page open, and only while a
warm window is running.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from modal.types import FunctionStats

from artio.modal_gateway import AppState, ModalGateway
from artio.registry import Backend

_STATS_TTL_S = 10.0
_APP_STATE_TTL_S = 60.0
_APP_STATE_ERROR_RETRY_S = 10.0  # retry a failed app-state read much sooner than a 60s success
_APP_STATE_ERROR_GRACE_S = 30.0  # ...and keep showing the last good state for this long regardless
_COLD_START_ESTIMATE_USD = 0.04
_WARM_MINUTES = (5, 15, 30)
_ZERO_STATS = FunctionStats(backlog=0, num_total_runners=0, num_running_inputs=0, input_headroom=0)


class BackendStopped(Exception):
    """Raised by start_warm when the app is not deployed: warming a stopped app would just fail its
    first ping, so the route refuses it up front instead of spawning a ping Modal will reject."""


@dataclass(frozen=True, slots=True)
class GpuStatusView:
    """Everything a reader needs to render one backend: the two cached values, the warm window read
    fresh from the database, the pinger's last outcome, and any error from this read's own refresh."""

    backend_id: str
    now: float
    app_state: AppState
    stats: FunctionStats | None
    warm_until: float | None
    ping_ok: bool | None
    unhealthy: str | None
    error: str | None

    @property
    def window_open(self) -> bool:
        return self.warm_until is not None and self.now < self.warm_until

    @property
    def containers(self) -> int:
        return self.stats.num_total_runners if self.stats else 0

    @property
    def running_inputs(self) -> int:
        return self.stats.num_running_inputs if self.stats else 0

    @property
    def backlog(self) -> int:
        return self.stats.backlog if self.stats else 0


def display_state(status: GpuStatusView) -> str:
    """The single state label a page shows, checked in this priority order: unknown on error,
    stopped if not deployed, unhealthy if the last ping failed or the breaker notice stands,
    warming/warm depending on whether a ping has already succeeded in the open window, running for
    containers busy outside a window, else scaled to zero. "Warm" is therefore shown only after a
    successful ping, never merely because a warm-up window happens to still be open."""
    if status.error:
        return "unknown"
    if status.app_state.state != "deployed":
        return "stopped"
    if status.unhealthy:
        return "unhealthy"
    if status.window_open:
        return "warm" if status.ping_ok and status.containers >= 1 else "warming"
    if status.containers >= 1:
        return "running"
    return "scaled to zero"


class GpuStatus:
    """The on-read status caches plus the in-memory ping/unhealthy bookkeeping the worker's pinger
    and circuit breaker feed. One instance lives on the Worker for the process lifetime; caches are
    never persisted, since a restart just means the first read after startup is a real refresh."""

    def __init__(self, gateway: ModalGateway, clock: Callable[[], float] = time.time) -> None:
        self.gateway = gateway
        self.clock = clock
        self._stats: dict[str, FunctionStats] = {}
        self._stats_at: dict[str, float] = {}
        self._stats_error: dict[str, str] = {}
        self._app_state: dict[str, AppState] = {}
        self._app_state_at: dict[str, float] = {}
        self._app_state_error: dict[str, str] = {}
        self._app_state_error_since: dict[str, float] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._ping_ok: dict[str, bool | None] = {}
        self._unhealthy: dict[str, str | None] = {}

    def ping_ok(self, backend_id: str) -> None:
        """Records a successful probe: "warm" can now be shown, and any standing unhealthy notice
        for this backend is cleared -- a live ComfyUI is the one signal that clears it."""
        self._ping_ok[backend_id] = True
        self._unhealthy[backend_id] = None

    def ping_failed(self, backend_id: str, error: str | None) -> None:
        self._ping_ok[backend_id] = False
        self._unhealthy[backend_id] = error or "the warm-up ping failed"

    def reset_ping(self, backend_id: str) -> None:
        """Called when a fresh pinger task starts (a new warm window): a ping_ok left over from a
        previous window must never make the new one read as instantly warm."""
        self._ping_ok[backend_id] = None

    def set_unhealthy(self, backend_id: str, reason: str) -> None:
        """Used by the poller's circuit breaker path (instant ComfyUI-down job failures): unlike a
        failed ping, this doesn't touch ping_ok, since there was no ping to record an outcome for."""
        self._unhealthy[backend_id] = reason

    def clear_unhealthy(self, backend_id: str) -> None:
        """A signal that ComfyUI is fine now (a successful job render) that isn't a ping: clears any
        standing notice without asserting anything about ping_ok, which only a ping itself sets."""
        self._unhealthy[backend_id] = None

    def force(self, backend_id: str) -> None:
        """Marks both caches stale, so the very next get() re-reads Modal instead of serving a
        value that just turned wrong (right after Stop, or the moment a ping resolves)."""
        self._stats_at[backend_id] = 0.0
        self._app_state_at[backend_id] = 0.0

    def force_stats(self, backend_id: str) -> None:
        """Marks only the stats cache stale: a resolved ping can only have changed container/runner
        counts, never the app's deployment state, so there is no need to also force a CLI call."""
        self._stats_at[backend_id] = 0.0

    async def get(self, backend: Backend, conn: sqlite3.Connection, *, force: bool = False) -> GpuStatusView:
        async with self._refresh_locks[backend.id]:  # one real refresh in flight per backend
            await self._refresh(backend, force=force)
        warm_until, _ = warm_state(conn, backend.id)
        # app_state's own error, if any, is the more informative one (without it we don't even know
        # whether stats should have been attempted); negative-cached, so a waiting or later reader
        # within the backoff window sees the same standing error instead of a fresh attempt. A
        # single transient app-state failure is suppressed for a short grace period -- but only when
        # there is an actual last known-good state to keep showing instead (and retries sooner, see
        # _refresh); a backend that has NEVER once been read successfully has nothing to fall back
        # on, and must still read "unknown" (not silently default to "stopped") right away.
        app_state_error = self._app_state_error.get(backend.id)
        if app_state_error is not None and backend.id in self._app_state:
            since = self._app_state_error_since.get(backend.id, self.clock())
            if self.clock() - since < _APP_STATE_ERROR_GRACE_S:
                app_state_error = None
        error = app_state_error or self._stats_error.get(backend.id)
        return GpuStatusView(
            backend_id=backend.id,
            now=self.clock(),
            app_state=self._app_state.get(backend.id, AppState("unknown", None)),
            stats=self._stats.get(backend.id),
            warm_until=warm_until,
            ping_ok=self._ping_ok.get(backend.id),
            unhealthy=self._unhealthy.get(backend.id),
            error=error,
        )

    async def _refresh(self, backend: Backend, *, force: bool) -> None:
        """Refreshes whichever of the two caches is stale (or both, if force). Re-reads the cache
        ages after acquiring the lock, so a concurrent reader that just refreshed while this call
        was waiting means this call does no Modal I/O at all, and reuses whatever that refresh (or
        this one) just produced -- including a negative-cached failure (see below).

        app_state is always checked first. When it is known (fresh or still-cached) to say the app
        isn't deployed, the stats lookup is skipped entirely: nothing can be running, skipping it
        also avoids the SDK's own NotFoundError for a stopped app's handle, and neither may outrank
        "stopped" with "unknown". A failed attempt still advances its "last refreshed" timestamp
        (negative caching): the next read reuses the cached error for the same TTL window instead of
        every reader re-running a failing SDK or CLI call of its own -- except a *failed* app-state
        read is retried much sooner (_APP_STATE_ERROR_RETRY_S) than a successful one is re-checked
        (_APP_STATE_TTL_S), so a transient CLI hiccup recovers quickly instead of being stuck for a
        full minute (get() also hides it behind a short grace period while retries are in flight)."""
        now = self.clock()

        app_state_ttl = _APP_STATE_ERROR_RETRY_S if backend.id in self._app_state_error else _APP_STATE_TTL_S
        if force or now - self._app_state_at.get(backend.id, 0.0) >= app_state_ttl:
            try:
                new_state = await self.gateway.app_state(backend)
                old_state = self._app_state.get(backend.id)
                if old_state is not None and old_state.app_id != new_state.app_id:
                    self.gateway.invalidate(backend)  # the handle follows the deployment
                self._app_state[backend.id] = new_state
                self._app_state_error.pop(backend.id, None)
                self._app_state_error_since.pop(backend.id, None)
            except Exception as exc:  # noqa: BLE001 -- any failure here just means "unknown", never a 500
                self._app_state_error[backend.id] = str(exc)
                self._app_state_error_since.setdefault(backend.id, now)  # keep the FIRST failure's time
            finally:
                self._app_state_at[backend.id] = self.clock()

        known = self._app_state.get(backend.id)
        not_deployed = known is not None and known.state != "deployed"

        if force or now - self._stats_at.get(backend.id, 0.0) >= _STATS_TTL_S:
            if not_deployed:
                self._stats[backend.id] = _ZERO_STATS
                self._stats_error.pop(backend.id, None)
                self._stats_at[backend.id] = self.clock()
            else:
                try:
                    self._stats[backend.id] = await self.gateway.stats(backend)
                    self._stats_error.pop(backend.id, None)
                except Exception as exc:  # noqa: BLE001
                    self._stats_error[backend.id] = str(exc)
                finally:
                    self._stats_at[backend.id] = self.clock()


@dataclass(frozen=True, slots=True)
class GpuSummary:
    """The header badge's view of one backend: just enough to show a state, a warm-until time and
    an unhealthy notice, without the panel's per-card detail."""

    backend_id: str
    label: str
    state: str
    warm_until: float | None
    window_open: bool
    unhealthy: str | None


async def summarize(status: GpuStatus, backend: Backend, conn: sqlite3.Connection) -> GpuSummary:
    view = await status.get(backend, conn)
    return GpuSummary(
        backend_id=backend.id,
        label=backend.label,
        state=display_state(view),
        warm_until=view.warm_until,
        window_open=view.window_open,
        unhealthy=view.unhealthy,
    )


# -- backend_state: the only readers and writers of this table -----------------------------------


def warm_state(conn: sqlite3.Connection, backend_id: str) -> tuple[float | None, str | None]:
    """Returns (warm_until, ping_call_id) for one backend, or (None, None) if it has no row yet."""
    row = conn.execute(
        "SELECT warm_until, ping_call_id FROM backend_state WHERE backend_id = ?", (backend_id,)
    ).fetchone()
    if row is None:
        return None, None
    return row["warm_until"], row["ping_call_id"]


def _ensure_row(conn: sqlite3.Connection, backend_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO backend_state (backend_id) VALUES (?)", (backend_id,))


def set_ping(conn: sqlite3.Connection, backend_id: str, call_id: str) -> None:
    _ensure_row(conn, backend_id)
    conn.execute("UPDATE backend_state SET ping_call_id = ? WHERE backend_id = ?", (call_id, backend_id))


def clear_ping(conn: sqlite3.Connection, backend_id: str) -> None:
    conn.execute("UPDATE backend_state SET ping_call_id = NULL WHERE backend_id = ?", (backend_id,))


def clear_warm(conn: sqlite3.Connection, backend_id: str) -> None:
    """Unconditionally clears both warm_until and ping_call_id: for Stop only, which is an explicit
    user action that must end the window regardless of anything written concurrently. Stop already
    holds locks[backend_id] for its whole sequence, which serializes it against the pinger's own
    steps; the warm route does not take that lock, so a stale-expiry clear must use
    clear_expired_warm() below instead of this one."""
    conn.execute(
        "UPDATE backend_state SET warm_until = NULL, ping_call_id = NULL WHERE backend_id = ?", (backend_id,)
    )


def clear_expired_warm(conn: sqlite3.Connection, backend_id: str, observed_until: float) -> bool:
    """Compare-and-clear: clears warm_until/ping_call_id only if the row's current warm_until still
    equals `observed_until`, the value this caller read earlier in the same pinger step. Returns
    whether it actually cleared.

    The warm route writes a new warm_until without taking locks[backend_id] (see start_warm), so a
    warm click can land in the gap between the pinger reading a now-expired `until` and this clear.
    A caller that gets False back must treat the window as still open: a fresh warm-up (or a
    permanent-spawn-error clear racing the same way) changed the row after this caller's own read,
    and that value must survive untouched."""
    cursor = conn.execute(
        "UPDATE backend_state SET warm_until = NULL, ping_call_id = NULL WHERE backend_id = ? AND warm_until = ?",
        (backend_id, observed_until),
    )
    return cursor.rowcount > 0


def start_warm(conn: sqlite3.Connection, backend_id: str, minutes: int, now: float, *, deployed: bool) -> float:
    """Persists a new warm_until and returns it. Refused while the app isn't deployed (raises
    BackendStopped): warming a stopped app would just spawn a ping Modal refuses.

    A click only ever extends the window (warm_until = max(existing, now + minutes)); it never
    shortens one already open, so a 5-minute click during an active 30-minute window is a no-op on
    the end time, not a cut to about 6 minutes. Nothing here bounds how often it can be called:
    total spend is bounded by the Modal workspace spend limit, not by Artio."""
    if minutes not in _WARM_MINUTES:
        raise ValueError(f"minutes must be one of {_WARM_MINUTES}, got {minutes}")
    if not deployed:
        raise BackendStopped(backend_id)
    _ensure_row(conn, backend_id)
    existing_until, _ = warm_state(conn, backend_id)
    candidate = now + minutes * 60
    until = candidate if existing_until is None else max(existing_until, candidate)
    conn.execute("UPDATE backend_state SET warm_until = ? WHERE backend_id = ?", (until, backend_id))
    return until


# -- cost helpers ----------------------------------------------------------------------------------


def window_cost_estimate(minutes: int, usd_per_hour: float, *, cold: bool) -> float:
    """Estimated spend for a warm-up window: a cold start adds the one-off boot render's cost."""
    cost = minutes * usd_per_hour / 60
    return cost + _COLD_START_ESTIMATE_USD if cold else cost


def running_cost_estimate(since: float, now: float, usd_per_hour: float) -> float:
    """Estimated spend for a backend that has had at least one container since `since`."""
    return max(0.0, now - since) * usd_per_hour / 3600
