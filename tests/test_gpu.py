"""GPU status, warm-up and stop.

Gateway-boundary tests stub only `asyncio.create_subprocess_exec` (for the CLI helper) or
`modal.Cls.from_name` (for the SDK calls), exactly like test_modal_gateway.py, so the real parsing
and control flow run. Everything above the gateway (GpuStatus, the pinger, Stop, the breaker) is
driven through the fake gateway and an injected clock; no test here makes a real Modal call.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import modal.exception
import pytest
import uvloop
from modal.types import FunctionStats
from starlette.testclient import TestClient

from artio import db, gpu, jobs, modal_gateway
from artio import worker as worker_module
from artio.gpu import GpuStatus, GpuStatusView, display_state
from artio.main import create_app
from artio.modal_gateway import AppState, ModalSdkGateway, parse_app_state, parse_container_ids
from artio.registry import DEFAULT_REGISTRY
from artio.worker import _STOP_STEP_TIMEOUT_S, PING_INTERVAL_S, StopOutcome, Worker

BACKEND = DEFAULT_REGISTRY.backends["qwen21-uc"]


def _request(**overrides) -> jobs.BatchRequest:
    fields = {
        "model_id": "qwen-image-2.1-uc",
        "prompt": "a red fox in snow",
        "negative": "",
        "width": 1088,
        "height": 1920,
        "steps": 25,
        "cfg": 1.0,
        "seed_mode": "fixed",
        "seed": 42,
        "count": 1,
    }
    fields.update(overrides)
    return jobs.BatchRequest(**fields)


class _Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FastForwardClock:
    """A clock that advances by `step` on every single call, for tests that need Stop's own
    deadline to be reached deterministically without either sleeping in real time or having to
    count exactly how many times self.clock() is called along the way."""

    def __init__(self, step: float, start: float = 0.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


# ======================================================================================================
# Gateway: pure parsers on literal CLI JSON
# ======================================================================================================

_APP_LIST_STOPPED_THEN_REDEPLOYED = [
    {
        "app_id": "ap-oldoldoldoldoldoldoldoldold",
        "description": "qwen21-uc",
        "state": "stopped",
        "tasks": "0",
        "created_at": "2026-09-10 09:00:00+02:00",
        "stopped_at": "2026-09-20 09:59:00+02:00",
    },
    {
        "app_id": "ap-newnewnewnewnewnewnewnewnew",
        "description": "qwen21-uc",
        "state": "deployed",
        "tasks": "1",
        "created_at": "2026-09-20 10:00:00+02:00",
        "stopped_at": None,
    },
]

_APP_LIST_OTHER_APP_ONLY = [
    {
        "app_id": "ap-someotherapp00000000000000",
        "description": "some-other-app",
        "state": "deployed",
        "tasks": "1",
        "created_at": "2026-09-20 10:00:00+02:00",
        "stopped_at": None,
    }
]


def test_parse_app_state_prefers_the_deployed_row():
    state = parse_app_state(_APP_LIST_STOPPED_THEN_REDEPLOYED, "qwen21-uc")
    assert state == AppState("deployed", "ap-newnewnewnewnewnewnewnewnew")


def test_app_missing_from_the_list_reads_as_stopped():
    assert parse_app_state(_APP_LIST_OTHER_APP_ONLY, "qwen21-uc") == AppState("stopped", None)
    assert parse_app_state([], "qwen21-uc") == AppState("stopped", None)


def test_parse_app_state_picks_the_newest_row_when_none_is_deployed():
    rows = [
        {"app_id": "ap-older", "description": "qwen21-uc", "state": "stopped", "created_at": "2026-09-10 09:00:00+02:00"},
        {"app_id": "ap-newer", "description": "qwen21-uc", "state": "stopped", "created_at": "2026-09-20 09:59:00+02:00"},
    ]
    assert parse_app_state(rows, "qwen21-uc") == AppState("stopped", "ap-newer")


def test_parse_container_ids_returns_the_ids():
    rows = [
        {"container_id": "ta-01", "app_id": "ap-1", "app_name": "qwen21-uc", "start_time": "2026-09-20 10:00:00+02:00"},
        {"container_id": "ta-02", "app_id": "ap-1", "app_name": "qwen21-uc", "start_time": "2026-09-20 10:01:00+02:00"},
    ]
    assert parse_container_ids(rows) == ["ta-01", "ta-02"]


# ======================================================================================================
# Gateway: _modal_cli itself (stubs only asyncio.create_subprocess_exec)
# ======================================================================================================


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int, *, hang: bool = False) -> None:
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode
        self._hang = hang
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(10)
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def test_modal_cli_runs_with_no_color_dumb_terminal_and_closed_stdin(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess(b"[]", b"", 0)

    monkeypatch.setattr(modal_gateway.asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(modal_gateway._modal_cli("app", "list", "--json"))

    assert result == "[]"
    assert captured["args"] == (sys.executable, "-m", "modal", "app", "list", "--json")
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL
    assert captured["kwargs"]["env"]["NO_COLOR"] == "1"
    assert captured["kwargs"]["env"]["TERM"] == "dumb"


def test_modal_cli_raises_with_stderr_capped_at_500_characters(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProcess(b"", ("boom " * 200).encode(), 1)

    monkeypatch.setattr(modal_gateway.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(modal_gateway.ModalCliError) as exc_info:
        asyncio.run(modal_gateway._modal_cli("container", "stop", "--yes", "ta-1"))
    assert len(str(exc_info.value)) <= 500


def test_modal_cli_keeps_the_stderr_tail_not_the_head(monkeypatch):
    """A traceback's useful line (the actual error) is at the end: capping at the head instead of
    the tail would keep 500 characters of boilerplate and lose the one line that matters."""
    stderr = ("padding " * 100 + "USEFUL_TAIL_MARKER").encode()  # head and tail are distinct here

    async def fake_exec(*args, **kwargs):
        return _FakeProcess(b"", stderr, 1)

    monkeypatch.setattr(modal_gateway.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(modal_gateway.ModalCliError) as exc_info:
        asyncio.run(modal_gateway._modal_cli("app", "list", "--json"))

    assert "USEFUL_TAIL_MARKER" in str(exc_info.value)
    assert len(str(exc_info.value)) <= 500


def test_modal_cli_kills_the_process_and_raises_on_timeout(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProcess(b"", b"", 0, hang=True)

    monkeypatch.setattr(modal_gateway.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(modal_gateway, "_CLI_TIMEOUT_S", 0.05)
    with pytest.raises(modal_gateway.ModalCliError, match="timed out"):
        asyncio.run(modal_gateway._modal_cli("app", "list", "--json"))


@pytest.mark.parametrize("loop_factory", [None, uvloop.new_event_loop], ids=["default_loop", "uvloop"])
def test_run_cli_subprocess_kills_and_reaps_the_child_on_outer_cancellation(loop_factory):
    """An outer cancellation -- e.g. _bounded_step's own asyncio.timeout firing first, with a
    shorter budget than the CLI's own internal timeout -- must still kill and reap the child
    process, on both the default event loop and uvloop (production's loop). A plain CancelledError
    is not a TimeoutError, so it would otherwise bypass the cleanup entirely and leave the child
    (a real subprocess here, not a mock) running."""
    pids: list[int] = []

    async def go():
        async with asyncio.timeout(0.2):
            await modal_gateway._run_cli_subprocess(["sleep", "5"], timeout=30.0, on_spawn=pids.append)

    with pytest.raises(TimeoutError):
        asyncio.run(go(), loop_factory=loop_factory)

    assert pids, "the child process was never spawned"
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)  # signal 0: a pure existence probe, raises once the pid is fully reaped


# ======================================================================================================
# Gateway: stats, app_state, spawn_ping, stop_containers on the real ModalSdkGateway
# ======================================================================================================


def _cli_router(responses: dict[tuple[str, ...], _FakeProcess]):
    async def fake_exec(*args, **kwargs):
        key = args[3:]  # after (sys.executable, "-m", "modal")
        for prefix, proc in responses.items():
            if key[: len(prefix)] == prefix:
                return proc
        raise AssertionError(f"unexpected modal CLI invocation: {key}")

    return fake_exec


def test_stop_treats_already_stopped_containers_as_success(monkeypatch):
    app_list = json.dumps(
        [{"app_id": "ap-1", "description": "qwen21-uc", "state": "deployed", "created_at": "2026-09-20 10:00:00+02:00"}]
    ).encode()
    container_list = json.dumps(
        [
            {"container_id": "ta-1", "app_id": "ap-1", "app_name": "qwen21-uc", "start_time": "2026-09-20 10:00:00+02:00"},
            {"container_id": "ta-2", "app_id": "ap-1", "app_name": "qwen21-uc", "start_time": "2026-09-20 10:00:05+02:00"},
        ]
    ).encode()
    responses = {
        ("app", "list", "--json"): _FakeProcess(app_list, b"", 0),
        ("container", "list", "--app-id", "ap-1", "--json"): _FakeProcess(container_list, b"", 0),
        ("container", "stop", "--yes", "ta-1"): _FakeProcess(b"", b"Container 'ta-1' is already stopped.", 1),
        ("container", "stop", "--yes", "ta-2"): _FakeProcess(b"", b"", 0),
    }
    monkeypatch.setattr(modal_gateway.asyncio, "create_subprocess_exec", _cli_router(responses))

    count = asyncio.run(ModalSdkGateway().stop_containers(BACKEND))
    assert count == 2


def test_stop_containers_reraises_a_real_stop_failure(monkeypatch):
    app_list = json.dumps(
        [{"app_id": "ap-1", "description": "qwen21-uc", "state": "deployed", "created_at": "2026-09-20 10:00:00+02:00"}]
    ).encode()
    container_list = json.dumps(
        [{"container_id": "ta-1", "app_id": "ap-1", "app_name": "qwen21-uc", "start_time": "2026-09-20 10:00:00+02:00"}]
    ).encode()
    responses = {
        ("app", "list", "--json"): _FakeProcess(app_list, b"", 0),
        ("container", "list", "--app-id", "ap-1", "--json"): _FakeProcess(container_list, b"", 0),
        ("container", "stop", "--yes", "ta-1"): _FakeProcess(b"", b"internal error", 1),
    }
    monkeypatch.setattr(modal_gateway.asyncio, "create_subprocess_exec", _cli_router(responses))
    with pytest.raises(modal_gateway.ModalCliError, match="internal error"):
        asyncio.run(ModalSdkGateway().stop_containers(BACKEND))


def test_stop_containers_returns_zero_when_the_app_is_not_deployed(monkeypatch):
    responses = {("app", "list", "--json"): _FakeProcess(b"[]", b"", 0)}
    monkeypatch.setattr(modal_gateway.asyncio, "create_subprocess_exec", _cli_router(responses))
    assert asyncio.run(ModalSdkGateway().stop_containers(BACKEND)) == 0


def test_app_state_parses_a_real_cli_response(monkeypatch):
    app_list = json.dumps(
        [{"app_id": "ap-1", "description": "qwen21-uc", "state": "deployed", "created_at": "2026-09-20 10:00:00+02:00"}]
    ).encode()
    monkeypatch.setattr(
        modal_gateway.asyncio, "create_subprocess_exec", _cli_router({("app", "list", "--json"): _FakeProcess(app_list, b"", 0)})
    )
    assert asyncio.run(ModalSdkGateway().app_state(BACKEND)) == AppState("deployed", "ap-1")


def test_stats_calls_get_current_stats_on_the_ping_bound_method(monkeypatch):
    stats = FunctionStats(backlog=1, num_total_runners=2, num_running_inputs=3, input_headroom=4)

    async def get_current_stats_aio():
        return stats

    handle = SimpleNamespace(ping=SimpleNamespace(get_current_stats=SimpleNamespace(aio=get_current_stats_aio)))
    monkeypatch.setattr(modal_gateway.modal.Cls, "from_name", staticmethod(lambda app, cls: lambda: handle))

    assert asyncio.run(ModalSdkGateway().stats(BACKEND)) == stats


def test_spawn_ping_returns_the_call_id(monkeypatch):
    async def spawn_aio():
        return SimpleNamespace(object_id="fc-pingcall01")

    handle = SimpleNamespace(ping=SimpleNamespace(spawn=SimpleNamespace(aio=spawn_aio)))
    monkeypatch.setattr(modal_gateway.modal.Cls, "from_name", staticmethod(lambda app, cls: lambda: handle))

    assert asyncio.run(ModalSdkGateway().spawn_ping(BACKEND)) == "fc-pingcall01"


def test_spawn_ping_permanent_error_drops_the_cached_handle(monkeypatch):
    async def spawn_aio():
        raise modal.exception.NotFoundError("app qwen21-uc is not deployed")

    handle = SimpleNamespace(ping=SimpleNamespace(spawn=SimpleNamespace(aio=spawn_aio)))
    monkeypatch.setattr(modal_gateway.modal.Cls, "from_name", staticmethod(lambda app, cls: lambda: handle))

    gateway = ModalSdkGateway()
    gateway._handles[BACKEND.id] = handle
    with pytest.raises(modal.exception.NotFoundError):
        asyncio.run(gateway.spawn_ping(BACKEND))
    assert BACKEND.id not in gateway._handles


# ======================================================================================================
# GpuStatus: on-read caches, display_state, handle invalidation
# ======================================================================================================


def test_status_is_computed_on_read_with_ten_and_sixty_second_caches(registry, fake_gateway, conn):
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    status = GpuStatus(fake_gateway, clock=clock)
    fake_gateway.set_stats(backend.id, runners=1)
    fake_gateway.set_app_state(backend.id, "deployed", "ap-1")

    asyncio.run(status.get(backend, conn))
    assert fake_gateway.stats_calls == 1
    assert fake_gateway.app_state_calls == 1

    clock.advance(5)  # inside both TTLs
    asyncio.run(status.get(backend, conn))
    assert (fake_gateway.stats_calls, fake_gateway.app_state_calls) == (1, 1)

    clock.advance(6)  # 11s since the first read: stats stale, app_state still fresh
    asyncio.run(status.get(backend, conn))
    assert (fake_gateway.stats_calls, fake_gateway.app_state_calls) == (2, 1)

    clock.advance(55)  # 66s since the first read: app_state now stale too
    asyncio.run(status.get(backend, conn))
    assert (fake_gateway.stats_calls, fake_gateway.app_state_calls) == (3, 2)


def test_concurrent_readers_share_one_refresh(registry, fake_gateway, conn):
    backend = registry.backends["qwen21-uc"]
    status = GpuStatus(fake_gateway, clock=_Clock())

    async def go():
        await asyncio.gather(*(status.get(backend, conn) for _ in range(5)))

    asyncio.run(go())
    assert fake_gateway.stats_calls == 1
    assert fake_gateway.app_state_calls == 1


def _view(**overrides) -> GpuStatusView:
    base = {
        "backend_id": "qwen21-uc",
        "now": 1000.0,
        "app_state": AppState("deployed", "ap-1"),
        "stats": None,
        "warm_until": None,
        "ping_ok": None,
        "unhealthy": None,
        "error": None,
    }
    return GpuStatusView(**{**base, **overrides})


def test_status_shows_deployed_warm_scaled_to_zero_and_stopped():
    assert display_state(_view()) == "scaled to zero"
    assert display_state(_view(app_state=AppState("stopped", None))) == "stopped"
    assert display_state(_view(warm_until=2000.0, ping_ok=True, stats=FunctionStats(0, 1, 0, 0))) == "warm"
    assert display_state(_view(error="boom")) == "unknown"


def test_changed_app_id_drops_the_cached_handle(registry, fake_gateway, conn):
    backend = registry.backends["qwen21-uc"]
    status = GpuStatus(fake_gateway, clock=_Clock())
    fake_gateway.set_app_state(backend.id, "deployed", "ap-old")

    asyncio.run(status.get(backend, conn, force=True))
    assert ("invalidate", backend.id) not in fake_gateway.calls  # nothing to compare against yet

    fake_gateway.set_app_state(backend.id, "deployed", "ap-new")  # a redeploy after `modal app stop`
    asyncio.run(status.get(backend, conn, force=True))
    assert ("invalidate", backend.id) in fake_gateway.calls


def test_display_state_checks_stopped_before_unhealthy():
    """The documented priority order: "stopped" (the app isn't deployed) outranks "unhealthy" -- a
    recycled, undeployed app must never show a scary red badge for a problem it no longer has."""
    view = _view(app_state=AppState("stopped", None), unhealthy="ComfyUI stopped answering: boom")
    assert display_state(view) == "stopped"


def test_status_shows_stopped_even_when_stats_would_fail(registry, fake_gateway, conn):
    """Once app_state is known to say the app isn't deployed, stats() must be skipped entirely
    -- not merely have its failure suppressed -- so a stopped app never even risks the SDK's
    NotFoundError, and always reads as "stopped", never "unknown"."""
    backend = registry.backends["qwen21-uc"]
    status = GpuStatus(fake_gateway, clock=_Clock())
    fake_gateway.set_app_state(backend.id, "stopped", None)

    async def boom(backend_arg):
        raise AssertionError("stats() must not be called for an undeployed app")

    fake_gateway.stats = boom

    view = asyncio.run(status.get(backend, conn, force=True))

    assert display_state(view) == "stopped"
    assert view.error is None
    assert view.containers == 0


def test_status_shows_unknown_when_stats_fails_on_a_deployed_app(registry, fake_gateway, conn):
    """A stats() failure on a genuinely deployed app (unlike the stopped-app case above) must still
    be caught inside GpuStatus._refresh and surfaced as status.error, never propagate out of get()."""
    backend = registry.backends["qwen21-uc"]
    status = GpuStatus(fake_gateway, clock=_Clock())
    fake_gateway.set_app_state(backend.id, "deployed", f"ap-{backend.id}")

    async def flaky_stats(backend_arg):
        raise RuntimeError("simulated stats RPC error")

    fake_gateway.stats = flaky_stats

    view = asyncio.run(status.get(backend, conn, force=True))  # must not raise

    assert display_state(view) == "unknown"
    assert view.error is not None
    assert "simulated stats RPC error" in view.error


def test_status_shows_unknown_and_negative_caches_a_failing_gateway(registry, fake_gateway, conn):
    """A failing gateway (here, app_state itself) must not be re-attempted by every reader -- one
    real attempt, then the same cached error is served for the rest of the 60s TTL window."""
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    status = GpuStatus(fake_gateway, clock=clock)
    call_count = {"n": 0}

    async def flaky_app_state(backend_arg):
        call_count["n"] += 1
        raise RuntimeError("simulated CLI failure")

    fake_gateway.app_state = flaky_app_state

    view = asyncio.run(status.get(backend, conn))
    assert display_state(view) == "unknown"
    assert view.error is not None
    assert call_count["n"] == 1

    clock.advance(5)  # well inside the 60s app_state TTL: must reuse the cached failure
    view = asyncio.run(status.get(backend, conn))
    assert display_state(view) == "unknown"
    assert call_count["n"] == 1  # no new attempt

    clock.advance(56)  # 61s since the first attempt: the negative-cache window has elapsed
    asyncio.run(status.get(backend, conn))
    assert call_count["n"] == 2


def test_failed_stats_refresh_is_negative_cached_for_the_ttl_window(registry, fake_gateway, conn):
    """The same negative-caching guarantee proven for app_state above must also hold for stats: a
    failing stats() call must not be re-attempted by every reader within its own (10s) TTL."""
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    status = GpuStatus(fake_gateway, clock=clock)
    fake_gateway.set_app_state(backend.id, "deployed", f"ap-{backend.id}")  # so stats is attempted

    call_count = {"n": 0}

    async def flaky_stats(backend_arg):
        call_count["n"] += 1
        raise RuntimeError("simulated stats RPC error")

    fake_gateway.stats = flaky_stats

    asyncio.run(status.get(backend, conn, force=True))
    assert call_count["n"] == 1

    clock.advance(5)  # inside the 10s stats TTL
    asyncio.run(status.get(backend, conn))
    assert call_count["n"] == 1  # no new attempt

    clock.advance(6)  # 11s since the first attempt: due for a retry
    asyncio.run(status.get(backend, conn))
    assert call_count["n"] == 2


def test_a_transient_app_state_failure_keeps_the_last_good_state_during_a_grace_period(
    registry, fake_gateway, conn
):
    """One failed app-state read, right after a prior successful one, must not immediately flash
    "unknown" for up to a full 60s: the last known-good state keeps showing for a short grace
    period while retries (sooner than 60s) are still in flight."""
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    status = GpuStatus(fake_gateway, clock=clock)
    fake_gateway.set_app_state(backend.id, "deployed", f"ap-{backend.id}")
    good_view = asyncio.run(status.get(backend, conn, force=True))  # a good read to start
    assert display_state(good_view) != "unknown"

    call_count = {"n": 0}

    async def flaky(backend_arg):
        call_count["n"] += 1
        raise RuntimeError("simulated CLI failure")

    fake_gateway.app_state = flaky

    clock.advance(61)  # past the normal 60s success TTL: due for a refresh
    view = asyncio.run(status.get(backend, conn))
    assert call_count["n"] == 1
    assert view.error is None  # grace period: still showing the last good state
    assert display_state(view) != "unknown"

    clock.advance(30)  # now well past the grace period
    view = asyncio.run(status.get(backend, conn))
    assert view.error is not None
    assert display_state(view) == "unknown"


def test_a_failed_app_state_read_is_retried_sooner_than_a_successful_one(registry, fake_gateway, conn):
    """A failed read must be retried far sooner than the normal 60s success TTL (around 10s),
    instead of waiting a full minute to recover from a single dropped CLI call."""
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    status = GpuStatus(fake_gateway, clock=clock)
    fake_gateway.set_app_state(backend.id, "deployed", f"ap-{backend.id}")
    asyncio.run(status.get(backend, conn, force=True))

    call_count = {"n": 0}

    async def flaky(backend_arg):
        call_count["n"] += 1
        raise RuntimeError("simulated CLI failure")

    fake_gateway.app_state = flaky

    clock.advance(61)
    asyncio.run(status.get(backend, conn))
    assert call_count["n"] == 1

    clock.advance(11)  # only ~11s later: must retry sooner than the 60s success TTL
    asyncio.run(status.get(backend, conn))
    assert call_count["n"] == 2


# ======================================================================================================
# Warm-up: the pinger, one ping in flight, restart resumption, expiry
# ======================================================================================================


def test_pinger_runs_only_while_a_window_is_open(conn, registry, settings, fake_gateway):
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    gpu.start_warm(conn, backend.id, 5, clock.now, deployed=True)
    conn.commit()
    worker = Worker(settings, registry, fake_gateway, clock=clock, pinger_step_s=0.01)

    async def go():
        worker.ensure_pinger(backend.id)
        assert not worker.pingers[backend.id].done()

        clock.advance(5 * 60 + 1)  # end the window from under the running pinger
        for _ in range(500):
            if backend.id not in worker.pingers:
                break
            await asyncio.sleep(0.01)
        assert backend.id not in worker.pingers

    asyncio.run(go())
    until, ping_id = gpu.warm_state(conn, backend.id)
    assert until is None and ping_id is None


def test_warm_is_shown_only_after_a_successful_ping(conn, registry, settings, fake_gateway):
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    fake_gateway.set_stats(backend.id, runners=1)
    gpu.start_warm(conn, backend.id, 5, clock.now, deployed=True)
    conn.commit()

    asyncio.run(worker._warm_step(backend))  # spawns the first ping immediately
    ping_call_id = worker.ping_calls[backend.id]
    status = asyncio.run(worker.status.get(backend, conn, force=True))
    assert display_state(status) == "warming"  # a ping is in flight, none has succeeded yet

    fake_gateway.finish(ping_call_id, b"ok")
    asyncio.run(worker._warm_step(backend))  # polls it: succeeds
    status = asyncio.run(worker.status.get(backend, conn, force=True))
    assert display_state(status) == "warm"


def test_ping_resolution_forces_a_fresh_stats_read(conn, registry, settings, fake_gateway):
    """When a ping resolves, _warm_step must force the stats cache stale (force_stats), so a page
    open right now sees the new container count immediately instead of waiting out the 10s TTL."""
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    gpu.start_warm(conn, backend.id, 5, clock.now, deployed=True)
    conn.commit()
    asyncio.run(worker._warm_step(backend))  # spawns the ping
    ping_call_id = worker.ping_calls[backend.id]
    asyncio.run(worker.status.get(backend, conn, force=True))  # a fresh stats read, cached for 10s
    stats_calls_before = fake_gateway.stats_calls

    fake_gateway.finish(ping_call_id, b"ok")
    asyncio.run(worker._warm_step(backend))  # resolves the ping: must force a fresh stats read

    asyncio.run(worker.status.get(backend, conn))  # NOT forced, and well within the 10s TTL
    assert fake_gateway.stats_calls == stats_calls_before + 1


def test_running_is_shown_for_containers_outside_a_warm_window(conn, registry, settings, fake_gateway):
    backend = registry.backends["qwen21-uc"]
    fake_gateway.set_stats(backend.id, runners=1)  # a job is running; no warm window at all
    worker = Worker(settings, registry, fake_gateway)

    status = asyncio.run(worker.status.get(backend, conn))
    assert display_state(status) == "running"


def test_warm_pings_immediately_then_every_30_seconds_with_one_in_flight(conn, registry, settings, fake_gateway):
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    gpu.start_warm(conn, backend.id, 15, clock.now, deployed=True)
    conn.commit()

    def ping_spawns():
        return [c for c in fake_gateway.calls if c[0] == "spawn_ping"]

    asyncio.run(worker._warm_step(backend))  # pings immediately
    assert len(ping_spawns()) == 1
    first_call_id = ping_spawns()[0][1]

    clock.advance(5)  # well under 30s: no second ping while one is in flight
    asyncio.run(worker._warm_step(backend))
    assert len(ping_spawns()) == 1

    fake_gateway.finish(first_call_id, b"ok")
    clock.advance(5)  # 10s since the first spawn: still under 30s
    asyncio.run(worker._warm_step(backend))
    assert len(ping_spawns()) == 1

    clock.advance(30)  # 40s since the first spawn: due for a new one
    asyncio.run(worker._warm_step(backend))
    assert len(ping_spawns()) == 2


def test_warm_window_and_ping_resume_after_worker_restart(conn, registry, settings, fake_gateway):
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    fake_gateway.set_stats(backend.id, runners=1)
    gpu.start_warm(conn, backend.id, 15, clock.now, deployed=True)
    conn.commit()
    asyncio.run(worker._warm_step(backend))
    until_before, ping_id_before = gpu.warm_state(conn, backend.id)
    assert ping_id_before is not None

    # A brand-new Worker, same DB, same fake gateway: nothing carried over in memory.
    fresh_worker = Worker(settings, registry, fake_gateway, clock=clock)
    fake_gateway.finish(ping_id_before, b"ok")
    keep_going = asyncio.run(fresh_worker._warm_step(backend))

    assert keep_going is True
    status = asyncio.run(fresh_worker.status.get(backend, conn, force=True))
    assert display_state(status) == "warm"
    until_after, _ = gpu.warm_state(conn, backend.id)
    assert until_after == until_before


def test_expired_window_stops_pings_and_clears_state(conn, registry, settings, fake_gateway):
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    gpu.start_warm(conn, backend.id, 5, clock.now, deployed=True)
    conn.commit()
    asyncio.run(worker._warm_step(backend))
    ping_call_id = worker.ping_calls[backend.id]
    fake_gateway.finish(ping_call_id, b"ok")
    asyncio.run(worker._warm_step(backend))  # resolves the ping, still inside the window

    clock.advance(5 * 60 + 1)
    keep_going = asyncio.run(worker._warm_step(backend))

    assert keep_going is False
    until, ping_id = gpu.warm_state(conn, backend.id)
    assert until is None and ping_id is None


def test_pinger_exits_after_stop_clears_the_window(conn, registry, settings, fake_gateway):
    """A regression here would leak a spinning pinger task: once Stop has cleared warm_until (a
    user-initiated end, not a natural expiry), the very next pinger step must see there is nothing
    left to serve and exit, exactly like a naturally expired window does."""
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock, converge_poll_interval_s=0.01)
    gpu.start_warm(conn, backend.id, 15, clock.now, deployed=True)
    conn.commit()
    asyncio.run(worker._warm_step(backend))  # a pinger step: spawns a ping, window is open

    outcome = asyncio.run(worker.stop_backend(backend, confirm=True))
    assert outcome.kind == "stopped"

    keep_going = asyncio.run(worker._warm_step(backend))

    assert keep_going is False


def test_clear_expired_warm_does_not_erase_a_fresh_window(conn, registry):
    """At the SQL layer: a compare-and-clear must be a no-op (and report so) once the row's
    warm_until no longer matches what the caller observed -- exactly what happens when a warm click
    lands in the gap between the pinger reading a stale `until` and clearing it."""
    backend = registry.backends["qwen21-uc"]
    stale_until = 1_000_000.0
    gpu.start_warm(conn, backend.id, 5, stale_until - 300, deployed=True)  # persists stale_until
    conn.commit()

    fresh_until = 2_000_000.0  # a warm click landed first and extended the window
    conn.execute("UPDATE backend_state SET warm_until = ? WHERE backend_id = ?", (fresh_until, backend.id))
    conn.commit()

    cleared = gpu.clear_expired_warm(conn, backend.id, stale_until)

    assert cleared is False
    until, _ = gpu.warm_state(conn, backend.id)
    assert until == fresh_until  # the fresh window survives untouched


def test_pinger_expiry_never_erases_a_warm_click_that_landed_first(
    conn, registry, settings, fake_gateway, monkeypatch
):
    """End to end: a warm click that extends the window in the gap between the pinger's read
    and its stale-expiry clear must survive -- the pinger must keep going, not report the window as
    ended."""
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    gpu.start_warm(conn, backend.id, 5, clock.now, deployed=True)
    conn.commit()
    clock.advance(5 * 60 + 1)  # the window has now expired

    # Simulates the route's own write landing between the pinger's warm_state() read (which
    # _warm_step already did) and its clear_expired_warm() call: a fresh warm-up lands first.
    real_clear = gpu.clear_expired_warm

    def racing_clear(conn_arg, backend_id, observed_until):
        gpu.start_warm(conn_arg, backend_id, 15, clock.now, deployed=True)
        return real_clear(conn_arg, backend_id, observed_until)

    monkeypatch.setattr(gpu, "clear_expired_warm", racing_clear)
    keep_going = asyncio.run(worker._warm_step(backend))

    assert keep_going is True  # the pinger must not exit: a fresh window is now open
    until, _ = gpu.warm_state(conn, backend.id)
    assert until is not None and until > clock.now  # the new window survived


def test_permanent_spawn_error_clear_is_also_race_safe(conn, registry, settings, fake_gateway, monkeypatch):
    """The same compare-and-clear guard must apply to the OTHER place _warm_step ends a window: a
    PERMANENT spawn_ping error. A race there (a fresh click landing before the guarded clear) must
    also leave the new window untouched, not just the natural-expiry path above."""
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    gpu.start_warm(conn, backend.id, 5, clock.now, deployed=True)
    conn.commit()  # window not yet expired: now < until, so only the spawn-error path is reached

    real_clear = gpu.clear_expired_warm

    def racing_clear(conn_arg, backend_id, observed_until):
        gpu.start_warm(conn_arg, backend_id, 15, clock.now, deployed=True)  # a fresh click lands first
        return real_clear(conn_arg, backend_id, observed_until)

    monkeypatch.setattr(gpu, "clear_expired_warm", racing_clear)
    fake_gateway.raise_on_spawn_ping(modal.exception.NotFoundError("app qwen21-uc is not deployed"))

    keep_going = asyncio.run(worker._warm_step(backend))

    assert keep_going is True  # the fresh window must survive; the pinger keeps going
    until, _ = gpu.warm_state(conn, backend.id)
    assert until is not None and until > clock.now


def test_warm_click_extends_but_never_shortens_an_open_window(conn, registry):
    """Owner decision: warm_until = max(existing, now + minutes); a short click during a long
    window is a no-op on the end time, never a cut."""
    backend = registry.backends["qwen21-uc"]
    now = 1_000_000.0
    long_until = gpu.start_warm(conn, backend.id, 30, now, deployed=True)
    conn.commit()

    short_until = gpu.start_warm(conn, backend.id, 5, now + 60, deployed=True)  # a minute later

    assert short_until == long_until  # unchanged: 5 minutes from now would be earlier than that
    until, _ = gpu.warm_state(conn, backend.id)
    assert until == long_until


def test_new_warm_window_resets_to_warming_even_after_a_previous_successful_ping(
    conn, registry, settings, fake_gateway
):
    """A ping_ok left over from an earlier window must never make a brand new window read as
    instantly warm: ensure_pinger() must reset it, since the new window has no successful ping of
    its own yet."""
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock, pinger_step_s=0.01)
    fake_gateway.set_stats(backend.id, runners=1)
    gpu.start_warm(conn, backend.id, 5, clock.now, deployed=True)
    conn.commit()
    asyncio.run(worker._warm_step(backend))  # spawns the first ping
    ping_call_id = worker.ping_calls[backend.id]
    fake_gateway.finish(ping_call_id, b"ok")
    asyncio.run(worker._warm_step(backend))  # resolves it: ping_ok is now True
    status = asyncio.run(worker.status.get(backend, conn, force=True))
    assert display_state(status) == "warm"

    clock.advance(5 * 60 + 1)  # the window ends
    asyncio.run(worker._warm_step(backend))
    until, _ = gpu.warm_state(conn, backend.id)
    assert until is None  # confirms the window really ended

    gpu.start_warm(conn, backend.id, 5, clock.now, deployed=True)  # a brand new window
    conn.commit()

    async def go():
        worker.ensure_pinger(backend.id)  # must reset ping_ok for the new window; needs a running
        # loop (it schedules a task), but nothing has to actually execute for reset_ping's effect
        # to already be visible in the very next status read.
        return await worker.status.get(backend, conn, force=True)

    status = asyncio.run(go())
    assert display_state(status) == "warming"  # not "warm": no ping has succeeded in THIS window


# ======================================================================================================
# Stop: confirmation, gather-cancel order, convergence, restart, locking
# ======================================================================================================


def test_stop_requires_confirmation_when_jobs_are_active(conn, registry, settings, fake_gateway, rng):
    backend = registry.backends["qwen21-uc"]
    jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=2), rng)
    conn.commit()
    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())

    outcome = asyncio.run(worker.stop_backend(backend, confirm=False))

    assert outcome.kind == "needs_confirmation"
    assert (outcome.queued, outcome.running) == (0, 2)
    assert fake_gateway.calls == []  # nothing touched
    assert {row["status"] for row in conn.execute("SELECT status FROM jobs")} == {"submitted"}


def test_stop_cancels_every_call_at_once_ping_first_then_stops_containers(conn, registry, settings, fake_gateway, rng):
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock, converge_poll_interval_s=0.01)
    gpu.start_warm(conn, backend.id, 5, clock.now, deployed=True)
    conn.commit()
    asyncio.run(worker._warm_step(backend))
    ping_call_id = worker.ping_calls[backend.id]

    jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=2), rng)
    conn.commit()
    asyncio.run(worker.dispatch_once())
    job_call_ids = {row["call_id"] for row in conn.execute("SELECT call_id FROM jobs")}

    outcome = asyncio.run(worker.stop_backend(backend, confirm=True))

    assert outcome.kind == "stopped"
    actions = [c[0] for c in fake_gateway.calls]
    cancel_positions = [i for i, a in enumerate(actions) if a == "cancel"]
    stop_container_positions = [i for i, a in enumerate(actions) if a == "stop_containers"]
    assert fake_gateway.calls[cancel_positions[0]] == ("cancel", ping_call_id)  # ping first
    assert {fake_gateway.calls[i][1] for i in cancel_positions[1:]} == job_call_ids
    assert max(cancel_positions) < min(stop_container_positions)  # every cancel before any stop

    assert {row["status"] for row in conn.execute("SELECT status FROM jobs")} == {"cancelled"}
    until, ping_id = gpu.warm_state(conn, backend.id)
    assert until is None and ping_id is None


def test_stop_converges_until_runners_and_backlog_are_zero(conn, registry, settings, fake_gateway):
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway, converge_poll_interval_s=0.01)
    fake_gateway.queue_stats(backend.id, runners=1, backlog=1)  # 1st check: not converged
    fake_gateway.queue_stats(backend.id, runners=1, backlog=0)  # 2nd check: still not converged
    fake_gateway.set_stats(backend.id, runners=0, backlog=0)  # 3rd and later: converged

    outcome = asyncio.run(worker.stop_backend(backend, confirm=True))

    assert outcome.kind == "stopped"
    assert fake_gateway.stats_calls == 3


def test_stop_reuses_the_app_state_read_across_stop_containers(conn, registry, settings, fake_gateway):
    """One convergence pass must do one app-state read, not two: the pre-fetched state from the
    pass's own app_state() check is passed into stop_containers instead of it re-fetching its own
    (which, on the real gateway, means a second `modal app list` per iteration)."""
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway, converge_poll_interval_s=0.01)

    outcome = asyncio.run(worker.stop_backend(backend, confirm=True))

    assert outcome.kind == "stopped"  # converges immediately: exactly one pass
    assert fake_gateway.app_state_calls == 1
    assert len(fake_gateway.stop_containers_app_state_args) == 1
    passed_state = fake_gateway.stop_containers_app_state_args[0]
    assert passed_state is not None
    assert passed_state.state == "deployed"


def test_stop_reports_not_converged_after_the_deadline(conn, registry, settings, fake_gateway):
    backend = registry.backends["qwen21-uc"]
    # Every self.clock() call (the deadline, each bounded step's remaining budget, and the deadline
    # check) advances by 20s: after just one full loop iteration the deadline is already behind.
    clock = _FastForwardClock(step=20.0)
    worker = Worker(settings, registry, fake_gateway, clock=clock, converge_poll_interval_s=0.01)
    fake_gateway.set_stats(backend.id, runners=1, backlog=0)  # never converges

    outcome = asyncio.run(worker.stop_backend(backend, confirm=True))

    assert outcome.kind == "not_converged"
    assert outcome.stats.num_total_runners == 1


def test_cancel_once_is_bounded_by_the_deadline_it_is_given(registry, settings, fake_gateway):
    """The per-attempt cancel timeout must be derived from whatever of the deadline remains, not a
    fixed constant independent of it -- otherwise a slow cancel phase could run on top of, rather
    than inside, the whole sequence's shared budget."""
    worker = Worker(settings, registry, fake_gateway)

    async def hang(call_id, *, terminate_containers=False):
        await asyncio.sleep(30)

    fake_gateway.cancel = hang

    async def go():
        start = asyncio.get_running_loop().time()
        failed = await worker._cancel_once("qwen21-uc", ["fake-call-1"], worker.clock() + 0.3)
        elapsed = asyncio.get_running_loop().time() - start
        return failed, elapsed

    failed, elapsed = asyncio.run(go())

    assert failed == ["fake-call-1"]
    assert elapsed < 2.0  # bounded by the ~0.3s remaining budget (floored at 0.5s), not a fixed 10s


def test_stop_computes_the_deadline_before_the_cancel_phase(conn, registry, settings, fake_gateway, rng):
    """The deadline must exist before cancelling starts, so a slow cancel phase eats into the SAME
    overall budget as convergence, instead of running before it and adding on top -- keeping the
    whole sequence's worst case well under Cloudflare's roughly 100s edge timeout."""
    backend = registry.backends["qwen21-uc"]
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    # Every self.clock() call advances by 61s: the deadline (clock()+60), computed first, is
    # already behind by the time anything after it runs, if it truly is computed up front.
    clock = _FastForwardClock(step=61.0)
    worker = Worker(settings, registry, fake_gateway, clock=clock, converge_poll_interval_s=0.01)
    asyncio.run(worker.dispatch_once())

    async def hang(call_id, *, terminate_containers=False):
        await asyncio.sleep(30)

    fake_gateway.cancel = hang

    async def go():
        start = asyncio.get_running_loop().time()
        outcome = await worker.stop_backend(backend, confirm=True)
        elapsed = asyncio.get_running_loop().time() - start
        return outcome, elapsed

    outcome, elapsed = asyncio.run(go())

    assert elapsed < 5.0  # the whole sequence, cancel included, must not hang anywhere near 30s
    assert outcome.kind in ("stopped", "not_converged")


def test_bounded_step_reports_a_readable_timeout_message(registry, settings, fake_gateway):
    """A timed-out step must show a readable reason, never an empty message: a bare TimeoutError's
    str() is '', so `f"{label}: {exc}"` alone would render as "some_step: " with nothing after."""
    worker = Worker(settings, registry, fake_gateway)

    async def hang():
        await asyncio.sleep(5)

    async def go():
        return await worker._bounded_step("qwen21-uc", worker.clock() + 0.3, "some_step", hang)

    result, error = asyncio.run(go())

    assert result is None
    assert error.startswith("some_step: timed out after")
    assert error != "some_step: "


def test_bounded_step_reports_a_readable_message_for_a_non_timeout_failure(registry, settings, fake_gateway):
    """The same empty-message risk applies to any exception whose str() is empty, not just a
    timeout -- the label must still be paired with a non-empty reason."""
    worker = Worker(settings, registry, fake_gateway)

    async def boom():
        raise RuntimeError()  # deliberately no message: str(RuntimeError()) == ""

    async def go():
        return await worker._bounded_step("qwen21-uc", worker.clock() + 30, "some_step", boom)

    result, error = asyncio.run(go())

    assert result is None
    assert error == "some_step: RuntimeError"


def test_stop_bounds_a_hanging_step_at_the_deadline(conn, registry, settings, fake_gateway):
    """A step that never returns (e.g. a wedged Modal RPC) must still be cut off at the deadline,
    not hang for the caller's full internal timeout or forever."""
    backend = registry.backends["qwen21-uc"]
    clock = _FastForwardClock(step=90.0)  # the deadline (clock()+60) is already behind by the time
    # any step actually runs, so every step's own remaining-budget timeout floors at 0.5s
    worker = Worker(settings, registry, fake_gateway, clock=clock, converge_poll_interval_s=0.01)

    async def hang(backend_arg):
        await asyncio.sleep(30)  # far longer than the 0.5s floor _bounded_step will actually wait

    fake_gateway.stats = hang

    async def go():
        start = asyncio.get_running_loop().time()
        outcome = await worker.stop_backend(backend, confirm=True)
        elapsed = asyncio.get_running_loop().time() - start
        return outcome, elapsed

    outcome, elapsed = asyncio.run(go())

    assert elapsed < 5.0  # cut off at the deadline, not left hanging for anywhere near 30s
    assert outcome.kind == "not_converged"
    assert "timed out after" in (outcome.error or "")


def test_stop_step_timeout_caps_each_step_even_with_plenty_of_budget_left(registry, settings, fake_gateway, monkeypatch):
    """_STOP_STEP_TIMEOUT_S must actually be applied (as min(_STOP_STEP_TIMEOUT_S, remaining)),
    capping one step even when the overall deadline is still far away -- not dead code, and not
    only a fallback for when the deadline is already close. Spies on asyncio.timeout's own argument
    instead of actually waiting it out, so this stays a fast, deterministic unit test."""
    worker = Worker(settings, registry, fake_gateway)
    far_deadline = worker.clock() + 10_000  # nowhere near expiring: only the per-step cap applies

    captured: dict[str, float] = {}
    real_timeout = asyncio.timeout

    def spy_timeout(delay):
        captured["delay"] = delay
        return real_timeout(delay)

    monkeypatch.setattr(worker_module.asyncio, "timeout", spy_timeout)

    async def quick():
        return "ok"

    result, error = asyncio.run(worker._bounded_step("qwen21-uc", far_deadline, "some_step", quick))

    assert result == "ok"
    assert error is None
    assert captured["delay"] == pytest.approx(_STOP_STEP_TIMEOUT_S)


def test_stop_reports_a_failed_outcome_instead_of_raising(conn, registry, settings, fake_gateway, monkeypatch):
    """Any unexpected exception inside the sequence (not just a per-step Modal failure) must
    become a StopOutcome, never propagate as a 500."""
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway, converge_poll_interval_s=0.01)

    def broken_active_counts(*args, **kwargs):
        raise RuntimeError("simulated database fault")

    monkeypatch.setattr(jobs, "active_counts", broken_active_counts)
    outcome = asyncio.run(worker.stop_backend(backend, confirm=True))

    assert outcome.kind == "failed"
    assert "simulated database fault" in outcome.error


def test_stop_backend_never_raises_when_stop_containers_fails(conn, registry, settings, fake_gateway):
    """A stop_containers failure (e.g. a CLI timeout, or a real stop failure) must not end Stop
    with an exception -- it is caught, logged, and retried on the next convergence tick."""
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway, converge_poll_interval_s=0.01)
    fake_gateway.queue_stats(backend.id, runners=1, backlog=0)  # 1st tick: not converged yet

    calls = {"n": 0}
    real_stop_containers = fake_gateway.stop_containers

    async def flaky_stop_containers(backend_arg, *, app_state=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated stop_containers failure")
        return await real_stop_containers(backend_arg, app_state=app_state)

    fake_gateway.stop_containers = flaky_stop_containers

    outcome = asyncio.run(worker.stop_backend(backend, confirm=True))

    assert outcome.kind == "stopped"  # the second convergence tick succeeds
    assert calls["n"] >= 2


def test_stop_backend_never_raises_when_stats_fails_on_a_deployed_app(conn, registry, settings, fake_gateway):
    """stats() can also raise (a transient RPC error) on an app that IS deployed; Stop must keep
    retrying instead of ending with an exception."""
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway, converge_poll_interval_s=0.01)

    calls = {"n": 0}
    real_stats = fake_gateway.stats

    async def flaky_stats(backend_arg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient stats RPC error")
        return await real_stats(backend_arg)

    fake_gateway.stats = flaky_stats

    outcome = asyncio.run(worker.stop_backend(backend, confirm=True))

    assert outcome.kind == "stopped"
    assert calls["n"] >= 2


def test_stop_on_a_stopped_app_converges_at_once_without_calling_stats(conn, registry, settings, fake_gateway):
    """Pressing Stop on an already-stopped (or never deployed) app must not call stats() at all --
    nothing can be running -- and must report "stopped" immediately, never raising along the way."""
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway, converge_poll_interval_s=0.01)
    fake_gateway.set_app_state(backend.id, "stopped", None)

    outcome = asyncio.run(worker.stop_backend(backend, confirm=True))

    assert outcome.kind == "stopped"
    assert fake_gateway.stats_calls == 0


