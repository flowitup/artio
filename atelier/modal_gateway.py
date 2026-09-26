"""The only module that talks to Modal.

`poll()`'s exception mapping is order-sensitive: `get(timeout=0)` on a still-running call raises Python's
BUILTIN `TimeoutError` (not `modal.exception.TimeoutError`), because `modal._functions` never imports the
builtin name and `_Invocation.poll_function` raises a bare `TimeoutError()` when there are outputs pending.
`OutputExpiredError` and `FunctionTimeoutError` both subclass Modal's own `TimeoutError`, so they are
checked first; `modal.exception.TimeoutError` itself is treated as pending too, defensively, since a future
SDK version could start raising it directly.

`TRANSIENT` is an allowlist, not "everything that subclasses grpclib.GRPCError": every Modal RPC error
(including NotFoundError and AuthError) subclasses GRPCError, so treating that whole family as transient
would retry an undeployed app or a revoked token forever instead of failing visibly. `InternalFailure` and
`aiohttp.ClientError` join the allowlist because a retriable internal error or a failed blob download of an
already-finished result are exactly as transient as a dropped connection; `ExecutionError` stays out of it
and falls through to "failed", since Atelier can't tell whether it is retriable and the user can retry by
hand regardless.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp
import grpclib.exceptions
import modal
import modal.exception

from atelier.registry import Backend

TRANSIENT: tuple[type[BaseException], ...] = (
    modal.exception.ConnectionError,
    modal.exception.ServiceError,
    grpclib.exceptions.StreamTerminatedError,
    modal.exception.InternalFailure,
    aiohttp.ClientError,
)
PERMANENT: tuple[type[BaseException], ...] = (
    modal.exception.NotFoundError,
    modal.exception.AuthError,
    modal.exception.PermissionDeniedError,
    modal.exception.InvalidError,
    modal.exception.ConflictError,
)


@dataclass(frozen=True, slots=True)
class PollResult:
    """The outcome of one poll of a spawned call: pending (maybe with a transient reason), done or failed."""

    state: str
    value: bytes | None = None
    error: str | None = None
    transient: str | None = None

    @classmethod
    def pending(cls, transient: str | None = None) -> PollResult:
        return cls(state="pending", transient=transient)

    @classmethod
    def done(cls, value: bytes) -> PollResult:
        return cls(state="done", value=value)

    @classmethod
    def failed(cls, error: str) -> PollResult:
        return cls(state="failed", error=error)


def classify_poll_exception(exc: BaseException) -> PollResult:
    """Map one exception raised by `FunctionCall.get()` to a PollResult. The check order is the contract."""
    if isinstance(exc, modal.exception.OutputExpiredError):
        return PollResult.failed("Result expired on Modal (results are kept 7 days).")
    if isinstance(exc, modal.exception.FunctionTimeoutError):
        return PollResult.failed(f"Backend timed out: {exc}")
    if isinstance(exc, (TimeoutError, modal.exception.TimeoutError)):  # builtin TimeoutError means "still running"
        return PollResult.pending()
    if isinstance(exc, (*TRANSIENT, modal.exception.ResourceExhaustedError)):
        return PollResult.pending(transient=str(exc)[:300])
    if isinstance(exc, PERMANENT):
        return PollResult.failed(f"Modal refused the call: {exc}"[:2000])
    return PollResult.failed(str(exc)[-2000:] or type(exc).__name__)  # remote ComfyUI errors and the rest


class ModalGateway(Protocol):
    """Everything the job engine needs from Modal. `ModalSdkGateway` is the only implementation that
    performs network I/O; `tests/fakes.py:FakeModalGateway` stands in for it in every other test."""

    async def spawn_workflow(self, backend: Backend, graph: dict) -> str: ...

    async def poll(self, call_id: str) -> PollResult: ...

    async def cancel(self, call_id: str, *, terminate_containers: bool = False) -> None: ...

    def invalidate(self, backend: Backend) -> None: ...


class ModalSdkGateway:
    """Talks to real Modal. Keeps one hydrated `Cls` handle per backend for the process lifetime, since
    re-resolving it on every call would cost an extra RPC; a permanent error drops it instead, so the next
    spawn re-resolves the app by name (needed after e.g. a `modal app stop` plus redeploy)."""

    def __init__(self) -> None:
        # Any: modal.Cls.from_name(...)() returns a dynamically hydrated, synchronicity-wrapped instance
        # with no public static type; only its attribute access (handle.run_workflow.spawn.aio) is used.
        self._handles: dict[str, Any] = {}

    def _handle(self, backend: Backend) -> Any:
        handle = self._handles.get(backend.id)
        if handle is None:
            handle = modal.Cls.from_name(backend.modal_app, backend.modal_class)()
            self._handles[backend.id] = handle
        return handle

    async def spawn_workflow(self, backend: Backend, graph: dict) -> str:
        handle = self._handle(backend)
        try:
            call = await handle.run_workflow.spawn.aio(graph)
        except PERMANENT:
            self._handles.pop(backend.id, None)
            raise
        return call.object_id

    async def poll(self, call_id: str) -> PollResult:
        call = modal.FunctionCall.from_id(call_id)  # no I/O; the SDK deprecates .aio on this one call
        try:
            value = await call.get.aio(timeout=0)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise  # never swallow the loop's own cancellation or an operator interrupt
        except BaseException as exc:  # noqa: BLE001 -- classify_poll_exception() handles every remaining
            # type, including a deserialized remote BaseException such as SystemExit
            return classify_poll_exception(exc)
        return PollResult.done(value)

    async def cancel(self, call_id: str, *, terminate_containers: bool = False) -> None:
        """For Stop only (phase 6): a user cancel never reaches this, since it would SIGINT the whole
        container under @modal.concurrent and reschedule every sibling render."""
        await modal.FunctionCall.from_id(call_id).cancel.aio(terminate_containers=terminate_containers)

    def invalidate(self, backend: Backend) -> None:
        """Drops the cached handle. Phase 6 calls this when the backend's app_id changes."""
        self._handles.pop(backend.id, None)
