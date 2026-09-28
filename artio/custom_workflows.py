"""Custom ComfyUI workflows: API-format graph validation, the seed override and storage.

Uploaded JSON is untrusted owner input: it is parsed with the stdlib `json` module only, never
evaluated, and this module never logs a graph's content (only names, ids and error text may be logged
by callers).
"""

from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from artio.registry import Registry
from artio.storage import INPUT_IMAGE_NAME_RE, read_input_image

MAX_GRAPH_BYTES = 2_000_000
# A real API-format export is a flat object of nodes; their "inputs" nest at most a couple of levels
# (a list of [node_id, index] pairs, say). 64 is generous headroom for that while still refusing a
# JSON bomb of nested arrays/objects, which would otherwise reach with_seed's copy.deepcopy later and
# blow the recursion limit there instead (see graph_depth and its caller in jobs.py).
MAX_GRAPH_DEPTH = 64
MAX_WORKFLOW_NAME_LEN = 100

# The seed-family input each node class exposes as a plain integer. RandomNoise is ComfyUI's newer
# advanced-sampling seed source; KSampler/KSamplerAdvanced are the classic one-node samplers. This is
# a small, explicit allowlist (data, not architecture): a graph using a different sampler node's own
# seed input is expected to be rejected by the "no seed input" check below, and extending it for a
# new node class is a one-line, well-tested change (see the phase's risk table).
SEED_INPUTS: dict[str, str] = {
    "KSampler": "seed",
    "KSamplerAdvanced": "noise_seed",
    "RandomNoise": "noise_seed",
}

# _run (modal/qwen21_uc_app.py) returns only the first saved image, so a graph with neither node
# would silently produce nothing to show the owner.
OUTPUT_NODES = {"SaveImage", "PreviewImage"}


# The stock ComfyUI nodes that read a picture from ComfyUI's input folder by file name. A fresh Modal
# container's input folder holds none of the owner's files, so each of these needs an image uploaded
# with the run (see with_images); a linked "image" input (fed by another node) is not a slot.
IMAGE_INPUT_NODES = ("LoadImage", "LoadImageMask")
MAX_IMAGE_SLOTS = 16
MAX_SLOT_TITLE_LEN = 80

# The stock prompt nodes whose text the run form lets the owner rewrite each time, and the input that
# holds that text. Anything else in a graph keeps the value it was uploaded with.
PROMPT_INPUTS: dict[str, str] = {
    "TextEncodeQwenImage21": "prompt",
    "TextEncodeQwenImageEdit": "prompt",
    "TextEncodeQwenImageEditPlus": "prompt",
    "CLIPTextEncode": "text",
}
MAX_PROMPT_LEN = 8000


class WorkflowError(Exception):
    """Raised for any workflow upload or run request that must be shown to the owner inline."""


class UnknownWorkflow(Exception):
    """Raised when a workflow id has no row."""


class MissingInputImage(Exception):
    """Raised when a job graph references an input image this app no longer has on disk."""


@dataclass(frozen=True, slots=True)
class ImageSlot:
    """One LoadImage-style node that needs an uploaded picture: its node id and a label for the form
    (the node's own title from an API export's _meta, else its class name)."""

    node_id: str
    title: str


@dataclass(frozen=True, slots=True)
class TextSlot:
    """One prompt node whose text the run form can rewrite: node id, the input holding the text, a
    label (as for ImageSlot) and the text the graph was uploaded with."""

    node_id: str
    key: str
    title: str
    value: str


@dataclass(frozen=True, slots=True)
class StoredWorkflow:
    id: int
    name: str
    backend_id: str
    graph: dict
    created_at: float


def graph_depth(value: object) -> int:
    """The maximum nesting depth of a parsed JSON value (1 for a scalar or an empty container),
    computed with an explicit stack rather than recursion, so this check itself can never raise
    RecursionError on the very input it is meant to bound. Stops early once the result is already
    over MAX_GRAPH_DEPTH, so a wide-and-deep adversarial value can't force it to keep walking every
    remaining node just to prove what the caller already knows it needs to reject."""
    max_seen = 1
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > max_seen:
            max_seen = depth
            if max_seen > MAX_GRAPH_DEPTH:
                return max_seen
        if isinstance(current, dict):
            children = current.values()
        elif isinstance(current, list):
            children = current
        else:
            continue
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
            elif depth + 1 > max_seen:
                # A scalar child is one level deeper but can't nest further: count it without
                # stacking it, so a huge flat array costs no memory beyond the parsed value itself.
                max_seen = depth + 1
                if max_seen > MAX_GRAPH_DEPTH:
                    return max_seen
    return max_seen