def test_stop_cancels_queued_jobs_too(conn, registry, settings, fake_gateway, rng):
    """A queued job (never dispatched, no call_id yet) must also end up cancelled: otherwise the
    dispatcher would spawn it moments later and cold-start the backend Stop just stopped."""
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway, converge_poll_interval_s=0.01)
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "queued"

    outcome = asyncio.run(worker.stop_backend(backend, confirm=True))

    assert outcome.kind == "stopped"
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "cancelled"


def test_stop_requires_confirmation_for_queued_only_jobs(conn, registry, settings, fake_gateway, rng):
    """The confirmation prompt must count queued jobs too, not just running (submitted) ones."""
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway, converge_poll_interval_s=0.01)
    jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=3), rng)
    conn.commit()  # nothing dispatched yet: every job is 'queued', none 'submitted'

    outcome = asyncio.run(worker.stop_backend(backend, confirm=False))

    assert outcome.kind == "needs_confirmation"
    assert (outcome.queued, outcome.running) == (3, 0)
    assert {row["status"] for row in conn.execute("SELECT status FROM jobs")} == {"queued"}


def test_queued_jobs_null_call_ids_never_reach_modal_cancel(conn, registry, settings, fake_gateway, rng):
    """cancel_all_for_backend's RETURNING call_id includes a NULL for every queued job (it never had
    one); those NULLs must be filtered out before Stop gathers Modal cancels, or a fake/lenient
    gateway would accept `cancel(None)` and, on the real one, would show bogus failure counts."""
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway, converge_poll_interval_s=0.01)
    jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=2), rng)
    conn.commit()  # both queued, no call_id yet

    outcome = asyncio.run(worker.stop_backend(backend, confirm=True))

    assert outcome.kind == "stopped"
    assert outcome.cancel_failures == 0
    cancel_call_ids = [c[1] for c in fake_gateway.calls if c[0] == "cancel"]
    assert None not in cancel_call_ids
    assert cancel_call_ids == []  # nothing was ever submitted, so nothing to cancel on Modal


