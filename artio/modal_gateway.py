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
and falls through to "failed", since Artio can't tell whether it is retriable and the user can retry by
hand regardless.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import aiohttp
import grpclib.exceptions
import modal
import modal.exception
from modal.types import FunctionStats

from artio.registry import Backend

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


@dataclass(frozen=True, slots=True)
class AppState:
    """One backend's deployment state, as `modal app list` sees it: `state` is the CLI's own text
    (`deployed`, `stopped`, `stopping...`, ...), and `app_id` is None only when no row matched."""

    state: str
    app_id: str | None


def _created_at_key(row: dict) -> datetime:
    """Sort key for `parse_app_state`'s "otherwise the newest" rule. A missing or unparsable
    timestamp sorts oldest, rather than crashing on a malformed CLI row."""
    raw = row.get("created_at")
    if not raw:
        return datetime.min.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


def parse_app_state(rows: list[dict], app_name: str) -> AppState:
    """Picks the row for `app_name` out of a `modal app list --json` response: prefers a `deployed`
    row when several share the name (a redeploy after `modal app stop` can leave both an old,
    recently-stopped row and a new deployed one), otherwise the newest by `created_at`. No matching
    row at all means stopped: `modal app list` only shows recently stopped apps, so an absent row is
    a stopped app, not an unknown one."""
    matches = [row for row in rows if row.get("description") == app_name]
    if not matches:
        return AppState("stopped", None)
    for row in matches:
        if row.get("state") == "deployed":
            return AppState("deployed", row.get("app_id"))
    newest = max(matches, key=_created_at_key)
    return AppState(newest.get("state") or "unknown", newest.get("app_id"))


def parse_container_ids(rows: list[dict]) -> list[str]:
    """The container IDs from a `modal container list --json` response."""
    return [row["container_id"] for row in rows]


class ModalCliError(Exception):
    """Raised when the `modal` CLI subprocess exits non-zero or times out. The message is capped at
    500 characters of stderr and never includes the subprocess's arguments' environment."""


_CLI_TIMEOUT_S = 30.0
_CLI_ERROR_TEXT_LIMIT = 500


def _json_array(text: str) -> list[dict]:
    """Parses a CLI JSON-array response, tolerating any banner text the CLI printed before the
    array itself (NO_COLOR and TERM=dumb already suppress most of it): parses from the first `[`
    onward rather than the raw text."""
    start = text.find("[")
    if start == -1:
        raise json.JSONDecodeError("no JSON array in modal CLI output", text, 0)
    return json.loads(text[start:])


def _kill_quietly(proc: asyncio.subprocess.Process) -> None:
    """Kills the child, tolerating one that exited at the same moment: a ProcessLookupError here
    must never replace the timeout or cancellation that is already propagating."""
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def _run_cli_subprocess(
    argv: list[str],
    *,
    timeout: float = _CLI_TIMEOUT_S,
    on_spawn: Callable[[int], None] | None = None,
) -> str:
    """Runs `argv` as a subprocess: stdin closed (a non-TTY stdin would otherwise make an
    un-`--yes`'d command hang or abort), no colour, a plain terminal, and a hard timeout. Only
    stdout is ever returned, and a failure's stderr is capped at 500 characters.

    The child is killed and reaped (`kill()` then `wait()`) whenever this call ends early for any
    reason, not only its own internal timeout: a caller wrapping this in a shorter outer
    `asyncio.timeout` (Stop's own per-step budget) cancels it with the builtin `CancelledError`,
    which `except TimeoutError` alone would never catch, orphaning the child under both the default
    loop and uvloop (production's loop) alike. `on_spawn` exists only for tests, to observe the
    child's pid; production callers never pass it."""
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    if on_spawn is not None:
        on_spawn(proc.pid)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        _kill_quietly(proc)
        await proc.wait()
        raise ModalCliError(f"{' '.join(argv)} timed out after {timeout:.0f}s") from None
    except BaseException:
        # Any other reason this await ended early -- most importantly an outer cancellation, e.g.
        # _bounded_step's own asyncio.timeout expiring first with a shorter budget -- still needs
        # the same cleanup before propagating (CancelledError is a BaseException, not a subclass of
        # TimeoutError or Exception, so it would otherwise skip the branch above entirely).
        _kill_quietly(proc)
        await proc.wait()
        raise
    if proc.returncode != 0:
        # The tail, not the head: a traceback's useful line (the actual error) is at the end, same
        # as classify_poll_exception's own [-2000:] truncation for a remote exception's text.
        raise ModalCliError(stderr.decode(errors="replace").strip()[-_CLI_ERROR_TEXT_LIMIT:])
    return stdout.decode()


