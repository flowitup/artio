"""Presets, stars, tags and search: the library.py functions directly, and the HTML routes that
front them."""

from __future__ import annotations

import asyncio
import re
import time

import pytest

from artio import library


def _finished_image(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, *, prompt: str, negative: str = "", seed: int = 1
):
    model = next(iter(registry.models.values()))
    preset = model.param_schema.default_size()
    response = app_client.post(
        "/generate",
        headers=owner_headers,
        follow_redirects=False,
        data={
            "model_id": model.id,
            "prompt": prompt,
            "negative": negative,
            "preset": preset.name,
            "width": preset.width,
            "height": preset.height,
            "steps": model.param_schema.steps_default,
            "cfg": model.param_schema.cfg_default,
            "seed_mode": "fixed",
            "seed": seed,
            "count": 1,
        },
    )
    assert response.status_code == 303
    asyncio.run(app_client.app.state.worker.dispatch_once())
    job = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    fake_gateway.finish(job["call_id"], png_bytes)
    asyncio.run(app_client.app.state.worker.poll_once())
    return conn.execute("SELECT id FROM images WHERE job_id = ?", (job["id"],)).fetchone()["id"]


# -- presets ---------------------------------------------------------------------------------------


def test_preset_round_trip_save_load_overwrite_delete(app_client, owner_headers, registry, conn):
    model = next(iter(registry.models.values()))
    preset = model.param_schema.default_size()
    form = {
        "preset_name": "portrait template",
        "model_id": model.id,
        "prompt": "a portrait, soft light",
        "negative": "blurry",
        "preset": preset.name,
        "width": preset.width,
        "height": preset.height,
        "steps": model.param_schema.steps_default,
        "cfg": model.param_schema.cfg_default,
    }

    save = app_client.post("/presets", headers=owner_headers, data=form)
    assert save.status_code == 200
    assert "Saved preset" in save.text
    assert "portrait template" in save.text

    listing = app_client.get("/library", headers=owner_headers)
    assert listing.status_code == 200
    assert "portrait template" in listing.text
    preset_id = int(re.search(r"/presets/(\d+)/delete", listing.text).group(1))

    load = app_client.get(f"/generate?preset={preset_id}", headers=owner_headers)
    assert load.status_code == 200
    assert "a portrait, soft light" in load.text
    assert "blurry" in load.text

    # Overwrite: same name, a different prompt. Still exactly one row, same id, new content.
    form["prompt"] = "a portrait, hard light"
    overwrite = app_client.post("/presets", headers=owner_headers, data=form)
    assert overwrite.status_code == 200
    assert conn.execute("SELECT COUNT(*) AS n FROM presets").fetchone()["n"] == 1
    reloaded = library.get_preset(conn, preset_id)
    assert reloaded.id == preset_id
    assert reloaded.params["prompt"] == "a portrait, hard light"

    delete = app_client.post(f"/presets/{preset_id}/delete", headers=owner_headers)
    assert delete.status_code == 200
    assert "portrait template" not in delete.text
    assert conn.execute("SELECT COUNT(*) AS n FROM presets").fetchone()["n"] == 0


def test_save_preset_upsert_bumps_updated_at_not_created_at(conn):
    first = time.time()
    preset_id = library.save_preset(conn, "p", "model-a", {"prompt": "one"}, first)
    later = first + 100
    same_id = library.save_preset(conn, "p", "model-a", {"prompt": "two"}, later)
    assert same_id == preset_id
    row = conn.execute("SELECT created_at, updated_at FROM presets WHERE id = ?", (preset_id,)).fetchone()
    assert row["created_at"] == first
    assert row["updated_at"] == later


def test_save_preset_rejects_an_empty_name(conn):
    with pytest.raises(library.InvalidPresetName):
        library.save_preset(conn, "   ", "model-a", {}, time.time())


def test_delete_unknown_preset_raises(conn):
    with pytest.raises(library.UnknownPreset):
        library.delete_preset(conn, 999999)


def test_save_preset_rejects_a_name_over_the_length_cap(conn):
    with pytest.raises(library.InvalidPresetName):
        library.save_preset(conn, "x" * (library.MAX_PRESET_NAME_LEN + 1), "model-a", {}, time.time())
    # The cap itself is a valid length: this is what pins the cap at exactly 200, not merely "some cap".
    preset_id = library.save_preset(conn, "x" * library.MAX_PRESET_NAME_LEN, "model-a", {}, time.time())
    assert library.get_preset(conn, preset_id) is not None


