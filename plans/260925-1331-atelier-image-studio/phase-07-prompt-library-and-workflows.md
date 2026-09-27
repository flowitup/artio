---
phase: 7
title: "Prompt library & custom workflows"
status: completed
priority: P2
effort: "6h"
dependencies: [6]
---

# Phase 7: Prompt library & custom workflows

## Context Links

- Contract criteria 8 (presets, stars, tags, search) and 9 (custom workflows): [brainstorm report](../reports/brainstorm-260925-1507-atelier-image-studio-hetzner.md)
- Brief §3 (the `presets`, `workflows`, `tags`, `image_tags` and `images_fts` tables) and §9 item 7 (API-format validation, reject UI format with a hint, seed override on KSampler-family nodes, run N times): [architecture brief](./reports/architecture-brief.md)
- Red-team evidence: [scope](./reports/red-team-scope-complexity-critic.md) (Findings 3, 4), [security](./reports/red-team-security-adversary.md) (Finding 4, fact checks 11 and 13 for phase 7).
- Backend behavior, `modal/qwen21_uc_app.py`:
  - `run_workflow` at :147-150.
  - `_run` raises `RuntimeError("ComfyUI rejected workflow: …")` at :123-124 when validation fails.
  - It raises the ComfyUI error messages at :130-131.
  - It returns only the **first** image of the outputs at :135-138.
- Schema and FTS triggers already created by `atelier/migrations/0001_init.sql` in phase 2. The body-size middleware and the HTML response rules come from phase 3.

## Overview

When this phase is done, the owner can:
- save the generate form as a named preset, load it back and delete it;
- star and tag images, and search the gallery by prompt text or tag, combined with the model and starred filters;
- upload a ComfyUI API-format workflow under a name for a chosen backend, run it once or N times with distinct seeds, and find the result saved with its exact graph attached;
- see ComfyUI's own validation message on the failed job when a graph is invalid.

Priority P2. No schema change, because every table exists since the initial migration.

## Key Insights

<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

- **Search stays in sync with no service code.** The migration's triggers already maintain `images_fts` on image insert and delete and on tag add and remove.
- **Search is a filter, not a ranking.** FTS5 narrows the result set; results are newest first everywhere, in the same batch-grouped gallery. There is no relevance order and no second layout, so one pagination scheme works for the gallery and for `/api/v1`.
- **User text never reaches FTS5 unquoted.** Each term is double-quoted, with inner quotes doubled, and given a `*` prefix operator. Operators such as `NEAR`, `AND` and `-` in user input are therefore treated as plain words, and a stray quote can never cause a syntax error.
- **ComfyUI's validation error needs no new code path.**
  - An invalid graph reaches ComfyUI's `/prompt`, which rejects it. `_run` raises `ComfyUI rejected workflow: {…}` (`:123-124`), the gateway classifies it as a remote failure, and the job fails with that text.
  - The queue row and the job view already show failure text from phase 3, which satisfies criterion 9's "invalid graph shows ComfyUI's validation error".
- **Local validation checks structure only.**
  - A top-level `nodes` plus `links` means a UI-format export, which is rejected with an export hint.
  - Otherwise the graph must be an object of nodes, each with `class_type` and `inputs`.
  - It must also contain a `SaveImage` or `PreviewImage` node, because `_run` returns only the first saved image (`:135-138`).
- **The upload limit applies before parsing.** Phase 3's body-size middleware gets a 3 MB limit for exactly `POST /workflows`, checked from `Content-Length` and by counting streamed bytes, so a huge upload is refused before multipart parsing spools it. The 2 MB file-size check then runs on the parsed file.
- **These routes are owner-only.** Presets, stars, tags and workflow upload, run and delete are HTML routes, so the plugin's service identity gets a 403 on all of them (phase 3's route authorization). The plugin runs stored workflows only through `/api/v1` (phase 8).
- **HTML responses are always 200.** Star, tags, "save as preset", preset delete and workflow delete are `hx-post` and return their partial, with any error inline. Workflow upload and run are plain forms that redirect with a 303 on success and re-render with the message on error.
- **The seed override touches only literal integers.**
  - `KSampler.inputs.seed`, `KSamplerAdvanced.inputs.noise_seed` and `RandomNoise.inputs.noise_seed` are overridden, and a linked input (a list) is left alone.
  - When no seed input exists, N > 1 would render identical images, so it is rejected with a clear message.
