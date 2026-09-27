"""The job and batch service.

Every state change is a single `UPDATE ... WHERE status IN (...)` whose rowcount decides the outcome, so
two writers (a user cancel and the poller, say) can never both believe they made the transition: at most
one UPDATE affects a row, and the loser's rowcount is 0.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from dataclasses import dataclass
from typing import Literal

from artio import custom_workflows
from artio.config import Settings
from artio.registry import InvalidParams, Registry
from artio.storage import DiskGuardError, SavedImage, disk_status
from artio.workflows import GenParams

_ERROR_TEXT_LIMIT = 2000
_MAX_SEED = 2**63 - 1


class UnknownJob(Exception):
    """Raised when a job id does not exist."""


class RetryNotAllowed(Exception):
    """Raised when retry_job is called on a job that is not failed or cancelled."""


@dataclass(frozen=True, slots=True)
class BatchRequest:
    """One generate submission: a model, its parameters, a seed strategy and how many jobs to create."""

    model_id: str
    prompt: str
    negative: str
    width: int
    height: int
    steps: int
    cfg: float
    seed_mode: Literal["random", "fixed"]
    seed: int | None
    count: int


def _seeds_for(seed_mode: str, seed: int | None, count: int, rng: random.Random) -> list[int]:
    """Distinct random seeds, or an incrementing run starting at a fixed seed. Shared by generate
    batches (BatchRequest, below) and custom-workflow batches (create_workflow_batch)."""
    if seed_mode == "random":
        return rng.sample(range(1, 2**31), count)
    if seed_mode != "fixed":
        raise ValueError(f"seed_mode must be 'random' or 'fixed', got {seed_mode!r}")
    if seed is None:
        raise ValueError("fixed seed mode requires a seed")
    last = seed + count - 1
    if seed < 0 or last > _MAX_SEED:
        raise InvalidParams(
            f"fixed seed {seed} with count {count} must satisfy 0 <= seed and "
            f"seed + count - 1 <= {_MAX_SEED}, got a range ending at {last}"
        )
    return [seed + i for i in range(count)]


def _make_seeds(request: BatchRequest, rng: random.Random) -> list[int]:
    return _seeds_for(request.seed_mode, request.seed, request.count, rng)


def create_batch(
    conn: sqlite3.Connection,
    registry: Registry,
    settings: Settings,
    request: BatchRequest,
    rng: random.Random,
) -> int:
    """Validate against the model schema, check the disk guard, then insert one batch plus N queued jobs
    with distinct seeds. Raises DiskGuardError and inserts no row when the guard is tripped."""
    if not (1 <= request.count <= 8):
        raise ValueError(f"count must be between 1 and 8, got {request.count}")

    model = registry.model(request.model_id)
    backend = registry.backend_for(model)
    model.param_schema.validate(width=request.width, height=request.height, steps=request.steps, cfg=request.cfg)

    status = disk_status(conn, settings)
    if status.refusal:
        raise DiskGuardError(status.refusal)

    seeds = _make_seeds(request, rng)
    now = time.time()
    base_params = {
        "prompt": request.prompt,
        "negative": request.negative,
        "width": request.width,
        "height": request.height,
        "steps": request.steps,
        "cfg": request.cfg,
    }
    cursor = conn.execute(
        "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) VALUES (?, ?, 'generate', ?, ?)",
        (now, model.id, json.dumps(base_params), request.count),
    )
    batch_id = cursor.lastrowid
    assert batch_id is not None

    for seed in seeds:
        params = {**base_params, "seed": seed}
        graph = model.build_graph(
            GenParams(
                prompt=request.prompt,
                negative=request.negative,
                width=request.width,
                height=request.height,
                steps=request.steps,
                seed=seed,
                cfg=request.cfg,
            )
        )
        conn.execute(
            "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, status, created_at) "
            "VALUES (?, ?, ?, 'generate', ?, ?, 'queued', ?)",
            (batch_id, model.id, backend.id, json.dumps(params), json.dumps(graph, ensure_ascii=False), now),
        )
    return batch_id


def cancel_job(conn: sqlite3.Connection, job_id: int) -> str:
    """Mark a queued or submitted job cancelled in the DB only. Never touches Modal: a queued job simply
    never gets spawned, and a submitted job's late result is discarded by complete()'s rowcount check.
    Returns the job's previous status, so the caller can explain that a running render may still finish.

    Runs as its own BEGIN IMMEDIATE unit of work: without it, another writer (the poller completing the
    same job, say) could change the row between the SELECT and the UPDATE, and the rowcount would then
    say "nothing changed" while this function still reported the stale, now-wrong previous status."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise UnknownJob(job_id)
        conn.execute(
            "UPDATE jobs SET status = 'cancelled' WHERE id = ? AND status IN ('queued', 'submitted')",
            (job_id,),
        )
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return row["status"]


