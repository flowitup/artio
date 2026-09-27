"""Opt-in live test: real Modal generations, costing GPU money. Skipped unless ARTIO_LIVE_TESTS=1.

Renders A, then B, then waits for the backend to scale to zero, then renders A again on a fresh
container. ComfyUI keeps an identical graph's outputs cached across prompts (an A → B → A sequence on one
container returned the second A in about 2 s), so only a fresh container proves that the same seed and
settings really re-render to the same pixels.
"""

from __future__ import annotations

import asyncio
import io
import os
import time

import modal
import pytest
from PIL import Image, ImageChops, ImageStat

from artio.modal_gateway import ModalSdkGateway
from artio.registry import DEFAULT_REGISTRY
from artio.workflows import GenParams, qwen_image_21

pytestmark = pytest.mark.live

_RENDER_TIMEOUT_S = 180.0
_SCALE_DOWN_TIMEOUT_S = 300.0
_POLL_INTERVAL_S = 1.0
_PROMPT = "a lighthouse at dusk, dramatic clouds, cinematic lighting"


@pytest.fixture(autouse=True)
def _require_live_tests_enabled():
    if os.environ.get("ARTIO_LIVE_TESTS") != "1":
        pytest.skip("set ARTIO_LIVE_TESTS=1 to run live Modal generations (spends GPU money)")


def _decode(png_bytes: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png_bytes)).convert("RGB")


def _mean_abs_diff(a: Image.Image, b: Image.Image) -> float:
    means = ImageStat.Stat(ImageChops.difference(a, b)).mean
    return sum(means) / len(means)


async def _render(gateway: ModalSdkGateway, backend, seed: int) -> bytes:
    graph = qwen_image_21.build_graph(
        GenParams(prompt=_PROMPT, negative="", width=1088, height=1920, steps=25, seed=seed, cfg=1.0)
    )
    call_id = await gateway.spawn_workflow(backend, graph)
    deadline = time.monotonic() + _RENDER_TIMEOUT_S
    while True:
        result = await gateway.poll(call_id)
        if result.state == "done":
            return result.value
        if result.state == "failed":
            raise RuntimeError(f"generation failed: {result.error}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"generation did not finish within {_RENDER_TIMEOUT_S:.0f}s")
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _wait_until_scaled_to_zero(backend) -> None:
    """Wait for the backend's containers to exit, so the next render boots a fresh ComfyUI with an empty cache."""
    handle = modal.Cls.from_name(backend.modal_app, backend.modal_class)()
    deadline = time.monotonic() + _SCALE_DOWN_TIMEOUT_S
    while (await handle.run_workflow.get_current_stats.aio()).num_total_runners > 0:
        if time.monotonic() > deadline:
            raise TimeoutError(f"backend still running after {_SCALE_DOWN_TIMEOUT_S:.0f}s")
        await asyncio.sleep(5.0)


def test_same_seed_renders_are_identical_and_a_different_seed_differs():
    backend = DEFAULT_REGISTRY.backends["qwen21-uc"]
    gateway = ModalSdkGateway()

    async def run_all() -> tuple[bytes, bytes, bytes, float]:
        png_a1 = await _render(gateway, backend, 424242)
        png_b = await _render(gateway, backend, 424243)
        await _wait_until_scaled_to_zero(backend)
        started_at = time.monotonic()
        png_a2 = await _render(gateway, backend, 424242)
        return png_a1, png_b, png_a2, time.monotonic() - started_at

    png_a1, png_b, png_a2, second_a_elapsed = asyncio.run(run_all())

    image_a1 = _decode(png_a1)
    image_b = _decode(png_b)
    image_a2 = _decode(png_a2)

    assert image_a1.tobytes() == image_a2.tobytes(), "the same seed and settings must render pixel-identically"
    assert second_a_elapsed > 5.0, "the second A must be a real re-render on a fresh container, not a cached return"
    assert _mean_abs_diff(image_a1, image_b) > 5.0, "a different seed must produce a visibly different image"