- **Custom-workflow jobs have no model.** A custom-workflow job belongs to a backend, not a model, so `kind='workflow'` and `model_id` is NULL, which the schema's `CHECK` allows. The gallery's model filter gets a "Custom workflows" option.
- **The result keeps its workflow twice over.** `jobs.graph_json` stores the exact graph sent, with its seed, and `jobs.workflow_id` links the stored workflow. Deleting the workflow sets the link to NULL, but the graph stays on the job.

## Requirements

<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

Functional:
- **Presets** (`library.py` plus `routes/library.py`):
  - `POST /presets` ("Save as preset" on the generate form, `hx-post`) saves the current fields with a name. The same name overwrites and bumps `updated_at`. It returns a 200 partial with a confirmation or an inline error.
  - `GET /library` lists presets with Load (`/generate?preset=<id>`) and Delete (`POST /presets/{id}/delete`, `hx-post` with `hx-confirm`, returning the refreshed list).
  - Loading validates against the current model schema at submit time, as any form does.
- **Stars and tags:**
  - `POST /images/{id}/star` toggles the star and returns the button partial.
  - `POST /images/{id}/tags` replaces the tag set from a comma-separated field and returns the tag editor partial, with any validation error inline.
  - Tags are normalized: lower-case, trimmed, pattern `[a-z0-9][a-z0-9 _-]{0,31}`, at most 20 per image.
- **Search and filters:**
  - `/gallery?q=&tag=&starred=1&model=&page=` works with any combination.
  - Matching images show in the usual batch-grouped gallery, newest first.
