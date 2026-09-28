"""The starter workflows migrations 0004 and 0005 store: what they contain, that they run like any
upload, and that a migration never overwrites or resurrects a workflow."""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image

from artio import custom_workflows, db, storage
from artio.workflows.qwen_image_21 import CLIP, UNET, VAE

MIGRATIONS = Path(db.__file__).parent / "migrations"
MIGRATION = MIGRATIONS / "0004_starter_workflows.sql"
REFERENCE_MIGRATION = MIGRATIONS / "0005_reference_workflows.sql"
# name: (encoder resolution, number of images, canvas). An edit renders on the encoder's latent of
# image_1 (its output 2); a reference workflow renders a new picture on a blank 9:16 canvas.
EDITS = {
    "Qwen 2.1 Image Edit": (1024, 1, "edit"),
    "Qwen 2.1 Remove Background": (1024, 1, "edit"),
    "Qwen 2.1 2K Upscale": (2048, 1, "edit"),
}
REFERENCES = {
    "Qwen 2.1 Reference to Image": (1024, 1, (1088, 1920)),
    "Qwen 2.1 Two References to Image": (1024, 2, (1088, 1920)),
}
STARTERS = EDITS | REFERENCES
# The built-in text-to-image graph's own node types plus Load Image: nothing the Modal backend lacks.
NATIVE_NODES = {
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "LoadImage",
    "TextEncodeQwenImage21",
    "EmptyLatentImage",
    "KSampler",
    "VAEDecode",
    "SaveImage",
}


def _starters(conn) -> dict[str, dict]:
    rows = conn.execute("SELECT name, backend_id, graph_json FROM workflows ORDER BY id").fetchall()
    assert {row["backend_id"] for row in rows} == {"qwen21-uc"}
    return {row["name"]: json.loads(row["graph_json"]) for row in rows}


def _workflow_id(conn, name: str) -> int:
    return conn.execute("SELECT id FROM workflows WHERE name = ?", (name,)).fetchone()["id"]


def test_migrate_stores_every_starter(settings):
    db.migrate(settings)
    with db.session(settings) as conn:
        graphs = _starters(conn)
        summaries = {s.name: s for s in custom_workflows.list_workflows(conn)}
    assert list(graphs) == list(STARTERS)
    for name, graph in graphs.items():
        # Stored exactly as an upload of the same graph would be, slots included.
        assert custom_workflows.validate_api_graph(json.dumps(graph).encode()) == graph
        assert summaries[name].image_slots == tuple(custom_workflows.image_slots(graph))
        resolution, image_count, canvas = STARTERS[name]
        slots = custom_workflows.image_slots(graph)
        assert len(slots) == image_count
        (text,) = custom_workflows.text_slots(graph)
        assert text.value.strip()
        assert {node["class_type"] for node in graph.values()} <= NATIVE_NODES
        nodes = {node["class_type"]: node["inputs"] for node in graph.values()}
        assert nodes["UNETLoader"]["unet_name"] == UNET
        assert nodes["CLIPLoader"]["clip_name"] == CLIP
        assert nodes["VAELoader"]["vae_name"] == VAE
        (encoder_id,) = [
            node_id for node_id, node in graph.items() if node["class_type"] == "TextEncodeQwenImage21"
        ]
        encoder = graph[encoder_id]["inputs"]
        for index, slot in enumerate(slots, start=1):
            assert encoder[f"images.image_{index}"] == [slot.node_id, 0]
        assert encoder["resolution"] == resolution
        latent = nodes["KSampler"]["latent_image"]
        if canvas == "edit":
            assert latent == [encoder_id, 2]
        else:
            assert graph[latent[0]]["class_type"] == "EmptyLatentImage"
            assert (nodes["EmptyLatentImage"]["width"], nodes["EmptyLatentImage"]["height"]) == canvas


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
    assert set(graphs) == set(EDITS)


def test_a_starter_runs_with_an_image_and_a_new_prompt(app_client, owner_headers, settings, conn):
    conn.executescript(MIGRATION.read_text())  # app_client starts from an empty Workflows page
    wid = _workflow_id(conn, "Qwen 2.1 Remove Background")
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


def test_a_reference_workflow_runs_with_two_images(app_client, owner_headers, settings, conn):
    conn.executescript(REFERENCE_MIGRATION.read_text())
    wid = _workflow_id(conn, "Qwen 2.1 Two References to Image")
    page = app_client.get(f"/workflows/{wid}", headers=owner_headers).text
    assert 'name="image:4"' in page and 'name="image:5"' in page and 'name="text:10"' in page
    assert "Reference &lt;image2&gt;" in page

    images = []
    for color in ((200, 40, 40), (40, 40, 200)):
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), color=color).save(buf, format="PNG")
        images.append(buf.getvalue())
    response = app_client.post(
        f"/workflows/{wid}/run",
        headers=owner_headers,
        data={"seed_mode": "random", "count": "1", "text:10": "<image1> wearing <image2>"},
        files={
            "image:4": ("me.png", images[0], "image/png"),
            "image:5": ("shirt.png", images[1], "image/png"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    graph = json.loads(conn.execute("SELECT graph_json FROM jobs").fetchone()["graph_json"])
    assert storage.read_input_image(settings.data_dir, graph["4"]["inputs"]["image"]) == images[0]
    assert storage.read_input_image(settings.data_dir, graph["5"]["inputs"]["image"]) == images[1]
    assert graph["10"]["inputs"]["prompt"] == "<image1> wearing <image2>"
