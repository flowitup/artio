"""Test double for the Modal gateway boundary.

Holds no loop-bound asyncio primitives (no asyncio.Lock/Event/Queue): its state is guarded by a plain
threading.Lock, so a test can drive it from a different thread than the one running the Worker loops.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from atelier.modal_gateway import PollResult
from atelier.registry import Backend


@dataclass
class _Call:
    backend_id: str
    graph: dict
    outcome: PollResult | None = None


class FakeModalGateway:
    """Stands in for Modal at the gateway boundary. A test scripts each call's outcome with finish(),
    fail() or expire(); raise_on_spawn() scripts the next spawn_workflow call to fail instead."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, _Call] = {}
        self._next_id = 1
        self._spawn_errors: list[BaseException] = []
        self.calls: list[tuple[str, str]] = []  # ordered (action, call_id) log, e.g. cancels and invalidations

    async def spawn_workflow(self, backend: Backend, graph: dict) -> str:
        with self._lock:
            if self._spawn_errors:
                raise self._spawn_errors.pop(0)
            call_id = f"fake-{self._next_id}"
            self._next_id += 1
            self._calls[call_id] = _Call(backend_id=backend.id, graph=graph)
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

    def graph_for(self, call_id: str) -> dict:
        with self._lock:
            return self._calls[call_id].graph

    @property
    def spawn_count(self) -> int:
        with self._lock:
            return len(self._calls)