def retry_job(conn: sqlite3.Connection, job_id: int) -> int:
    """Create a new job in the same batch with the same params, seed and graph. Only failed or cancelled
    jobs may be retried. Returns the new job's id."""
    row = conn.execute(
        "SELECT status, batch_id, model_id, backend_id, kind, params_json, graph_json, workflow_id, attempt "
        "FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise UnknownJob(job_id)
    if row["status"] not in ("failed", "cancelled"):
        raise RetryNotAllowed(f"job {job_id} is {row['status']}, not failed or cancelled")

    cursor = conn.execute(
        "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, workflow_id, "
        "status, attempt, retry_of, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
        (
            row["batch_id"],
            row["model_id"],
            row["backend_id"],
            row["kind"],
            row["params_json"],
            row["graph_json"],
            row["workflow_id"],
            row["attempt"] + 1,
            job_id,
            time.time(),
        ),
    )
    new_id = cursor.lastrowid
    assert new_id is not None
    return new_id


def note_waiting(conn: sqlite3.Connection, backend_id: str, reason: str) -> None:
    """Writes the last dispatch problem into `error` on that backend's queued jobs, so the UI can show
    "waiting: <reason>" instead of leaving them silently queued."""
    conn.execute(
        "UPDATE jobs SET error = ? WHERE backend_id = ? AND status = 'queued'",
        (reason[:_ERROR_TEXT_LIMIT], backend_id),
    )


def mark_submitted(conn: sqlite3.Connection, job_id: int, call_id: str, now: float) -> None:
    """A job cancelled meanwhile stays cancelled: this only affects rows still 'queued', so the render
    that Modal starts for it is simply never read."""
    conn.execute(
        "UPDATE jobs SET status = 'submitted', call_id = ?, submitted_at = ?, error = NULL "
        "WHERE id = ? AND status = 'queued'",
        (call_id, now, job_id),
    )


def fail_queued(conn: sqlite3.Connection, job_id: int, error: str) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ? AND status = 'queued'",
        (error[:_ERROR_TEXT_LIMIT], time.time(), job_id),
    )


def fail_submitted(conn: sqlite3.Connection, job_id: int, error: str, now: float) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ? AND status = 'submitted'",
        (error[:_ERROR_TEXT_LIMIT], now, job_id),
    )


def fail_stale_queued(conn: sqlite3.Connection, backend_id: str, older_than: float) -> None:
    """Fails queued jobs older than 30 minutes with "not dispatched: <last reason>", so a backend that
    never recovers doesn't leave jobs waiting forever with no visible end."""
    rows = conn.execute(
        "SELECT id, error FROM jobs WHERE backend_id = ? AND status = 'queued' AND created_at < ?",
        (backend_id, older_than),
    ).fetchall()
    now = time.time()
    for row in rows:
        reason = row["error"] or "no reason recorded"
        conn.execute(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ? AND status = 'queued'",
            (f"not dispatched: {reason}"[:_ERROR_TEXT_LIMIT], now, row["id"]),
        )


