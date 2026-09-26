"""Shared fixtures for the Atelier test suite."""

import importlib.util
import io
import random
from pathlib import Path

import pytest
from PIL import Image

from atelier import db
from atelier.config import Settings, load_settings
from atelier.registry import DEFAULT_REGISTRY, Registry
from tests.fakes import FakeModalGateway

BACKEND_SCRIPT = Path(__file__).resolve().parent.parent / "modal" / "qwen21_uc_app.py"


@pytest.fixture(scope="session")
def backend_source() -> str:
    """Source text of the Modal backend script, for structural checks."""
    return BACKEND_SCRIPT.read_text()


@pytest.fixture(scope="session")
def backend_script():
    """The Modal backend script loaded by path. Defining a Modal app is lazy, so this needs no network."""
    spec = importlib.util.spec_from_file_location("qwen21_uc_app", BACKEND_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def settings(tmp_path) -> Settings:
    """A `test`-env Settings backed by a fresh temporary data directory."""
    return load_settings(
        {
            "ATELIER_ENV": "test",
            "ATELIER_DATA_DIR": str(tmp_path),
        }
    )


@pytest.fixture
def conn(settings):
    """A ready-to-use connection on a migrated database. Tests call conn.commit() themselves whenever a
    second connection (e.g. one opened internally by Worker) needs to see the change."""
    db.migrate(settings)
    connection = db.connect(settings)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def registry() -> Registry:
    return DEFAULT_REGISTRY


@pytest.fixture
def fake_gateway() -> FakeModalGateway:
    return FakeModalGateway()


@pytest.fixture
def png_bytes() -> bytes:
    """A real, small 8-bit RGB PNG, made with Pillow."""
    im = Image.new("RGB", (64, 64), color=(120, 60, 200))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def rng() -> random.Random:
    return random.Random(1234567890)
