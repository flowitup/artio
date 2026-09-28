"""Model-neutral registry: which backends and models exist, and their parameter contracts.

A `Registry` is a plain, injectable value: adding a model or backend never touches the database schema
(model_id is just a string in jobs/images), so tests build their own Registry with an extra model to
prove that a second model needs no migration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from artio.workflows import GenParams, qwen_image_21


class UnknownModel(Exception):
    """Raised when a model id has no entry in the registry."""


class InvalidParams(ValueError):
    """Raised when generation parameters violate a model's ParamSchema bounds."""


@dataclass(frozen=True, slots=True)
class SizePreset:
    name: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class SizeTier:
    """A resolution level: the same shapes as the presets, at fewer pixels. A tier with no sizes of its
    own stands for the presets themselves, so the presets stay the full-size level."""

    name: str
    sizes: tuple[SizePreset, ...] = ()


@dataclass(frozen=True, slots=True)
class ParamSchema:
    """Validated bounds for one model's generation parameters."""

    steps_min: int
    steps_max: int
    steps_default: int
    cfg_min: float
    cfg_max: float
    cfg_default: float
    size_min: int
    size_max: int
    size_multiple: int
    presets: tuple[SizePreset, ...]
    default_preset: str
    tiers: tuple[SizeTier, ...] = ()
    default_tier: str = ""

    def default_size(self) -> SizePreset:
        return self.size(self.default_preset, self.default_tier) or next(
            p for p in self.presets if p.name == self.default_preset
        )

    def size(self, shape: str, tier: str = "") -> SizePreset | None:
        """The preset named `shape` at resolution level `tier` ("" means the full-size presets), or
        None when either name is unknown."""
        sizes = self.presets
        if tier:
            level = next((t for t in self.tiers if t.name == tier), None)
            if level is None:
                return None
            sizes = level.sizes or self.presets
        return next((p for p in sizes if p.name == shape), None)

    def match(self, width: int, height: int) -> tuple[str, str] | None:
        """(shape, tier) whose size is exactly width x height, or None for a custom size. The tier is
        "" when the model has no tiers."""
        for tier in self.tiers or (SizeTier(""),):
            for p in tier.sizes or self.presets:
                if (p.width, p.height) == (width, height):
                    return p.name, tier.name
        return None

    def validate(self, *, width: int, height: int, steps: int, cfg: float) -> None:
        """Raise InvalidParams with a clear message on the first bound violated."""
        if not (self.steps_min <= steps <= self.steps_max):
            raise InvalidParams(f"steps must be between {self.steps_min} and {self.steps_max}, got {steps}")
        if not (self.cfg_min <= cfg <= self.cfg_max):
            raise InvalidParams(f"cfg must be between {self.cfg_min} and {self.cfg_max}, got {cfg}")
        for label, value in (("width", width), ("height", height)):
            if not (self.size_min <= value <= self.size_max):
                raise InvalidParams(f"{label} must be between {self.size_min} and {self.size_max}, got {value}")
            if value % self.size_multiple != 0:
                raise InvalidParams(f"{label} must be a multiple of {self.size_multiple}, got {value}")


@dataclass(frozen=True, slots=True)
class Backend:
    id: str
    label: str
    modal_app: str
    modal_class: str
    usd_per_hour: float
    max_inflight: int


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    label: str
    backend_id: str
    build_graph: Callable[[GenParams], dict]
    param_schema: ParamSchema


@dataclass(frozen=True, slots=True)
class Registry:
    backends: dict[str, Backend]
    models: dict[str, Model]

    def model(self, model_id: str) -> Model:
        try:
            return self.models[model_id]
        except KeyError:
            raise UnknownModel(model_id) from None

    def backend_for(self, model: Model) -> Backend:
        return self.backends[model.backend_id]


QWEN21_UC_BACKEND = Backend(
    id="qwen21-uc",
    label="Qwen-Image 2.1 UC",
    modal_app="qwen21-uc",
    modal_class="Qwen21UC",
    usd_per_hour=1.95,
    max_inflight=4,
)

QWEN21_UC_PARAMS = ParamSchema(
    steps_min=1,
    steps_max=60,
    steps_default=25,
    cfg_min=0.0,
    cfg_max=10.0,
    cfg_default=1.0,
    size_min=512,
    size_max=2048,
    size_multiple=16,
    presets=(
        SizePreset("9:16", 1088, 1920),
        SizePreset("16:9", 1920, 1088),
        SizePreset("1:1", 1328, 1328),
    ),
    default_preset="9:16",
    # Lower levels keep each shape and scale its short side to 720 or 512 (the model's minimum). Cost
    # tracks pixel count, so 720p is about half of 1080p and 512p about a quarter.
    tiers=(
        SizeTier(
            "512p",
            (SizePreset("9:16", 512, 912), SizePreset("16:9", 912, 512), SizePreset("1:1", 624, 624)),
        ),
        SizeTier(
            "720p",
            (SizePreset("9:16", 720, 1280), SizePreset("16:9", 1280, 720), SizePreset("1:1", 880, 880)),
        ),
        SizeTier("1080p"),
    ),
    default_tier="1080p",
)

QWEN21_UC_MODEL = Model(
    id="qwen-image-2.1-uc",
    label="Qwen-Image 2.1 UC",
    backend_id=QWEN21_UC_BACKEND.id,
    build_graph=qwen_image_21.build_graph,
    param_schema=QWEN21_UC_PARAMS,
)

DEFAULT_REGISTRY = Registry(
    backends={QWEN21_UC_BACKEND.id: QWEN21_UC_BACKEND},
    models={QWEN21_UC_MODEL.id: QWEN21_UC_MODEL},
)
