# Code review: prompt library, stars/tags/search and custom ComfyUI workflows (uncommitted phase-7 work)

Date: 2026-09-27 (Europe/Paris). Reviewer: code-reviewer. This review was read-only. No repo file was edited, and nothing called Modal for real.

## Code Review Summary

### Scope
- **Modified (+634/−58):**
  - `atelier/{library,jobs,main}.py`;
  - `atelier/routes/{generate,jobs,pages}.py`;
  - `templates/{base,gallery,generate,image}.html`;
  - `static/app.css`, `tests/conftest.py`, `tests/test_ui_gallery.py`;
  - the deployment guide and the plan.
- **New (1,418 lines):**
  - `atelier/custom_workflows.py`;
  - `atelier/routes/{library,workflows}.py`;
  - `templates/{library,workflows}.html` and `partials/{star_button,tag_editor}.html`;
  - `tests/test_library.py` (31 tests) and `tests/test_custom_workflows.py` (50 tests).
- **Checks:** `uv run ruff check` is clean; `uv run pytest -q` gives 488 passed, 1 deselected. `auth.py` and `worker.py` are unchanged.
- **Evidence gathered:**
  - 38 probe tests against the real schema and routes with the fake gateway.
  - Raw-socket checks against a real in-process uvicorn (httptools).
  - A headless-Chrome harness driving the **real app** (real routes and templates, Access JWT injected by a scratch ASGI wrapper).
  - A 10,000-image timing run.
  - A registry check of the production base image.
  - 40 source and template mutants, each run through the full suite.
- **Owner input applied:** stars and tags apply to any image, including workflow results. The implementation already does this, since `image.html` renders both controls for every kind.

### Overall Assessment
The security-critical rules hold under adversarial probing:
- FTS5 quoting;
- escaping of every user string;
- JSON downloads never rendered as HTML;
- the 3 MB pre-parse body limit, including chunked and lying-Content-Length requests;
- owner-only access, with CSRF on every POST.

The workflow engine integration holds too: exact graph and `workflow_id` per job, a seed override limited to literal KSampler-family seeds, and correct history after a workflow is deleted. HTMX wiring and performance are sound.

The defects are one **Medium** search-correctness bug (the negative prompt is searched) and a cluster of **Low** robustness gaps:
- crafted or extreme inputs that produce 500s on routes that must answer 200;
- a lost paid render in one edge case;
- missing caps.

No Critical or High findings. Fine to commit after the Medium fix. The Lows can follow.

---

## Critical Issues
None.

## High Priority
None.

## Medium Priority

### M1. Search also matches the **negative** prompt, returning images that explicitly exclude the searched term
- **Where:** `atelier/library.py:109-115` and `:144-146`. `images_fts` indexes `prompt, negative, tags` (migration 0001), and `fts_query` builds an unrestricted `"term"*` expression, so the search covers all three columns.
- **Failure scenario:**
  - A search for `cat` returns an image whose prompt is "a dog running" and whose negative is "cat, blurry, watermark". That is an image made specifically without a cat.
  - Negatives usually carry generic quality words (blurry, lowres, text, watermark, bad hands). Searching "hands", "text" or "blurry" would therefore return a large share of the gallery.
- **Spec:** the spec's own wording, and the search box placeholder, is "search by prompt text or tag".
- **Evidence (probe F2):** `q='cat'` matched both the prompt match (image 1) **and** the negative-only image (image 2).
- **Fix (verified on the same SQLite):** restrict the MATCH to the searched columns, `{prompt tags}: ("t1"* "t2"*)`. `'"cat"*'` returned `[1, 2, 3]`; `'{prompt tags}: ("cat"*)'` returned `[1, 3]`: prompt and tag matches kept, negative-only dropped. Add a test for it; none exists today.

## Low Priority
1. **L1. Upload JSON that is too deeply nested, or has a huge integer, returns 500 instead of the inline message.**
   - **Where:** `custom_workflows.py:64-67` catches only `UnicodeDecodeError` and `JSONDecodeError`.
   - **Evidence:**
     - A 200 KB file of `[`, or 100k nested arrays inside `inputs`, raises `RecursionError`, and `POST /workflows` returns **500** (probe U2).
     - A 5,000-digit integer literal raises a plain `ValueError` ("Exceeds the limit (4300 digits)"), which is uncaught.
   - **Impact:** owner-only, with no persistence impact. It violates the "re-render with the message at 200" rule.
   - **Fix:** `except (ValueError, RecursionError)` (`JSONDecodeError` is a `ValueError`), plus an explicit depth check (next item).
