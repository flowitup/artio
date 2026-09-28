"""The chat view: sessions, the three ways a message becomes a prompt, polling and removal."""

from __future__ import annotations

import asyncio
import json

from artio import chat
from artio.worker import Worker


def _form(registry, text: str, **overrides) -> dict:
    model = next(iter(registry.models.values()))
    size = model.param_schema.default_size()
    form = {
        "model_id": model.id,
        "text": text,
        "mode": "new",
        "preset": size.name,
        "width": size.width,
        "height": size.height,
        "steps": model.param_schema.steps_default,
        "cfg": model.param_schema.cfg_default,
        "negative": "",
        "count": 2,
    }
    return form | overrides


def _post(app_client, owner_headers, path: str, data: dict):
    return app_client.post(path, headers=owner_headers, data=data, follow_redirects=False)


def _ids_from(location: str) -> tuple[int, int]:
    path, _, fragment = location.partition("#turn-")
    return int(path.rsplit("/", 1)[1]), int(fragment)


def _batch(conn, batch_id: int):
    batch = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    seeds = [
        json.loads(row["params_json"])["seed"]
        for row in conn.execute("SELECT params_json FROM jobs WHERE batch_id = ? ORDER BY id", (batch_id,))
    ]
    return batch, json.loads(batch["base_params_json"]), seeds


def _finish_batch(settings, registry, fake_gateway, conn, batch_id: int, png_bytes: bytes) -> None:
    worker = Worker(settings, registry, fake_gateway)
    asyncio.run(worker.dispatch_once())
    for job in conn.execute("SELECT call_id FROM jobs WHERE batch_id = ?", (batch_id,)).fetchall():
        fake_gateway.finish(job["call_id"], png_bytes)
    asyncio.run(worker.poll_once())