def test_save_preset_route_requires_a_model(app_client, owner_headers):
    response = app_client.post("/presets", headers=owner_headers, data={"preset_name": "x", "model_id": ""})
    assert response.status_code == 200
    assert "Choose a model" in response.text


def test_preset_name_is_escaped_in_the_library_list(app_client, owner_headers, registry):
    model = next(iter(registry.models.values()))
    name = "<script>alert(1)</script>"
    app_client.post("/presets", headers=owner_headers, data={"preset_name": name, "model_id": model.id})
    listing = app_client.get("/library", headers=owner_headers)
    assert "<script>alert(1)</script>" not in listing.text
    assert "&lt;script&gt;" in listing.text


def test_service_identity_cannot_save_or_delete_presets(app_client, service_headers, route_ids):
    assert app_client.post("/presets", headers=service_headers, data={"preset_name": "x"}).status_code == 403
    assert (
        app_client.post(f"/presets/{route_ids['preset_id']}/delete", headers=service_headers).status_code == 403
    )


def test_library_delete_button_carries_hx_select_for_the_partial_swap(app_client, owner_headers, route_ids):
    """`hx-select`/`hx-target` are what make a Delete POST -- which re-renders the whole library.html
    page, the only way to reuse it without a dedicated partial file -- swap in only #library-content
    client-side. htmx itself isn't running in this test, so nothing here proves the swap happens; it
    proves the attribute that makes it happen is still on the button, which a template edit could
    silently drop without any other test noticing."""
    response = app_client.get("/library", headers=owner_headers)
    assert 'hx-target="#library-content"' in response.text
    assert 'hx-select="#library-content"' in response.text


# -- stars and tags ----------------------------------------------------------------------------------


def test_star_toggle_and_tag_set_are_searchable(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn
):
    starred_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="a lake at dawn", seed=1)
    other_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="a city at night", seed=2)

    toggled = app_client.post(f"/images/{starred_id}/star", headers=owner_headers)
    assert toggled.status_code == 200
    assert "Starred" in toggled.text

    only_starred = app_client.get("/gallery?starred=1", headers=owner_headers)
    assert f"/images/{starred_id}" in only_starred.text
    assert f"/images/{other_id}" not in only_starred.text

    tagged = app_client.post(f"/images/{starred_id}/tags", headers=owner_headers, data={"tags": "landscape, calm"})
    assert tagged.status_code == 200

    by_tag = app_client.get("/gallery?tag=landscape", headers=owner_headers)
    assert f"/images/{starred_id}" in by_tag.text
    assert f"/images/{other_id}" not in by_tag.text

    # A tag is also findable through the free-text search box (the FTS trigger keeps images_fts.tags
    # in sync with image_tags): searching a tag word finds the tagged image, not the untagged one.
    by_text = app_client.get("/gallery?q=landscape", headers=owner_headers)
    assert f"/images/{starred_id}" in by_text.text
    assert f"/images/{other_id}" not in by_text.text


def test_star_toggle_is_reversible(app_client, owner_headers, registry, fake_gateway, png_bytes, conn):
    image_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="p")
    first = app_client.post(f"/images/{image_id}/star", headers=owner_headers)
    assert "Starred" in first.text
    second = app_client.post(f"/images/{image_id}/star", headers=owner_headers)
    assert "Starred" not in second.text
    assert "Star" in second.text


def test_star_on_unknown_image_shows_inline_message_not_a_500(app_client, owner_headers):
    response = app_client.post("/images/999999/star", headers=owner_headers)
    assert response.status_code == 200
    assert "no longer exists" in response.text


@pytest.mark.parametrize(
    "raw",
    ["Bad!Tag", "-startswithdash", "a" * 33, ",".join(f"t{i}" for i in range(21))],
    ids=["bad-char", "bad-start", "too-long", "too-many"],
)
def test_invalid_tags_are_rejected_inline_with_200(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn, raw
):
    image_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="p")
    response = app_client.post(f"/images/{image_id}/tags", headers=owner_headers, data={"tags": raw})
    assert response.status_code == 200
    assert "tag-editor" in response.text
    assert response.text.count("error-text") == 1
    # The invalid text the owner typed is preserved in the field, not silently discarded.
    assert raw.split(",")[0].strip().lower() in response.text or raw in response.text