async def _modal_cli(*args: str) -> str:
    """Runs `sys.executable -m modal <args>`. The CLI reads MODAL_TOKEN_ID/MODAL_TOKEN_SECRET/
    MODAL_ENVIRONMENT from the inherited environment; the environment itself is never logged or
    returned. See `_run_cli_subprocess` for the actual process handling.

    Passes _CLI_TIMEOUT_S explicitly (read at call time) rather than relying on
    _run_cli_subprocess's own default: a default argument is bound once, at definition time, so a
    test (or anything else) patching the module-level constant afterward would otherwise never
    affect it."""
    return await _run_cli_subprocess([sys.executable, "-m", "modal", *args], timeout=_CLI_TIMEOUT_S)


class ModalGateway(Protocol):
    """Everything the job engine needs from Modal. `ModalSdkGateway` is the only implementation that
    performs network I/O; `tests/fakes.py:FakeModalGateway` stands in for it in every other test."""

    async def spawn_workflow(self, backend: Backend, graph: dict, images: dict[str, bytes] | None = None) -> str: ...

    async def poll(self, call_id: str) -> PollResult: ...

    async def cancel(self, call_id: str, *, terminate_containers: bool = False) -> None: ...

    def invalidate(self, backend: Backend) -> None: ...

    async def stats(self, backend: Backend) -> FunctionStats: ...

    async def app_state(self, backend: Backend) -> AppState: ...

    async def spawn_ping(self, backend: Backend) -> str: ...

    async def stop_containers(self, backend: Backend, *, app_state: AppState | None = None) -> int: ...


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

    async def spawn_workflow(self, backend: Backend, graph: dict, images: dict[str, bytes] | None = None) -> str:
        handle = self._handle(backend)
        try:
            # A graph with no input images keeps the one-argument call, so it still runs on a backend
            # deployed before run_workflow learned its `images` parameter.
            if images:
                call = await handle.run_workflow.spawn.aio(graph, images)
            else:
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
        """For Stop only: a user cancel never reaches this, since it would SIGINT the whole
        container under @modal.concurrent and reschedule every sibling render."""
        await modal.FunctionCall.from_id(call_id).cancel.aio(terminate_containers=terminate_containers)

    def invalidate(self, backend: Backend) -> None:
        """Drops the cached handle. `gpu.GpuStatus` calls this when the backend's app_id changes,
        e.g. after a redeploy that followed a `modal app stop`."""
        self._handles.pop(backend.id, None)

    async def stats(self, backend: Backend) -> FunctionStats:
        """Any bound method hydrates from the same class-level service function's object_id
        (cls.py:90), so pinging's own stats already cover run_workflow and generate too."""
        handle = self._handle(backend)
        return await handle.ping.get_current_stats.aio()

    async def app_state(self, backend: Backend) -> AppState:
        output = await _modal_cli("app", "list", "--json")
        return parse_app_state(_json_array(output), backend.modal_app)

    async def spawn_ping(self, backend: Backend) -> str:
        handle = self._handle(backend)
        try:
            call = await handle.ping.spawn.aio()
        except PERMANENT:
            self._handles.pop(backend.id, None)
            raise
        return call.object_id

    async def stop_containers(self, backend: Backend, *, app_state: AppState | None = None) -> int:
        """`app_state`, when given, is reused instead of this method running its own `app list` --
        Stop's own convergence loop already reads it once per pass and passes it through, so the
        pass costs one app-state read, not two."""
        state = app_state if app_state is not None else await self.app_state(backend)
        if state.state != "deployed" or not state.app_id:
            return 0
        rows = _json_array(await _modal_cli("container", "list", "--app-id", state.app_id, "--json"))
        for container_id in parse_container_ids(rows):
            try:
                await _modal_cli("container", "stop", "--yes", container_id)
            except ModalCliError as exc:
                if "already stopped" not in str(exc):  # a container that exited on its own is fine
                    raise
        return len(rows)
