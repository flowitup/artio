"""Chat sessions: generate batches grouped into conversations, each batch one turn.

A turn is an ordinary generate batch (jobs.create_batch) tagged with its session and the words the
owner typed. Those words become the batch's prompt in one of three ways:

- a new prompt: the text is the whole prompt;
- a refinement of the last turn: the text is appended to that turn's prompt, with fresh random seeds;
- a refinement of one image: the text is appended to that image's prompt and its seed is kept, so
  the first image of the new batch starts from the same composition.

Nothing here rewrites prompts beyond that plain append. Size, steps, CFG, negative and count carry
over from the previous turn unless the composer changes them.
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass
from typing import Literal

from artio import jobs
from artio.config import Settings
from artio.registry import Model, Registry, SizeTier

TITLE_LIMIT = 60
MESSAGE_LIMIT = 2000
SESSION_LIST_LIMIT = 30
DEFAULT_COUNT = 4
# How many recent finished jobs of a model feed the per-image cost estimate shown in the composer.
_ESTIMATE_SAMPLE = 20

Mode = Literal["new", "refine"]


class ChatError(ValueError):
    """Raised for a message the composer must reject, shown inline above the composer."""


@dataclass(frozen=True, slots=True)
class ChatSession:
    id: int
    title: str
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class TurnJob:
    id: int
    status: str
    seed: int | None
    error: str | None
    image_id: int | None


@dataclass(frozen=True, slots=True)
class Turn:
    batch_id: int
    session_id: int | None
    message: str | None
    model_id: str | None
    prompt: str
    negative: str
    width: int
    height: int
    steps: int
    cfg: float
    created_at: float
    jobs: list[TurnJob]
    duration_s: float | None
    est_cost_usd: float | None

    @property
    def active(self) -> bool:
        return any(job.status in ("queued", "submitted") for job in self.jobs)

    @property
    def done_count(self) -> int:
        return sum(1 for job in self.jobs if job.status == "done")

    @property
    def seed_label(self) -> str | None:
        """"seed 42", or "seeds 42–45" for a fixed run; None for unrelated random seeds, which say
        nothing useful as a range."""
        seeds = [job.seed for job in self.jobs if job.seed is not None]
        if len(seeds) == 1:
            return f"seed {seeds[0]}"
        if seeds and seeds == list(range(seeds[0], seeds[0] + len(seeds))):
            return f"seeds {seeds[0]}–{seeds[-1]}"
        return None


@dataclass(frozen=True, slots=True)
class Composer:
    """What the composer shows before the owner types: carried-over settings and an optional image
    being refined."""

    model: Model
    preset: str
    tier: str
    width: int
    height: int
    steps: int
    cfg: float
    negative: str
    count: int
    refine_image_id: int | None
    refine_thumb_prompt: str | None
    cost_per_megapixel_usd: float | None

    @property
    def per_image_cost_usd(self) -> float | None:
        rate = self.cost_per_megapixel_usd
        return None if rate is None else rate * self.width * self.height / 1_000_000

    def size_options(self) -> dict[str, list[int]]:
        """"shape|level" -> [width, height] for every pickable size, so the page can re-price the
        estimate when the picked size changes."""
        schema = self.model.param_schema
        options = {}
        for tier in schema.tiers or (SizeTier(""),):
            for p in tier.sizes or schema.presets:
                options[f"{p.name}|{tier.name}"] = [p.width, p.height]
        return options


def _session_from_row(row: sqlite3.Row) -> ChatSession:
    return ChatSession(id=row["id"], title=row["title"], created_at=row["created_at"], updated_at=row["updated_at"])


def list_sessions(conn: sqlite3.Connection, limit: int = SESSION_LIST_LIMIT) -> list[ChatSession]:
    rows = conn.execute("SELECT * FROM chat_sessions ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [_session_from_row(row) for row in rows]


def get_session(conn: sqlite3.Connection, session_id: int) -> ChatSession | None:
    row = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
    return _session_from_row(row) if row is not None else None


def latest_session_id(conn: sqlite3.Connection) -> int | None:
    sessions = list_sessions(conn, limit=1)
    return sessions[0].id if sessions else None


def delete_session(conn: sqlite3.Connection, session_id: int) -> None:
    """Removes the conversation only. Its batches stay (session_id goes NULL via the foreign key), so
    every image remains in the gallery."""
    conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))


def _turn_from_batch(conn: sqlite3.Connection, batch: sqlite3.Row) -> Turn:
    params = json.loads(batch["base_params_json"])
    rows = conn.execute(
        "SELECT jobs.id, jobs.status, jobs.params_json, jobs.error, jobs.duration_s, jobs.est_cost_usd, "
        "images.id AS image_id FROM jobs LEFT JOIN images ON images.job_id = jobs.id "
        "WHERE jobs.batch_id = ? ORDER BY jobs.id",
        (batch["id"],),
    ).fetchall()
    durations = [row["duration_s"] for row in rows if row["duration_s"] is not None]
    costs = [row["est_cost_usd"] for row in rows if row["est_cost_usd"] is not None]
    return Turn(
        batch_id=batch["id"],
        session_id=batch["session_id"],
        message=batch["message"],
        model_id=batch["model_id"],
        prompt=params.get("prompt", ""),
        negative=params.get("negative", ""),
        width=params.get("width", 0),
        height=params.get("height", 0),
        steps=params.get("steps", 0),
        cfg=params.get("cfg", 0.0),
        created_at=batch["created_at"],
        jobs=[
            TurnJob(
                id=row["id"],
                status=row["status"],
                seed=json.loads(row["params_json"]).get("seed"),
                error=row["error"],
                image_id=row["image_id"],
            )
            for row in rows
        ],
        duration_s=sum(durations) if durations else None,
        est_cost_usd=sum(costs) if costs else None,
    )


def turns(conn: sqlite3.Connection, session_id: int) -> list[Turn]:
    """Every turn of a session, oldest first, the way a conversation reads."""
    batches = conn.execute(
        "SELECT * FROM batches WHERE session_id = ? AND kind = 'generate' ORDER BY id", (session_id,)
    ).fetchall()
    return [_turn_from_batch(conn, batch) for batch in batches]


def turn(conn: sqlite3.Connection, batch_id: int) -> Turn | None:
    batch = conn.execute("SELECT * FROM batches WHERE id = ? AND kind = 'generate'", (batch_id,)).fetchone()
    return _turn_from_batch(conn, batch) if batch is not None else None


def cost_per_megapixel(conn: sqlite3.Connection, model_id: str) -> float | None:
    """Average estimated cost per million pixels of this model's recent finished renders, or None
    before the first one. Per pixel, so one history can price every size and resolution level."""
    row = conn.execute(
        "SELECT AVG(est_cost_usd * 1000000.0 / (w * h)) AS rate FROM ("
        "SELECT est_cost_usd, json_extract(params_json, '$.width') AS w, json_extract(params_json, '$.height') AS h "
        "FROM jobs WHERE model_id = ? AND kind = 'generate' AND status = 'done' AND est_cost_usd IS NOT NULL "
        "AND w > 0 AND h > 0 ORDER BY id DESC LIMIT ?)",
        (model_id, _ESTIMATE_SAMPLE),
    ).fetchone()
    return row["rate"]


def _refine_source(conn: sqlite3.Connection, image_id: int) -> tuple[str, dict] | None:
    """(model_id, job params) of a generate image, or None if it is gone or came from a workflow."""
    row = conn.execute(
        "SELECT jobs.model_id, jobs.params_json FROM images JOIN jobs ON jobs.id = images.job_id "
        "WHERE images.id = ? AND jobs.kind = 'generate'",
        (image_id,),
    ).fetchone()
    if row is None:
        return None
    return row["model_id"], json.loads(row["params_json"])


def _size_choice(model: Model, width: int, height: int) -> tuple[str, str]:
    """(shape, resolution level) the composer shows as picked; ("custom", "") for any other size."""
    return model.param_schema.match(width, height) or ("custom", "")


def composer(
    conn: sqlite3.Connection, registry: Registry, session_turns: list[Turn], refine_image_id: int | None
) -> Composer:
    """Composer defaults: the previous turn's settings, else the model's own defaults."""
    last = session_turns[-1] if session_turns else None
    model_id = last.model_id if last and last.model_id in registry.models else next(iter(registry.models))
    model = registry.model(model_id)
    schema = model.param_schema

    refine_prompt = None
    if refine_image_id is not None:
        source = _refine_source(conn, refine_image_id)
        if source is None:
            refine_image_id = None
        else:
            refine_prompt = source[1].get("prompt", "")

    if last is not None:
        width, height, steps, cfg, negative = last.width, last.height, last.steps, last.cfg, last.negative
        count = len(last.jobs) or DEFAULT_COUNT
    else:
        size = schema.default_size()
        width, height, steps, cfg, negative = size.width, size.height, schema.steps_default, schema.cfg_default, ""
        count = DEFAULT_COUNT
    shape, tier = _size_choice(model, width, height)
    return Composer(
        model=model,
        preset=shape,
        tier=tier,
        width=width,
        height=height,
        steps=steps,
        cfg=cfg,
        negative=negative,
        count=count,
        refine_image_id=refine_image_id,
        refine_thumb_prompt=refine_prompt,
        cost_per_megapixel_usd=cost_per_megapixel(conn, model.id),
    )


def join_prompt(base: str, addition: str) -> str:
    base = base.strip().rstrip(",.;")
    return f"{base}, {addition}" if base else addition


@dataclass(frozen=True, slots=True)
class Message:
    """One composer submission, already parsed from the form."""

    text: str
    mode: Mode
    refine_image_id: int | None
    width: int
    height: int
    steps: int
    cfg: float
    negative: str
    count: int


def send(
    conn: sqlite3.Connection,
    registry: Registry,
    settings: Settings,
    session_id: int | None,
    message: Message,
    rng: random.Random,
    now: float,
) -> tuple[int, int]:
    """Turns one message into a batch in the given session (a new session when None), and returns
    (session_id, batch_id). Validation and the disk guard are jobs.create_batch's own, so a refused
    message inserts nothing, not even the new session."""
    text = message.text.strip()
    if not text:
        raise ChatError("Type a prompt or a change to make.")
    if len(text) > MESSAGE_LIMIT:
        raise ChatError(f"Keep a message under {MESSAGE_LIMIT} characters.")

    previous = None
    if session_id is not None:
        if get_session(conn, session_id) is None:
            raise ChatError("This conversation no longer exists.")
        last = conn.execute(
            "SELECT id FROM batches WHERE session_id = ? AND kind = 'generate' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        previous = turn(conn, last["id"]) if last is not None else None

    model_id = previous.model_id if previous and previous.model_id in registry.models else next(iter(registry.models))
    seed_mode: Literal["random", "fixed"] = "random"
    seed = None
    if message.refine_image_id is not None:
        source = _refine_source(conn, message.refine_image_id)
        if source is None:
            raise ChatError("That image no longer exists, so it can't be refined.")
        source_model, params = source
        if source_model in registry.models:
            model_id = source_model
        prompt = join_prompt(params.get("prompt", ""), text)
        seed_mode, seed = "fixed", params.get("seed")
    elif message.mode == "refine" and previous is not None:
        prompt = join_prompt(previous.prompt, text)
    else:
        prompt = text

    batch_id = jobs.create_batch(
        conn,
        registry,
        settings,
        jobs.BatchRequest(
            model_id=model_id,
            prompt=prompt,
            negative=message.negative,
            width=message.width,
            height=message.height,
            steps=message.steps,
            cfg=message.cfg,
            seed_mode=seed_mode,
            seed=seed,
            count=message.count,
        ),
        rng,
    )
    if session_id is None:
        title = text if len(text) <= TITLE_LIMIT else text[: TITLE_LIMIT - 1] + "…"
        cursor = conn.execute(
            "INSERT INTO chat_sessions (title, created_at, updated_at) VALUES (?, ?, ?)", (title, now, now)
        )
        session_id = cursor.lastrowid
        assert session_id is not None
    else:
        conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
    conn.execute("UPDATE batches SET session_id = ?, message = ? WHERE id = ?", (session_id, text, batch_id))
    return session_id, batch_id
