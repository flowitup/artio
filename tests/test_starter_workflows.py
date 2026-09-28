"""The starter workflows migration 0004 stores: what they contain, that they run like any upload, and
that the migration never overwrites or resurrects a workflow."""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image

from artio import custom_workflows, db, storage
from artio.workflows.qwen_image_21 import CLIP, UNET, VAE

MIGRATION = Path(db.__file__).parent / "migrations" / "0004_starter_workflows.sql"
STARTERS = {"Qwen 2.1 Image Edit": 1024, "Qwen 2.1 Remove Background": 1024, "Qwen 2.1 2K Upscale": 2048}
# The built-in text-to-image graph's own node types plus Load Image: nothing the Modal backend lacks.
NATIVE_NODES = {
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "LoadImage",
    "TextEncodeQwenImage21",
    "KSampler",
    "VAEDecode",
    "SaveImage",
}


def _starters(conn) -> dict[str, dict]:
    rows = conn.execute("SELECT name, backend_id, graph_json FROM workflows ORDER BY id").fetchall()
    assert {row["backend_id"] for row in rows} == {"qwen21-uc"}
    return {row["name"]: json.loads(row["graph_json"]) for row in rows}


def test_migrate_stores_the_three_starters(settings):
    db.migrate(settings)
    with db.session(settings) as conn:
        graphs = _starters(conn)
        summaries = {s.name: s for s in custom_workflows.list_workflows(conn)}
    assert list(graphs) == list(STARTERS)
    for name, graph in graphs.items():
        # Stored exactly as an upload of the same graph would be, slots included.
        assert custom_workflows.validate_api_graph(json.dumps(graph).encode()) == graph
        assert summaries[name].image_slots == tuple(custom_workflows.image_slots(graph))
        (slot,) = custom_workflows.image_slots(graph)
        (text,) = custom_workflows.text_slots(graph)
        assert text.value.strip()
        assert {node["class_type"] for node in graph.values()} <= NATIVE_NODES
        nodes = {node["class_type"]: node["inputs"] for node in graph.values()}
        assert nodes["UNETLoader"]["unet_name"] == UNET
        assert nodes["CLIPLoader"]["clip_name"] == CLIP
        assert nodes["VAELoader"]["vae_name"] == VAE
        encoder = nodes["TextEncodeQwenImage21"]
        assert encoder["images.image_1"] == [slot.node_id, 0]
        assert encoder["resolution"] == STARTERS[name]


def test_a_deleted_starter_stays_deleted(settings):
    db.migrate(settings)
    with db.session(settings) as conn:
        conn.execute("DELETE FROM workflows WHERE name = 'Qwen 2.1 2K Upscale'")
    db.migrate(settings)
    with db.session(settings) as conn:
        assert "Qwen 2.1 2K Upscale" not in _starters(conn)


def test_a_workflow_already_stored_under_a_starter_name_is_kept(settings):
    db.migrate(settings)
    with db.session(settings) as conn:
        conn.execute("DELETE FROM workflows")
        conn.execute(
            "INSERT INTO workflows (name, backend_id, graph_json, created_at) VALUES (?, 'qwen21-uc', '{}', 0)",
            ("Qwen 2.1 Image Edit",),
        )
        conn.executescript(MIGRATION.read_text())
        graphs = _starters(conn)
    assert graphs["Qwen 2.1 Image Edit"] == {}
    assert set(graphs) == set(STARTERS)


def test_a_starter_runs_with_an_image_and_a_new_prompt(app_client, owner_headers, settings, conn):
    conn.executescript(MIGRATION.read_text())  # app_client starts from an empty Workflows page
    wid = conn.execute("SELECT id FROM workflows WHERE name = 'Qwen 2.1 Remove Background'").fetchone()["id"]
    page = app_client.get(f"/workflows/{wid}", headers=owner_headers).text
    assert 'name="image:4"' in page and 'name="text:5"' in page

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color=(200, 40, 40)).save(buf, format="PNG")
    response = app_client.post(
        f"/workflows/{wid}/run",
        headers=owner_headers,
        data={"seed_mode": "random", "count": "1", "text:5": "Remove the background"},
        files={"image:4": ("cat.png", buf.getvalue(), "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    graph = json.loads(conn.execute("SELECT graph_json FROM jobs").fetchone()["graph_json"])
    assert storage.read_input_image(settings.data_dir, graph["4"]["inputs"]["image"]) == buf.getvalue()
    assert graph["5"]["inputs"]["prompt"] == "Remove the background"
