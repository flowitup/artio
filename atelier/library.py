"""Gallery listing, batch and image detail, and image delete. Presets, stars, tags and search are
expected to grow here later; for now it covers only the plain reads and the delete-cascade the UI needs.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from atelier import storage

PAGE_SIZE = 48


class UnknownImage(Exception):
    """Raised when an image id has no row."""


@dataclass(frozen=True, slots=True)
class BatchGroup:
    """One batch's images for the gallery grid, headed by the batch's own prompt excerpt and model."""

    batch_id: int
    model_id: str | None
    prompt: str
    created_at: float
    images: list[sqlite3.Row]


@dataclass(frozen=True, slots=True)
class BatchJobView:
    id: int
    status: str
    seed: int | None
    image_id: int | None


@dataclass(frozen=True, slots=True)
class BatchDetail:
    batch_id: int
    model_id: str | None
    prompt: str
    negative: str
    created_at: float
    jobs: list[BatchJobView]


@dataclass(frozen=True, slots=True)
class ImageDetail:
    id: int
    model_id: str | None
    prompt: str
    negative: str
    seed: int | None
    width: int
    height: int
    steps: int | None
    cfg: float | None
    duration_s: float | None
    est_cost_usd: float | None
    created_at: float
    batch_id: int


def list_batches(conn: sqlite3.Connection, model_id: str | None, page: int) -> list[BatchGroup]:
    """Batches newest first, each with its images, optionally filtered to one model. Paginates over
    images (48 per page, newest completed first) and groups the page's rows by batch_id: a batch's
    images can finish out of creation order (concurrent Modal renders), so grouping by id matched
    rather than by row adjacency is required for correctness, not just tidiness."""
    offset = (page - 1) * PAGE_SIZE
    if model_id:
        rows = conn.execute(
            "SELECT images.*, jobs.batch_id AS batch_id FROM images JOIN jobs ON jobs.id = images.job_id "
            "WHERE images.model_id = ? ORDER BY images.id DESC LIMIT ? OFFSET ?",
            (model_id, PAGE_SIZE, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT images.*, jobs.batch_id AS batch_id FROM images JOIN jobs ON jobs.id = images.job_id "
            "ORDER BY images.id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, offset),
        ).fetchall()

    groups: dict[int, BatchGroup] = {}
    for row in rows:
        batch_id = row["batch_id"]
        group = groups.get(batch_id)
        if group is None:
            group = BatchGroup(
                batch_id=batch_id,
                model_id=row["model_id"],
                prompt=row["prompt"],
                created_at=row["created_at"],
                images=[],
            )
            groups[batch_id] = group
        group.images.append(row)
    return list(groups.values())


def batch_detail(conn: sqlite3.Connection, batch_id: int) -> BatchDetail | None:
    """The batch's own params plus every job it ever had (any status), each left-joined to its image."""
    batch = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if batch is None:
        return None
    base_params = json.loads(batch["base_params_json"])
    rows = conn.execute(
        "SELECT jobs.id, jobs.status, jobs.params_json, images.id AS image_id "
        "FROM jobs LEFT JOIN images ON images.job_id = jobs.id "
        "WHERE jobs.batch_id = ? ORDER BY jobs.id",
        (batch_id,),
    ).fetchall()
    jobs = [
        BatchJobView(
            id=row["id"],
            status=row["status"],
            seed=json.loads(row["params_json"]).get("seed"),
            image_id=row["image_id"],
        )
        for row in rows
    ]
    return BatchDetail(
        batch_id=batch_id,
        model_id=batch["model_id"],
        prompt=base_params.get("prompt", ""),
        negative=base_params.get("negative", ""),
        created_at=batch["created_at"],
        jobs=jobs,
    )


def image_detail(conn: sqlite3.Connection, image_id: int) -> ImageDetail | None:
    """Full metadata for one image: the saved file's own dimensions plus its job's steps, cfg, timing
    and cost."""
    row = conn.execute(
        "SELECT images.*, jobs.batch_id AS batch_id, jobs.params_json AS params_json, "
        "jobs.duration_s AS duration_s, jobs.est_cost_usd AS est_cost_usd "
        "FROM images JOIN jobs ON jobs.id = images.job_id WHERE images.id = ?",
        (image_id,),
    ).fetchone()
    if row is None:
        return None
    params = json.loads(row["params_json"])
    return ImageDetail(
        id=row["id"],
        model_id=row["model_id"],
        prompt=row["prompt"],
        negative=row["negative"],
        seed=row["seed"],
        width=row["width"],
        height=row["height"],
        steps=params.get("steps"),
        cfg=params.get("cfg"),
        duration_s=row["duration_s"],
        est_cost_usd=row["est_cost_usd"],
        created_at=row["created_at"],
        batch_id=row["batch_id"],
    )


def delete_image(conn: sqlite3.Connection, data_dir: Path, image_id: int) -> None:
    """Deletes the image row, its job row, and the batch if it has no job left, in one transaction, then
    removes the PNG and thumbnail files. Raises UnknownImage if the id doesn't exist.

    retry_of references naming the deleted job are cleared to NULL by the schema's ON DELETE SET NULL,
    so a retry of a since-deleted original keeps its own row untouched."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT job_id, file_png, file_thumb, width, height, bytes, sha256 FROM images WHERE id = ?",
            (image_id,),
        ).fetchone()
        if row is None:
            raise UnknownImage(image_id)
        job_id = row["job_id"]
        batch_id = conn.execute("SELECT batch_id FROM jobs WHERE id = ?", (job_id,)).fetchone()["batch_id"]

        conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        remaining = conn.execute("SELECT 1 FROM jobs WHERE batch_id = ? LIMIT 1", (batch_id,)).fetchone()
        if remaining is None:
            conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()

    saved = storage.SavedImage(
        file_png=Path(row["file_png"]),
        file_thumb=Path(row["file_thumb"]),
        width=row["width"],
        height=row["height"],
        bytes=row["bytes"],
        sha256=row["sha256"],
    )
    storage.delete_files(data_dir, saved)