2. **L2. A graph that validates can still make Run return 500.**
   - **Where:** `custom_workflows.py:104`.
   - **Mechanism:** `json.loads` accepts nesting that `copy.deepcopy` in `with_seed` cannot handle.
   - **Evidence (probe U3):** a depth-700 graph uploads (303) and downloads (200), then `POST /workflows/{id}/run` returns **500**.
   - **Fix:** reject graphs nested deeper than a small bound (say 64) in `validate_api_graph`. Real API exports are shallow.
3. **L3. A "keep"-mode seed of 2^63 or more loses a paid render.**
   - **Where:** `jobs.py:348-351` and `:393`. The graph's own seed is recorded unvalidated in `params_json`.
   - **Mechanism:** at completion, `jobs.complete` inserts it into `images.seed` (`jobs.py:267`), which fails for ≥ 2^63. ComfyUI allows seeds up to 2^64−1.
   - **Evidence (probe U7):** with seed 2^64−1, the render finished on Modal, then the job **failed** with "Could not store the result: Python int too large to convert to SQLite INTEGER", and 0 images were stored after the retries.
   - **Reach:** ComfyUI's own randomize control stays below 2^50, so this needs a hand-typed seed.
   - **Fix:** record `None` (or refuse) when the kept seed is outside `0..2^63−1`. Fixed and random modes are already range-checked.
4. **L4. A NUL in `q` returns 500 on `/gallery`.**
   - **Where:** `library.py:115`. FTS5 reads the MATCH text as a C string, so an embedded NUL truncates it into an unterminated phrase.
   - **Evidence (probe F1):** 42 adversarial inputs (listed under What held) all worked **except** `a\x00b` and `\x00`, which raised `OperationalError: unterminated string`; `GET /gallery?q=a%00b` returned **500**.
   - **Fix:** in `fts_query`, drop control characters (at least `\x00`) before quoting.
5. **L5. Workflow names are not length-capped.**
   - **Where:** `custom_workflows.py:127-131`.
   - **Evidence (probe U4):** a 500,000-character name was accepted (303) and stored.
   - **Impact:** the name is repeated in `batches.base_params_json` and in every job's `params_json`. The implementer's report says names are capped at 200 characters, but only presets have that cap.
   - **Fix:** apply the same 200-character cap to workflow names.
6. **L6. The 2 MB graph cap is not a cap on what is stored or sent.**
   - **Where:** `custom_workflows.py:135` and `jobs.py:413`. `json.dumps` defaults to `ensure_ascii=True`.
   - **Evidence (probe U5):** a 1.92 MB non-ASCII upload was stored as **3.84 MB**, and it is copied into every job (up to 8 per batch) and sent to Modal each time.
   - **Fix:** `json.dumps(..., ensure_ascii=False)`, or store the validated raw bytes.
7. **L7. `/workflows` parses every stored graph on each render.**
   - **Where:** `custom_workflows.py:144-146`, called from `routes/workflows.py:47/66/115`.
   - **Mechanism:** the list only needs id, name, backend and date, but `_from_row` runs `json.loads` on each whole `graph_json`, on the event loop.
   - **Evidence (probe U6):** 10 stored graphs of 1.97 MB each made `GET /workflows` take **94 ms**, all loop-blocking, and it scales linearly. Real exports are small, so the impact is minor.
   - **Fix:** `SELECT id, name, backend_id, created_at` for the listing.
8. **L8. The tag filter is exact and case-sensitive while tags are stored lower-case.**
   - **Where:** `library.py:147-149` and `routes/pages.py:52`.
   - **Evidence (probe F3b):** for an image tagged `beach`, `tag=beach` matched, `tag=Beach` did not, and `tag=' beach '` did not. The filter is a free-text box.
   - **Fix:** normalize with `.strip().lower()` before querying.