def test_home_opens_a_new_chat_until_a_conversation_exists(app_client, owner_headers, registry):
    response = app_client.get("/", headers=owner_headers, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/chat"

    sent = _post(app_client, owner_headers, "/chat", _form(registry, "a red fox in snow"))
    session_id, _ = _ids_from(sent.headers["location"])
    response = app_client.get("/", headers=owner_headers, follow_redirects=False)
    assert response.headers["location"] == f"/chat/{session_id}"


def test_first_message_starts_a_session_titled_by_its_text(app_client, owner_headers, registry, conn):
    response = _post(app_client, owner_headers, "/chat", _form(registry, "a lighthouse kitchen at dawn"))
    assert response.status_code == 303
    session_id, batch_id = _ids_from(response.headers["location"])

    batch, params, seeds = _batch(conn, batch_id)
    assert batch["session_id"] == session_id
    assert batch["message"] == "a lighthouse kitchen at dawn"
    assert params["prompt"] == "a lighthouse kitchen at dawn"
    assert len(set(seeds)) == 2
    assert chat.get_session(conn, session_id).title == "a lighthouse kitchen at dawn"

    page = app_client.get(f"/chat/{session_id}", headers=owner_headers)
    assert page.status_code == 200
    assert "a lighthouse kitchen at dawn" in page.text
    assert f'id="turn-{batch_id}"' in page.text


def test_a_refine_reply_appends_to_the_last_prompt_and_keeps_its_settings(app_client, owner_headers, registry, conn):
    first = _post(app_client, owner_headers, "/chat", _form(registry, "a lighthouse kitchen", steps=30, count=3))
    session_id, _ = _ids_from(first.headers["location"])

    page = app_client.get(f"/chat/{session_id}", headers=owner_headers)
    assert 'name="steps"' in page.text and 'value="30"' in page.text
    assert '<option value="3" selected>' in page.text

    reply = _post(
        app_client, owner_headers, f"/chat/{session_id}",
        _form(registry, "warmer light", mode="refine", steps=30, count=3),
    )
    same_session, batch_id = _ids_from(reply.headers["location"])
    assert same_session == session_id
    batch, params, seeds = _batch(conn, batch_id)
    assert params["prompt"] == "a lighthouse kitchen, warmer light"
    assert batch["message"] == "warmer light"
    assert len(seeds) == 3


def test_a_new_prompt_in_an_existing_session_ignores_the_last_prompt(app_client, owner_headers, registry, conn):
    first = _post(app_client, owner_headers, "/chat", _form(registry, "a lighthouse kitchen"))
    session_id, _ = _ids_from(first.headers["location"])
    reply = _post(app_client, owner_headers, f"/chat/{session_id}", _form(registry, "a night market", mode="new"))
    _, batch_id = _ids_from(reply.headers["location"])
    _, params, _ = _batch(conn, batch_id)
    assert params["prompt"] == "a night market"


def test_refining_one_image_keeps_its_seed(
    app_client, owner_headers, registry, conn, settings, fake_gateway, png_bytes
):
    first = _post(app_client, owner_headers, "/chat", _form(registry, "a beekeeper portrait"))
    session_id, batch_id = _ids_from(first.headers["location"])
    _finish_batch(settings, registry, fake_gateway, conn, batch_id, png_bytes)
    image = conn.execute(
        "SELECT images.id, images.seed FROM images JOIN jobs ON jobs.id = images.job_id "
        "WHERE jobs.batch_id = ? ORDER BY images.id DESC LIMIT 1",
        (batch_id,),
    ).fetchone()

    page = app_client.get(f"/chat/{session_id}?refine={image['id']}", headers=owner_headers)
    assert f'name="refine_image_id" value="{image["id"]}"' in page.text
    assert "Refining this image" in page.text

    reply = _post(
        app_client, owner_headers, f"/chat/{session_id}",
        _form(registry, "overcast sky", mode="refine", refine_image_id=image["id"], count=1),
    )
    _, new_batch = _ids_from(reply.headers["location"])
    _, params, seeds = _batch(conn, new_batch)
    assert params["prompt"] == "a beekeeper portrait, overcast sky"
    assert seeds == [image["seed"]]


def test_an_empty_message_is_refused_inline_and_creates_nothing(app_client, owner_headers, registry, conn):
    response = _post(app_client, owner_headers, "/chat", _form(registry, "   "))
    assert response.status_code == 200
    assert "Type a prompt" in response.text
    assert conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


def test_an_invalid_setting_is_refused_inline_and_keeps_the_typed_text(app_client, owner_headers, registry, conn):
    response = _post(app_client, owner_headers, "/chat", _form(registry, "a red fox", steps=999))
    assert response.status_code == 200
    assert "steps must be between" in response.text
    assert ">a red fox</textarea>" in response.text
    assert conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0] == 0


def test_the_turn_partial_polls_until_every_job_settles(
    app_client, owner_headers, registry, conn, settings, fake_gateway, png_bytes
):
    sent = _post(app_client, owner_headers, "/chat", _form(registry, "a red fox"))
    session_id, batch_id = _ids_from(sent.headers["location"])

    running = app_client.get(f"/chat/turns/{batch_id}", headers=owner_headers)
    assert running.status_code == 200
    assert 'hx-trigger="every 2s"' in running.text

    _finish_batch(settings, registry, fake_gateway, conn, batch_id, png_bytes)
    done = app_client.get(f"/chat/turns/{batch_id}", headers=owner_headers)
    assert done.status_code == 286
    assert "hx-trigger" not in done.text
    assert f"/chat/{session_id}?refine=" in done.text


def test_removing_a_conversation_keeps_its_images(
    app_client, owner_headers, registry, conn, settings, fake_gateway, png_bytes
):
    sent = _post(app_client, owner_headers, "/chat", _form(registry, "a red fox"))
    session_id, batch_id = _ids_from(sent.headers["location"])
    _finish_batch(settings, registry, fake_gateway, conn, batch_id, png_bytes)

    response = app_client.post(f"/chat/{session_id}/delete", headers=owner_headers)
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/chat"
    assert chat.get_session(conn, session_id) is None
    batch = conn.execute("SELECT session_id FROM batches WHERE id = ?", (batch_id,)).fetchone()
    assert batch["session_id"] is None
    images = conn.execute(
        "SELECT COUNT(*) FROM images JOIN jobs ON jobs.id = images.job_id WHERE jobs.batch_id = ?", (batch_id,)
    ).fetchone()[0]
    assert images == 2
    assert app_client.get(f"/chat/{session_id}", headers=owner_headers).status_code == 404