def validate_api_graph(raw: bytes) -> dict:
    """Parses and structurally validates an uploaded ComfyUI API-format graph.

    Never evaluates the input (json.loads only). Checks, in order: the 2 MB file-size cap (checked
    again here even though the body-size middleware already bounds the whole request, because a
    multipart request can carry other fields alongside the file); valid JSON; nesting depth (a JSON
    bomb of nested arrays would otherwise reach with_seed's copy.deepcopy as a stored, "already
    validated" graph, and blow the recursion limit there instead, at run time); not a UI-format
    export (a top-level object with both "nodes" and "links" is ComfyUI's UI/workflow export shape,
    not the API shape this app runs); the API shape itself (an object of nodes, each with a string
    class_type and an inputs object); and at least one output node. Raises WorkflowError with a
    message that is safe, and useful, to show the owner."""
    if len(raw) > MAX_GRAPH_BYTES:
        raise WorkflowError(f"The file is larger than {MAX_GRAPH_BYTES // 1_000_000} MB.")
    try:
        graph = json.loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # ValueError covers json.JSONDecodeError and Python's own integer-string-conversion limit (a
        # huge integer literal raises a plain ValueError, "Exceeds the limit (4300 digits) ...").
        # RecursionError covers JSON nested deeper than json.loads's own recursive decoder can walk
        # -- str(exc) is empty for that one, so the message below still reads as "not valid JSON".
        raise WorkflowError(f"Not valid JSON: {exc}" if str(exc) else "Not valid JSON: too deeply nested.") from None

    if graph_depth(graph) > MAX_GRAPH_DEPTH:
        raise WorkflowError(f"The graph is nested more than {MAX_GRAPH_DEPTH} levels deep.")

    if isinstance(graph, dict) and "nodes" in graph and "links" in graph:
        raise WorkflowError(
            "This is a UI-format workflow. In ComfyUI, export it in API format "
            "(Workflow -> Export (API)) and upload that file."
        )
    if (
        not isinstance(graph, dict)
        or not graph
        or not all(
            isinstance(node, dict) and isinstance(node.get("class_type"), str) and isinstance(node.get("inputs"), dict)
            for node in graph.values()
        )
    ):
        raise WorkflowError(
            "Expected an API-format graph: an object of nodes, each with 'class_type' and 'inputs'."
        )
    if not any(node["class_type"] in OUTPUT_NODES for node in graph.values()):
        raise WorkflowError("The graph has no SaveImage or PreviewImage node, so it would produce no image.")
    if len(image_slots(graph)) > MAX_IMAGE_SLOTS:
        raise WorkflowError(f"The graph has more than {MAX_IMAGE_SLOTS} Load Image nodes.")
    return graph


def image_slots(graph: dict) -> list[ImageSlot]:
    """Every node in an (already validated) API graph that loads a picture by file name, in the
    graph's own order."""
    slots = []
    for node_id, node in graph.items():
        if node["class_type"] not in IMAGE_INPUT_NODES or not isinstance(node["inputs"].get("image"), str):
            continue
        slots.append(ImageSlot(node_id=node_id, title=_title(node)))
    return slots


def _title(node: dict) -> str:
    meta = node.get("_meta")
    title = meta.get("title") if isinstance(meta, dict) else None
    title = title.strip() if isinstance(title, str) else ""
    return title[:MAX_SLOT_TITLE_LEN] or node["class_type"]


def text_slots(graph: dict) -> list[TextSlot]:
    """Every prompt node (PROMPT_INPUTS) in an API graph whose text is a literal string, in the
    graph's own order. A linked text input (fed by another node) is not a slot."""
    slots = []
    for node_id, node in graph.items():
        key = PROMPT_INPUTS.get(node["class_type"])
        if key is None or not isinstance(node["inputs"].get(key), str):
            continue
        slots.append(TextSlot(node_id=node_id, key=key, title=_title(node), value=node["inputs"][key]))
    return slots