def test_stop_retries_failed_cancels_and_reports_the_count(conn, registry, settings, fake_gateway, rng):
    """A Modal cancel that fails is logged and retried once; a call that still fails after the
    retry is reported in the outcome, not silently dropped."""
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway, converge_poll_interval_s=0.01)
    jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=2), rng)
    conn.commit()
    asyncio.run(worker.dispatch_once())
    call_ids = [row["call_id"] for row in conn.execute("SELECT call_id FROM jobs")]

    attempts: dict[str, int] = {}
    real_cancel = fake_gateway.cancel

    async def flaky_cancel(call_id, *, terminate_containers=False):
        attempts[call_id] = attempts.get(call_id, 0) + 1
        if call_id == call_ids[0]:
            raise RuntimeError("simulated cancel failure")
        return await real_cancel(call_id, terminate_containers=terminate_containers)

    fake_gateway.cancel = flaky_cancel

    outcome = asyncio.run(worker.stop_backend(backend, confirm=True))

    assert outcome.kind == "stopped"
    assert outcome.cancel_failures == 1  # still failing after the retry
    assert attempts[call_ids[0]] == 2  # the original attempt plus exactly one retry
    assert attempts[call_ids[1]] == 1  # the healthy call was never retried


def test_stop_after_restart_cancels_the_persisted_ping(conn, registry, settings, fake_gateway):
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock, converge_poll_interval_s=0.01)
    gpu.start_warm(conn, backend.id, 5, clock.now, deployed=True)
    conn.commit()
    asyncio.run(worker._warm_step(backend))
    ping_call_id_before = worker.ping_calls[backend.id]

    fresh_worker = Worker(settings, registry, fake_gateway, clock=clock, converge_poll_interval_s=0.01)
    assert fresh_worker.ping_calls == {}  # simulates a restart: nothing carried over in memory

    outcome = asyncio.run(fresh_worker.stop_backend(backend, confirm=True))

    assert outcome.kind == "stopped"
    assert ("cancel", ping_call_id_before) in fake_gateway.calls
    until, ping_id = gpu.warm_state(conn, backend.id)
    assert until is None and ping_id is None


