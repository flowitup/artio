"""Gallery listing, batch and image detail, image delete, presets, stars, tags and search.

Search stays in sync with no service code here: `atelier/migrations/0001_init.sql`'s triggers already
maintain `images_fts` on image insert/delete and on tag add/remove. This module only ever narrows the
result set through `images_fts MATCH`; ordering is always `images.id DESC`, the same as the plain,
unfiltered gallery, so there is exactly one pagination scheme for both the gallery and (later) the API.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from atelier import storage

PAGE_SIZE = 48

# A reserved `model` filter value (never a real registry model id) that selects images from a
# custom-workflow job instead: those jobs have model_id NULL (see jobs.create_workflow_batch).
WORKFLOW_MODEL_FILTER = "__workflows__"

MAX_TAGS_PER_IMAGE = 20
MAX_PRESET_NAME_LEN = 200
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,31}$")


class UnknownImage(Exception):
    """Raised when an image id has no row."""


class UnknownPreset(Exception):
    """Raised when a preset id has no row."""


class InvalidTag(ValueError):
    """Raised when a submitted tag fails normalization, or the per-image count is exceeded."""


class InvalidPresetName(ValueError):
    """Raised when a preset name is empty or exceeds the length cap."""


@dataclass(frozen=True, slots=True)
class BatchGroup:
    """One batch's images for the gallery grid, headed by the batch's own prompt excerpt and model.
    workflow_name is set only for a custom-workflow batch (model_id is NULL for those); it is the
    header's fallback when there is no prompt to show."""

    batch_id: int
    model_id: str | None
    prompt: str
    workflow_name: str | None
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
    job_id: int
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
    kind: str
    backend_id: str | None
    workflow_id: int | None
    workflow_name: str | None
    starred: bool
    tags: list[str]


@dataclass(frozen=True, slots=True)
class Preset:
    id: int
    name: str
    model_id: str
    params: dict
    created_at: float
    updated_at: float


def fts_query(text: str) -> str | None:
    """Turns free user text into a safe, quoted, prefix-matched FTS5 MATCH expression restricted to
    the prompt and tags columns: each term is double-quoted (inner quotes doubled) and given a
    trailing '*' prefix operator, so FTS5 syntax (NEAR, AND, OR, -, parentheses, bare quotes) in the
    input is always plain text, never a query operator. Adjacent quoted terms combine with FTS5's
    default AND, so "red fox" matches only images with both words. Returns None for text with no
    terms (including one that is only a NUL byte, stripped below), so callers can skip the MATCH
    filter entirely and treat it exactly like a blank search box.

    The column restriction `{prompt tags}:` deliberately excludes `negative`: images_fts also indexes
    the negative prompt, and searching it would surface images made specifically to exclude the
    searched term -- a negative prompt of "cat, blurry" is not a sensible match for a search for
    "cat". The spec's own wording is "search by prompt text or tag"."""
    cleaned = text.replace("\x00", "")  # FTS5 reads MATCH as a C string; an embedded NUL truncates it
    terms = cleaned.split()
    if not terms:
        return None
    expr = " ".join('"' + t.replace('"', '""') + '"*' for t in terms)
    return f"{{prompt tags}}: ({expr})"


