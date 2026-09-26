"""The job and batch service: creation, cancel, retry and the queries the worker relies on."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time

import pytest

from atelier import jobs, storage
from atelier.registry import InvalidParams, UnknownModel


def _make_request(**overrides) -> jobs.BatchRequest:
    fields = {
        "model_id": "qwen-image-2.1-uc",
        "prompt": "a red fox in snow",
        "negative": "",
        "width": 1088,
        "height": 1920,
        "steps": 25,
        "cfg": 1.0,
        "seed_mode": "random",
        "seed": None,
        "count": 1,
    }
    fields.update(overrides)
    return jobs.BatchRequest(**fields)


def _insert_job(conn: sqlite3.Connection, *, status: str = "queued", backend_id: str = "qwen21-uc", **overrides) -> int:
    now = overrides.pop("created_at", time.time())
    cursor = conn.execute(
        "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) VALUES (?, 'm', 'generate', '{}', 1)",
        (now,),
    )
    batch_id = cursor.lastrowid
    params = {"seed": 7, "prompt": "p", "negative": "n"}
    fields = {
        "batch_id": batch_id,
        "model_id": "qwen-image-2.1-uc",
        "backend_id": backend_id,
        "kind": "generate",
        "params_json": json.dumps(params),
        "graph_json": "{}",
        "status": status,
        "created_at": now,
        "call_id": None,
        "submitted_at": None,
    }
    fields.update(overrides)
    cursor = conn.execute(
        "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, status, created_at, "
        "call_id, submitted_at) VALUES (:batch_id, :model_id, :backend_id, :kind, :params_json, :graph_json, "
        ":status, :created_at, :call_id, :submitted_at)",
        fields,
    )
    return cursor.lastrowid


# -- create_batch --------------------------------------------------------------------------------------


def test_create_batch_random_mode_draws_n_distinct_seeds(conn, registry, settings, rng):
    batch_id = jobs.create_batch(conn, registry, settings, _make_request(seed_mode="random", count=4), rng)
    rows = conn.execute("SELECT params_json, graph_json FROM jobs WHERE batch_id = ?", (batch_id,)).fetchall()
    assert len(rows) == 4
    seeds = [json.loads(row["params_json"])["seed"] for row in rows]
    assert len(set(seeds)) == 4
    for row in rows:
        assert json.loads(row["graph_json"])  # a graph was built and stored for every job


def test_create_batch_fixed_mode_uses_sequential_seeds(conn, registry, settings, rng):
    batch_id = jobs.create_batch(conn, registry, settings, _make_request(seed_mode="fixed", seed=100, count=3), rng)
    rows = conn.execute("SELECT params_json FROM jobs WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
    seeds = [json.loads(row["params_json"])["seed"] for row in rows]
    assert seeds == [100, 101, 102]


class _StubRng:
    """Mimics only the one method create_batch calls, and can be made to violate distinctness -- if
    the random-mode branch stopped calling .sample() and used something like repeated .randint() calls
    instead, a real random.Random would hide that, but a stub that returns exactly what it's told won't."""

    def __init__(self, values: list[int]) -> None:
        self._values = values
        self.calls: list[tuple[range, int]] = []

    def sample(self, population: range, k: int) -> list[int]:
        self.calls.append((population, k))
        return list(self._values[:k])


def test_create_batch_random_mode_calls_rng_sample_and_stores_exactly_what_it_returns(conn, registry, settings):
    stub = _StubRng([111, 111, 222, 222])  # deliberately not distinct
    jobs.create_batch(conn, registry, settings, _make_request(seed_mode="random", count=4), stub)
    rows = conn.execute("SELECT params_json FROM jobs ORDER BY id").fetchall()
    seeds = [json.loads(row["params_json"])["seed"] for row in rows]
    assert seeds == [111, 111, 222, 222]  # create_batch trusts the rng completely; it dedups nothing
    assert stub.calls == [(range(1, 2**31), 4)]


@pytest.mark.parametrize(
    "seed,count",
    [
        (-1, 1),  # below the range floor
        (2**63 - 1, 2),  # seed + count - 1 overflows the range ceiling
    ],
)
def test_create_batch_fixed_mode_rejects_seeds_outside_the_valid_range(conn, registry, settings, rng, seed, count):
    with pytest.raises(InvalidParams):
        jobs.create_batch(conn, registry, settings, _make_request(seed_mode="fixed", seed=seed, count=count), rng)


@pytest.mark.parametrize(
    "seed,count",
    [
        (0, 1),  # the floor itself is valid
        (2**63 - 2, 2),  # seed + count - 1 lands exactly on the ceiling
    ],
)
def test_create_batch_fixed_mode_accepts_seeds_at_the_valid_range_boundary(conn, registry, settings, rng, seed, count):
    batch_id = jobs.create_batch(
        conn, registry, settings, _make_request(seed_mode="fixed", seed=seed, count=count), rng
    )
    rows = conn.execute("SELECT params_json FROM jobs WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
    seeds = [json.loads(row["params_json"])["seed"] for row in rows]
    assert seeds == [seed + i for i in range(count)]


def test_create_batch_raises_unknown_model(conn, registry, settings, rng):
    with pytest.raises(UnknownModel):
        jobs.create_batch(conn, registry, settings, _make_request(model_id="nope"), rng)


def test_create_batch_raises_invalid_params_for_out_of_bounds_steps(conn, registry, settings, rng):
    with pytest.raises(InvalidParams):
        jobs.create_batch(conn, registry, settings, _make_request(steps=0), rng)


@pytest.mark.parametrize("count", [0, 9])
def test_create_batch_raises_for_count_out_of_range(conn, registry, settings, rng, count):
    with pytest.raises(ValueError):
        jobs.create_batch(conn, registry, settings, _make_request(count=count), rng)


def test_create_batch_raises_disk_guard_error_at_the_free_floor_and_inserts_no_row(conn, registry, settings, rng):
    # A floor set far above any real machine's free space trips deterministically off the real
    # filesystem, with no need to fake shutil.disk_usage itself.
    huge_floor_settings = dataclasses.replace(settings, min_free_gb=100_000_000)
    with pytest.raises(storage.DiskGuardError):
        jobs.create_batch(conn, registry, huge_floor_settings, _make_request(), rng)
    assert conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0


def test_create_batch_raises_disk_guard_error_at_the_cap_and_inserts_no_row(conn, registry, settings, rng):
    conn.execute(
        "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) VALUES (0, 'm', 'generate', '{}', 1)"
    )
    conn.execute(
        "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, status, created_at) "
        "VALUES (1, 'm', 'qwen21-uc', 'generate', '{}', '{}', 'done', 0)"
    )
    conn.execute(
        "INSERT INTO images (job_id, model_id, file_png, file_thumb, width, height, bytes, sha256, created_at) "
        "VALUES (1, 'm', 'a.png', 'a.webp', 8, 8, 1, 'sha', 0)"
    )
    tiny_cap_settings = dataclasses.replace(settings, data_cap_gb=0)
    with pytest.raises(storage.DiskGuardError):
        jobs.create_batch(conn, registry, tiny_cap_settings, _make_request(), rng)
    assert conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"] == 1  # only the pre-seeded one
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 1


# -- cancel_job -----------------------------------------------------------------------------------------


def test_cancel_job_moves_a_queued_job_to_cancelled(conn):
    job_id = _insert_job(conn, status="queued")
    conn.commit()  # cancel_job runs its own BEGIN IMMEDIATE unit of work
    previous = jobs.cancel_job(conn, job_id)
    assert previous == "queued"
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"] == "cancelled"


def test_cancel_job_moves_a_submitted_job_to_cancelled(conn):
    job_id = _insert_job(conn, status="submitted", call_id="fake-1", submitted_at=time.time())
    conn.commit()
    previous = jobs.cancel_job(conn, job_id)
    assert previous == "submitted"
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"] == "cancelled"


def test_cancel_job_raises_unknown_job_for_a_missing_id(conn):
    with pytest.raises(jobs.UnknownJob):
        jobs.cancel_job(conn, 999999)


def test_cancel_job_leaves_a_done_job_untouched(conn):
    job_id = _insert_job(conn, status="done")
    conn.commit()
    previous = jobs.cancel_job(conn, job_id)
    assert previous == "done"
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"] == "done"


def test_cancel_job_begins_an_immediate_transaction_before_its_select(conn):
    """The exclusive lock must be acquired before the SELECT, not just before the UPDATE: that is what
    stops a concurrent writer from changing the row in between. set_trace_callback observes the actual
    SQL cancel_job sends, in order, without needing to intercept the (C-level, unpatchable) connection."""
    job_id = _insert_job(conn, status="queued")
    conn.commit()

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        jobs.cancel_job(conn, job_id)
    finally:
        conn.set_trace_callback(None)

    assert statements[0].strip().upper() == "BEGIN IMMEDIATE"
    select_index = next(i for i, s in enumerate(statements) if s.strip().upper().startswith("SELECT"))
    assert select_index == 1  # nothing runs between the immediate lock and the read it protects


def test_cancel_job_rolls_back_cleanly_when_the_job_is_unknown(conn):
    with pytest.raises(jobs.UnknownJob):
        jobs.cancel_job(conn, 999999)
    assert conn.in_transaction is False

    # The connection must still be usable afterward: an aborted attempt must not leave a stray lock.
    job_id = _insert_job(conn, status="queued")
    conn.commit()
    previous = jobs.cancel_job(conn, job_id)
    assert previous == "queued"
    assert conn.in_transaction is False


# -- retry_job ------------------------------------------------------------------------------------------


def test_retry_job_creates_a_new_job_with_the_same_params_seed_and_graph(conn):
    job_id = _insert_job(conn, status="failed", graph_json=json.dumps({"1": "x"}))
    new_id = jobs.retry_job(conn, job_id)
    original = conn.execute("SELECT params_json, graph_json, attempt FROM jobs WHERE id = ?", (job_id,)).fetchone()
    new = conn.execute("SELECT params_json, graph_json, attempt, retry_of, status FROM jobs WHERE id = ?", (new_id,)).fetchone()
    assert new["params_json"] == original["params_json"]
    assert new["graph_json"] == original["graph_json"]
    assert new["retry_of"] == job_id
    assert new["attempt"] == original["attempt"] + 1
    assert new["status"] == "queued"


def test_retry_job_works_on_a_cancelled_job(conn):
    job_id = _insert_job(conn, status="cancelled")
    new_id = jobs.retry_job(conn, job_id)
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (new_id,)).fetchone()["status"] == "queued"


def test_retry_job_raises_retry_not_allowed_for_a_queued_job(conn):
    job_id = _insert_job(conn, status="queued")
    with pytest.raises(jobs.RetryNotAllowed):
        jobs.retry_job(conn, job_id)


def test_retry_job_raises_unknown_job_for_a_missing_id(conn):
    with pytest.raises(jobs.UnknownJob):
        jobs.retry_job(conn, 999999)


# -- waiting reasons and submission ------------------------------------------------------------------------


def test_note_waiting_writes_reason_only_to_queued_jobs_of_that_backend(conn):
    queued = _insert_job(conn, status="queued", backend_id="qwen21-uc")
    other_backend = _insert_job(conn, status="queued", backend_id="other-backend")
    submitted = _insert_job(conn, status="submitted", backend_id="qwen21-uc", call_id="c", submitted_at=time.time())

    jobs.note_waiting(conn, "qwen21-uc", "Modal unavailable, retrying: x")

    assert conn.execute("SELECT error FROM jobs WHERE id = ?", (queued,)).fetchone()["error"] is not None
    assert conn.execute("SELECT error FROM jobs WHERE id = ?", (other_backend,)).fetchone()["error"] is None
    assert conn.execute("SELECT error FROM jobs WHERE id = ?", (submitted,)).fetchone()["error"] is None


def test_mark_submitted_sets_call_id_and_clears_error(conn):
    job_id = _insert_job(conn, status="queued")
    conn.execute("UPDATE jobs SET error = 'waiting: x' WHERE id = ?", (job_id,))
    jobs.mark_submitted(conn, job_id, "fake-42", 1000.0)
    row = conn.execute("SELECT status, call_id, submitted_at, error FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "submitted"
    assert row["call_id"] == "fake-42"
    assert row["submitted_at"] == 1000.0
    assert row["error"] is None


def test_mark_submitted_is_a_no_op_for_a_job_no_longer_queued(conn):
    job_id = _insert_job(conn, status="cancelled")
    jobs.mark_submitted(conn, job_id, "fake-42", 1000.0)
    row = conn.execute("SELECT status, call_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "cancelled"
    assert row["call_id"] is None


# -- stale queue and terminal writes -------------------------------------------------------------------------


def test_fail_stale_queued_fails_old_jobs_with_their_last_reason(conn):
    now = time.time()
    old_job = _insert_job(conn, status="queued", created_at=now - 3600, backend_id="qwen21-uc")
    conn.execute("UPDATE jobs SET error = 'Modal unavailable, retrying: boom' WHERE id = ?", (old_job,))
    fresh_job = _insert_job(conn, status="queued", created_at=now, backend_id="qwen21-uc")

    jobs.fail_stale_queued(conn, "qwen21-uc", older_than=now - 1800)

    old_row = conn.execute("SELECT status, error FROM jobs WHERE id = ?", (old_job,)).fetchone()
    assert old_row["status"] == "failed"
    assert "not dispatched" in old_row["error"]
    assert "boom" in old_row["error"]

    fresh_row = conn.execute("SELECT status FROM jobs WHERE id = ?", (fresh_job,)).fetchone()
    assert fresh_row["status"] == "queued"


def test_fail_stale_queued_uses_a_placeholder_when_no_reason_was_recorded(conn):
    now = time.time()
    old_job = _insert_job(conn, status="queued", created_at=now - 3600)
    jobs.fail_stale_queued(conn, "qwen21-uc", older_than=now - 1800)
    row = conn.execute("SELECT error FROM jobs WHERE id = ?", (old_job,)).fetchone()
    assert "no reason recorded" in row["error"]


def test_fail_queued_marks_the_job_failed_with_the_error_text(conn):
    job_id = _insert_job(conn, status="queued")
    jobs.fail_queued(conn, job_id, "Could not start: boom")
    row = conn.execute("SELECT status, error FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "Could not start: boom"


def test_fail_submitted_marks_the_job_failed_with_the_error_text(conn):
    job_id = _insert_job(conn, status="submitted", call_id="c", submitted_at=time.time())
    jobs.fail_submitted(conn, job_id, "backend timed out", 2000.0)
    row = conn.execute("SELECT status, error, finished_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "backend timed out"
    assert row["finished_at"] == 2000.0


# -- worker read queries ---------------------------------------------------------------------------------


def test_count_submitted_and_has_queued_and_next_queued(conn):
    _insert_job(conn, status="queued")
    _insert_job(conn, status="queued")
    _insert_job(conn, status="submitted", call_id="c", submitted_at=time.time())

    assert jobs.count_submitted(conn, "qwen21-uc") == 1
    assert jobs.has_queued(conn, "qwen21-uc") is True
    assert len(jobs.next_queued(conn, "qwen21-uc", limit=1)) == 1
    assert len(jobs.next_queued(conn, "qwen21-uc", limit=10)) == 2


def test_list_submitted_returns_only_submitted_jobs_across_backends(conn):
    _insert_job(conn, status="queued")
    submitted_a = _insert_job(conn, status="submitted", call_id="a", submitted_at=time.time(), backend_id="qwen21-uc")
    submitted_b = _insert_job(conn, status="submitted", call_id="b", submitted_at=time.time(), backend_id="other")

    ids = {row["id"] for row in jobs.list_submitted(conn)}
    assert ids == {submitted_a, submitted_b}