- **Workflows** (`custom_workflows.py` plus `routes/workflows.py`):
  - `GET /workflows` lists workflows with an upload form (name, backend picked from the registry, `.json` file of at most 2 MB).
  - `POST /workflows` is a plain multipart form. Its body is limited to 3 MB before parsing. It validates and stores the workflow (names are unique), then redirects with a 303 to `/workflows`, or re-renders with the message and a 200.
  - `POST /workflows/{id}/run` is a plain form that takes a seed mode (random, fixed, or keep the graph's own seeds) and a count from 1 to 8. It creates a batch of N jobs through `jobs.create_workflow_batch` (disk guard included) and redirects with a 303 to `/queue?batch=<id>`.
  - `GET /workflows/{id}/download` returns the stored graph, and `POST /workflows/{id}/delete` (`hx-post` with `hx-confirm`) deletes it.
  - `GET /jobs/{id}/graph.json` returns the exact graph a job sent.
- **Image page:** for a workflow result it shows "Workflow: <name> on <backend>" with the graph download. For a generate result it shows the star, the tag editor and "Save these settings as preset".

Non-functional:
- An upload is parsed as JSON only and never evaluated. The request body is limited before parsing, and the file size is checked again after.
- A filtered gallery page answers in under 50 ms on 10,000 images, which the FTS index and the `images_model` index provide.

## Architecture

<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

```
generate form ─"save as preset" (hx-post)─► presets(name, model_id, params_json)   /generate?preset=id ─► prefill
image page ─► star toggle / tag editor (hx-post) ─► images.starred, tags + image_tags ─► triggers update images_fts.tags
gallery ?q=&tag=&starred=&model= ─► images_fts MATCH fts_query(q) (filter only) ─► join images ─► newest first, batch groups
workflows: POST /workflows (body ≤ 3 MB before parsing) ─► validate_api_graph ─► workflows(name, backend_id, graph_json)
           run(N, seed mode) ─► with_seed(graph, seed_i) per job ─► jobs(kind=workflow, model_id NULL,
           workflow_id, graph_json) ─► existing dispatcher/poller ─► run_workflow on the chosen backend
           invalid graph ─► ComfyUI /prompt rejects ─► RuntimeError text ─► job failed, text shown
```

Sketches:

```python
SEED_INPUTS = {"KSampler": "seed", "KSamplerAdvanced": "noise_seed", "RandomNoise": "noise_seed"}
OUTPUT_NODES = {"SaveImage", "PreviewImage"}

def validate_api_graph(raw: bytes) -> dict:
    if len(raw) > 2_000_000:
        raise WorkflowError("The file is larger than 2 MB.")
    try:
        graph = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Not valid JSON: {exc}") from None
    if isinstance(graph, dict) and "nodes" in graph and "links" in graph:
        raise WorkflowError("This is a UI-format workflow. In ComfyUI, export it in API format "
                            "(Workflow → Export (API)) and upload that file.")
    if not isinstance(graph, dict) or not graph or not all(
            isinstance(n, dict) and isinstance(n.get("class_type"), str) and isinstance(n.get("inputs"), dict)
            for n in graph.values()):
        raise WorkflowError("Expected an API-format graph: an object of nodes, each with 'class_type' and 'inputs'.")
    if not any(n["class_type"] in OUTPUT_NODES for n in graph.values()):
        raise WorkflowError("The graph has no SaveImage or PreviewImage node, so it would produce no image.")
    return graph

def seed_targets(graph: dict) -> list[tuple[str, str]]:
    return [(nid, SEED_INPUTS[n["class_type"]]) for nid, n in graph.items()
            if n["class_type"] in SEED_INPUTS and isinstance(n["inputs"].get(SEED_INPUTS[n["class_type"]]), int)]

def with_seed(graph: dict, seed: int) -> dict:
    out = copy.deepcopy(graph)
    for nid, key in seed_targets(out):
        out[nid]["inputs"][key] = seed
    return out

def fts_query(text: str) -> str | None:
    terms = text.split()
    return " ".join('"' + t.replace('"', '""') + '"*' for t in terms) or None
```

The "Workflow → Export (API)" menu label in ComfyUI's current frontend is [UNVERIFIED]. The hint text may be adjusted to whatever label the owner's ComfyUI shows.

## Related Code Files

<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->

Create:
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/custom_workflows.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/routes/library.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/routes/workflows.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/templates/library.html`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/templates/workflows.html`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/templates/partials/star_button.html`
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/templates/partials/tag_editor.html`
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_library.py`
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_custom_workflows.py`

Modify:
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/library.py`: presets CRUD, `toggle_star`, `set_tags`, `search` and the combined gallery filters.
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/jobs.py`: `create_workflow_batch(conn, registry, workflow, seed_mode, seed, count, rng)`.
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/routes/generate.py`: `?preset=` prefill.
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/routes/pages.py`: the gallery's `q`, `tag` and `starred` filters and the "Custom workflows" model option.
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/routes/jobs.py`: `GET /jobs/{id}/graph.json`.
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/main.py`: include the two routers, and register the 3 MB body limit for `POST /workflows` in the body-size middleware.
- `/Users/sweet-home/Works/qwen21-uc-modal/atelier/templates/generate.html`, `gallery.html`, `image.html` and `base.html` (nav links to Library and Workflows).
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/conftest.py`: add `preset_id` and `workflow_id` to the `route_ids` fixture, so the route-enumerating crawl can visit the new GET routes.

Delete: none.

## Implementation Steps

<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

1. **Presets.**
   - Add the presets functions to `library.py`: `save_preset` (upsert by name), `list_presets`, `get_preset` and `delete_preset`.
   - Wire `/library`, `POST /presets` (`hx-post` from the generate form, with a name field) and `POST /presets/{id}/delete`. Prefill on `?preset=`.
2. **Stars and tags.**
   - Add `toggle_star` and `set_tags`. `set_tags` normalizes, upserts `tags`, and replaces `image_tags` in one transaction, so the triggers refresh the FTS row.
   - Add the two partials and their routes. Errors render inline, with a 200.
3. **Search.**
   - Add `fts_query` and `search(conn, q, tag, starred, model, limit, offset)`: `images_fts MATCH` as a filter, joined to `images`, ordered by `images.id DESC`.
   - Extend the gallery route and template (search box, tag chip filter, starred toggle), keeping the batch-grouped layout.
4. **Workflows.**
   - Write `custom_workflows.py`: `validate_api_graph`, `seed_targets`, `with_seed`, `store_workflow` (unique name, backend must exist in the registry) and `delete_workflow`.
   - Write `jobs.create_workflow_batch`:
     - disk guard first;
     - count from 1 to 8;
     - reject when `count > 1` and `seed_targets` is empty;
     - seeds are distinct random, or fixed with increments, or "keep", which is allowed only when `count == 1`;
     - `params_json` = `{"workflow": name, "seed": s}`.
5. **Routes and templates** for workflows: upload with `python-multipart`, run, download and delete, with the response rules from Requirements. Register the 3 MB limit for `POST /workflows`. Add `/jobs/{id}/graph.json`, and update the image page to show the workflow name and link.
6. **Tests.** Write them as listed in the Todo list, then run `uv run ruff check` and `uv run pytest -q`. The route-enumerating crawl from phase 3 must still pass with the new GET routes.
7. **Commit and push.** Commit as `feat: presets, stars, tags, prompt search and custom ComfyUI workflows`. **[OWNER-GATED]** The push to `main` deploys.
8. **[OWNER-GATED] Live check on production,** spending about $0.10 of GPU. Ask the owner before starting, and run it while the backend is warm to avoid a second cold start.
   - Download the graph JSON of an existing image (`/jobs/{id}/graph.json`), upload it as the workflow `qwen-portrait` for `qwen21-uc`, and run it with count 2 and random seeds. Both images appear, with distinct seeds and the workflow link.
   - Edit one `class_type` to `NoSuchNode`, upload it as `broken-graph`, and run it. The job fails and shows ComfyUI's validation text, starting with `ComfyUI rejected workflow:`.
   - Delete `broken-graph`.

## Todo List

- [x] Presets save, load, overwrite and delete (`POST /presets`, `POST /presets/{id}/delete`), with the generate-form integration. Verified: `test_preset_round_trip_save_load_overwrite_delete`, `test_save_preset_upsert_bumps_updated_at_not_created_at`, `test_save_preset_rejects_an_empty_name`, `test_delete_unknown_preset_raises`, `test_save_preset_route_requires_a_model` pass.
- [x] Star toggle and tag editor with normalization; FTS triggers keep tags searchable. Verified: `test_star_toggle_and_tag_set_are_searchable`, `test_star_toggle_is_reversible`, `test_star_on_unknown_image_shows_inline_message_not_a_500`, `test_invalid_tags_are_rejected_inline_with_200` (4 cases), `test_tags_are_normalized_lowercase_trimmed_and_deduplicated` pass.
- [x] Prompt search as a quoted, prefix-matched filter with combined filters; newest first in the batch-grouped gallery. Verified: `test_search_matches_prompt_words_and_prefixes`, `test_search_results_are_newest_first`, `test_search_treats_operators_and_quotes_as_text` (8 adversarial inputs, including quotes/parens/NEAR/AND/OR/unicode), `test_deleted_image_leaves_the_search_index`, `test_gallery_combines_q_tag_starred_and_model_filters` pass.
- [x] Workflow upload: body limited before parsing, UI-format hint, structure, output node, size, unique name. Verified: `test_ui_format_upload_is_rejected_with_export_hint`, `test_graph_without_output_node_is_rejected`, `test_upload_over_the_body_limit_is_refused_before_parsing` (413, no row created, garbage bytes prove it never reaches the parser), `test_upload_file_over_2mb_is_rejected_with_the_body_under_3mb` (the file-size cap, distinct from the request-body cap), `test_upload_duplicate_name_is_rejected`, plus the `validate_api_graph`/`store_workflow` unit tests in `test_custom_workflows.py` pass.
- [x] Seed override on KSampler-family nodes; N > 1 without a seed input is rejected. Verified: `test_run_creates_n_jobs_with_distinct_overridden_seeds`, `test_linked_seed_inputs_are_left_untouched`, `test_run_rejects_count_over_one_when_the_graph_has_no_seed_input`, `test_run_allows_count_one_when_the_graph_has_no_seed_input`, `test_keep_seed_mode_requires_count_one`, `test_keep_seed_mode_sends_the_graph_unmodified` pass.
- [x] Workflow runs store the exact graph and the `workflow_id`; image page shows the workflow and graph download. Verified: `test_result_keeps_workflow_link_and_exact_graph`, `test_delete_workflow_clears_the_jobs_link_but_keeps_the_graph`, `test_download_returns_the_exact_stored_graph` pass.
- [x] `tests/test_library.py` and `tests/test_custom_workflows.py` green; the crawl still visits every GET route. 31 + 50 tests pass (including escaping proofs for preset/workflow names, the tag editor's rejected-value echo and ComfyUI error text); `route_ids` gained `preset_id` and `workflow_id`; `test_pages_hide_secrets.py` and `test_auth.py::test_service_identity_is_refused_outside_the_api_allowlist` pass unmodified with the four new GET routes (`/library`, `/workflows`, `/workflows/{id}/download`, `/jobs/{id}/graph.json`) swept in. Full suite: 488 passed, 1 deselected (`uv run pytest -q`); `uv run ruff check` clean.
- [x] Also fixed (logged for this phase): the gallery's "Next" link no longer appears past the last page. `gallery_page` now fetches one row past the page size to compute a real `has_next`, under the same filters. Verified: `test_gallery_last_page_shows_no_next_link`, `test_gallery_exactly_one_full_page_shows_no_next_link`, `test_gallery_full_page_with_more_rows_shows_next_link`, `test_gallery_pagination_links_preserve_the_active_filters` pass (added to `tests/test_ui_gallery.py`, the existing gallery test file).
- [x] Independent-review fix round: the one Medium (search matched the negative prompt) and all ten Lows fixed in place. Search is now restricted to `{prompt tags}:`; a NUL in the search box is stripped instead of reaching FTS5; the tag filter normalizes case and whitespace before matching; upload rejects deeply nested JSON, a huge integer literal and a graph nested past a 64-level bound (at upload, and again defensively before `with_seed` for a graph already stored); a "keep"-mode seed at or above 2**63 is refused before the batch is created, not after a paid render; workflow names are capped at 100 characters; non-ASCII graphs are stored without `ensure_ascii` doubling their size; `/workflows` lists id/name/backend/date only, without parsing every stored graph; a workflow batch's gallery header now names the workflow instead of showing an empty link and "None"; and `/gallery?page=` is bounded so the offset can no longer overflow (a non-integer path id, and other hand-typed URLs, keep FastAPI's existing 422, per the owner's decision). 29 new tests added (`tests/test_library.py`: 39 total; `tests/test_custom_workflows.py`: 69 total; `tests/test_ui_gallery.py`: +2). Full suite: 517 passed, 1 deselected; `uv run ruff check` clean. A hand-rolled mutation re-run (20 targeted mutants: the 8 previously-surviving ones this round could fix, plus 12 new ones for M1/L1-L10) caught all 20; a 12-mutant spot-check of previously-killed mutants in the same files confirmed no regression. See the fullstack-developer report's "Review fixes" section for the full list and the mutation-run detail.
- [x] [OWNER-GATED] Live run of a stored workflow and of an invalid graph

### Live verification (2026-09-27, owner-approved, about $0.10 of Modal credits)

The feature deployed as `2b52414`: the deploy was green, `/healthz` was ok, and the app log showed no errors.

- **Download:** `/jobs/1/graph.json` returned 200 as `application/json`, an 8-node Qwen graph (UNETLoader, CLIPLoader, VAELoader, TextEncodeQwenImage21, EmptyLatentImage, KSampler, VAEDecode, SaveImage).
- **Upload and run:** the graph was uploaded as `qwen-portrait` for `qwen21-uc`, then run with count 2 and random seeds (batch 5).
  - Both jobs finished (66 s and 53 s, one shared cold start) with distinct seeds, 820447650 and 1127088148.
  - Both image pages link the workflow and offer their own graph download. Job 8's stored graph carries its seed (820447650) in the KSampler.
- **Invalid graph:** `broken-graph` (node 7's `VAEDecode` changed to `NoSuchNode`) uploaded fine, since node types are ComfyUI's to judge. Its run (batch 6) failed within about 20 s, showing ComfyUI's escaped text: `ComfyUI rejected workflow: {"error": {"type": "missing_node_type", "message": "Node 'NoSuchNode' not found. …", "details": "Node ID '#7'" …}}`.
- **Delete:** `broken-graph` was deleted (200). Only `qwen-portrait` remains, and the failed job still shows its reason.
- **Gap found and fixed:** the queue's Model column and the batch page printed "None" for workflow jobs, the sibling of the gallery-header fix. Both now say "custom workflow". The new test fails without the fix, and the suite now has 522 tests.

## Success Criteria

<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

- **Criterion 8:**
  - `test_preset_round_trip_save_load_overwrite_delete` passes.
  - `test_star_toggle_and_tag_set_are_searchable` passes: a tag is findable through `tag=` and through `q=`.
  - `test_search_matches_prompt_words_and_prefixes` and `test_search_results_are_newest_first` pass.
  - `test_search_treats_operators_and_quotes_as_text` passes: an input such as `a" OR -b NEAR(` returns results or none, never an error.
  - `test_deleted_image_leaves_the_search_index` passes.
  - Live, the owner saves, loads and deletes a preset, and stars, tags and finds an image by prompt text.
- **Criterion 9:**
  - `test_api_graph_is_stored_for_the_chosen_backend` passes.
  - `test_ui_format_upload_is_rejected_with_export_hint` passes, with the message re-rendered and a 200.
  - `test_upload_over_the_body_limit_is_refused_before_parsing` passes.
  - `test_graph_without_output_node_is_rejected` passes.
  - `test_run_creates_n_jobs_with_distinct_overridden_seeds` passes.
  - `test_linked_seed_inputs_are_left_untouched` passes.
  - `test_result_keeps_workflow_link_and_exact_graph` passes.
  - `test_comfyui_rejection_text_is_shown_on_the_failed_job` passes: the fake gateway returns the real `_run` message format, and the queue row shows it.
  - `test_service_identity_cannot_upload_or_delete_workflows` passes.
  - The live check reproduces both the success and the validation-error path.
- **Criterion 4** (supporting): the invalid-graph job shows ComfyUI's error text in the queue, and Retry creates a new queued job with the same graph.

## Verification

```bash
cd /Users/sweet-home/Works/qwen21-uc-modal
uv run ruff check
uv run pytest -q tests/test_library.py tests/test_custom_workflows.py tests/test_pages_hide_secrets.py -v
uv run pytest -q
# [OWNER-GATED] live check from Implementation Step 8, in the browser behind Access
```

## Risk Assessment

| Risk | Likelihood × impact | Observable signal | Pre-decided response |
|---|---|---|---|
| An exported graph uses a seed node outside `SEED_INPUTS`, for example a custom sampler | Medium × Low | N > 1 is rejected with "no seed input" for a graph the owner knows is seeded | Add that class and input name to `SEED_INPUTS` with a test. It is data, not design. |
| A graph needs custom nodes that are not installed in the Modal image | Medium × Low | The job fails with ComfyUI's "node type not found" text | Expected, and it is shown to the user. Installing nodes would change the Modal script, which is a non-goal. |
| A graph produces several images, and only the first is saved | Medium × Low | The owner expects more outputs | This is documented backend behavior (`:135-138`). Changing it would be a Modal-script change, which is a non-goal. |
| A legitimate workflow export is larger than 2 MB | Low × Low | Upload refused with the size message | Raise both limits together (file check and body limit) with a test, after telling the owner. |

**Rollback:** `git revert`, then **[OWNER-GATED]** push to redeploy. The tables stay, and there is no migration to undo.

## Security Considerations

<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->

- Upload bodies are limited before parsing (Content-Length and a streamed count), the file is size-checked again, parsed as JSON and never executed by Atelier. Names and error text are rendered escaped.
- Presets, stars, tags and workflow routes are owner-only HTML; the plugin's service identity is refused on all of them.
- A graph runs only inside the backend's own Modal container, which holds no Atelier secrets.
- Tag and preset names are validated, and file names for downloads come from IDs, not from user text.

## Next Steps

Phase 8 exposes these capabilities (`list_images`, `list_workflows`, `run_workflow` by ID) to Claude through `/api/v1` and the plugin, then runs the full acceptance pass.
