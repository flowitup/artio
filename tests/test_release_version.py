"""The release number shows in the sidebar and /healthz, and matches pyproject.toml."""

import tomllib
from pathlib import Path

from artio import __version__


def test_release_matches_pyproject():
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__


def test_healthz_reports_release(app_client):
    body = app_client.get("/healthz").json()
    assert body["release"] == __version__


def test_sidebar_shows_release(app_client, owner_headers):
    assert f"Artio v{__version__}" in app_client.get("/gallery", headers=owner_headers).text