def test_stop_waits_for_a_slow_ping_spawn_in_flight(conn, registry, settings, fake_gateway):
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock, converge_poll_interval_s=0.01)
    gpu.start_warm(conn, backend.id, 5, clock.now, deployed=True)
    conn.commit()

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_spawn():
        started.set()
        await release.wait()

    fake_gateway.spawn_ping_hook = slow_spawn

    async def pinger_like_step():
        async with worker.locks[backend.id]:  # exactly what _pinger does around _warm_step
            await worker._warm_step(backend)

    async def go():
        pinger_task = asyncio.create_task(pinger_like_step())
        await started.wait()  # the pinger is now inside spawn_ping, holding the lock

        stop_task = asyncio.create_task(worker.stop_backend(backend, confirm=True))
        await asyncio.sleep(0.05)
        assert not stop_task.done()  # Stop is blocked behind the same per-backend lock

        release.set()
        await pinger_task
        return await stop_task

    outcome = asyncio.run(go())
    assert outcome.kind == "stopped"


def test_dispatcher_spawns_nothing_while_stop_runs(conn, registry, settings, fake_gateway, rng):
    """A real, concurrently-running stop_backend must not stall dispatch_once() -- the dispatcher
    skips a backend whose lock is held instead of waiting for it -- and dispatch_once()
    must still complete its tick (so /healthz's staleness check never sees it stop ticking)."""
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway, converge_poll_interval_s=0.01)

    started = asyncio.Event()
    release = asyncio.Event()
    real_stats = fake_gateway.stats

    async def slow_stats(backend_arg):
        started.set()  # signals that stop_backend is now mid-convergence, holding the lock
        await release.wait()
        return await real_stats(backend_arg)

    fake_gateway.stats = slow_stats

    async def go():
        stop_task = asyncio.create_task(worker.stop_backend(backend, confirm=True))
        await started.wait()

        # A batch becomes queued while Stop still holds locks[backend.id].
        jobs.create_batch(conn, registry, settings, _request(), rng)
        conn.commit()

        dispatch_task = asyncio.create_task(worker.dispatch_once())
        await asyncio.wait_for(dispatch_task, timeout=1.0)  # must complete quickly, not block
        assert not stop_task.done()  # Stop itself is still running, mid-convergence

        release.set()
        return await stop_task

    outcome = asyncio.run(go())

    assert outcome.kind == "stopped"
    assert worker.last_dispatch_tick is not None  # the tick completed despite the lock being held
    row = conn.execute("SELECT status FROM jobs").fetchone()
    assert row["status"] == "queued"  # never spawned while Stop held the backend's lock


