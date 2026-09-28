"""The registry's data shape, plus proof that a second model needs no schema change."""

import asyncio
import json

import pytest

from artio import jobs
from artio.registry import (
    DEFAULT_REGISTRY,
    QWEN21_UC_BACKEND,
    InvalidParams,
    Model,
    ParamSchema,
    Registry,
    SizePreset,
    UnknownModel,
)
from artio.worker import Worker
from artio.workflows import GenParams


def test_model_returns_the_qwen_entry(registry):
    model = registry.model("qwen-image-2.1-uc")
    assert model.backend_id == "qwen21-uc"
    assert registry.backend_for(model) is QWEN21_UC_BACKEND


def test_model_raises_unknown_model_for_a_missing_id(registry):
    with pytest.raises(UnknownModel):
        registry.model("does-not-exist")


def test_param_schema_defaults_and_bounds():
    schema = DEFAULT_REGISTRY.model("qwen-image-2.1-uc").param_schema
    assert schema.steps_default == 25
    assert schema.cfg_default == 1.0
    assert schema.default_size() == SizePreset("9:16", 1088, 1920)
    schema.validate(width=1920, height=1088, steps=25, cfg=1.0)  # does not raise


@pytest.mark.parametrize(
    "kwargs",
    [
        {"width": 1088, "height": 1920, "steps": 0, "cfg": 1.0},
        {"width": 1088, "height": 1920, "steps": 61, "cfg": 1.0},
        {"width": 1088, "height": 1920, "steps": 25, "cfg": -1},
        {"width": 1088, "height": 1920, "steps": 25, "cfg": 11},
        {"width": 500, "height": 1920, "steps": 25, "cfg": 1.0},
        {"width": 1088, "height": 2064, "steps": 25, "cfg": 1.0},
        {"width": 1090, "height": 1920, "steps": 25, "cfg": 1.0},  # not a multiple of 16
    ],
)
def test_param_schema_rejects_out_of_bounds_values(kwargs):
    schema = DEFAULT_REGISTRY.model("qwen-image-2.1-uc").param_schema
    with pytest.raises(InvalidParams):
        schema.validate(**kwargs)


def test_second_model_registers_without_schema_change(conn, settings, fake_gateway, rng, png_bytes):
    second_schema = ParamSchema(
        steps_min=1,
        steps_max=20,
        steps_default=4,
        cfg_min=0.0,
        cfg_max=5.0,
        cfg_default=1.0,
        size_min=64,
        size_max=1024,
        size_multiple=16,
        presets=(SizePreset("square", 512, 512),),
        default_preset="square",
    )
    second_model = Model(
        id="second-model",
        label="Second Model",
        backend_id=QWEN21_UC_BACKEND.id,
        build_graph=lambda p: {"1": {"class_type": "Noop", "inputs": {"prompt": p.prompt, "seed": p.seed}}},
        param_schema=second_schema,
    )
    registry = Registry(
        backends={QWEN21_UC_BACKEND.id: QWEN21_UC_BACKEND},
        models={**DEFAULT_REGISTRY.models, second_model.id: second_model},
    )

    request = jobs.BatchRequest(
        model_id="second-model",
        prompt="a second model prompt",
        negative="",
        width=512,
        height=512,
        steps=4,
        cfg=1.0,
        seed_mode="fixed",
        seed=42,
        count=1,
    )
    batch_id = jobs.create_batch(conn, registry, settings, request, rng)
    conn.commit()

    job_id = conn.execute("SELECT id FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()["id"]
    graph = json.loads(conn.execute("SELECT graph_json FROM jobs WHERE id = ?", (job_id,)).fetchone()["graph_json"])
    assert graph == second_model.build_graph(GenParams("a second model prompt", "", 512, 512, 4, 42, 1.0))

    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())

    call_id = conn.execute("SELECT call_id FROM jobs WHERE id = ?", (job_id,)).fetchone()["call_id"]
    fake_gateway.finish(call_id, png_bytes)
    asyncio.run(worker.poll_once())

    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "done"

    images = conn.execute("SELECT * FROM images WHERE model_id = ?", ("second-model",)).fetchall()
    assert len(images) == 1
    assert images[0]["job_id"] == job_id

    # Only the migrations the app already ships: a second model needs no schema change of its own.
    versions = [row["version"] for row in conn.execute("SELECT version FROM schema_version")]
    assert versions == [1, 2]