def test_sidebar_lists_conversations_newest_first_and_marks_the_current_one(app_client, owner_headers, registry):
    older = _post(app_client, owner_headers, "/chat", _form(registry, "older idea"))
    newer = _post(app_client, owner_headers, "/chat", _form(registry, "newer idea"))
    older_id, _ = _ids_from(older.headers["location"])
    newer_id, _ = _ids_from(newer.headers["location"])

    response = app_client.get(f"/partials/chat-sessions?current={older_id}", headers=owner_headers)
    assert response.status_code == 200
    assert response.text.index("newer idea") < response.text.index("older idea")
    assert f'href="/chat/{older_id}" aria-current="page"' in response.text
    assert f'href="/chat/{newer_id}" aria-current' not in response.text


def test_composer_estimates_cost_from_recent_renders(conn, registry):
    model = next(iter(registry.models.values()))
    assert chat.cost_per_megapixel(conn, model.id) is None


def test_join_prompt_trims_trailing_punctuation():
    assert chat.join_prompt("a red fox.", "in snow") == "a red fox, in snow"
    assert chat.join_prompt("", "in snow") == "in snow"


def test_seed_label_shows_a_range_only_for_consecutive_seeds():
    def turn_with(seeds):
        jobs = [chat.TurnJob(id=i, status="done", seed=s, error=None, image_id=None) for i, s in enumerate(seeds)]
        return chat.Turn(
            batch_id=1, session_id=1, message="m", model_id="x", prompt="p", negative="", width=1, height=1,
            steps=1, cfg=1.0, created_at=0.0, jobs=jobs, duration_s=None, est_cost_usd=None,
        )

    assert turn_with([7]).seed_label == "seed 7"
    assert turn_with([7, 8, 9]).seed_label == "seeds 7–9"
    assert turn_with([7, 900]).seed_label is None


def test_a_lower_resolution_scales_the_shape_and_carries_over(app_client, owner_headers, registry, conn):
    sent = _post(app_client, owner_headers, "/chat", _form(registry, "a harbour", preset="16:9", tier="720p"))
    session_id, batch_id = _ids_from(sent.headers["location"])
    _, params, _ = _batch(conn, batch_id)
    assert (params["width"], params["height"]) == (1280, 720)

    page = app_client.get(f"/chat/{session_id}", headers=owner_headers)
    assert 'name="tier" value="720p"\n               checked' in page.text
    assert 'name="preset" value="16:9" checked' in page.text


def test_an_unknown_resolution_is_refused(app_client, owner_headers, registry):
    response = _post(app_client, owner_headers, "/chat", _form(registry, "a harbour", tier="8K"))
    assert response.status_code == 200
    assert "Unknown resolution" in response.text


def test_cost_estimate_scales_with_pixel_count(app_client, owner_headers, registry, conn):
    model = next(iter(registry.models.values()))
    full_mp, small_mp = 1920 * 1088 / 1e6, 1280 * 720 / 1e6
    for tier, cost in (("1080p", 0.04), ("720p", 0.04 * small_mp / full_mp)):
        sent = _post(app_client, owner_headers, "/chat", _form(registry, "a harbour", preset="16:9", tier=tier))
        _, batch_id = _ids_from(sent.headers["location"])
        conn.execute("UPDATE jobs SET status = 'done', est_cost_usd = ? WHERE batch_id = ?", (cost, batch_id))
        conn.commit()

    rate = chat.cost_per_megapixel(conn, model.id)
    assert abs(rate * full_mp - 0.04) < 1e-9
    assert abs(rate * small_mp - 0.04 * small_mp / full_mp) < 1e-9