def with_texts(graph: dict, texts: dict[str, str]) -> dict:
    """A deep copy of graph with each prompt slot's text replaced (node_id -> text). Raises
    WorkflowError for a text over MAX_PROMPT_LEN characters or a node that is not a prompt slot."""
    keys = {slot.node_id: slot.key for slot in text_slots(graph)}
    out = copy.deepcopy(graph)
    for node_id, text in texts.items():
        if node_id not in keys:
            raise WorkflowError(f"Node {node_id} has no prompt text to replace.")
        if len(text) > MAX_PROMPT_LEN:
            raise WorkflowError(f"A prompt can be at most {MAX_PROMPT_LEN} characters.")
        out[node_id]["inputs"][keys[node_id]] = text
    return out


def with_images(graph: dict, names: dict[str, str]) -> dict:
    """A deep copy of graph with each slot node's "image" input set to its stored input file name
    (node_id -> name, from storage.save_input_image)."""
    out = copy.deepcopy(graph)
    for node_id, name in names.items():
        out[node_id]["inputs"]["image"] = name
    return out


def missing_images(graph: dict) -> list[ImageSlot]:
    """The slots of a graph about to run that still point at a file this app never stored, i.e. at a
    name from the machine the graph was exported on, which the GPU container will never have."""
    return [slot for slot in image_slots(graph) if not INPUT_IMAGE_NAME_RE.match(graph[slot.node_id]["inputs"]["image"])]


def input_images_for(data_dir: Path, graph: dict) -> dict[str, bytes]:
    """The bytes of every stored input image a job graph references, by file name, to send along
    with the graph. Raises MissingInputImage if one of them is gone from disk."""
    images: dict[str, bytes] = {}
    for node in graph.values():
        if not isinstance(node, dict) or node.get("class_type") not in IMAGE_INPUT_NODES:
            continue
        inputs = node.get("inputs")
        name = inputs.get("image") if isinstance(inputs, dict) else None
        if not isinstance(name, str) or not INPUT_IMAGE_NAME_RE.match(name) or name in images:
            continue
        data = read_input_image(data_dir, name)
        if data is None:
            raise MissingInputImage(f"Input image {name} is no longer stored; run the workflow again with a new upload.")
        images[name] = data
    return images


def seed_targets(graph: dict) -> list[tuple[str, str]]:
    """(node_id, input_key) pairs for every seed-family input the graph sets to a literal integer.
    A linked input (a ["node_id", output_index] list, fed by another node) is deliberately left out:
    overriding it would silently disconnect that link."""
    return [
        (node_id, SEED_INPUTS[node["class_type"]])
        for node_id, node in graph.items()
        if node["class_type"] in SEED_INPUTS
        and isinstance(node["inputs"].get(SEED_INPUTS[node["class_type"]]), int)
    ]


def with_seed(graph: dict, seed: int) -> dict:
    """A deep copy of graph with every seed target (see seed_targets) overridden to `seed`."""
    out = copy.deepcopy(graph)
    for node_id, key in seed_targets(out):
        out[node_id]["inputs"][key] = seed
    return out


def _from_row(row: sqlite3.Row) -> StoredWorkflow:
    return StoredWorkflow(
        id=row["id"],
        name=row["name"],
        backend_id=row["backend_id"],
        graph=json.loads(row["graph_json"]),
        created_at=row["created_at"],
    )


def _slots_json(slots: list[ImageSlot]) -> str:
    return json.dumps([{"node": slot.node_id, "title": slot.title} for slot in slots], ensure_ascii=False)


def _slots_from_json(raw: str) -> tuple[ImageSlot, ...]:
    return tuple(ImageSlot(node_id=str(item["node"]), title=str(item["title"])) for item in json.loads(raw))