def complete(conn: sqlite3.Connection, job: sqlite3.Row, saved: SavedImage, now: float, registry: Registry) -> bool:
    """Marks a submitted job done and inserts its image row. Returns False if the job was cancelled
    meanwhile (rowcount 0), so the caller knows to discard the just-saved files instead."""
    duration_s = now - job["submitted_at"]
    backend = registry.backends[job["backend_id"]]
    est_cost_usd = duration_s * backend.usd_per_hour / 3600

    cursor = conn.execute(
        "UPDATE jobs SET status = 'done', finished_at = ?, duration_s = ?, est_cost_usd = ? "
        "WHERE id = ? AND status = 'submitted'",
        (now, duration_s, est_cost_usd, job["id"]),
    )
    if cursor.rowcount == 0:
        return False

    params = json.loads(job["params_json"])
    conn.execute(
        "INSERT INTO images (job_id, model_id, file_png, file_thumb, width, height, bytes, sha256, seed, "
        "prompt, negative, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job["id"],
            job["model_id"],
            str(saved.file_png),
            str(saved.file_thumb),
            saved.width,
            saved.height,
            saved.bytes,
            saved.sha256,
            params.get("seed"),
            params.get("prompt", ""),
            params.get("negative", ""),
            now,
        ),
    )
    return True


def count_submitted(conn: sqlite3.Connection, backend_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE backend_id = ? AND status = 'submitted'", (backend_id,)
    ).fetchone()
    return row["n"]


def has_queued(conn: sqlite3.Connection, backend_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE backend_id = ? AND status = 'queued' LIMIT 1", (backend_id,)
    ).fetchone()
    return row is not None


def next_queued(conn: sqlite3.Connection, backend_id: str, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE backend_id = ? AND status = 'queued' ORDER BY id LIMIT ?",
        (backend_id, limit),
    ).fetchall()


