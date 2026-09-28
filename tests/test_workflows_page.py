"""The Workflows page's list + run panel layout and the per-run prompt override."""

from __future__ import annotations

import asyncio
import json
import re
import time

import pytest

from artio import custom_workflows, jobs

PROMPT_GRAPH = {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "m.safetensors"}},
    "2": {
        "class_type": "TextEncodeQwenImage21",
        "inputs": {"clip": ["9", 0], "prompt": "phố cổ Hội An về đêm", "negative_prompt": ""},
    },
    "3": {"class_type": "KSampler", "inputs": {"seed": 7, "positive": ["2", 0]}},
    "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0]}},
}
TWO_TEXT_GRAPH = {
    "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "a red fox"}, "_meta": {"title": "Positive"}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry"}, "_meta": {"title": "Negative"}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": ["5", 0]}},  # linked: not a slot
    "8": {"class_type": "KSampler", "inputs": {"seed": 1}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
}


def _upload(app_client, owner_headers, registry, name: str, graph: dict) -> int:
    backend = next(iter(registry.backends.values()))
    files = {"graph_file": ("wf.json", json.dumps(graph).encode(), "application/json")}
    response = app_client.post(
        "/workflows",
        headers=owner_headers,
        data={"name": name, "backend_id": backend.id},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


# -- text slots ------------------------------------------------------------------------------------------


def test_text_slots_find_prompt_nodes_with_literal_text():
    assert custom_workflows.text_slots(PROMPT_GRAPH) == [
        custom_workflows.TextSlot("2", "prompt", "TextEncodeQwenImage21", "phố cổ Hội An về đêm")
    ]
    assert [(s.node_id, s.title) for s in custom_workflows.text_slots(TWO_TEXT_GRAPH)] == [
        ("5", "Positive"),
        ("6", "Negative"),
    ]


def test_with_texts_replaces_only_the_named_slots():
    out = custom_workflows.with_texts(TWO_TEXT_GRAPH, {"6": "low quality"})
    assert out["6"]["inputs"]["text"] == "low quality"
    assert out["5"]["inputs"]["text"] == "a red fox"
    assert TWO_TEXT_GRAPH["6"]["inputs"]["text"] == "blurry"
    with pytest.raises(custom_workflows.WorkflowError, match="no prompt text"):
        custom_workflows.with_texts(TWO_TEXT_GRAPH, {"7": "x"})
    with pytest.raises(custom_workflows.WorkflowError, match="at most"):
        custom_workflows.with_texts(TWO_TEXT_GRAPH, {"5": "x" * (custom_workflows.MAX_PROMPT_LEN + 1)})


def test_a_workflow_run_records_its_prompt_with_the_job(conn, registry, settings, rng):
    backend = next(iter(registry.backends.values()))
    wid = custom_workflows.store_workflow(conn, registry, "p", backend.id, PROMPT_GRAPH, time.time())
    batch_id = jobs.create_workflow_batch(
        conn, registry, settings, custom_workflows.get_workflow(conn, wid), "random", None, 1, rng
    )
    params = json.loads(
        conn.execute("SELECT params_json FROM jobs WHERE batch_id = ?", (batch_id,)).fetchone()[0]
    )
    assert params["prompt"] == "phố cổ Hội An về đêm"


# -- page ------------------------------------------------------------------------------------------------


def test_plain_workflows_page_selects_the_first_workflow_by_name(app_client, owner_headers, registry):
    _upload(app_client, owner_headers, registry, "b second", PROMPT_GRAPH)
    first = _upload(app_client, owner_headers, registry, "a first", TWO_TEXT_GRAPH)
    page = app_client.get("/workflows", headers=owner_headers).text
    assert '<h2 id="wf-title">a first</h2>' in page
    assert f'href="/workflows/{first}" class="wf-row" aria-current="page"' in page
    assert page.count('class="wf-row"') == 2


def test_workflow_page_shows_its_run_panel(app_client, owner_headers, registry):
    wid = _upload(app_client, owner_headers, registry, "hoi an", PROMPT_GRAPH)
    page = app_client.get(f"/workflows/{wid}", headers=owner_headers).text
    assert '<h2 id="wf-title">hoi an</h2>' in page
    assert f'action="/workflows/{wid}/run"' in page
    assert f"/workflows/{wid}/download" in page
    # one prompt node: a single box labelled "Prompt", prefilled with the graph's own text
    assert 'name="text:2"' in page and "phố cổ Hội An về đêm</textarea>" in page
    assert ">Prompt</label>" in page
    assert "text only" in page and "not run yet" in page
    assert "4 nodes" in page


def test_several_prompt_nodes_get_one_labelled_box_each(app_client, owner_headers, registry):
    wid = _upload(app_client, owner_headers, registry, "fox", TWO_TEXT_GRAPH)
    page = app_client.get(f"/workflows/{wid}", headers=owner_headers).text
    assert 'name="text:5"' in page and 'name="text:6"' in page and 'name="text:7"' not in page
    assert "Positive <small" in page and "Negative <small" in page


def test_unknown_workflow_page_is_a_404(app_client, owner_headers):
    assert app_client.get("/workflows/424242", headers=owner_headers).status_code == 404


def test_empty_page_opens_the_upload_form(app_client, owner_headers):
    page = app_client.get("/workflows", headers=owner_headers).text
    assert '<details class="wf-upload" open>' in page
    assert "No workflows yet" in page


def test_upload_error_reopens_the_upload_form_with_the_message(app_client, owner_headers, registry):
    _upload(app_client, owner_headers, registry, "taken", PROMPT_GRAPH)
    backend = next(iter(registry.backends.values()))
    response = app_client.post(
        "/workflows",
        headers=owner_headers,
        data={"name": "taken", "backend_id": backend.id},
        files={"graph_file": ("wf.json", json.dumps(PROMPT_GRAPH).encode(), "application/json")},
    )
    assert response.status_code == 200
    assert '<details class="wf-upload" open>' in response.text
    assert "already exists" in response.text


# -- run with a new prompt --------------------------------------------------------------------------------


def test_run_sends_the_edited_prompt_and_keeps_the_stored_graph(app_client, owner_headers, registry, conn):
    wid = _upload(app_client, owner_headers, registry, "hoi an", PROMPT_GRAPH)
    response = app_client.post(
        f"/workflows/{wid}/run",
        headers=owner_headers,
        data={"seed_mode": "random", "count": "2", "text:2": "Hà Nội mùa thu\r\nlá vàng"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    rows = conn.execute("SELECT graph_json, params_json FROM jobs ORDER BY id").fetchall()
    assert len(rows) == 2
    for row in rows:
        assert json.loads(row["graph_json"])["2"]["inputs"]["prompt"] == "Hà Nội mùa thu\nlá vàng"
        assert json.loads(row["params_json"])["prompt"] == "Hà Nội mùa thu\nlá vàng"
    stored = json.loads(conn.execute("SELECT graph_json FROM workflows").fetchone()["graph_json"])
    assert stored["2"]["inputs"]["prompt"] == "phố cổ Hội An về đêm"


def test_run_without_a_prompt_field_keeps_the_graphs_text(app_client, owner_headers, registry, conn):
    wid = _upload(app_client, owner_headers, registry, "hoi an", PROMPT_GRAPH)
    app_client.post(
        f"/workflows/{wid}/run", headers=owner_headers, data={"seed_mode": "random", "count": "1"}
    )
    graph = json.loads(conn.execute("SELECT graph_json FROM jobs").fetchone()["graph_json"])
    assert graph["2"]["inputs"]["prompt"] == "phố cổ Hội An về đêm"


def test_run_with_an_overlong_prompt_shows_a_message_in_the_panel(app_client, owner_headers, registry, conn):
    wid = _upload(app_client, owner_headers, registry, "hoi an", PROMPT_GRAPH)
    response = app_client.post(
        f"/workflows/{wid}/run",
        headers=owner_headers,
        data={"seed_mode": "random", "count": "1", "text:2": "x" * (custom_workflows.MAX_PROMPT_LEN + 1)},
    )
    assert response.status_code == 200
    assert "A prompt can be at most" in response.text
    assert '<h2 id="wf-title">hoi an</h2>' in response.text
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


# -- results ---------------------------------------------------------------------------------------------


def test_panel_lists_recent_results_and_the_list_counts_them(
    app_client, owner_headers, registry, conn, fake_gateway, png_bytes
):
    wid = _upload(app_client, owner_headers, registry, "hoi an", PROMPT_GRAPH)
    app_client.post(
        f"/workflows/{wid}/run", headers=owner_headers, data={"seed_mode": "random", "count": "1"}
    )
    worker = app_client.app.state.worker
    asyncio.run(worker.dispatch_once())
    call_id = conn.execute("SELECT call_id FROM jobs").fetchone()["call_id"]
    fake_gateway.finish(call_id, png_bytes)
    asyncio.run(worker.poll_once())
    image_id = conn.execute("SELECT id FROM images").fetchone()["id"]

    page = app_client.get(f"/workflows/{wid}", headers=owner_headers).text
    assert f'<a href="/images/{image_id}"><img src="/images/{image_id}/thumb"' in page
    assert re.search(r">1 result<", page)