# ======================================================================================================
# Circuit breaker
# ======================================================================================================


def test_circuit_breaker_recycles_after_three_instant_comfyui_failures(conn, registry, settings, fake_gateway, rng):
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway, converge_poll_interval_s=0.01)

    async def go():
        for _ in range(3):
            jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=1), rng)
            conn.commit()
            await worker.dispatch_once()
            job = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            fake_gateway.fail(job["call_id"], "Connection refused: could not reach ComfyUI")
            await worker.poll_once()  # an instant failure: within one poll interval of submission

        assert len(worker._recycle_tasks) == 1  # stop_backend scheduled exactly once
        await asyncio.gather(*worker._recycle_tasks)  # let the scheduled recycle actually run
        return await worker.status.get(backend, conn, force=True)

    status = asyncio.run(go())
    assert status.unhealthy is not None
    assert display_state(status) == "unhealthy"


def test_recycle_task_result_is_logged(conn, registry, settings, fake_gateway, rng, caplog):
    """The breaker's scheduled recycle runs unobserved (nothing awaits it directly): its outcome
    must be logged via a done callback, or a failing recycle would be invisible except as asyncio's
    own "exception was never retrieved" warning at garbage-collection time."""
    worker = Worker(settings, registry, fake_gateway, converge_poll_interval_s=0.01)

    async def go():
        for _ in range(3):
            jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=1), rng)
            conn.commit()
            await worker.dispatch_once()
            job = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            fake_gateway.fail(job["call_id"], "Connection refused: could not reach ComfyUI")
            await worker.poll_once()
        await asyncio.gather(*worker._recycle_tasks)

    with caplog.at_level("INFO", logger="artio.worker"):
        asyncio.run(go())

    assert any("scheduled backend recycle" in record.message for record in caplog.records)


