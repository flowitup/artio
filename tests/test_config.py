"""Settings loading and its production/development guards."""

import pytest

from atelier.config import ConfigError, load_settings

PRODUCTION_REQUIRED = {
    "ATELIER_ENV": "production",
    "ATELIER_CF_AUD": "aud-123",
    "ATELIER_OWNER_EMAIL": "owner@example.com",
    "ATELIER_PLUGIN_CLIENT_ID": "plugin-123",
}


def test_production_without_aud_owner_or_client_id_fails(tmp_path):
    (tmp_path / ".atelier-volume").touch()
    with pytest.raises(ConfigError):
        load_settings({"ATELIER_ENV": "production", "ATELIER_DATA_DIR": str(tmp_path)})


@pytest.mark.parametrize("missing_key", ["ATELIER_CF_AUD", "ATELIER_OWNER_EMAIL", "ATELIER_PLUGIN_CLIENT_ID"])
def test_production_requires_each_key_separately(tmp_path, missing_key):
    """Each key is its own check: dropping just one must fail, proving the guard isn't satisfied by
    the other two alone."""
    (tmp_path / ".atelier-volume").touch()
    environ = {k: v for k, v in PRODUCTION_REQUIRED.items() if k != missing_key}
    environ["ATELIER_DATA_DIR"] = str(tmp_path)
    with pytest.raises(ConfigError):
        load_settings(environ)


def test_production_with_a_dev_identity_fails(tmp_path):
    (tmp_path / ".atelier-volume").touch()
    environ = {**PRODUCTION_REQUIRED, "ATELIER_DATA_DIR": str(tmp_path), "ATELIER_DEV_IDENTITY": "owner"}
    with pytest.raises(ConfigError):
        load_settings(environ)


def test_development_with_a_dev_identity_loads(tmp_path):
    settings = load_settings(
        {
            "ATELIER_ENV": "development",
            "ATELIER_DATA_DIR": str(tmp_path),
            "ATELIER_DEV_IDENTITY": "owner",
        }
    )
    assert settings.env == "development"
    assert settings.dev_identity == "owner"


def test_production_fails_when_the_sentinel_is_missing(tmp_path):
    environ = {**PRODUCTION_REQUIRED, "ATELIER_DATA_DIR": str(tmp_path)}
    with pytest.raises(ConfigError):
        load_settings(environ)


def test_production_loads_with_everything_present(tmp_path):
    (tmp_path / ".atelier-volume").touch()
    environ = {**PRODUCTION_REQUIRED, "ATELIER_DATA_DIR": str(tmp_path)}
    settings = load_settings(environ)
    assert settings.is_production
    assert settings.cf_aud == "aud-123"


def test_defaults_are_applied_when_unset(tmp_path):
    settings = load_settings({"ATELIER_ENV": "test", "ATELIER_DATA_DIR": str(tmp_path)})
    assert settings.data_cap_gb == 40
    assert settings.min_free_gb == 5
    assert settings.job_timeout_s == 1800
    assert settings.timezone == "Europe/Paris"
    assert settings.version == "dev"
    assert settings.public_origin == "https://atelier.flowitup.com"


def test_invalid_env_value_fails(tmp_path):
    with pytest.raises(ConfigError):
        load_settings({"ATELIER_ENV": "staging", "ATELIER_DATA_DIR": str(tmp_path)})


def test_invalid_dev_identity_value_fails(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(
            {"ATELIER_ENV": "development", "ATELIER_DATA_DIR": str(tmp_path), "ATELIER_DEV_IDENTITY": "root"}
        )


def test_non_integer_override_fails(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(
            {"ATELIER_ENV": "test", "ATELIER_DATA_DIR": str(tmp_path), "ATELIER_JOB_TIMEOUT_S": "soon"}
        )


@pytest.mark.parametrize("key", ["ATELIER_JOB_TIMEOUT_S", "ATELIER_DATA_CAP_GB", "ATELIER_MIN_FREE_GB"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_numeric_settings_must_be_positive(tmp_path, key, value):
    with pytest.raises(ConfigError):
        load_settings({"ATELIER_ENV": "test", "ATELIER_DATA_DIR": str(tmp_path), key: value})


@pytest.mark.parametrize("key", ["ATELIER_JOB_TIMEOUT_S", "ATELIER_DATA_CAP_GB", "ATELIER_MIN_FREE_GB"])
def test_numeric_settings_accept_a_positive_value(tmp_path, key):
    settings = load_settings({"ATELIER_ENV": "test", "ATELIER_DATA_DIR": str(tmp_path), key: "1"})
    assert getattr(settings, key.removeprefix("ATELIER_").lower()) == 1


def test_unknown_timezone_fails(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(
            {"ATELIER_ENV": "test", "ATELIER_DATA_DIR": str(tmp_path), "ATELIER_TIMEZONE": "Not/AZone"}
        )


def test_known_timezone_loads(tmp_path):
    settings = load_settings(
        {"ATELIER_ENV": "test", "ATELIER_DATA_DIR": str(tmp_path), "ATELIER_TIMEZONE": "America/New_York"}
    )
    assert settings.timezone == "America/New_York"
