"""The release number shows in the sidebar and /healthz, and matches pyproject.toml; static URLs are versioned."""

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


def test_static_urls_carry_the_deploy_version(app_client, owner_headers, settings):
    page = app_client.get("/gallery", headers=owner_headers).text
    for asset in ("pico.min.css", "app.css", "htmx.min.js", "app.js"):
        assert f'/static/{asset}?v={settings.version}"' in page