def list_submitted(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE status = 'submitted' ORDER BY id").fetchall()


def list_recent(conn: sqlite3.Connection, *, batch_id: int | None = None, limit: int = 50) -> list[sqlite3.Row]:
    """Jobs for the queue view, each left-joined to its image (NULL unless the job is done). One
    batch's jobs, newest first, when batch_id is given; otherwise the most recent jobs across every
    batch, capped at limit."""
    if batch_id is not None:
        return conn.execute(
            "SELECT jobs.*, images.id AS image_id FROM jobs LEFT JOIN images ON images.job_id = jobs.id "
            "WHERE jobs.batch_id = ? ORDER BY jobs.id DESC",
            (batch_id,),
        ).fetchall()
    return conn.execute(
        "SELECT jobs.*, images.id AS image_id FROM jobs LEFT JOIN images ON images.job_id = jobs.id "
        "ORDER BY jobs.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def active_counts(conn: sqlite3.Connection, backend_id: str) -> tuple[int, int]:
    """Counts of a backend's active jobs, for Stop's confirmation prompt: (queued, submitted)."""
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued, "
        "SUM(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END) AS running "
        "FROM jobs WHERE backend_id = ? AND status IN ('queued', 'submitted')",
        (backend_id,),
    ).fetchone()
    return row["queued"] or 0, row["running"] or 0


def cancel_all_for_backend(conn: sqlite3.Connection, backend_id: str) -> list[str]:
    """Marks every queued and submitted job on a backend cancelled and returns the call IDs of the
    submitted ones -- Stop sends Modal cancels for those, plus its own ping's call ID, all at once.
    Queued jobs (no call_id yet) are cancelled here too: otherwise the dispatcher would spawn them
    moments later and cold-start the very backend Stop just stopped.

    A single UPDATE ... RETURNING, so this is atomic on its own in any caller context -- unlike
    cancel_job's own BEGIN IMMEDIATE (needed there because cancel_job runs a separate SELECT first),
    this needs no explicit transaction, so it never conflicts with an implicit one an earlier
    statement in the caller's own session() block (e.g. gpu.clear_warm()) may already have opened."""
    rows = conn.execute(
        "UPDATE jobs SET status = 'cancelled' WHERE backend_id = ? AND status IN ('queued', 'submitted') "
        "RETURNING call_id",
        (backend_id,),
    ).fetchall()
    return [row["call_id"] for row in rows if row["call_id"] is not None]


def _graph_seed_for_display(graph: dict, targets: list[tuple[str, str]]) -> int | None:
    """The seed already baked into a 'keep' run's graph, read (not written) for display in the queue
    and job views. None when the graph has no seed input at all -- 'keep' still runs it as-is."""
    if not targets:
        return None
    node_id, key = targets[0]
    return graph[node_id]["inputs"][key]


def create_workflow_batch(
    conn: sqlite3.Connection,
    registry: Registry,
    settings: Settings,
    workflow: custom_workflows.StoredWorkflow,
    seed_mode: Literal["random", "fixed", "keep"],
    seed: int | None,
    count: int,
    rng: random.Random,
) -> int:
    """Validates a custom-workflow run, checks the disk guard, then inserts one batch plus N queued
    jobs with kind='workflow' and no model_id (the schema's CHECK allows this exactly for this kind).
    Each job's graph is the workflow's own graph with its seed override applied (with_seed), except
    in 'keep' mode, which sends the graph unchanged -- allowed only when count == 1, since running an
    unmodified graph more than once would just render the same image N times."""
    if not (1 <= count <= 8):
        raise ValueError(f"count must be between 1 and 8, got {count}")
    if seed_mode == "keep" and count != 1:
        raise ValueError("'keep' seed mode is only allowed with count 1.")
    if workflow.backend_id not in registry.backends:
        raise ValueError(f"Unknown backend {workflow.backend_id!r}.")

    # validate_api_graph already refuses this depth at upload time, but a graph stored before that
    # check existed (or written straight to the database) must still be refused here, before
    # with_seed's copy.deepcopy would otherwise hit Python's recursion limit on an already-paid-for
    # dispatch -- checked with the same iterative (non-recursive) walk validate_api_graph itself uses.
    if custom_workflows.graph_depth(workflow.graph) > custom_workflows.MAX_GRAPH_DEPTH:
        raise ValueError(
            f"This stored graph is nested more than {custom_workflows.MAX_GRAPH_DEPTH} levels deep "
            "and can no longer be run safely; delete and re-upload it."
        )

    targets = custom_workflows.seed_targets(workflow.graph)
    if count > 1 and not targets:
        raise ValueError(
            "This graph has no seed input (KSampler, KSamplerAdvanced or RandomNoise with a plain "
            "integer value), so every run would render the same image. Use count 1, or upload a "
            "graph with a seed input."
        )

    status = disk_status(conn, settings)
    if status.refusal:
        raise DiskGuardError(status.refusal)

    seeds: list[int | None]
    if seed_mode == "keep":
        kept_seed = _graph_seed_for_display(workflow.graph, targets)
        # Fixed and random seeds are already range-checked (_seeds_for); a kept seed comes straight
        # from the graph itself, which can hold anything json.loads accepts, including ComfyUI's own
        # 0..2**64-1 range. images.seed is a plain SQLite INTEGER (signed 64-bit): binding a value at
        # or above 2**63 only fails once jobs.complete() tries to store the finished render, after
        # Modal has already been paid for it. Catching it here, before the batch is even created,
        # means the owner sees the problem instead of losing a render to it.
        if kept_seed is not None and not (0 <= kept_seed <= _MAX_SEED):
            raise ValueError(
                f"This graph's own seed ({kept_seed}) is outside the range 0..{_MAX_SEED} that this "
                "app can store; edit the graph's seed or run it with a different seed mode."
            )
        seeds = [kept_seed]
    else:
        seeds = list(_seeds_for(seed_mode, seed, count, rng))

    backend = registry.backends[workflow.backend_id]
    now = time.time()
    cursor = conn.execute(
        "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) "
        "VALUES (?, NULL, 'workflow', ?, ?)",
        (now, json.dumps({"workflow": workflow.name}), count),
    )
    batch_id = cursor.lastrowid
    assert batch_id is not None

    for s in seeds:
        graph = workflow.graph if seed_mode == "keep" else custom_workflows.with_seed(workflow.graph, s)
        params = {"workflow": workflow.name, "seed": s}
        conn.execute(
            "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, "
            "workflow_id, status, created_at) VALUES (?, NULL, ?, 'workflow', ?, ?, ?, 'queued', ?)",
            (batch_id, backend.id, json.dumps(params), json.dumps(graph, ensure_ascii=False), workflow.id, now),
        )
    return batch_id


def has_active(conn: sqlite3.Connection, *, batch_id: int | None = None) -> bool:
    """True while some job the queue view would show is still queued or submitted. The /queue/rows
    partial polls while this is true and stops (286) once it turns false."""
    if batch_id is not None:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE batch_id = ? AND status IN ('queued', 'submitted') LIMIT 1",
            (batch_id,),
        ).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM jobs WHERE status IN ('queued', 'submitted') LIMIT 1").fetchone()
    return row is not None
