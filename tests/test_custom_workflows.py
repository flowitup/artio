"""Custom ComfyUI workflows: API-format validation, the seed override, storage, runs and the routes."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time

import pytest

from atelier import custom_workflows, jobs, library
from atelier.custom_workflows import UnknownWorkflow, WorkflowError
from atelier.storage import DiskGuardError

VALID_GRAPH = {
    "1": {"class_type": "KSampler", "inputs": {"seed": 111, "steps": 20}},
    "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
}
NO_OUTPUT_GRAPH = {"1": {"class_type": "KSampler", "inputs": {"seed": 1}}}
UI_FORMAT_GRAPH = {"nodes": [{"id": 1, "type": "KSampler"}], "links": []}
LINKED_SEED_GRAPH = {
    "1": {"class_type": "KSampler", "inputs": {"seed": 42}},
    "2": {"class_type": "KSamplerAdvanced", "inputs": {"noise_seed": ["1", 0]}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0]}},
}
NO_SEED_INPUT_GRAPH = {"1": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}}}


def _upload(app_client, owner_headers, *, name: str, backend_id: str, graph: dict | bytes, **extra):
    raw = graph if isinstance(graph, (bytes, bytearray)) else json.dumps(graph).encode()
    files = {"graph_file": ("wf.json", raw, "application/json")}
    data = {"name": name, "backend_id": backend_id, **extra}
    return app_client.post("/workflows", headers=owner_headers, data=data, files=files, follow_redirects=False)


def _download_id_from_listing(app_client, owner_headers) -> int:
    listing = app_client.get("/workflows", headers=owner_headers)
    return int(re.findall(r"/workflows/(\d+)/download", listing.text)[-1])


# -- validate_api_graph ------------------------------------------------------------------------------


def test_validate_api_graph_accepts_a_well_formed_graph():
    graph = custom_workflows.validate_api_graph(json.dumps(VALID_GRAPH).encode())
    assert graph == VALID_GRAPH


def test_validate_api_graph_rejects_a_file_over_2mb():
    huge = json.dumps({"1": {"class_type": "SaveImage", "inputs": {"junk": "x" * 2_100_000}}}).encode()
    with pytest.raises(WorkflowError, match="larger than 2 MB"):
        custom_workflows.validate_api_graph(huge)


def test_validate_api_graph_rejects_invalid_json():
    with pytest.raises(WorkflowError, match="Not valid JSON"):
        custom_workflows.validate_api_graph(b"{not json")


def test_validate_api_graph_rejects_json_nested_deeper_than_the_decoder_can_walk():
    """json.loads itself is recursive for arrays/objects: nesting far past Python's own recursion
    limit raises RecursionError while still parsing, before graph_depth ever gets a value to check.
    That must still become a clean WorkflowError, never an uncaught 500 on POST /workflows."""
    n = 100_000
    bomb = (b"[" * n) + (b"]" * n)
    with pytest.raises(WorkflowError, match="Not valid JSON"):
        custom_workflows.validate_api_graph(bomb)


def test_validate_api_graph_rejects_a_huge_integer_literal():
    """Python's int-from-string conversion has its own limit (a plain ValueError, unrelated to
    json.JSONDecodeError); json.loads itself would otherwise raise uncaught for a large enough digit
    string, since it parses integers with the builtin int() constructor."""
    raw = b'{"1": {"class_type": "KSampler", "inputs": {"seed": ' + b"9" * 5000 + b"}}}"
    with pytest.raises(WorkflowError, match="Not valid JSON"):
        custom_workflows.validate_api_graph(raw)


def test_validate_api_graph_rejects_a_graph_nested_deeper_than_the_bound():
    """A graph that still parses fine, and still looks like a normal API-format object at the top
    (node ids -> class_type/inputs), can still smuggle a JSON bomb inside one input's own value. This
    is the explicit depth check, distinct from the previous test's RecursionError-during-parse case:
    here json.loads succeeds, and graph_depth is what catches it."""
    sneaky = "leaf"
    for _ in range(custom_workflows.MAX_GRAPH_DEPTH + 10):
        sneaky = [sneaky]
    graph = {
        "1": {"class_type": "KSampler", "inputs": {"seed": 1, "sneaky": sneaky}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    with pytest.raises(WorkflowError, match="nested more than"):
        custom_workflows.validate_api_graph(json.dumps(graph).encode())


def test_graph_depth_stops_early_past_the_bound_without_raising():
    """graph_depth itself must never raise RecursionError on the same adversarial input it exists to
    bound: it is called from validate_api_graph before any other check, on a value that has only
    already been confirmed to be valid JSON."""
    sneaky = "leaf"
    for _ in range(5000):
        sneaky = [sneaky]
    assert custom_workflows.graph_depth(sneaky) > custom_workflows.MAX_GRAPH_DEPTH


def test_validate_api_graph_rejects_ui_format_export():
    with pytest.raises(WorkflowError, match="API format"):
        custom_workflows.validate_api_graph(json.dumps(UI_FORMAT_GRAPH).encode())


@pytest.mark.parametrize(
    "bad_graph",
    [
        [],  # a list, not an object
        {},  # empty object
        {"1": "not-a-node"},
        {"1": {"class_type": 5, "inputs": {}}},  # class_type must be a string
        {"1": {"class_type": "SaveImage", "inputs": "not-a-dict"}},
    ],
)
def test_validate_api_graph_rejects_malformed_structures(bad_graph):
    with pytest.raises(WorkflowError, match="API-format graph"):
        custom_workflows.validate_api_graph(json.dumps(bad_graph).encode())


def test_graph_without_output_node_is_rejected_unit_level():
    with pytest.raises(WorkflowError, match="SaveImage or PreviewImage"):
        custom_workflows.validate_api_graph(json.dumps(NO_OUTPUT_GRAPH).encode())


def test_validate_api_graph_accepts_preview_image_as_an_output_node():
    graph = {"1": {"class_type": "PreviewImage", "inputs": {"images": ["0", 0]}}}
    assert custom_workflows.validate_api_graph(json.dumps(graph).encode()) == graph


# -- seed_targets / with_seed -------------------------------------------------------------------------


def test_seed_targets_finds_every_seed_family_node_with_a_literal_value():
    graph = {
        "1": {"class_type": "KSampler", "inputs": {"seed": 1}},
        "2": {"class_type": "KSamplerAdvanced", "inputs": {"noise_seed": 2}},
        "3": {"class_type": "RandomNoise", "inputs": {"noise_seed": 3}},
        "4": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    targets = custom_workflows.seed_targets(graph)
    assert set(targets) == {("1", "seed"), ("2", "noise_seed"), ("3", "noise_seed")}


def test_linked_seed_inputs_are_left_untouched():
    targets = custom_workflows.seed_targets(LINKED_SEED_GRAPH)
    assert targets == [("1", "seed")]  # node "2"'s noise_seed is a link (a list), not a literal

    overridden = custom_workflows.with_seed(LINKED_SEED_GRAPH, 999)
    assert overridden["1"]["inputs"]["seed"] == 999
    assert overridden["2"]["inputs"]["noise_seed"] == ["1", 0]  # unchanged
    # The source graph itself is never mutated (with_seed deep-copies).
    assert LINKED_SEED_GRAPH["1"]["inputs"]["seed"] == 42


def test_with_seed_on_a_graph_with_no_seed_input_changes_nothing():
    out = custom_workflows.with_seed(NO_SEED_INPUT_GRAPH, 7)
    assert out == NO_SEED_INPUT_GRAPH


# -- store_workflow / list_workflows / get_workflow / delete_workflow --------------------------------


def test_api_graph_is_stored_for_the_chosen_backend(conn, registry):
    backend = next(iter(registry.backends.values()))
    workflow_id = custom_workflows.store_workflow(conn, registry, "  my workflow  ", backend.id, VALID_GRAPH, time.time())
    stored = custom_workflows.get_workflow(conn, workflow_id)
    assert stored.name == "my workflow"  # trimmed
    assert stored.backend_id == backend.id
    assert stored.graph == VALID_GRAPH


def test_store_workflow_rejects_an_unknown_backend(conn, registry):
    with pytest.raises(WorkflowError, match="Unknown backend"):
        custom_workflows.store_workflow(conn, registry, "x", "no-such-backend", VALID_GRAPH, time.time())


def test_store_workflow_rejects_an_empty_name(conn, registry):
    backend = next(iter(registry.backends.values()))
    with pytest.raises(WorkflowError, match="cannot be empty"):
        custom_workflows.store_workflow(conn, registry, "   ", backend.id, VALID_GRAPH, time.time())


def test_store_workflow_rejects_a_duplicate_name(conn, registry):
    backend = next(iter(registry.backends.values()))
    custom_workflows.store_workflow(conn, registry, "dup", backend.id, VALID_GRAPH, time.time())
    with pytest.raises(WorkflowError, match="already exists"):
        custom_workflows.store_workflow(conn, registry, "dup", backend.id, VALID_GRAPH, time.time())


def test_store_workflow_rejects_a_name_over_the_length_cap(conn, registry):
    backend = next(iter(registry.backends.values()))
    with pytest.raises(WorkflowError, match="at most"):
        custom_workflows.store_workflow(
            conn, registry, "x" * (custom_workflows.MAX_WORKFLOW_NAME_LEN + 1), backend.id, VALID_GRAPH, time.time()
        )
    # The cap itself is a valid length: pins the cap at exactly its own bound, not merely "some cap".
    workflow_id = custom_workflows.store_workflow(
        conn, registry, "x" * custom_workflows.MAX_WORKFLOW_NAME_LEN, backend.id, VALID_GRAPH, time.time()
    )
    assert custom_workflows.get_workflow(conn, workflow_id) is not None


def test_store_workflow_does_not_inflate_non_ascii_content(conn, registry):
    """ensure_ascii=True (json.dumps's default) backslash-escapes every non-ASCII code point as
    \\uXXXX (6 bytes for a 2-3 byte UTF-8 character), which can roughly double a non-ASCII graph's
    stored size -- and that same bloat is copied into every job this workflow ever runs. Comparing
    against the ensure_ascii=True length directly proves the fix rather than asserting a size in
    isolation, which a coincidence could satisfy."""
    backend = next(iter(registry.backends.values()))
    non_ascii_graph = {
        "1": {"class_type": "KSampler", "inputs": {"seed": 1, "prompt": "北京市 café naïve" * 5000}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    workflow_id = custom_workflows.store_workflow(conn, registry, "non-ascii", backend.id, non_ascii_graph, time.time())
    stored_len = conn.execute("SELECT length(graph_json) AS n FROM workflows WHERE id = ?", (workflow_id,)).fetchone()["n"]
    inflated_len = len(json.dumps(non_ascii_graph, ensure_ascii=True))
    assert stored_len < inflated_len * 0.6  # well under the ~2x an ensure_ascii=True round trip would cost
    assert custom_workflows.get_workflow(conn, workflow_id).graph == non_ascii_graph  # round-trips exactly


def test_list_workflows_never_touches_graph_json(conn, registry):
    """The list view only ever needs id/name/backend/date; a WorkflowSummary structurally has no
    `graph` field at all, so a regression that goes back to parsing every stored graph on each
    `/workflows` render (list_workflows) would have to change this return type, not just its content
    -- there's nothing accidentally-populated for it to leave lying around unused."""
    backend = next(iter(registry.backends.values()))
    custom_workflows.store_workflow(conn, registry, "summary-only", backend.id, VALID_GRAPH, time.time())
    [summary] = custom_workflows.list_workflows(conn)
    assert not hasattr(summary, "graph")
    assert summary.name == "summary-only"
    assert summary.backend_id == backend.id


def test_delete_unknown_workflow_raises(conn):
    with pytest.raises(UnknownWorkflow):
        custom_workflows.delete_workflow(conn, 999999)


def test_delete_workflow_clears_the_jobs_link_but_keeps_the_graph(conn, registry, settings, rng):
    backend = next(iter(registry.backends.values()))
    workflow_id = custom_workflows.store_workflow(conn, registry, "keepme", backend.id, VALID_GRAPH, time.time())
    conn.commit()
    workflow = custom_workflows.get_workflow(conn, workflow_id)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    conn.commit()
    job = conn.execute("SELECT id, graph_json FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    assert job["graph_json"]

    custom_workflows.delete_workflow(conn, workflow_id)

    after = conn.execute("SELECT workflow_id, graph_json FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert after["workflow_id"] is None  # ON DELETE SET NULL
    assert after["graph_json"] == job["graph_json"]  # the exact graph the job sent stays on the job


# -- create_workflow_batch ----------------------------------------------------------------------------


def _store(conn, registry, name: str, graph: dict) -> custom_workflows.StoredWorkflow:
    backend = next(iter(registry.backends.values()))
    workflow_id = custom_workflows.store_workflow(conn, registry, name, backend.id, graph, time.time())
    conn.commit()
    return custom_workflows.get_workflow(conn, workflow_id)


def test_run_creates_n_jobs_with_distinct_overridden_seeds(conn, registry, settings, rng):
    workflow = _store(conn, registry, "wf", VALID_GRAPH)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 4, rng)
    rows = conn.execute("SELECT params_json, graph_json FROM jobs WHERE batch_id = ?", (batch_id,)).fetchall()
    assert len(rows) == 4
    param_seeds = [json.loads(r["params_json"])["seed"] for r in rows]
    assert len(set(param_seeds)) == 4  # distinct
    for row, seed in zip(rows, param_seeds, strict=True):
        graph = json.loads(row["graph_json"])
        assert graph["1"]["inputs"]["seed"] == seed  # the graph's own literal seed input was overridden


def test_run_fixed_seed_mode_uses_sequential_seeds(conn, registry, settings, rng):
    workflow = _store(conn, registry, "wf-fixed", VALID_GRAPH)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "fixed", 500, 3, rng)
    rows = conn.execute("SELECT params_json FROM jobs WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
    assert [json.loads(r["params_json"])["seed"] for r in rows] == [500, 501, 502]


def test_run_rejects_count_over_one_when_the_graph_has_no_seed_input(conn, registry, settings, rng):
    workflow = _store(conn, registry, "wf-noseed", NO_SEED_INPUT_GRAPH)
    with pytest.raises(ValueError, match="no seed input"):
        jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 2, rng)


def test_run_allows_count_one_when_the_graph_has_no_seed_input(conn, registry, settings, rng):
    workflow = _store(conn, registry, "wf-noseed-one", NO_SEED_INPUT_GRAPH)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()["n"] == 1


def test_keep_mode_on_a_graph_with_no_seed_input_records_a_null_seed(conn, registry, settings, rng):
    workflow = _store(conn, registry, "wf-keep-noseed", NO_SEED_INPUT_GRAPH)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "keep", None, 1, rng)
    row = conn.execute("SELECT params_json, graph_json FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    assert json.loads(row["params_json"])["seed"] is None
    assert json.loads(row["graph_json"]) == NO_SEED_INPUT_GRAPH  # nothing to override; sent unchanged


@pytest.mark.parametrize("count", [0, 9])
def test_run_rejects_count_out_of_range(conn, registry, settings, rng, count):
    workflow = _store(conn, registry, "wf-range", VALID_GRAPH)
    with pytest.raises(ValueError, match="count"):
        jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, count, rng)