def _bare_image(conn) -> int:
    """A minimal image row, for library.py unit tests that need no real job engine or files."""
    conn.execute(
        "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) VALUES (0,'m','generate','{}',1)"
    )
    batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, status, created_at) "
        "VALUES (?, 'm', 'qwen21-uc', 'generate', '{}', '{}', 'done', 0)",
        (batch_id,),
    )
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO images (job_id, model_id, file_png, file_thumb, width, height, bytes, sha256, created_at) "
        "VALUES (?, 'm', 'a.png', 'a.webp', 8, 8, 1, 'sha', 0)",
        (job_id,),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_tag_editor_escapes_the_rejected_raw_value(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn
):
    image_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="p")
    response = app_client.post(
        f"/images/{image_id}/tags", headers=owner_headers, data={"tags": '"><script>alert(1)</script>'}
    )
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_tags_are_normalized_lowercase_trimmed_and_deduplicated(conn):
    image_id = _bare_image(conn)
    conn.commit()  # set_tags runs its own BEGIN IMMEDIATE unit of work
    result = library.set_tags(conn, image_id, "  Outdoor ,OUTDOOR, Sunset ")
    assert result == ["outdoor", "sunset"]
    assert conn.execute("SELECT COUNT(*) AS n FROM tags").fetchone()["n"] == 2


def test_set_tags_replaces_the_previous_set_not_appends(conn):
    image_id = _bare_image(conn)
    conn.commit()
    library.set_tags(conn, image_id, "a, b")
    conn.commit()
    result = library.set_tags(conn, image_id, "c")
    assert result == ["c"]  # not ["a", "b", "c"]: the whole set was replaced, nothing was appended
    stored = {
        row["name"]
        for row in conn.execute(
            "SELECT tags.name AS name FROM image_tags JOIN tags ON tags.id = image_tags.tag_id "
            "WHERE image_tags.image_id = ?",
            (image_id,),
        ).fetchall()
    }
    assert stored == {"c"}


def test_set_tags_on_unknown_image_raises(conn):
    with pytest.raises(library.UnknownImage):
        library.set_tags(conn, 999999, "a")


def test_toggle_star_on_unknown_image_raises(conn):
    with pytest.raises(library.UnknownImage):
        library.toggle_star(conn, 999999)


def test_service_identity_cannot_star_or_tag(app_client, service_headers, route_ids):
    image_id = route_ids["image_id"]
    assert app_client.post(f"/images/{image_id}/star", headers=service_headers).status_code == 403
    assert app_client.post(f"/images/{image_id}/tags", headers=service_headers).status_code == 403


# -- search ------------------------------------------------------------------------------------------


def test_search_matches_prompt_words_and_prefixes(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn
):
    fox_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="a red fox in snow", seed=1)
    whale_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="a blue whale in ocean", seed=2)

    whole_word = app_client.get("/gallery?q=fox", headers=owner_headers)
    assert f"/images/{fox_id}" in whole_word.text
    assert f"/images/{whale_id}" not in whole_word.text

    prefix = app_client.get("/gallery?q=wha", headers=owner_headers)
    assert f"/images/{whale_id}" in prefix.text
    assert f"/images/{fox_id}" not in prefix.text

    no_match = app_client.get("/gallery?q=giraffe", headers=owner_headers)
    assert "No images yet" in no_match.text


def test_search_never_matches_the_negative_prompt(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn
):
    """images_fts also indexes the negative prompt (migration 0001), but a search must never surface
    an image made specifically to exclude the searched term: a negative prompt of "cat, blurry" is
    not a sensible match for a search for "cat". The spec's own wording is "search by prompt text or
    tag" -- prompt and tag, not negative."""
    prompt_match_id = _finished_image(
        app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="a cat on a mat", seed=1
    )
    negative_only_id = _finished_image(
        app_client, owner_headers, registry, fake_gateway, png_bytes, conn,
        prompt="a dog running", negative="cat, blurry, watermark", seed=2,
    )

    response = app_client.get("/gallery?q=cat", headers=owner_headers)
    assert response.status_code == 200
    assert f"/images/{prompt_match_id}" in response.text
    assert f"/images/{negative_only_id}" not in response.text