9. **L9. Gallery headers for custom-workflow batches are blank.**
   - **Where:** `gallery.html:33-34`.
   - **Evidence (probe F5):** the header renders as `<a href="/batches/1"><strong></strong></a> · None · 1 image`. The prompt is empty, so the batch link is invisible, and `model_id` is NULL, so it prints "None".
   - **Fix:** fall back to "workflow: {name}" and "custom workflow".
10. **L10. Crafted URLs still produce non-200 responses (partly pre-existing).**
    - `/gallery?page=9223372036854775807` returns **500** because the offset overflows SQLite. The old `list_batches` had the same arithmetic.
    - `/gallery?starred=abc` returns 422.
    - Non-integer ids (`/workflows/abc/run`, `/images/abc/star`, `/workflows/abc/delete`) return FastAPI's 422.
    - Only reachable by hand-edited URLs. Clamp `page`, and treat these as "not found" messages if the "always 200" rule is meant literally.

## Edge Cases Found by Scout
- **Search:** the negative prompt is searched (M1); a NUL byte breaks FTS5 (L4); the tag filter is case-sensitive (L8).
- **Upload:** nesting depth and huge integers (L1); a validates-but-can't-run graph (L2); an unbounded name (L5); `ensure_ascii` inflation (L6).
- **Run:** a kept seed ≥ 2^63 fails only at result-store time, after the GPU was paid for (L3).
- **Display:** workflow batches have blank gallery headers (L9).
- **Not a regression:** a "Load" of a preset whose model has since been removed falls back to the default model and still pre-fills the prompt (probe R1).

## Positive Observations (what held, with evidence)
- **FTS5 construction.** 42 adversarial `q` values raised no FTS5 error, other than the NUL case in L4. Each ran in under 50 ms, and each also went through `GET /gallery`. The inputs:
  - quotes (`"`, `""`, `"""`), `*`, `**`, `(`, `)`, `-`, `+`, `^`;
  - `NEAR`, `NEAR(a b, 2)`, `AND` / `OR` / `NOT` in context;
  - column filters `prompt:cat`, `{prompt}:cat`, `{prompt negative}:`;
  - unicode, CJK, emoji, zero-width and line-separator characters, `\t`, `\`, `'`, `;`;
  - a 5,000-char term, 300 terms and 3,000 terms.
- **Escaping.** A `"><img src=x onerror=alert(1)>` payload was placed in the prompt, negative, preset name and values, workflow name, tag echo, ComfyUI failure text, and the `q`, `tag` and `model` echoes. No raw payload appeared on any of 17 pages or POST responses:
  - gallery, image, both batch pages, queue and queue rows;
  - library, both generate prefills, workflows;
  - the tag, preset-save, run-error and upload-error echoes.

  `hx-confirm` attributes are attribute-escaped too.
- **Downloads.** `/workflows/{id}/download` is `application/json`, `attachment; filename="workflow-<id>.json"` (id-based), plus `nosniff`. `/jobs/{id}/graph.json` is `application/json` plus `nosniff`, so it never renders as HTML. It is served inline, but the page link carries `download`. The stored bytes are returned exactly.
- **Body limit (real uvicorn, raw sockets).**

  | Request | Result |
  |---|---|
  | declared Content-Length 4 MB | **413 before multipart parsing started** |
  | chunked, no Content-Length, 4 MB | **413**; parsing starts, but is cut off at 3 MB |
  | lying Content-Length (small), 4 MB actually sent | **400** from the HTTP layer; the body read is bounded by the declared length |
  | small valid upload | 303 |

  Other POST routes keep the 64 KB default.
- **JSON handling.** Only `json.loads` is used, and the 2 MB file check runs on the parsed file part. The UI-format hint, the structure check, the output-node rule (with `PreviewImage` accepted) and unique names are all tested and caught by mutants. Invalid UTF-8 is rejected cleanly.
- **Authorization.** All 30 method+route pairs were swept, including the 11 new ones. For every one, the service identity, a request with no token, an owner POST without Origin and an owner POST with a foreign Origin all got **403**. `SERVICE_ROUTES` is unchanged, and the crawl visits `/library`, `/workflows`, `/workflows/{id}/download` and `/jobs/{id}/graph.json`.
- **Workflow runs.**
  - **Seed override.** It changes only literal-int `KSampler.seed` and `KSamplerAdvanced.noise_seed`. Linked `RandomNoise`, custom-node seeds and all other inputs are untouched, and the stored workflow graph is never mutated (probe W1).
  - **Per-job records.** Each job stores its exact graph, its seed and its `workflow_id`, with `model_id` NULL and kind `workflow`.
  - **Rejected inputs.** Out-of-range fixed seeds, counts outside 1–8, "keep" with N>1, a bogus mode and N>1 with no seed input are all refused with a 200 and a message (probe R1).
