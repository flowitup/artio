"""SQLite access: short-lived connections and a numbered migration runner.

Every migration file is applied through a single `executescript()` call whose text embeds its own
`BEGIN;`/`COMMIT;`: `executescript()` disregards the connection's isolation level and manages its own
transaction, so wrapping it with `conn.execute("BEGIN")` from Python would not give per-file atomicity.
Embedding the transaction control in the script text itself does: a failure mid-script leaves the
transaction open (never reaching its `COMMIT;`), and the caller rolls it back.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from atelier.config import Settings

DB_FILENAME = "atelier.db"

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_NAME_RE = re.compile(r"^(\d+)_.*\.sql$")


def _db_path(settings: Settings) -> Path:
    return settings.data_dir / DB_FILENAME


def connect(settings: Settings) -> sqlite3.Connection:
    """Open a short-lived connection. Callers must close it themselves, or use session()."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_path(settings), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def session(settings: Settings) -> Iterator[sqlite3.Connection]:
    """One unit of work: commits on success, rolls back on error, always closes."""
    conn = connect(settings)
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def _migration_files() -> list[tuple[int, Path]]:
    found = []
    for path in _MIGRATIONS_DIR.glob("*.sql"):
        match = _MIGRATION_NAME_RE.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    return sorted(found)


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    # schema_version itself is created by migration 0001, so it may not exist yet on a fresh database.
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'").fetchone()
    if exists is None:
        return set()
    return {row[0] for row in conn.execute("SELECT version FROM schema_version")}


def migrate(settings: Settings) -> None:
    """Apply every migration file not yet recorded in schema_version, in numeric order. Idempotent."""
    conn = connect(settings)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        applied = _applied_versions(conn)
        for version, path in _migration_files():
            if version in applied:
                continue
            script = (
                "BEGIN;\n"
                f"{path.read_text()}\n"
                f"INSERT INTO schema_version (version, applied_at) VALUES ({version}, {time.time()!r});\n"
                "COMMIT;\n"
            )
            try:
                conn.executescript(script)
            except BaseException:
                conn.rollback()
                raise
            applied.add(version)
    finally:
        conn.close()
