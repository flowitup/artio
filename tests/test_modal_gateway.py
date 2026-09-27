"""Gateway tests: the real SDK poll path with exactly two stubs, and classification with real exceptions.

`ModalSdkGateway.poll()` drives the actual `modal.FunctionCall.from_id(...).get.aio(timeout=0)` path.
Only `_Invocation.pop_function_call_outputs` (the single RPC underneath it) and `_Client.from_env` (so
`FunctionCall.from_id` can hydrate a client handle) are stubbed; everything else -- hydration, the
poll_function control flow, (de)serialization -- is the real SDK. That is what proves the pending signal
really is the builtin TimeoutError, not a hand-built exception.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiohttp
import grpclib.exceptions
import modal
import modal.client as _client_module
import modal.exception
import pytest
from modal import _functions
from modal._serialization import serialize
from modal_proto import api_pb2

from artio import modal_gateway
from artio.modal_gateway import ModalSdkGateway, classify_poll_exception
from artio.registry import Backend

CALL_ID = "fc-01testcafef00dfeedfacecafe"

BACKEND = Backend(
    id="qwen21-uc",
    label="Qwen-Image 2.1 UC",
    modal_app="qwen21-uc",
    modal_class="Qwen21UC",
    usd_per_hour=1.95,
    max_inflight=4,
)


async def _fake_client_from_env(cls, _override_config=None):
    # A real, minimal _Client: hydration needs a client object, but no RPC is ever issued against it,
    # because pop_function_call_outputs (the only method that would use its stub) is replaced below.
    client = _client_module._Client("https://api.modal.com", api_pb2.CLIENT_TYPE_CLIENT, None)
    client._stub = object()
    return client


@pytest.fixture(autouse=True)
def _stub_client(monkeypatch):
    monkeypatch.setattr(_client_module._Client, "from_env", classmethod(_fake_client_from_env))


def _stub_outputs(monkeypatch, response: api_pb2.FunctionGetOutputsResponse) -> None:
    async def fake_pop(self, index=0, timeout=None, clear_on_success=False, input_jwts=None):
        return response

    monkeypatch.setattr(_functions._Invocation, "pop_function_call_outputs", fake_pop)


def _poll() -> modal_gateway.PollResult:
    return asyncio.run(ModalSdkGateway().poll(CALL_ID))


def test_running_call_polls_as_pending(monkeypatch):
    _stub_outputs(monkeypatch, api_pb2.FunctionGetOutputsResponse(outputs=[], num_unfinished_inputs=1))
    result = _poll()
    assert result.state == "pending"
    assert result.transient is None


def test_expired_call_polls_as_failed(monkeypatch):
    _stub_outputs(monkeypatch, api_pb2.FunctionGetOutputsResponse(outputs=[], num_unfinished_inputs=0))
    result = _poll()
    assert result.state == "failed"
    assert "expired" in result.error.lower()


def test_finished_call_polls_as_done(monkeypatch, png_bytes):
    item = api_pb2.FunctionGetOutputsItem(
        result=api_pb2.GenericResult(
            status=api_pb2.GenericResult.GENERIC_STATUS_SUCCESS,
            data=serialize(png_bytes),
        ),
        idx=0,
        data_format=api_pb2.DATA_FORMAT_PICKLE,
    )
    _stub_outputs(monkeypatch, api_pb2.FunctionGetOutputsResponse(outputs=[item], num_unfinished_inputs=0))
    result = _poll()
    assert result.state == "done"
    assert result.value == png_bytes


@pytest.mark.parametrize(
    "exc",
    [
        modal.exception.NotFoundError("x"),
        modal.exception.AuthError("x"),
        modal.exception.PermissionDeniedError("x"),
        modal.exception.InvalidError("x"),
        modal.exception.ConflictError("x"),
    ],
)
def test_permanent_errors_are_classified_as_failed(exc):
    result = classify_poll_exception(exc)
    assert result.state == "failed"
    assert result.error.startswith("Modal refused the call:")
    assert "x" in result.error


@pytest.mark.parametrize(
    "exc",
    [
        modal.exception.ConnectionError("x"),
        modal.exception.ServiceError("x"),
        grpclib.exceptions.StreamTerminatedError(),
        modal.exception.ResourceExhaustedError("x"),
        modal.exception.InternalFailure("x"),
        aiohttp.ClientError("x"),
    ],
)
def test_transient_errors_are_classified_as_pending_with_a_reason(exc):
    result = classify_poll_exception(exc)
    assert result.state == "pending"
    assert result.transient is not None


def test_execution_error_stays_failed_not_transient():
    # Ambiguous (Artio can't tell whether a failed deserialization is retriable): stays failed, so
    # the user can retry it by hand rather than the dispatcher retrying it forever on its own.
    result = classify_poll_exception(modal.exception.ExecutionError("could not deserialize"))
    assert result.state == "failed"


def test_builtin_timeout_error_is_classified_as_pending():
    result = classify_poll_exception(TimeoutError())
    assert result.state == "pending"


def test_modal_exception_timeout_error_is_also_classified_as_pending():
    # Defensive: a future SDK version could raise Modal's own TimeoutError instead of the builtin.
    result = classify_poll_exception(modal.exception.TimeoutError("still running"))
    assert result.state == "pending"


def test_output_expired_error_is_classified_as_failed():
    result = classify_poll_exception(modal.exception.OutputExpiredError())
    assert result.state == "failed"
    assert "expired" in result.error.lower()


def test_function_timeout_error_is_classified_as_failed():
    result = classify_poll_exception(modal.exception.FunctionTimeoutError("backend deadline"))
    assert result.state == "failed"
    assert "backend deadline" in result.error


def test_remote_comfyui_error_is_classified_as_failed_with_its_text():
    result = classify_poll_exception(RuntimeError("ComfyUI rejected workflow: bad node"))
    assert result.state == "failed"
    assert "ComfyUI rejected workflow" in result.error


def test_poll_reraises_cancelled_error_instead_of_classifying_it(monkeypatch):
    async def fake_pop(self, index=0, timeout=None, clear_on_success=False, input_jwts=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(_functions._Invocation, "pop_function_call_outputs", fake_pop)
    with pytest.raises(asyncio.CancelledError):
        _poll()


def test_poll_reraises_keyboard_interrupt_instead_of_classifying_it(monkeypatch):
    async def fake_pop(self, index=0, timeout=None, clear_on_success=False, input_jwts=None):
        raise KeyboardInterrupt()

    monkeypatch.setattr(_functions._Invocation, "pop_function_call_outputs", fake_pop)
    with pytest.raises(KeyboardInterrupt):
        _poll()


def test_poll_classifies_a_deserialized_remote_base_exception(monkeypatch):
    # A container that hard-exits (e.g. an unhandled SystemExit inside run_workflow) is pickled and
    # re-raised locally as the same BaseException subclass. Only Exception was ever caught before, so
    # this used to escape poll() entirely instead of failing just this one job.
    async def fake_pop(self, index=0, timeout=None, clear_on_success=False, input_jwts=None):
        raise SystemExit("container exited")

    monkeypatch.setattr(_functions._Invocation, "pop_function_call_outputs", fake_pop)
    result = _poll()
    assert result.state == "failed"
    assert "container exited" in result.error


def test_permanent_spawn_error_drops_the_cached_handle(monkeypatch):
    calls: list[tuple[str, str]] = []
    outcomes = [
        modal.exception.NotFoundError("app qwen21-uc is not deployed"),
        SimpleNamespace(object_id="fc-freshcall01"),
    ]

    def make_handle(outcome):
        async def spawn_aio(graph):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return SimpleNamespace(run_workflow=SimpleNamespace(spawn=SimpleNamespace(aio=spawn_aio)))

    def fake_from_name(app, cls):
        calls.append((app, cls))
        handle = make_handle(outcomes[len(calls) - 1])
        return lambda: handle

    monkeypatch.setattr(modal_gateway.modal.Cls, "from_name", staticmethod(fake_from_name))

    gateway = ModalSdkGateway()
    with pytest.raises(modal.exception.NotFoundError):
        asyncio.run(gateway.spawn_workflow(BACKEND, {}))
    assert len(calls) == 1  # the failed handle stays cached only for the duration of that one call

    call_id = asyncio.run(gateway.spawn_workflow(BACKEND, {}))
    assert call_id == "fc-freshcall01"
    assert len(calls) == 2  # re-resolved the app by name after the permanent error