- **History after deleting a workflow (probe W2).**
  - The image page shows "deleted workflow".
  - `graph.json` still returns the exact graph.
  - The queue shows the ComfyUI rejection text **escaped**.
  - The batch page answers 200.
  - Retry creates a job with the exact same graph and `workflow_id` NULL.
  - `worker.py` is unmodified, so dispatch, poll and cancel semantics are unchanged.
- **Library.**
  - Tags stay searchable through both `q` and `tag` across add, replace and clear, and the FTS row follows each edit (F3).
  - The star toggle round-trips.
  - Preset save, overwrite, delete and prefill work.
  - Combined `q`+`tag`+`starred`+`model` filters return results newest first, unique, and batch-grouped even when interleaved.
  - `has_next` is correct at 48/49/96/97 matching rows with all filters active (F4).
- **HTMX (real app in headless Chrome).**
  - **No load loops:** idle `/library`, `/workflows`, image page and filtered gallery make only the header's load plus one 10 s poll.
  - **The `hx-select` trick works:** a Delete on `/library` or `/workflows` swaps in only `#library-content` / `#workflow-list`. There are no duplicate ids, and exactly one `h1`, `nav` and `#header-status` remain.
  - Star and tag swaps leave one control each, and a rejected tag re-renders the form with the error.
  - "Save as preset" writes an escaped confirmation into `#preset-status` and leaves the generate form and its values intact.
- **Performance.** On 10,000 images with 3,000 tagged, every gallery variant takes **≤ 5 ms** locally: plain, deep page 150, model, workflows, starred, tag, common `q`, one-letter prefix `q`, all filters combined, and deep `q` page 60.
  - **Query plans:** the model filter uses `images_model`; the plain listing walks rowid order with no sort; `q` scans the FTS index, joins by rowid and sorts in a temp B-tree; the tag filter probes the `image_tags` primary key.
  - There is no N+1 anywhere: one query per gallery page, two per image page.
- **Production SQLite supports `RETURNING`** and FTS5: the pinned base image is Python 3.12.14 on Debian trixie (SQLite 3.46).

## Test quality: mutation run (40 mutants, full suite each)
**31 killed, 9 survived.**
- **Killed:**
  - FTS quote-doubling, unquoting and prefix;
  - ascending order;
  - `has_next` off-by-one and the look-ahead row;
  - the starred and workflow filters, and the gallery ignoring `q`;
  - tag regex, lower-casing and the 20-tag cap;
  - the star toggle;
  - preset upsert and the `updated_at` bump;
  - the UI hint, output-node check and 2 MB cap;
  - the linked-seed and in-place-mutation rules;
  - `KSamplerAdvanced` being dropped;
  - count bounds, keep×N, the no-seed rule and the disk guard;
  - `workflow_id` being dropped;
  - the 3 MB body limit;
  - the download `Content-Disposition`;
  - preset, workflow-list and tag-echo escaping.
- **Survived:**
  - `fts_or_semantics`: multi-term AND semantics are unpinned.
  - `set_tags_appends`: "replace the tag set" is unpinned. Behaviour is correct per F3.
  - `preset_name_cap_off`: the 200-character cap.
  - `graph_json_as_html`: `/jobs/{id}/graph.json`'s `application/json` type is unpinned. If it regressed to `text/html`, a graph string holding `<script>` would become stored XSS. **Worth a test.**
  - `tpl_q_echo_unescaped` and `tpl_workflow_name_on_image_unescaped`: escaping on the gallery `q` echo and the image-page workflow name is unpinned. Autoescape covers both today.
  - `tpl_library_no_hx_select` / `tpl_workflow_list_no_hx_select`: client-side; covered only by the browser harness.
  - `wf_keep_reseeds`: near-equivalent for single-seed graphs.

