"""Test double for the Modal gateway boundary.

Holds no loop-bound asyncio primitives (no asyncio.Lock/Event/Queue): its state is guarded by a plain
threading.Lock, so a test can drive it from a different thread than the one running the Worker loops.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from modal.types import FunctionStats

from artio.modal_gateway import AppState, PollResult
from artio.registry import Backend


@dataclass
class _Call:
    backend_id: str
    graph: dict
    images: dict[str, bytes] = field(default_factory=dict)
    outcome: PollResult | None = None


class FakeModalGateway:
    """Stands in for Modal at the gateway boundary. A test scripts each call's outcome with finish(),
    fail() or expire(); raise_on_spawn() scripts the next spawn_workflow call to fail instead.

    Holds no loop-bound asyncio primitives (no asyncio.Lock/Event/Queue): its state is guarded by a
    plain threading.Lock, so a test can drive it from a different thread than the one running the
    Worker loops. `spawn_ping_hook`, an optional test-supplied async callable, is the one deliberate
    exception: it is awaited from inside the running event loop by whichever test set it, never
    called across threads, so it doesn't need the same guarantee.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, _Call] = {}
        self._next_id = 1
        self._spawn_errors: list[BaseException] = []
        self._ping_errors: list[BaseException] = []
        self.calls: list[tuple[str, str]] = []  # ordered (action, call_id) log, e.g. cancels and invalidations

        # GPU status, warm-up and stop: defaults describe an idle, deployed, scaled-to-zero
        # backend, so a test that never scripts these still gets a coherent status reading.
        self._stats: dict[str, FunctionStats] = {}
        self._stats_sequence: dict[str, list[FunctionStats]] = {}
        self._app_state: dict[str, AppState] = {}
        self._stopped_container_count: dict[str, int] = {}
        self.spawn_ping_hook: Callable[[], Awaitable[None]] | None = None
        # Every app_state stop_containers() was called with, in call order: lets a test confirm the
        # caller reused an already-fetched read instead of stop_containers doing its own.
        self.stop_containers_app_state_args: list[AppState | None] = []
        self.stats_calls = 0
        self.app_state_calls = 0

    async def spawn_workflow(self, backend: Backend, graph: dict, images: dict[str, bytes] | None = None) -> str:
        with self._lock:
            if self._spawn_errors:
                raise self._spawn_errors.pop(0)
            call_id = f"fake-{self._next_id}"
            self._next_id += 1
            self._calls[call_id] = _Call(backend_id=backend.id, graph=graph, images=dict(images or {}))
        return call_id

    async def poll(self, call_id: str) -> PollResult:
        with self._lock:
            call = self._calls.get(call_id)
            if call is None or call.outcome is None:
                return PollResult.pending()
            return call.outcome

    async def cancel(self, call_id: str, *, terminate_containers: bool = False) -> None:
        with self._lock:
            self.calls.append(("cancel", call_id))

    def invalidate(self, backend: Backend) -> None:
        with self._lock:
            self.calls.append(("invalidate", backend.id))

    # -- GPU status, warm-up and stop ------------------------------------------------------------

    async def stats(self, backend: Backend) -> FunctionStats:
        with self._lock:
            self.stats_calls += 1
            queued = self._stats_sequence.get(backend.id)
            if queued:
                return queued.pop(0)
            return self._stats.get(
                backend.id, FunctionStats(backlog=0, num_total_runners=0, num_running_inputs=0, input_headroom=0)
            )

    async def app_state(self, backend: Backend) -> AppState:
        with self._lock:
            self.app_state_calls += 1
            return self._app_state.get(backend.id, AppState("deployed", f"ap-{backend.id}"))

    async def spawn_ping(self, backend: Backend) -> str:
        if self.spawn_ping_hook is not None:  # a test's own hook, e.g. to hold a ping "in flight"
            await self.spawn_ping_hook()
        with self._lock:
            if self._ping_errors:
                raise self._ping_errors.pop(0)
            call_id = f"fake-ping-{self._next_id}"
            self._next_id += 1
            self._calls[call_id] = _Call(backend_id=backend.id, graph={})
            self.calls.append(("spawn_ping", call_id))
        return call_id

    async def stop_containers(self, backend: Backend, *, app_state: AppState | None = None) -> int:
        with self._lock:
            self.calls.append(("stop_containers", backend.id))
            self.stop_containers_app_state_args.append(app_state)
            return self._stopped_container_count.get(backend.id, 0)

    def finish(self, call_id: str, png: bytes) -> None:
        with self._lock:
            self._calls[call_id].outcome = PollResult.done(png)

    def fail(self, call_id: str, text: str) -> None:
        with self._lock:
            self._calls[call_id].outcome = PollResult.failed(text)

    def expire(self, call_id: str) -> None:
        with self._lock:
            self._calls[call_id].outcome = PollResult.failed("Result expired on Modal (results are kept 7 days).")

    def stay_pending_with_reason(self, call_id: str, reason: str) -> None:
        """Scripts a transient "still pending, and here's why" poll outcome, e.g. to simulate a
        dropped connection observed while a call is still running on Modal."""
        with self._lock:
            self._calls[call_id].outcome = PollResult.pending(transient=reason)

    def raise_on_spawn(self, exc: BaseException) -> None:
        with self._lock:
            self._spawn_errors.append(exc)

    def raise_on_spawn_ping(self, exc: BaseException) -> None:
        with self._lock:
            self._ping_errors.append(exc)

    def set_stats(self, backend_id: str, *, runners: int = 0, running_inputs: int = 0, backlog: int = 0) -> None:
        with self._lock:
            self._stats[backend_id] = FunctionStats(
                backlog=backlog, num_total_runners=runners, num_running_inputs=running_inputs, input_headroom=0
            )

    def queue_stats(self, backend_id: str, *, runners: int, running_inputs: int = 0, backlog: int = 0) -> None:
        """Appends one snapshot returned by the NEXT stats() call for this backend, before falling
        back to set_stats()'s value. Scripts Stop's convergence loop seeing "not converged yet" for
        the first N calls, then converging on a later one."""
        with self._lock:
            self._stats_sequence.setdefault(backend_id, []).append(
                FunctionStats(
                    backlog=backlog, num_total_runners=runners, num_running_inputs=running_inputs, input_headroom=0
                )
            )

    def set_app_state(self, backend_id: str, state: str, app_id: str | None) -> None:
        with self._lock:
            self._app_state[backend_id] = AppState(state, app_id)

    def set_stopped_container_count(self, backend_id: str, count: int) -> None:
        with self._lock:
            self._stopped_container_count[backend_id] = count

    def graph_for(self, call_id: str) -> dict:
        with self._lock:
            return self._calls[call_id].graph

    def images_for(self, call_id: str) -> dict[str, bytes]:
        with self._lock:
            return self._calls[call_id].images

    @property
    def spawn_count(self) -> int:
        with self._lock:
            return len(self._calls)
