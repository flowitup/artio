"""Shared types for model-specific ComfyUI graph builders."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GenParams:
    """Everything one graph builder needs to produce a single generation's ComfyUI graph."""

    prompt: str
    negative: str
    width: int
    height: int
    steps: int
    seed: int
    cfg: float