def test_failed_ping_marks_unhealthy_and_feeds_the_breaker(conn, registry, settings, fake_gateway):
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    gpu.start_warm(conn, backend.id, 5, clock.now, deployed=True)
    conn.commit()

    asyncio.run(worker._warm_step(backend))
    ping_call_id = worker.ping_calls[backend.id]
    fake_gateway.fail(ping_call_id, "ComfyUI is not answering: /system_stats returned HTTP 500")
    asyncio.run(worker._warm_step(backend))

    status = asyncio.run(worker.status.get(backend, conn, force=True))
    assert status.unhealthy is not None
    assert display_state(status) == "unhealthy"
    assert worker._breaker[backend.id] == 1


def test_breaker_resets_on_success_or_other_failure(conn, registry, settings, fake_gateway, rng):
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway)

    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs").fetchone()
    fake_gateway.fail(job["call_id"], "Connection refused: could not reach ComfyUI")
    asyncio.run(worker.poll_once())
    assert worker._breaker[backend.id] == 1

    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    asyncio.run(worker.dispatch_once())
    job2 = conn.execute("SELECT * FROM jobs WHERE id != ?", (job["id"],)).fetchone()
    fake_gateway.fail(job2["call_id"], "ComfyUI rejected workflow: bad node")  # doesn't match: resets
    asyncio.run(worker.poll_once())
    assert worker._breaker[backend.id] == 0