def test_keep_seed_mode_requires_count_one(conn, registry, settings, rng):
    workflow = _store(conn, registry, "wf-keep-reject", VALID_GRAPH)
    with pytest.raises(ValueError, match="count 1"):
        jobs.create_workflow_batch(conn, registry, settings, workflow, "keep", None, 2, rng)


def test_keep_seed_mode_sends_the_graph_unmodified(conn, registry, settings, rng):
    workflow = _store(conn, registry, "wf-keep", VALID_GRAPH)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "keep", None, 1, rng)
    row = conn.execute("SELECT graph_json FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    assert json.loads(row["graph_json"]) == VALID_GRAPH  # the graph's own seed (111), never touched


@pytest.mark.parametrize("out_of_range_seed", [2**63, 2**64 - 1])
def test_keep_seed_mode_rejects_a_graphs_own_seed_outside_sqlite_range(conn, registry, settings, rng, out_of_range_seed):
    """ComfyUI itself accepts seeds up to 2**64-1, but images.seed is a plain SQLite INTEGER (signed
    64-bit): storing anything at or above 2**63 only fails once jobs.complete() runs, after Modal has
    already rendered (and been paid for) the image. This must be caught before the batch (and so the
    paid dispatch) is ever created, not discovered afterward."""
    graph = {
        "1": {"class_type": "KSampler", "inputs": {"seed": out_of_range_seed}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    workflow = _store(conn, registry, f"wf-keep-oor-{out_of_range_seed}", graph)
    with pytest.raises(ValueError, match="outside the range"):
        jobs.create_workflow_batch(conn, registry, settings, workflow, "keep", None, 1, rng)
    assert conn.execute("SELECT COUNT(*) AS n FROM batches WHERE kind = 'workflow'").fetchone()["n"] == 0


def test_keep_seed_mode_accepts_a_seed_at_the_sqlite_range_boundary(conn, registry, settings, rng):
    graph = {
        "1": {"class_type": "KSampler", "inputs": {"seed": 2**63 - 1}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    workflow = _store(conn, registry, "wf-keep-boundary", graph)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "keep", None, 1, rng)
    row = conn.execute("SELECT params_json FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    assert json.loads(row["params_json"])["seed"] == 2**63 - 1


def test_run_rejects_a_graph_already_stored_that_is_nested_too_deeply(conn, registry, settings, rng):
    """validate_api_graph refuses this depth at upload time, but a row written before that check
    existed (simulated here by inserting directly, bypassing store_workflow entirely) must still be
    refused at run time -- with a plain, catchable message, never a RecursionError from with_seed's
    copy.deepcopy after the disk guard and job rows would otherwise already be committed."""
    backend = next(iter(registry.backends.values()))
    sneaky = "leaf"
    for _ in range(custom_workflows.MAX_GRAPH_DEPTH + 10):
        sneaky = [sneaky]
    deep_graph = {
        "1": {"class_type": "KSampler", "inputs": {"seed": 1, "sneaky": sneaky}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    conn.execute(
        "INSERT INTO workflows (name, backend_id, graph_json, created_at) VALUES (?, ?, ?, ?)",
        ("pre-existing-deep", backend.id, json.dumps(deep_graph), time.time()),
    )
    conn.commit()
    workflow = custom_workflows.get_workflow(
        conn, conn.execute("SELECT id FROM workflows WHERE name = 'pre-existing-deep'").fetchone()["id"]
    )

    with pytest.raises(ValueError, match="nested more than"):
        jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    assert conn.execute("SELECT COUNT(*) AS n FROM batches WHERE kind = 'workflow'").fetchone()["n"] == 0


def test_create_workflow_batch_raises_disk_guard_error_and_inserts_no_row(conn, registry, settings, rng):
    workflow = _store(conn, registry, "wf-disk", VALID_GRAPH)
    huge_floor_settings = dataclasses.replace(settings, min_free_gb=100_000_000)
    with pytest.raises(DiskGuardError):
        jobs.create_workflow_batch(conn, registry, huge_floor_settings, workflow, "random", None, 1, rng)
    assert conn.execute("SELECT COUNT(*) AS n FROM batches WHERE kind = 'workflow'").fetchone()["n"] == 0


def test_workflow_jobs_have_no_model_id(conn, registry, settings, rng):
    workflow = _store(conn, registry, "wf-nomodel", VALID_GRAPH)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    row = conn.execute("SELECT model_id, kind FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    assert row["model_id"] is None
    assert row["kind"] == "workflow"


# -- routes: upload ----------------------------------------------------------------------------------


def test_ui_format_upload_is_rejected_with_export_hint(app_client, owner_headers, registry):
    backend = next(iter(registry.backends.values()))
    response = _upload(app_client, owner_headers, name="ui-wf", backend_id=backend.id, graph=UI_FORMAT_GRAPH)
    assert response.status_code == 200
    assert "API format" in response.text
    assert "Export (API)" in response.text


def test_graph_without_output_node_is_rejected(app_client, owner_headers, registry, conn):
    backend = next(iter(registry.backends.values()))
    response = _upload(app_client, owner_headers, name="no-out", backend_id=backend.id, graph=NO_OUTPUT_GRAPH)
    assert response.status_code == 200
    assert "SaveImage" in response.text
    assert conn.execute("SELECT COUNT(*) AS n FROM workflows").fetchone()["n"] == 0


def test_upload_over_the_body_limit_is_refused_before_parsing(app_client, owner_headers, registry, conn):
    backend = next(iter(registry.backends.values()))
    # Deliberately not even valid JSON or UTF-8: proves the 413 fires from the raw byte count alone,
    # before python-multipart or json.loads ever look at the content.
    garbage = bytes(range(256)) * 15000  # ~3.84 MB, over the 3 MB per-path limit
    assert len(garbage) > 3 * 1024 * 1024
    response = _upload(app_client, owner_headers, name="too-big", backend_id=backend.id, graph=garbage)
    assert response.status_code == 413
    assert conn.execute("SELECT COUNT(*) AS n FROM workflows").fetchone()["n"] == 0


def test_upload_file_over_2mb_is_rejected_with_the_body_under_3mb(app_client, owner_headers, registry, conn):
    # The file itself is over validate_api_graph's 2 MB cap, but the whole multipart body stays under
    # the 3 MB per-path limit: this must fail on content size, not on the outer body-size middleware.
    big_graph = {
        "1": {"class_type": "KSampler", "inputs": {"seed": 1, "junk": "x" * 2_100_000}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    raw = json.dumps(big_graph).encode()
    assert 2 * 1024 * 1024 < len(raw) < 3 * 1024 * 1024 - 1000
    backend = next(iter(registry.backends.values()))
    response = _upload(app_client, owner_headers, name="big-file", backend_id=backend.id, graph=raw)
    assert response.status_code == 200
    assert "larger than 2 MB" in response.text


def test_upload_deeply_nested_json_is_rejected_with_200_not_500(app_client, owner_headers, registry, conn):
    n = 100_000
    bomb = (b"[" * n) + (b"]" * n)  # well under the 2 MB file cap and the 3 MB body limit
    assert len(bomb) < 2_000_000
    backend = next(iter(registry.backends.values()))
    response = _upload(app_client, owner_headers, name="json-bomb", backend_id=backend.id, graph=bomb)
    assert response.status_code == 200
    assert "Not valid JSON" in response.text
    assert conn.execute("SELECT COUNT(*) AS n FROM workflows").fetchone()["n"] == 0


def test_upload_a_huge_integer_literal_is_rejected_with_200_not_500(app_client, owner_headers, registry, conn):
    raw = b'{"1": {"class_type": "KSampler", "inputs": {"seed": ' + b"9" * 5000 + b"}}}"
    backend = next(iter(registry.backends.values()))
    response = _upload(app_client, owner_headers, name="huge-int", backend_id=backend.id, graph=raw)
    assert response.status_code == 200
    assert "Not valid JSON" in response.text
    assert conn.execute("SELECT COUNT(*) AS n FROM workflows").fetchone()["n"] == 0


def test_upload_a_graph_nested_past_the_bound_is_rejected_with_200(app_client, owner_headers, registry, conn):
    sneaky = "leaf"
    for _ in range(custom_workflows.MAX_GRAPH_DEPTH + 10):
        sneaky = [sneaky]
    graph = {
        "1": {"class_type": "KSampler", "inputs": {"seed": 1, "sneaky": sneaky}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    backend = next(iter(registry.backends.values()))
    response = _upload(app_client, owner_headers, name="too-deep", backend_id=backend.id, graph=graph)
    assert response.status_code == 200
    assert "nested more than" in response.text
    assert conn.execute("SELECT COUNT(*) AS n FROM workflows").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM workflows").fetchone()["n"] == 0


def test_upload_missing_file_shows_a_message(app_client, owner_headers, registry):
    backend = next(iter(registry.backends.values()))
    response = app_client.post(
        "/workflows", headers=owner_headers, data={"name": "no-file", "backend_id": backend.id}
    )
    assert response.status_code == 200
    assert "Choose a workflow file" in response.text


def test_upload_unknown_backend_is_rejected(app_client, owner_headers):
    response = _upload(app_client, owner_headers, name="bad-backend", backend_id="no-such-backend", graph=VALID_GRAPH)
    assert response.status_code == 200
    assert "Unknown backend" in response.text


def test_upload_duplicate_name_is_rejected(app_client, owner_headers, registry):
    backend = next(iter(registry.backends.values()))
    first = _upload(app_client, owner_headers, name="same-name", backend_id=backend.id, graph=VALID_GRAPH)
    assert first.status_code == 303
    second = _upload(app_client, owner_headers, name="same-name", backend_id=backend.id, graph=VALID_GRAPH)
    assert second.status_code == 200
    assert "already exists" in second.text


def test_upload_name_over_the_length_cap_is_rejected(app_client, owner_headers, registry, conn):
    backend = next(iter(registry.backends.values()))
    long_name = "x" * (custom_workflows.MAX_WORKFLOW_NAME_LEN + 1)
    response = _upload(app_client, owner_headers, name=long_name, backend_id=backend.id, graph=VALID_GRAPH)
    assert response.status_code == 200
    assert "at most" in response.text
    assert conn.execute("SELECT COUNT(*) AS n FROM workflows").fetchone()["n"] == 0


def test_workflow_name_is_escaped_in_the_list(app_client, owner_headers, registry):
    backend = next(iter(registry.backends.values()))
    name = "<script>alert(1)</script>"
    _upload(app_client, owner_headers, name=name, backend_id=backend.id, graph=VALID_GRAPH)
    listing = app_client.get("/workflows", headers=owner_headers)
    assert "<script>alert(1)</script>" not in listing.text
    assert "&lt;script&gt;" in listing.text


def test_upload_success_redirects_and_appears_in_the_list(app_client, owner_headers, registry):
    backend = next(iter(registry.backends.values()))
    response = _upload(app_client, owner_headers, name="listed-wf", backend_id=backend.id, graph=VALID_GRAPH)
    assert response.status_code == 303
    assert response.headers["location"] == "/workflows"
    listing = app_client.get("/workflows", headers=owner_headers)
    assert "listed-wf" in listing.text


# -- routes: download, run, delete --------------------------------------------------------------------


def test_download_returns_the_exact_stored_graph(app_client, owner_headers, registry):
    backend = next(iter(registry.backends.values()))
    _upload(app_client, owner_headers, name="dl-wf", backend_id=backend.id, graph=VALID_GRAPH)
    workflow_id = _download_id_from_listing(app_client, owner_headers)
    response = app_client.get(f"/workflows/{workflow_id}/download", headers=owner_headers)
    assert response.status_code == 200
    assert json.loads(response.text) == VALID_GRAPH
    assert response.headers["content-disposition"] == f'attachment; filename="workflow-{workflow_id}.json"'


def test_download_unknown_workflow_answers_404(app_client, owner_headers):
    response = app_client.get("/workflows/999999/download", headers=owner_headers)
    assert response.status_code == 404


def test_run_route_redirects_to_the_new_batchs_queue(app_client, owner_headers, registry):
    backend = next(iter(registry.backends.values()))
    _upload(app_client, owner_headers, name="run-wf", backend_id=backend.id, graph=VALID_GRAPH)
    workflow_id = _download_id_from_listing(app_client, owner_headers)
    response = app_client.post(
        f"/workflows/{workflow_id}/run",
        headers=owner_headers,
        data={"seed_mode": "random", "count": "2"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/queue?batch=")


def test_run_route_on_unknown_workflow_shows_a_message(app_client, owner_headers):
    response = app_client.post(
        "/workflows/999999/run", headers=owner_headers, data={"seed_mode": "random", "count": "1"}
    )
    assert response.status_code == 200
    assert "no longer exists" in response.text


def test_run_route_rejects_count_with_no_seed_input_and_creates_no_batch(app_client, owner_headers, registry, conn):
    backend = next(iter(registry.backends.values()))
    _upload(app_client, owner_headers, name="run-noseed", backend_id=backend.id, graph=NO_SEED_INPUT_GRAPH)
    workflow_id = _download_id_from_listing(app_client, owner_headers)
    before = conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
    response = app_client.post(
        f"/workflows/{workflow_id}/run", headers=owner_headers, data={"seed_mode": "random", "count": "3"}
    )
    assert response.status_code == 200
    assert "no seed input" in response.text
    assert conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"] == before


def test_delete_route_removes_it_from_the_list(app_client, owner_headers, registry):
    backend = next(iter(registry.backends.values()))
    _upload(app_client, owner_headers, name="del-wf", backend_id=backend.id, graph=VALID_GRAPH)
    workflow_id = _download_id_from_listing(app_client, owner_headers)
    response = app_client.post(f"/workflows/{workflow_id}/delete", headers=owner_headers)
    assert response.status_code == 200
    assert "del-wf" not in response.text


def test_workflow_delete_button_carries_hx_select_for_the_partial_swap(app_client, owner_headers, registry):
    """The counterpart of the same library.html proof: a Delete POST here re-renders the whole
    workflows.html page too, and only the client-side hx-select/hx-target pair makes that swap in
    just #workflow-list. Nothing here runs htmx itself; this pins the attribute a template edit could
    otherwise silently drop."""
    backend = next(iter(registry.backends.values()))
    _upload(app_client, owner_headers, name="hx-select-check", backend_id=backend.id, graph=VALID_GRAPH)
    response = app_client.get("/workflows", headers=owner_headers)
    assert 'hx-target="#workflow-list"' in response.text
    assert 'hx-select="#workflow-list"' in response.text


# -- result linking and failure text -------------------------------------------------------------------


def test_result_keeps_workflow_link_and_exact_graph(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, settings, rng
):
    workflow = _store(conn, registry, "linked-wf", VALID_GRAPH)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    conn.commit()

    worker = app_client.app.state.worker
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    sent_graph = fake_gateway.graph_for(job["call_id"])
    assert sent_graph == json.loads(job["graph_json"])  # exactly what create_workflow_batch stored

    fake_gateway.finish(job["call_id"], png_bytes)
    asyncio.run(worker.poll_once())

    image_id = conn.execute("SELECT id FROM images WHERE job_id = ?", (job["id"],)).fetchone()["id"]
    detail = library.image_detail(conn, image_id)
    assert detail.kind == "workflow"
    assert detail.workflow_id == workflow.id
    assert detail.workflow_name == "linked-wf"
    assert detail.backend_id == workflow.backend_id

    page = app_client.get(f"/images/{image_id}", headers=owner_headers)
    assert "Workflow: linked-wf on " + workflow.backend_id in page.text
    assert f"/jobs/{job['id']}/graph.json" in page.text

    graph_route = app_client.get(f"/jobs/{job['id']}/graph.json", headers=owner_headers)
    assert graph_route.status_code == 200
    assert json.loads(graph_route.text) == sent_graph


def test_job_graph_json_is_served_as_json_never_html_stored_xss_style(
    conn, registry, settings, rng, app_client, owner_headers
):
    """A graph is untrusted text end to end: it may legitimately contain "<script>"-shaped content
    (a prompt, a class_type, a filename_prefix). That must never become stored XSS, which depends
    entirely on this route's response headers, not on escaping the JSON body (JSON doesn't escape
    "<" the way HTML does, and shouldn't have to). Pins the three headers a regression could each
    independently drop without any of the other assertions in this file noticing."""
    payload_graph = {
        "1": {"class_type": "KSampler", "inputs": {"seed": 1}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}, "filename_prefix": "<script>alert(1)</script>"},
    }
    workflow = _store(conn, registry, "xss-graph-wf", payload_graph)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "keep", None, 1, rng)
    conn.commit()
    job = conn.execute("SELECT id FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()

    response = app_client.get(f"/jobs/{job['id']}/graph.json", headers=owner_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0].strip() == "application/json"  # never text/html
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in response.headers["content-disposition"]
    # The payload appears verbatim: correct for a JSON response (no HTML escaping needed), and safe
    # only because a browser is told, twice over, never to render this body as HTML.
    assert "<script>alert(1)</script>" in response.text


def test_image_page_workflow_name_is_escaped(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, settings, rng
):
    backend = next(iter(registry.backends.values()))
    name = "<script>alert(1)</script>"
    workflow_id = custom_workflows.store_workflow(conn, registry, name, backend.id, VALID_GRAPH, time.time())
    conn.commit()
    workflow = custom_workflows.get_workflow(conn, workflow_id)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    conn.commit()

    worker = app_client.app.state.worker
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    fake_gateway.finish(job["call_id"], png_bytes)
    asyncio.run(worker.poll_once())
    image_id = conn.execute("SELECT id FROM images WHERE job_id = ?", (job["id"],)).fetchone()["id"]

    page = app_client.get(f"/images/{image_id}", headers=owner_headers)
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text


def test_comfyui_rejection_text_is_shown_on_the_failed_job(
    app_client, owner_headers, registry, fake_gateway, conn, settings, rng
):
    workflow = _store(conn, registry, "broken-wf", VALID_GRAPH)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    conn.commit()

    worker = app_client.app.state.worker
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()

    rejection_text = (
        "ComfyUI rejected workflow: {'type': 'invalid_prompt', 'message': '<b>Value</b> not in list'}"
    )
    fake_gateway.fail(job["call_id"], rejection_text)
    asyncio.run(worker.poll_once())

    row = conn.execute("SELECT status, error FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "failed"
    assert row["error"] == rejection_text  # stored verbatim; only rendering escapes it

    queue_page = app_client.get(f"/queue?batch={batch_id}", headers=owner_headers)
    assert "ComfyUI rejected workflow:" in queue_page.text
    assert "<b>Value</b>" not in queue_page.text  # the error text is escaped, not injected as HTML
    assert "&lt;b&gt;Value&lt;/b&gt;" in queue_page.text

    retried_id = jobs.retry_job(conn, job["id"])
    conn.commit()
    retried = conn.execute("SELECT status, graph_json FROM jobs WHERE id = ?", (retried_id,)).fetchone()
    assert retried["status"] == "queued"
    assert retried["graph_json"] == job["graph_json"]


# -- authorization ------------------------------------------------------------------------------------


def test_service_identity_cannot_upload_or_delete_workflows(app_client, service_headers, route_ids):
    workflow_id = route_ids["workflow_id"]
    upload = _upload(app_client, service_headers, name="x", backend_id="qwen21-uc", graph=VALID_GRAPH)
    assert upload.status_code == 403
    assert app_client.post(f"/workflows/{workflow_id}/delete", headers=service_headers).status_code == 403
    assert (
        app_client.post(
            f"/workflows/{workflow_id}/run", headers=service_headers, data={"seed_mode": "random", "count": "1"}
        ).status_code
        == 403
    )
    assert app_client.get(f"/workflows/{workflow_id}/download", headers=service_headers).status_code == 403


# -- gallery: Custom workflows filter -------------------------------------------------------------------


def test_gallery_custom_workflows_filter_shows_only_workflow_results(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, settings, rng
):
    workflow = _store(conn, registry, "gallery-wf", VALID_GRAPH)
    wf_batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    conn.commit()
    worker = app_client.app.state.worker
    asyncio.run(worker.dispatch_once())
    wf_job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (wf_batch_id,)).fetchone()
    fake_gateway.finish(wf_job["call_id"], png_bytes)
    asyncio.run(worker.poll_once())
    wf_image_id = conn.execute("SELECT id FROM images WHERE job_id = ?", (wf_job["id"],)).fetchone()["id"]

    model = next(iter(registry.models.values()))
    preset = model.param_schema.default_size()
    gen_response = app_client.post(
        "/generate",
        headers=owner_headers,
        follow_redirects=False,
        data={
            "model_id": model.id, "prompt": "a generate result", "negative": "",
            "preset": preset.name, "width": preset.width, "height": preset.height,
            "steps": model.param_schema.steps_default, "cfg": model.param_schema.cfg_default,
            "seed_mode": "random", "seed": "", "count": 1,
        },
    )
    assert gen_response.status_code == 303
    asyncio.run(worker.dispatch_once())
    gen_job = conn.execute("SELECT * FROM jobs WHERE kind = 'generate' ORDER BY id DESC LIMIT 1").fetchone()
    fake_gateway.finish(gen_job["call_id"], png_bytes)
    asyncio.run(worker.poll_once())
    gen_image_id = conn.execute("SELECT id FROM images WHERE job_id = ?", (gen_job["id"],)).fetchone()["id"]

    response = app_client.get(
        f"/gallery?model={library.WORKFLOW_MODEL_FILTER}", headers=owner_headers
    )
    assert response.status_code == 200
    assert f"/images/{wf_image_id}" in response.text
    assert f"/images/{gen_image_id}" not in response.text


def test_gallery_header_for_a_workflow_batch_names_the_workflow_not_none(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, settings, rng
):
    """A workflow-kind batch has no prompt and no model_id (both NULL/empty by design), so the
    gallery's header must fall back to naming the workflow instead of rendering an empty link and a
    literal "None" for the model."""
    workflow = _store(conn, registry, "header-wf", VALID_GRAPH)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    conn.commit()
    worker = app_client.app.state.worker
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    fake_gateway.finish(job["call_id"], png_bytes)
    asyncio.run(worker.poll_once())

    response = app_client.get("/gallery", headers=owner_headers)
    assert response.status_code == 200
    assert "workflow: header-wf" in response.text
    assert "custom workflow" in response.text
    assert ">None<" not in response.text
    assert "&middot; None" not in response.text


def test_keep_mode_sends_every_sampler_seed_exactly_as_uploaded(conn, registry, settings, rng):
    """Keep mode runs the graph as it is: a graph with two samplers seeded differently must reach the
    job unchanged, never with one recorded seed copied onto both."""
    two_samplers = {
        "1": {"class_type": "KSampler", "inputs": {"seed": 111, "steps": 20}},
        "2": {"class_type": "KSampler", "inputs": {"seed": 222, "steps": 20}},
        "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0]}},
    }
    workflow = _store(conn, registry, "wf-two-samplers", two_samplers)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "keep", None, 1, rng)
    row = conn.execute("SELECT graph_json FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    assert json.loads(row["graph_json"]) == two_samplers


def test_job_graph_keeps_non_ascii_text_unescaped(conn, registry, settings, rng):
    """A job's stored graph keeps non-ASCII text as UTF-8, like the stored workflow, instead of
    \\uXXXX escapes that multiply its size in the database."""
    graph = {
        "1": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "chat noir déjà vu 猫"}},
        "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    workflow = _store(conn, registry, "wf-unicode", graph)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    stored = conn.execute("SELECT graph_json FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()["graph_json"]
    assert "déjà vu 猫" in stored
    assert "\\u" not in stored


def test_graph_depth_counts_scalar_children_one_level_deeper():
    assert custom_workflows.graph_depth(7) == 1
    assert custom_workflows.graph_depth({}) == 1
    assert custom_workflows.graph_depth({"a": 1}) == 2
    assert custom_workflows.graph_depth([1, [2, [3]]]) == 4
    assert custom_workflows.graph_depth(list(range(100_000))) == 2


def test_gallery_header_escapes_the_workflow_name(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, settings, rng
):
    workflow = _store(conn, registry, "<b>bold-wf</b>", VALID_GRAPH)
    batch_id = jobs.create_workflow_batch(conn, registry, settings, workflow, "random", None, 1, rng)
    conn.commit()
    worker = app_client.app.state.worker
    asyncio.run(worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()
    fake_gateway.finish(job["call_id"], png_bytes)
    asyncio.run(worker.poll_once())

    response = app_client.get("/gallery", headers=owner_headers)
    assert response.status_code == 200
    assert "&lt;b&gt;bold-wf&lt;/b&gt;" in response.text
    assert "<b>bold-wf</b>" not in response.text