def search(
    conn: sqlite3.Connection,
    *,
    q: str | None,
    tag: str | None,
    starred: bool,
    model: str | None,
    limit: int,
    offset: int,
) -> list[sqlite3.Row]:
    """Images newest first, each with its batch id and workflow name (if any), filtered by any
    combination of a quoted prefix-matched prompt/tag search (q), an exact tag name, a starred flag
    and a model id (or WORKFLOW_MODEL_FILTER for custom-workflow results). images_fts narrows the
    result set only: the order is always images.id DESC, matching the unfiltered gallery."""
    match = fts_query(q) if q else None
    # Tags are always stored lower-case and trimmed (normalize_tags); a free-text filter box must
    # match the same way, or "Beach"/" beach " would silently find nothing for a tag stored as "beach".
    tag = tag.strip().lower() if tag else tag

    joins = []
    if match is not None:
        joins.append("JOIN images_fts ON images_fts.rowid = images.id")
    joins.append("JOIN jobs ON jobs.id = images.job_id")
    joins.append("LEFT JOIN workflows ON workflows.id = jobs.workflow_id")
    if tag:
        joins.append("JOIN image_tags ON image_tags.image_id = images.id")
        joins.append("JOIN tags ON tags.id = image_tags.tag_id")

    conditions: list[str] = []
    params: list[object] = []
    if match is not None:
        conditions.append("images_fts MATCH ?")
        params.append(match)
    if tag:
        conditions.append("tags.name = ?")
        params.append(tag)
    if starred:
        conditions.append("images.starred = 1")
    if model == WORKFLOW_MODEL_FILTER:
        conditions.append("images.model_id IS NULL")
    elif model:
        conditions.append("images.model_id = ?")
        params.append(model)

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = (
        "SELECT images.*, jobs.batch_id AS batch_id, workflows.name AS workflow_name FROM images "
        + " ".join(joins)
        + where
        + " ORDER BY images.id DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    return conn.execute(sql, params).fetchall()


def _group_by_batch(rows: list[sqlite3.Row]) -> list[BatchGroup]:
    """Groups a newest-first page of image rows by their batch_id. A batch's images can finish out
    of creation order (concurrent Modal renders), so grouping by id matched rather than by row
    adjacency is required for correctness, not just tidiness."""
    groups: dict[int, BatchGroup] = {}
    for row in rows:
        batch_id = row["batch_id"]
        group = groups.get(batch_id)
        if group is None:
            group = BatchGroup(
                batch_id=batch_id,
                model_id=row["model_id"],
                prompt=row["prompt"],
                workflow_name=row["workflow_name"],
                created_at=row["created_at"],
                images=[],
            )
            groups[batch_id] = group
        group.images.append(row)
    return list(groups.values())


def gallery_page(
    conn: sqlite3.Connection,
    *,
    model_id: str | None,
    page: int,
    q: str | None = None,
    tag: str | None = None,
    starred: bool = False,
) -> tuple[list[BatchGroup], bool]:
    """One batch-grouped gallery page (48 images), newest first, under any combination of the
    search/tag/starred/model filters, plus whether a further page exists.

    Fetches one row past the page size to compute a real has_next without a second query or a
    separate COUNT(*): a full page whose (page_size + 1)-th row exists has a next page: a page with
    fewer than page_size + 1 rows is the last one, whatever the filters."""
    offset = (page - 1) * PAGE_SIZE
    rows = search(conn, q=q, tag=tag, starred=starred, model=model_id, limit=PAGE_SIZE + 1, offset=offset)
    has_next = len(rows) > PAGE_SIZE
    return _group_by_batch(rows[:PAGE_SIZE]), has_next


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
    """Full metadata for one image: the saved file's own dimensions, its job's steps/cfg/timing/cost,
    its workflow link (if it came from a custom-workflow job) and its star/tag state."""
    row = conn.execute(
        "SELECT images.*, jobs.batch_id AS batch_id, jobs.params_json AS params_json, "
        "jobs.duration_s AS duration_s, jobs.est_cost_usd AS est_cost_usd, jobs.kind AS kind, "
        "jobs.backend_id AS backend_id, jobs.workflow_id AS workflow_id, workflows.name AS workflow_name "
        "FROM images JOIN jobs ON jobs.id = images.job_id "
        "LEFT JOIN workflows ON workflows.id = jobs.workflow_id "
        "WHERE images.id = ?",
        (image_id,),
    ).fetchone()
    if row is None:
        return None
    params = json.loads(row["params_json"])
    tags = [
        r["name"]
        for r in conn.execute(
            "SELECT tags.name AS name FROM image_tags JOIN tags ON tags.id = image_tags.tag_id "
            "WHERE image_tags.image_id = ? ORDER BY tags.name",
            (image_id,),
        ).fetchall()
    ]
    return ImageDetail(
        id=row["id"],
        job_id=row["job_id"],
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
        kind=row["kind"],
        backend_id=row["backend_id"],
        workflow_id=row["workflow_id"],
        workflow_name=row["workflow_name"],
        starred=bool(row["starred"]),
        tags=tags,
    )


def delete_image(conn: sqlite3.Connection, data_dir: Path, image_id: int) -> None:
    """Deletes the image row, its job row, and the batch if it has no job left, in one transaction, then
    removes the PNG and thumbnail files. Raises UnknownImage if the id doesn't exist.

    retry_of references naming the deleted job are cleared to NULL by the schema's ON DELETE SET NULL,
    so a retry of a since-deleted original keeps its own row untouched. image_tags rows cascade with
    the image (ON DELETE CASCADE), and the images_ad trigger removes the images_fts row."""
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


# -- stars and tags -------------------------------------------------------------------------------


def toggle_star(conn: sqlite3.Connection, image_id: int) -> bool:
    """Flips images.starred and returns the new value. Raises UnknownImage if the id doesn't exist.

    Runs its own BEGIN IMMEDIATE unit of work, the same reasoning as jobs.cancel_job: without it, a
    concurrent writer could change the row between the SELECT and the UPDATE."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT starred FROM images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            raise UnknownImage(image_id)
        new_value = 0 if row["starred"] else 1
        conn.execute("UPDATE images SET starred = ? WHERE id = ?", (new_value, image_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return bool(new_value)


def normalize_tags(raw: str) -> list[str]:
    """Splits a comma-separated field into normalized (lower-case, trimmed), deduplicated tags, in
    first-seen order. Raises InvalidTag if any piece fails the pattern or the count exceeds the cap.
    An empty or all-blank input normalizes to an empty list (clearing every tag), never an error."""
    seen: list[str] = []
    for piece in raw.split(","):
        name = piece.strip().lower()
        if not name:
            continue
        if not _TAG_RE.fullmatch(name):
            raise InvalidTag(
                f"{name!r} is not a valid tag: 1-32 characters, lower-case letters, digits, spaces, "
                "'_' or '-', starting with a letter or digit."
            )
        if name not in seen:
            seen.append(name)
    if len(seen) > MAX_TAGS_PER_IMAGE:
        raise InvalidTag(f"at most {MAX_TAGS_PER_IMAGE} tags per image, got {len(seen)}.")
    return seen


def set_tags(conn: sqlite3.Connection, image_id: int, raw: str) -> list[str]:
    """Replaces image_id's whole tag set from a comma-separated field: normalizes first (raising
    InvalidTag before touching the database on a bad tag), then upserts into `tags` and replaces
    `image_tags` in one transaction, so the image_tags_ai/ad triggers observe the final set and
    images_fts.tags ends up correct. Raises UnknownImage if the image doesn't exist."""
    names = normalize_tags(raw)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM images WHERE id = ?", (image_id,)).fetchone() is None:
            raise UnknownImage(image_id)
        conn.execute("DELETE FROM image_tags WHERE image_id = ?", (image_id,))
        for name in names:
            conn.execute("INSERT INTO tags (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
            tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
            conn.execute("INSERT INTO image_tags (image_id, tag_id) VALUES (?, ?)", (image_id, tag_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return names


# -- presets ---------------------------------------------------------------------------------------


def _preset_from_row(row: sqlite3.Row) -> Preset:
    return Preset(
        id=row["id"],
        name=row["name"],
        model_id=row["model_id"],
        params=json.loads(row["params_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def save_preset(conn: sqlite3.Connection, name: str, model_id: str, params: dict, now: float) -> int:
    """Upserts a preset by name: an existing name overwrites its model and params and bumps
    updated_at; a new name inserts a fresh row (created_at = updated_at = now). Returns the id.

    No validation against a model's ParamSchema happens here: the generate form validates on submit,
    same as any remix or a hand-edited form would, so a preset saved under one model registry can
    still be loaded (and will then be checked) after the registry changes."""
    name = name.strip()
    if not name:
        raise InvalidPresetName("Preset name cannot be empty.")
    if len(name) > MAX_PRESET_NAME_LEN:
        raise InvalidPresetName(f"Preset name must be at most {MAX_PRESET_NAME_LEN} characters.")

    params_json = json.dumps(params)
    existing = conn.execute("SELECT id FROM presets WHERE name = ?", (name,)).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE presets SET model_id = ?, params_json = ?, updated_at = ? WHERE id = ?",
            (model_id, params_json, now, existing["id"]),
        )
        return existing["id"]
    cursor = conn.execute(
        "INSERT INTO presets (name, model_id, params_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (name, model_id, params_json, now, now),
    )
    preset_id = cursor.lastrowid
    assert preset_id is not None
    return preset_id


def list_presets(conn: sqlite3.Connection) -> list[Preset]:
    rows = conn.execute("SELECT * FROM presets ORDER BY name").fetchall()
    return [_preset_from_row(row) for row in rows]


def get_preset(conn: sqlite3.Connection, preset_id: int) -> Preset | None:
    row = conn.execute("SELECT * FROM presets WHERE id = ?", (preset_id,)).fetchone()
    return _preset_from_row(row) if row is not None else None


def delete_preset(conn: sqlite3.Connection, preset_id: int) -> None:
    cursor = conn.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
    if cursor.rowcount == 0:
        raise UnknownPreset(preset_id)