def test_breaker_does_not_recycle_before_three_failures(conn, registry, settings, fake_gateway, rng):
    """The threshold is exactly 3: two instant ComfyUI-down failures must not yet mark the backend
    unhealthy or schedule a recycle."""
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway)

    for _ in range(2):
        jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=1), rng)
        conn.commit()
        asyncio.run(worker.dispatch_once())
        job = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        fake_gateway.fail(job["call_id"], "Connection refused: could not reach ComfyUI")
        asyncio.run(worker.poll_once())

    assert worker._breaker[backend.id] == 2
    assert worker._recycle_tasks == []
    status = asyncio.run(worker.status.get(backend, conn, force=True))
    assert status.unhealthy is None


def test_successful_ping_resets_the_breaker_counter(conn, registry, settings, fake_gateway):
    """A successful ping resets the breaker counter, same as a successful job: two failed pings
    (below the threshold) followed by one successful ping must bring the count back to zero."""
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock)
    gpu.start_warm(conn, backend.id, 30, clock.now, deployed=True)
    conn.commit()

    for _ in range(2):
        asyncio.run(worker._warm_step(backend))  # spawns a ping
        ping_call_id = worker.ping_calls[backend.id]
        fake_gateway.fail(ping_call_id, "ComfyUI is not answering: HTTP 500")
        asyncio.run(worker._warm_step(backend))  # polls it: fails
        clock.advance(PING_INTERVAL_S)

    assert worker._breaker[backend.id] == 2

    asyncio.run(worker._warm_step(backend))  # spawns another ping
    ping_call_id = worker.ping_calls[backend.id]
    fake_gateway.finish(ping_call_id, b"ok")
    asyncio.run(worker._warm_step(backend))  # polls it: succeeds

    assert worker._breaker[backend.id] == 0


def test_breaker_ignores_a_late_comfyui_down_failure(conn, registry, settings, fake_gateway, rng):
    """Only a failure noticed within one poll interval of submission counts: the same ComfyUI-down
    text seen on a LATER poll (the render was genuinely running for a while first) must not count
    toward the breaker at all -- a job that ran a long time before failing isn't "ComfyUI never
    answered", so counting it would trip the breaker for an unrelated, one-off failure."""
    backend = registry.backends["qwen21-uc"]
    clock = _Clock()
    worker = Worker(settings, registry, fake_gateway, clock=clock, poll_interval_s=2.0)
    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs").fetchone()

    clock.advance(10.0)  # well past one poll interval since submission
    fake_gateway.fail(job["call_id"], "Connection refused: could not reach ComfyUI")
    asyncio.run(worker.poll_once())

    assert worker._breaker[backend.id] == 0  # a late failure resets it, exactly like a non-match


def test_successful_job_clears_the_unhealthy_notice(conn, registry, settings, fake_gateway, rng, png_bytes):
    """A successful job render clears both the breaker counter and a standing unhealthy notice --
    a finished render proves ComfyUI works, even outside a warm window where nothing else would
    clear it."""
    backend = registry.backends["qwen21-uc"]
    worker = Worker(settings, registry, fake_gateway)
    worker.status.set_unhealthy(backend.id, "ComfyUI stopped answering: some earlier failure")
    worker._breaker[backend.id] = 2  # below threshold, but must still reset on success

    jobs.create_batch(conn, registry, settings, _request(), rng)
    conn.commit()
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs").fetchone()
    fake_gateway.finish(job["call_id"], png_bytes)
    asyncio.run(worker.poll_once())

    assert worker._breaker[backend.id] == 0
    status = asyncio.run(worker.status.get(backend, conn, force=True))
    assert status.unhealthy is None


# ======================================================================================================
# Routes: /gpu, /gpu/panel, warm, stop, confirmation, the header badge -- all owner-only, always 200
# ======================================================================================================


def test_gpu_page_and_panel_are_owner_only_and_200(app_client, owner_headers, service_headers):
    assert app_client.get("/gpu", headers=owner_headers).status_code == 200
    assert app_client.get("/gpu/panel", headers=owner_headers).status_code == 200
    assert app_client.get("/gpu", headers=service_headers).status_code == 403
    assert app_client.get("/gpu/panel", headers=service_headers).status_code == 403


def test_warm_and_stop_routes_are_owner_only(app_client, service_headers):
    assert app_client.post("/gpu/qwen21-uc/warm", headers=service_headers, data={"minutes": "5"}).status_code == 403
    assert app_client.post("/gpu/qwen21-uc/stop", headers=service_headers).status_code == 403


def test_warm_route_starts_a_window(app_client, owner_headers, settings):
    response = app_client.post("/gpu/qwen21-uc/warm", headers=owner_headers, data={"minutes": "5"})
    assert response.status_code == 200
    with db.session(settings) as conn:
        until, _ = gpu.warm_state(conn, "qwen21-uc")
    assert until is not None and until > time.time()


def test_warm_route_rejects_an_unknown_minutes_value(app_client, owner_headers):
    response = app_client.post("/gpu/qwen21-uc/warm", headers=owner_headers, data={"minutes": "7"})
    assert response.status_code == 200
    assert "must be one of" in response.text


def test_warm_route_rejects_a_non_integer_minutes_value(app_client, owner_headers):
    response = app_client.post("/gpu/qwen21-uc/warm", headers=owner_headers, data={"minutes": "soon"})
    assert response.status_code == 200
    assert "whole number" in response.text


def test_a_refusal_keeps_the_whole_panel_with_its_buttons_and_poll(app_client, owner_headers, fake_gateway):
    """The GPU forms swap #gpu-panel itself, so a refusal must come back as the panel with the
    message inside it: a bare message would remove the panel, its buttons and its 10 s poll."""
    fake_gateway.set_app_state("qwen21-uc", "stopped", None)

    response = app_client.post("/gpu/qwen21-uc/warm", headers=owner_headers, data={"minutes": "5"})

    assert response.status_code == 200
    assert "is stopped: deploy it before warming it up." in response.text
    assert 'id="gpu-panel"' in response.text
    assert 'hx-trigger="every 10s"' in response.text
    assert "Warm 5 min" in response.text and ">Stop<" in response.text.replace(" ", "")