def test_search_multiple_terms_require_all_of_them(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn
):
    both_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="a red fox in snow", seed=1)
    red_only_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="a red car", seed=2)
    fox_only_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="a blue fox", seed=3)

    response = app_client.get("/gallery?q=red+fox", headers=owner_headers)
    assert response.status_code == 200
    assert f"/images/{both_id}" in response.text
    assert f"/images/{red_only_id}" not in response.text
    assert f"/images/{fox_only_id}" not in response.text


def test_search_with_a_nul_byte_does_not_500(app_client, owner_headers):
    embedded = app_client.get("/gallery", headers=owner_headers, params={"q": "a\x00b"})
    assert embedded.status_code == 200
    only_nul = app_client.get("/gallery", headers=owner_headers, params={"q": "\x00"})
    assert only_nul.status_code == 200  # normalizes to an empty query: same as no filter at all


def test_search_results_are_newest_first(app_client, owner_headers, registry, fake_gateway, png_bytes, conn):
    ids = [
        _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="match alpha", seed=i)
        for i in range(1, 4)
    ]
    assert ids == sorted(ids)  # sanity: created in ascending id order

    response = app_client.get("/gallery?q=match", headers=owner_headers)
    assert response.status_code == 200
    positions = [response.text.index(f'/images/{i}"') for i in ids]
    # Rendered newest-first means the highest id's link appears earliest in the HTML, so the
    # position list (in ascending-id order) must be strictly decreasing.
    assert positions == sorted(positions, reverse=True)


@pytest.mark.parametrize(
    "weird",
    [
        'a" OR -b NEAR(',
        '"""',
        "(unclosed",
        "col:value",
        "NEAR(a,b)",
        "café naïve",  # unicode
        "*",
        "-",
    ],
)
def test_search_treats_operators_and_quotes_as_text(app_client, owner_headers, weird):
    response = app_client.get("/gallery", headers=owner_headers, params={"q": weird})
    assert response.status_code == 200  # never a syntax error, whatever the input


def test_deleted_image_leaves_the_search_index(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn
):
    image_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="a unique gizmo prompt", seed=1)
    found = app_client.get("/gallery?q=gizmo", headers=owner_headers)
    assert f"/images/{image_id}" in found.text

    app_client.post(f"/images/{image_id}/delete", headers=owner_headers)

    assert conn.execute("SELECT 1 FROM images_fts WHERE rowid = ?", (image_id,)).fetchone() is None
    gone = app_client.get("/gallery?q=gizmo", headers=owner_headers)
    assert gone.status_code == 200
    assert "No images yet" in gone.text


def test_gallery_combines_q_tag_starred_and_model_filters(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn
):
    model = next(iter(registry.models.values()))
    match_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="combo target", seed=1)
    app_client.post(f"/images/{match_id}/star", headers=owner_headers)
    app_client.post(f"/images/{match_id}/tags", headers=owner_headers, data={"tags": "combo"})
    decoy_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="combo decoy", seed=2)
    app_client.post(f"/images/{decoy_id}/tags", headers=owner_headers, data={"tags": "combo"})

    response = app_client.get(
        "/gallery", headers=owner_headers, params={"q": "combo", "tag": "combo", "starred": "1", "model": model.id}
    )
    assert f"/images/{match_id}" in response.text
    assert f"/images/{decoy_id}" not in response.text


def test_tag_filter_matches_regardless_of_case_or_surrounding_whitespace(
    app_client, owner_headers, registry, fake_gateway, png_bytes, conn
):
    image_id = _finished_image(app_client, owner_headers, registry, fake_gateway, png_bytes, conn, prompt="a beach scene")
    app_client.post(f"/images/{image_id}/tags", headers=owner_headers, data={"tags": "beach"})  # stored lower-case

    for raw_tag in ("Beach", " beach ", "BEACH"):
        response = app_client.get("/gallery", headers=owner_headers, params={"tag": raw_tag})
        assert f"/images/{image_id}" in response.text, f"tag={raw_tag!r} should still match"


def test_gallery_search_echo_is_escaped(app_client, owner_headers):
    payload = '"><script>alert(1)</script>'
    response = app_client.get("/gallery", headers=owner_headers, params={"q": payload})
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