## Recommended Actions
1. **M1:** restrict the search to the prompt and tag columns, with a test.
2. **L1–L4:** catch `ValueError`/`RecursionError` plus a depth bound on upload; range-check the kept seed; strip NUL in `fts_query`.
3. **L5–L9:** name cap, `ensure_ascii=False`, listing without graphs, tag-filter normalization, and workflow-batch gallery headers.
4. **Tests:** pin the `graph.json` media type, multi-term AND, tag-set replacement, and escaping of the `q` echo and the image-page workflow name.

### Plan follow-ups (no plan edits made)
- **Todo items that hold:** presets, star/tags, upload, seed override, results-and-link, and tests green.
- **Correction:** the search item is true only with the M1 caveat, since it currently searches negatives too.
- **Still open:** the owner-gated live check.
- **Spec note:** the owner's answer to open question 1 matches the implementation, so no change is needed.

### Metrics
- **Type coverage:** not measured (no type checker configured).
- **Test coverage:** not measured. Mutation score on the targeted rules: 31/40 (78%).
- **Linting:** 0 issues (`ruff check`).
- **Suite:** 488 passed, 1 deselected.

### Unresolved Questions
1. **M1:** is searching the negative prompt ever intended? If so, it should be a separate, explicit filter, not the default.
2. **L10:** should the "HTML routes always return 200" rule cover crafted URLs (non-integer ids, `starred=abc`, huge `page`)? Or is FastAPI's 422 acceptable there, as on the pre-existing routes?

Status: DONE_WITH_CONCERNS
Summary: Security-critical rules hold under adversarial probes: FTS5 quoting, escaping, the pre-parse upload limit, owner-only access with CSRF, JSON downloads, and the exact graph per job. One Medium bug needs fixing: gallery search matches the negative prompt, returning images made to exclude the term. Ten Lows cover 500s on crafted inputs, a lost render for a seed ≥ 2^63, and missing caps; the suite catches 31 of 40 mutants.

---

## Re-review (2026-09-27, after the implementer's "Review fixes" round)

**Method.** Read-only, with the same probes re-pointed at the fixed code (8 new probe tests plus route-level checks), and 27 mutants (the 9 earlier survivors plus 18 aimed at the fix code), each run through the full suite. Checks: `uv run ruff check` is clean; `uv run pytest -q` gives 517 passed, 1 deselected.

**Owner decisions applied:** search covers prompts and tags only; a malformed hand-typed URL may return 422 but never 500.

### Verdicts