def test_unknown_backend_in_warm_route_returns_200_with_a_message(app_client, owner_headers):
    response = app_client.post("/gpu/not-a-backend/warm", headers=owner_headers, data={"minutes": "5"})
    assert response.status_code == 200
    assert "Unknown backend" in response.text


def test_stop_route_requires_confirmation_then_stops(app_client, owner_headers, fake_gateway, settings, registry, rng):
    with db.session(settings) as conn:
        jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=2), rng)
        conn.commit()
    worker = app_client.app.state.worker
    asyncio.run(worker.dispatch_once())

    needs_confirm = app_client.post("/gpu/qwen21-uc/stop", headers=owner_headers)
    assert needs_confirm.status_code == 200
    assert "queued" in needs_confirm.text.lower() and "running" in needs_confirm.text.lower()

    confirmed = app_client.post("/gpu/qwen21-uc/stop", headers=owner_headers, data={"confirm": "1"})
    assert confirmed.status_code == 200
    with db.session(settings) as conn:
        assert {row["status"] for row in conn.execute("SELECT status FROM jobs")} == {"cancelled"}


def test_header_status_shows_the_gpu_badge(app_client, owner_headers):
    response = app_client.get("/partials/header-status", headers=owner_headers)
    assert response.status_code == 200
    assert "/gpu" in response.text
    assert "scaled to zero" in response.text


def _seed_expired_warm_row(settings, backend_id: str) -> None:
    """Writes a `warm_until` in the past directly, bypassing start_warm/the pinger/startup-clear,
    to prove the TEMPLATES themselves gate on window_open rather than merely on warm_until being
    set -- regardless of how a stale row like this could come to exist."""
    with db.session(settings) as conn:
        conn.execute(
            "INSERT INTO backend_state (backend_id, warm_until) VALUES (?, ?) "
            "ON CONFLICT(backend_id) DO UPDATE SET warm_until = excluded.warm_until",
            (backend_id, time.time() - 100),
        )
        conn.commit()


def test_panel_hides_warm_until_for_an_expired_window(app_client, owner_headers, settings):
    _seed_expired_warm_row(settings, "qwen21-uc")

    response = app_client.get("/gpu/panel", headers=owner_headers)

    assert response.status_code == 200
    assert "warm until" not in response.text


def test_header_hides_warm_until_for_an_expired_window(app_client, owner_headers, settings):
    _seed_expired_warm_row(settings, "qwen21-uc")

    response = app_client.get("/partials/header-status", headers=owner_headers)

    assert response.status_code == 200
    assert "warm until" not in response.text


def test_panel_shows_the_status_error(app_client, owner_headers, fake_gateway):
    async def boom(backend_arg):
        raise RuntimeError("simulated outage for display")

    fake_gateway.app_state = boom

    response = app_client.get("/gpu/panel", headers=owner_headers)

    assert response.status_code == 200
    assert "simulated outage for display" in response.text


def test_panel_shows_cancel_failures(app_client, owner_headers, fake_gateway, settings, registry, rng):
    with db.session(settings) as conn:
        jobs.create_batch(conn, registry, settings, _request(seed_mode="random", count=1), rng)
        conn.commit()
    worker = app_client.app.state.worker
    asyncio.run(worker.dispatch_once())

    async def always_fail_cancel(call_id, *, terminate_containers=False):
        raise RuntimeError("simulated cancel failure")

    fake_gateway.cancel = always_fail_cancel

    response = app_client.post("/gpu/qwen21-uc/stop", headers=owner_headers, data={"confirm": "1"})

    assert response.status_code == 200
    assert "Modal cancel(s) failed" in response.text


def test_gpu_panel_has_no_self_load_trigger_and_carries_hx_sync(app_client, owner_headers):
    """The panel's own polling trigger must never include "load" (which would fire again on every
    outerHTML swap, polling in a tight loop), and both it and its forms must carry hx-sync so a
    POST's response is never dropped by a concurrent poll swap landing on the same target first."""
    response = app_client.get("/gpu/panel", headers=owner_headers)
    assert response.status_code == 200
    text = response.text

    panel_start = text.index('id="gpu-panel"')
    panel_tag = text[panel_start : text.index(">", panel_start)]
    assert 'hx-trigger="every 10s"' in panel_tag
    assert "load" not in panel_tag  # never re-fires itself on its own swap
    assert 'hx-sync="this:drop"' in panel_tag

    assert text.count('hx-sync="closest #gpu-panel:replace"') >= 2  # both the warm and stop forms


def test_last_stop_outcome_appears_on_a_later_panel_read(app_client, owner_headers):
    """The outcome must still be visible on a later, independent panel read -- not just the one
    HTML response returned in the same request as the POST -- since the panel's own poll and the
    POST race for the same #gpu-panel target and either could "win" the visible DOM swap."""
    stop_response = app_client.post("/gpu/qwen21-uc/stop", headers=owner_headers)
    assert stop_response.status_code == 200
    assert "Stopped" in stop_response.text

    later_response = app_client.get("/gpu/panel", headers=owner_headers)
    assert later_response.status_code == 200
    assert "Stopped" in later_response.text


def test_warm_route_uses_a_forced_read_not_a_stale_cache(app_client, owner_headers, fake_gateway, settings, registry):
    """Dropping force=True from the warm route's status read would let it trust an up-to-60s-stale
    "stopped" reading and wrongly refuse a backend that is, right now, actually deployed."""
    backend = registry.backends["qwen21-uc"]
    fake_gateway.set_app_state(backend.id, "stopped", None)
    worker = app_client.app.state.worker
    with db.session(settings) as conn:
        asyncio.run(worker.status.get(backend, conn))  # populates the 60s cache as "stopped"

    fake_gateway.set_app_state(backend.id, "deployed", f"ap-{backend.id}")  # now actually deployed

    response = app_client.post("/gpu/qwen21-uc/warm", headers=owner_headers, data={"minutes": "5"})

    assert response.status_code == 200
    assert "is stopped" not in response.text  # would appear only if the stale cache were trusted
    with db.session(settings) as conn:
        until, _ = gpu.warm_state(conn, backend.id)
    assert until is not None


def test_warm_route_refuses_when_status_is_unavailable(app_client, owner_headers, fake_gateway):
    """If the forced refresh itself fails, the route must not fall back to trusting a stale (or
    never-populated) cached "deployed" reading: an unknown status must refuse with the real
    reason, not silently let the click through or claim the backend is stopped when it might not be."""

    async def boom(backend_arg):
        raise RuntimeError("simulated CLI outage")

    fake_gateway.app_state = boom

    response = app_client.post("/gpu/qwen21-uc/warm", headers=owner_headers, data={"minutes": "5"})

    assert response.status_code == 200
    assert "Status unavailable" in response.text


def test_stop_route_returns_200_when_stop_backend_reports_a_failed_outcome(app_client, owner_headers, monkeypatch):
    """At the route layer: whatever kind of StopOutcome worker.stop_backend returns, the route must
    render it as 200, never let an exception surface as a 500. Patches the inner
    _stop_backend_locked (not stop_backend itself), so the outer wrapper still records the outcome
    for recent_stop_outcome() -- exactly as a real failure inside the sequence would."""

    async def fake_stop_backend_locked(backend, *, confirm):
        return StopOutcome.failed("simulated failure")

    monkeypatch.setattr(app_client.app.state.worker, "_stop_backend_locked", fake_stop_backend_locked)

    response = app_client.post("/gpu/qwen21-uc/stop", headers=owner_headers)

    assert response.status_code == 200
    assert "Stop failed" in response.text


def test_startup_resumes_the_pinger_for_an_open_window(settings, registry, fake_gateway):
    """The lifespan's own startup code (not _warm_step called directly) must call ensure_pinger for
    every backend with a still-open persisted window."""
    db.migrate(settings)
    backend = registry.backends["qwen21-uc"]
    with db.session(settings) as conn:
        gpu.start_warm(conn, backend.id, 15, time.time(), deployed=True)
        conn.commit()

    app = create_app(settings, registry=registry, gateway=fake_gateway, start_worker=True)
    with TestClient(app, base_url="http://testserver"):
        worker = app.state.worker
        assert backend.id in worker.pingers
        assert not worker.pingers[backend.id].done()


def test_startup_clears_a_window_that_expired_while_artio_was_down(settings, registry, fake_gateway):
    """A window that had already ended before Artio restarted must be cleared right away, instead
    of sitting there as a stale "warm until" nobody's pinger will ever reach and clear."""
    db.migrate(settings)
    backend = registry.backends["qwen21-uc"]
    with db.session(settings) as conn:
        gpu.start_warm(conn, backend.id, 5, time.time() - 3600, deployed=True)  # ended an hour ago
        conn.commit()

    app = create_app(settings, registry=registry, gateway=fake_gateway, start_worker=True)
    with TestClient(app, base_url="http://testserver"):
        assert backend.id not in app.state.worker.pingers

    with db.session(settings) as conn:
        until, ping_id = gpu.warm_state(conn, backend.id)
    assert until is None and ping_id is None