def store_workflow(
    conn: sqlite3.Connection, registry: Registry, name: str, backend_id: str, graph: dict, now: float
) -> int:
    """Stores an already-validated graph under a unique name for an existing backend. Raises
    WorkflowError for an empty or over-length name, an unknown backend, or a name already in use
    (the `workflows` table's own UNIQUE constraint is the source of truth for the last one, so a race
    between two uploads of the same name can never insert two rows).

    `ensure_ascii=False`: the default would backslash-escape every non-ASCII code point (`\\uXXXX`,
    6 bytes each), which can roughly double the stored size of a non-ASCII prompt or label -- and
    that same bloated copy is written again into every job this workflow ever runs (create_workflow_batch)
    and sent to Modal each time. The file's own bytes are already valid UTF-8 (validate_api_graph
    parsed them), so writing UTF-8 back out is lossless and needs no escaping."""
    name = name.strip()
    if not name:
        raise WorkflowError("Workflow name cannot be empty.")
    if len(name) > MAX_WORKFLOW_NAME_LEN:
        raise WorkflowError(f"Workflow name must be at most {MAX_WORKFLOW_NAME_LEN} characters.")
    if backend_id not in registry.backends:
        raise WorkflowError(f"Unknown backend {backend_id!r}.")
    try:
        cursor = conn.execute(
            "INSERT INTO workflows (name, backend_id, graph_json, image_inputs_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, backend_id, json.dumps(graph, ensure_ascii=False), _slots_json(image_slots(graph)), now),
        )
    except sqlite3.IntegrityError:
        raise WorkflowError(f"A workflow named {name!r} already exists.") from None
    workflow_id = cursor.lastrowid
    assert workflow_id is not None
    return workflow_id


@dataclass(frozen=True, slots=True)
class WorkflowSummary:
    """The list view's own shape: id, name, backend and date only, never the graph -- the listing
    has no use for it, and parsing every stored graph's JSON on each render doesn't scale with graph
    size (see list_workflows)."""

    id: int
    name: str
    backend_id: str
    created_at: float
    image_slots: tuple[ImageSlot, ...] = ()


def list_workflows(conn: sqlite3.Connection) -> list[WorkflowSummary]:
    """Every stored workflow for the `/workflows` list, without ever touching graph_json: selecting
    (let alone json.loads-ing) a potentially multi-megabyte column here would cost time on the event
    loop proportional to every stored graph's size, for data the list never displays."""
    rows = conn.execute(
        "SELECT id, name, backend_id, created_at, image_inputs_json FROM workflows ORDER BY name"
    ).fetchall()
    return [
        WorkflowSummary(
            id=row["id"],
            name=row["name"],
            backend_id=row["backend_id"],
            created_at=row["created_at"],
            image_slots=_slots_from_json(row["image_inputs_json"]),
        )
        for row in rows
    ]


def get_workflow(conn: sqlite3.Connection, workflow_id: int) -> StoredWorkflow | None:
    row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    return _from_row(row) if row is not None else None


def delete_workflow(conn: sqlite3.Connection, workflow_id: int) -> None:
    """Deletes the stored workflow. Jobs that ran it keep their own graph_json (the exact graph they
    were sent); their workflow_id is set to NULL by the schema's ON DELETE SET NULL. Raises
    UnknownWorkflow if the id doesn't exist."""
    cursor = conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
    if cursor.rowcount == 0:
        raise UnknownWorkflow(workflow_id)


def result_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """How many finished images each stored workflow has produced, by workflow id (absent = none)."""
    rows = conn.execute(
        "SELECT jobs.workflow_id AS wid, COUNT(*) AS n FROM images JOIN jobs ON jobs.id = images.job_id "
        "WHERE jobs.workflow_id IS NOT NULL GROUP BY jobs.workflow_id"
    ).fetchall()
    return {row["wid"]: row["n"] for row in rows}


def recent_image_ids(conn: sqlite3.Connection, workflow_id: int, limit: int = 8) -> list[int]:
    """The newest images one stored workflow produced, newest first."""
    rows = conn.execute(
        "SELECT images.id FROM images JOIN jobs ON jobs.id = images.job_id WHERE jobs.workflow_id = ? "
        "ORDER BY images.id DESC LIMIT ?",
        (workflow_id, limit),
    ).fetchall()
    return [row["id"] for row in rows]