| Finding | Verdict | Evidence |
|---|---|---|
| **M1** negative prompt searched | **Fixed** | `q=cat` returns the prompt match and the tag match; the negative-only image is excluded, and `q=blurry` (a negative-only word) returns nothing. The 42 hostile queries, plus 4 aimed at the new `{prompt tags}: (...)` wrapper (`) OR negative:cat`, `}: (x) OR {negative`, `"*) OR (negative:"cat`, `{negative}: cat`), gave **0 errors, 0 negative-column leaks, and none over 50 ms**, both directly and through `GET /gallery`. |
| **L1** deep nesting / huge integer → 500 | **Fixed** | 200 KB of `[`, 100k nested arrays and a 5,000-digit integer each re-render at **200** with "Not valid JSON: …". |
| **L2** deep graph → Run 500 | **Fixed** | A depth-700 upload gets 200 "nested more than 64 levels deep". A pre-existing deep row, inserted directly into the database, gets a 200 message at Run instead of a 500. |
| **L3** kept seed ≥ 2^63 loses a render | **Fixed** | "keep" with seeds 2^63, 2^64−1 and −1 is refused at 200 before any batch exists. Seeds 2^50−1 (the top of ComfyUI's randomize range) and 2^63−1 are accepted, and 2^63−1 completes end to end with the seed stored. |
| **L4** NUL in `q` → 500 | **Fixed** | `q=a%00b` and `q=%00` both return 200. |
| **L5** workflow name uncapped | **Fixed** | Names of 101 and 500,000 characters get a 200 with a message; exactly 100 is accepted (303). |
| **L6** stored size doubled | **Partly fixed** (R1) | The stored workflow is now 900,134 B for 900,134 B of UTF-8 input. |
| **L7** `/workflows` parses every graph | **Fixed** | With 10 × 2 MB graphs, `GET /workflows` takes **4 ms**, down from 94 ms. |
| **L8** case-sensitive tag filter | **Fixed** | `beach`, `Beach`, ` BEACH ` and `beach ` all match. |
| **L9** blank / "None" workflow header | **Fixed** | The header reads `workflow: <name> · custom workflow`, or `workflow: deleted workflow` after the workflow is deleted. |
| **L10** huge page → 500 | **Fixed per owner decision** | Page 10^12 returns 200; 10^12+1, 2^63−1 and 10^30 return **422**. Other malformed ids and `starred=abc` were already 422. |

**Realistic graphs are not rejected.**
- ComfyUI's default txt2img API export (with `_meta` titles and seed 156680208700286) and the app's own Qwen graph each measure **depth 5**, far below the bound of 64.
- Both upload successfully and run with random ×8, keep ×1 and fixed ×3 at seed 2^50−1 (all 303).

**No regressions found in:**
- **`graph.json`:** `application/json`, now with `attachment; filename="job-<id>-graph.json"`, plus `nosniff`, and the stored bytes returned exactly.
- **Escaping:** re-swept on the new output paths (gallery-header workflow name, workflows list, image page, `q` and `tag` echoes, the long-name and bad-JSON error messages). No raw payload appears anywhere.

### New and remaining findings (all Low)
1. **R1 (Low, remaining part of L6): each job copy is still double-sized.**
   - **Where:** `atelier/jobs.py:435` still writes each job's `graph_json` with the default `json.dumps(graph)`, which escapes non-ASCII characters.
   - **Evidence:** a 900,134 B non-ASCII graph is stored as 900,134 B, but each of its job copies is **1,800,143 B**, repeated for up to 8 jobs per batch.
   - **Impact:** database size only. The Modal payload is a dict, so it is unaffected.
   - **Fix:** `ensure_ascii=False` on that call too.
2. **R2 (Low): the depth check allocates heavily on wide input.**
   - **Where:** `custom_workflows.py:58-76`. `graph_depth` pushes a tuple for every element, scalars included.
   - **Evidence:** on a 2 MB flat array of 1M zeros the walk took **307 ms with a 64 MB peak** locally, synchronously on the event loop (a 150k-key object took 44 ms / 10 MB). Owner-only, and realistic graphs take under a millisecond.
   - **Fix:** push only dict/list children (`if isinstance(v, (dict, list))`), or run validation in `asyncio.to_thread`.
3. **Test gaps from the mutation run: 24 of 27 killed.** All 9 earlier survivors except `wf_keep_reseeds` are now caught, and every fix mutant except `list_parses_graphs` is caught. Survivors:
   - **`tpl_header_workflow_name_unescaped`:** the new gallery-header workflow name is escaped today (by autoescape), but no test pins it. **Worth one assertion.**
   - **`wf_keep_reseeds`:** not fully equivalent. On a graph with **two** samplers seeded differently (for example a hires-fix pass), the mutant would overwrite the second seed in "keep" mode. The existing test uses one sampler. A two-sampler "keep" test would pin it.
   - **`list_parses_graphs`:** a regression that re-selects and parses `graph_json` but discards the result keeps the no-`graph`-attribute test green. Low value.

### Re-review recommended actions
1. **Optional before commit:** `ensure_ascii=False` in the job INSERT (R1), and one escape assertion on the gallery header.
2. **Follow-ups:** a scalar-skipping depth walk (R2), and a two-sampler "keep" test.

Status: DONE
Summary: The Medium (negative-prompt search) and all ten Lows are fixed, or resolved per the owner's 422 decision, and were confirmed by probes: 46 hostile queries with no errors or leaks, no 500s left, and realistic ComfyUI graphs accepted at depth 5 with seeds up to ComfyUI's randomize range. Only two new Lows remain (each job copy of a non-ASCII graph still stored at double size, and a memory-heavy depth walk on adversarial input), plus 3 minor mutant survivors out of 27.
