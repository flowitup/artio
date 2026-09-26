"""Shared fixtures for the Atelier test suite."""

import importlib.util
from pathlib import Path

import pytest

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
