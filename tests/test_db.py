"""Migrations, WAL mode and FTS5 availability."""

import sqlite3

from atelier import db


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def test_migrate_creates_every_table_from_the_initial_migration(settings):
    db.migrate(settings)
    conn = db.connect(settings)
    try:
        names = _table_names(conn)
        for table in (
            "schema_version",
            "batches",
            "workflows",
            "jobs",
            "images",
            "tags",
            "image_tags",
            "presets",
            "backend_state",
            "images_fts",
        ):
            assert table in names
        version = conn.execute("SELECT version FROM schema_version").fetchall()
        assert [row["version"] for row in version] == [1]
    finally:
        conn.close()


def test_migrate_applied_twice_is_a_no_op(settings):
    db.migrate(settings)
    db.migrate(settings)  # must not raise "table already exists"
    conn = db.connect(settings)
    try:
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert [row["version"] for row in rows] == [1]
    finally:
        conn.close()


def test_migrate_sets_wal_mode(settings):
    db.migrate(settings)
    conn = db.connect(settings)
    try:
        (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_fts5_is_available(settings):
    db.migrate(settings)
    conn = db.connect(settings)
    try:
        conn.execute("INSERT INTO images_fts(rowid, prompt, negative, tags) VALUES (999, 'a fox', '', '')")
        rows = conn.execute("SELECT rowid FROM images_fts WHERE images_fts MATCH 'fox'").fetchall()
        assert [row["rowid"] for row in rows] == [999]
    finally:
        conn.close()


def test_connect_enables_foreign_keys_and_row_factory(settings):
    db.migrate(settings)
    conn = db.connect(settings)
    try:
        (fk,) = conn.execute("PRAGMA foreign_keys").fetchone()
        assert fk == 1
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_session_commits_on_success_and_rolls_back_on_error(settings):
    db.migrate(settings)
    now = 0.0
    with db.session(settings) as conn:
        conn.execute(
            "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) "
            "VALUES (?, 'm', 'generate', '{}', 1)",
            (now,),
        )

    with db.session(settings) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"] == 1

    class _Boom(Exception):
        pass

    try:
        with db.session(settings) as conn:
            conn.execute(
                "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) "
                "VALUES (?, 'm', 'generate', '{}', 1)",
                (now,),
            )
            raise _Boom()
    except _Boom:
        pass

    with db.session(settings) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"] == 1
