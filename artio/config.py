"""Process settings loaded from the environment, validated once at startup.

`load_settings` never reads `os.environ` itself: callers pass the mapping explicitly (`os.environ` in
`main.py`, a plain dict in tests), so tests never leak environment state between each other.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VOLUME_SENTINEL_NAME = ".artio-volume"
_ENVIRONMENTS = ("production", "development", "test")
_DEV_IDENTITIES = ("owner", "service")


class ConfigError(Exception):
    """Raised when the environment does not satisfy Artio's settings contract."""


@dataclass(frozen=True, slots=True)
class Settings:
    env: str
    data_dir: Path
    public_origin: str
    cf_team_domain: str
    cf_aud: str | None
    owner_email: str | None
    plugin_client_id: str | None
    dev_identity: str | None
    data_cap_gb: int
    min_free_gb: int
    job_timeout_s: int
    timezone: str
    version: str

    @property
    def is_production(self) -> bool:
        return self.env == "production"


def _int(environ: Mapping[str, str], key: str, default: int) -> int:
    raw = environ.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from None


def _positive_int(environ: Mapping[str, str], key: str, default: int) -> int:
    value = _int(environ, key, default)
    if value <= 0:
        raise ConfigError(f"{key} must be a positive integer, got {value}")
    return value


def _validate_timezone(name: str) -> None:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ConfigError(f"ARTIO_TIMEZONE is not a known zone: {name!r}") from None


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Build and validate Settings from an environment-like mapping. Raises ConfigError on any violation."""
    env = environ.get("ARTIO_ENV", "production")
    if env not in _ENVIRONMENTS:
        raise ConfigError(f"ARTIO_ENV must be one of {_ENVIRONMENTS}, got {env!r}")

    dev_identity = environ.get("ARTIO_DEV_IDENTITY") or None
    if dev_identity is not None:
        if dev_identity not in _DEV_IDENTITIES:
            raise ConfigError(f"ARTIO_DEV_IDENTITY must be one of {_DEV_IDENTITIES}, got {dev_identity!r}")
        if env != "development":
            raise ConfigError("ARTIO_DEV_IDENTITY is only allowed when ARTIO_ENV=development")

    settings = Settings(
        env=env,
        data_dir=Path(environ.get("ARTIO_DATA_DIR", "/data")),
        public_origin=environ.get("ARTIO_PUBLIC_ORIGIN", "https://artio.flowitup.com"),
        cf_team_domain=environ.get("ARTIO_CF_TEAM_DOMAIN", "https://flowitupteam.cloudflareaccess.com"),
        cf_aud=environ.get("ARTIO_CF_AUD") or None,
        owner_email=environ.get("ARTIO_OWNER_EMAIL") or None,
        plugin_client_id=environ.get("ARTIO_PLUGIN_CLIENT_ID") or None,
        dev_identity=dev_identity,
        data_cap_gb=_positive_int(environ, "ARTIO_DATA_CAP_GB", 40),
        min_free_gb=_positive_int(environ, "ARTIO_MIN_FREE_GB", 5),
        job_timeout_s=_positive_int(environ, "ARTIO_JOB_TIMEOUT_S", 1800),
        timezone=environ.get("ARTIO_TIMEZONE", "Europe/Paris"),
        version=environ.get("ARTIO_VERSION", "dev"),
    )
    _validate_timezone(settings.timezone)

    if settings.is_production:
        missing = [
            name
            for name, value in (
                ("ARTIO_CF_AUD", settings.cf_aud),
                ("ARTIO_OWNER_EMAIL", settings.owner_email),
                ("ARTIO_PLUGIN_CLIENT_ID", settings.plugin_client_id),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"production requires {', '.join(missing)}")
        sentinel = settings.data_dir / VOLUME_SENTINEL_NAME
        if not sentinel.exists():
            raise ConfigError(f"production requires the volume sentinel at {sentinel}")

    return settings
