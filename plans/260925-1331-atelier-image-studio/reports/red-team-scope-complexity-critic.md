# Red team: Scope & Complexity Critic (Contract Verifier role)

Date: 2026-09-25 · Scope: `plan.md` plus `phase-01` to `phase-08` of `260925-1331-atelier-image-studio`
Checked against: the contract (`plans/reports/brainstorm-260925-1507-atelier-image-studio-hetzner.md`), the brief, both scouts, both research reports, `qwen21_uc_app.py`, `README.md`, the modal 1.5.5 SDK, and the Folio and LearnFlow repos. I also checked `claude`/`gh`/`ak` CLI help locally and fetched two public docs pages (read-only).

Requested scope is treated as fixed. The findings below are additions beyond the contract, complexity heavier than the requested feature needs, or broken consumer contracts between phases.

---

## Finding 1: Plugin install and `userConfig` steps cannot work as written, and the `.plugin` zip has no consumer
- **Severity:** High
- **Location:** Phase 8, section "Implementation Steps" (steps 7 and 9), plus "Key Insights" (client secret) and "Requirements" (`build.sh`)
- **Flaw:** The plan's handling of the client secret depends on Claude Code's `userConfig` configuration dialog. Neither install route the plan names shows that dialog, and one of them cannot install a directory at all. The zip that `build.sh` produces is a Folio/Desktop artifact. No install step consumes it; it exists only so the secret grep can run on it.
- **Failure scenario:**
  1. At owner-gated step 9 the owner runs `claude plugin install ./plugin`. It fails, because the CLI only installs from marketplaces and the plan creates no `marketplace.json`.
  2. The fallback, `claude --plugin-dir plugin/`, loads the plugin for one session and shows no configuration dialog.
  3. `cf_client_id` and `cf_client_secret` stay empty, so Access answers every call with a 302.
  4. The plugin reports "service token rejected". The risk row for that (phase-08:264) sends the owner to check the Access policy, which is the wrong fix, so acceptance row 10 fails.
  5. The quick workaround is Folio's raw environment passthrough, which the plan says it improves on (phase-08:45).
- **Evidence:**
  - What the plan says:
    - phase-08:199: "Install from `plugin/`, then enter `cf_client_id` and `cf_client_secret` in the configuration dialog."
    - phase-08:196: "plugin install through `claude plugin install` or a local directory".
    - The Create list (phase-08:149-154) has no marketplace manifest.
  - What the tools and docs say:
    - `claude plugin install --help`: "Install a plugin from available marketplaces (use plugin@marketplace …)".
    - `claude --help`: "`--plugin-dir <path>` Load a plugin from a directory or .zip for this session only".
    - code.claude.com/docs/en/plugins/create, section "The userConfig dialog never appears": the dialog "is part of installing through `/plugin` in a session. Loading with `--plugin-dir` doesn't show it, and neither does `claude plugin install` in the shell … run `/plugin configure <plugin-name>`."
  - The Folio precedent is a different route:
    - `/Users/sweet-home/Works/folio/folio-plugin/README.md:61` installs the zip "in Claude (Settings → Capabilities, or via the presented `.plugin` file)".
    - Folio's `.mcp.json:10-14` passes `${FOLIO_PASSWORD}` from the environment, with no `userConfig`.
  - The zip has no consumer: `build.sh` (phase-08:73, :184-187) builds `atelier.plugin`, and only the grep checks use it (phase-08:250-251). The brief asked for it "as Folio does" (brief:160).
  - The manifest reference requires `title` and `description` on every `userConfig` option, and an unknown or invalid key stops the plugin loading. phase-08:71 specifies neither field.
- **Suggested fix:**
  - Choose one install route and write it out end to end. Options:
    - a one-entry local marketplace (`.claude-plugin/marketplace.json`, then `claude plugin marketplace add`, then `/plugin install atelier@…` in a session);
    - a skills-directory install.
  - Then run `/plugin configure atelier` for the secret.
  - Add `title` and `description` to each option.
  - Either name the zip's consumer (Claude Desktop) or drop `build.sh`.
  - Add a risk row for "configuration dialog never shown".

## Finding 2: The plugin gets GPU warm and stop, which criterion 10 does not list, and its skill steers Claude into paying for warm windows that cannot speed up the job
- **Severity:** Medium
- **Location:** Phase 8, section "Requirements → Functional, plugin" (tools, `SKILL.md`) and the `/api/v1/gpu/*` rows of the API table
- **Flaw:**
  - **Scope.** Criterion 10 lists exactly what Claude must do, ending with "see GPU status". The brief added `gpu_warm(minutes)` and `gpu_stop(confirm)` (brief:158). The plan carries both tools plus two POST endpoints without asking the owner. The contract's "a Claude plugin like Folio's" (contract:15) could be read as "full coverage", so this is raised as a question, not a cut.
  - **Cost.** The skill's flow is "check GPU status, optionally warm, generate and wait". Warming a cold backend does not shorten a job submitted right after it: a cold ping *is* the boot, so both wait for the same ~68 s. The warm window then keeps billing after the job is done.
- **Failure scenario:**
  - **One image, warm first.** Claude calls `gpu_warm(5)` and then `generate`.
    - Warm first: a 300 s window plus a 60 s tail at $0.000542/s ≈ **$0.195**.
    - The same job cold: 68 s boot + 16 s render + 60 s tail = 144 s ≈ **$0.078**.
    - That is 2.5× the cost for zero latency gain. `gpu_warm(30)` alone costs ≈ $1.01.
  - **Stop from Claude.** `gpu_stop(confirm=true)` cancels every queued and running job on the backend, including a browser batch the owner started, because Stop also cancels queued jobs. Getting past the 409 costs Claude one extra call.
- **Evidence:**
  - Scope: contract:67; brief:158; phase-08:64-65, :68, :69 (`destructiveHint`), :72 ("optionally warm").
  - Behaviour and pricing:
    - phase-06:41 ("A cold ping is the boot … about 68 s"), :50 ("Stop cancels queued jobs too"), :72 (cost helpers).
    - research-02:93 (a cold `ping()` is the cold start), :104 (idle bills the same as active, $0.000542/s), :105 (30 min ≈ $0.98).
    - README.md:26 (68 s cold, 15.6 s warm, ≈ $0.009 per warm image).
- **Suggested fix:**
  - Ask the owner whether Claude should be able to warm and stop.
  - If yes:
    - SKILL.md says "never warm before a single generate; warm only when several interactive jobs will follow inside the window";
    - the plugin's warm is capped at 5 minutes;
    - `gpu_stop` always returns the job counts first and SKILL.md requires asking the user before confirming.
  - If no: drop both tools, both endpoints, and `test_gpu_stop_needs_confirmation_then_stops` (phase-08:178).

## Finding 3: `/api/v1` claims to reuse the phase 7 services, but its parameters do not match them
- **Severity:** Medium
- **Location:** Phase 8, section "Requirements → Functional, JSON API" (table); Phase 7, section "Requirements → Search and filters / Workflows"
- **Flaw:** phase-08:46 says the API "reuses the HTML routes' services … so validation … stay identical (DRY)". The contracts diverge in five places:
  1. **Pagination.** `GET /api/v1/images?…&before=` uses an id cursor. The service is `search(conn, q, tag, starred, model, limit, offset)`, which pages by offset, and results for `q` or `tag` are ordered by bm25 (phase-07:63, :81, :162). An id cursor cannot page a relevance order. No tool sends `before` anyway: the brief's `list_images(query, tag, starred, model, limit)` has no cursor (brief:155).
  2. **Workflow addressing.** `POST /api/v1/workflows/{name}/run` puts the name in the path. Phase 7 validates tag and preset names but not workflow names ("names are unique" only, phase-07:66, :236). Every HTML workflow route uses `{id}` (phase-07:67-68). A name containing `/` never matches the route template, and names with spaces or non-ASCII characters need URL encoding that no phase specifies.
  3. **Seed mode.** The body `{seed?, count?}` cannot express phase 7's three modes: random, fixed, and keep (phase-07:67, :170).
  4. **Workflow discovery.** `GET /api/v1/workflows` (phase-08:61) has no consuming tool. None of the nine tools (phase-08:68) lists workflows, so Claude cannot find names for criterion 10's "run a stored workflow".
  5. **Duplicate path.** The flow diagram fetches the HTML route `GET /images/{id}/file` (phase-08:91), while the table defines `GET /api/v1/images/{id}/file` (phase-08:60).
- **Failure scenario:**
  - The owner uploads a workflow named "Portrait / warm light" through the UI, which is allowed. `run_workflow(name="Portrait / warm light")` returns 404, and Claude has no tool to see which names exist.
  - A `list_images(query="fox")` page followed by `before=<last id>` returns rows unrelated to the second relevance page.
- **Evidence:** phase-08:46, :58, :60-62, :68, :91; phase-07:62-63, :66-68, :81, :162, :170, :236; phase-03:64, :218 (the HTML gallery pages by `page`); brief:155.
- **Suggested fix:**
  - Address workflows by `{id}` in the API; the tool can take a name and resolve it through `GET /api/v1/workflows`. Alternatively, restrict workflow names to `[a-z0-9-]{1,64}`.
  - Use the service's own `offset` parameter, or drop API pagination, since `limit` is already capped at 50.
  - Add `seed_mode` to the run body.
  - Add a `list_workflows` tool.
  - Question the bm25 flat grid: it is plan-added (the contract asks only to "search them by prompt text", contract:65), and phase-07:228 itself says ordering by id gives "the same results". Without bm25, one cursor works everywhere and the second gallery layout goes away.

## Finding 4: One HTML Stop route gets two response contracts, and the 4xx one never renders under HTMX
- **Severity:** Medium
- **Location:** Phase 6, section "Requirements → Routes" and "Implementation Steps" step 4 (overlaps the Phase 8 API table)
- **Flaw:**
  - phase-06:76 says that without `confirm`, `POST /gpu/{backend}/stop` "returns a confirmation partial … and a 409 status for API clients".
    - Phase 6 has no API clients; the API arrives in phase 8 with its own `/api/v1/gpu/{backend}/stop` returning 409 (phase-08:65).
    - Nothing says how the HTML route would tell an API client from HTMX.
  - If the partial is sent with a 409, htmx 2 drops it. Its default `responseHandling` swaps only 2xx responses, the same rule the plan relies on at phase-03:284.
  - The same trap exists elsewhere:
    - "Save as preset" is explicitly `hx-post` (phase-07:157);
    - generate errors re-render with 422 (phase-03:59, :210), and no phase says whether that form is a plain form or `hx-post`.
- **Failure scenario:**
  1. The owner presses Stop while a batch runs.
  2. The server answers 409 with the confirmation partial, and htmx treats it as an error and doesn't swap it.
  3. Nothing appears; pressing again gives the same result.
  4. `test_stop_requires_confirmation_when_jobs_are_active` (phase-06:225) asserts on the TestClient response, so CI stays green.
  5. Live step 7.5 and the "after confirmation" part of criterion 6 fail in production.
- **Evidence:** phase-06:76, :186, :225; phase-03:59, :210, :284; phase-07:157; phase-08:65.
- **Suggested fix:**
  - HTML routes always answer 200 with the partial, or use `HX-Reswap`/`HX-Retarget`. Status-code semantics live only in `/api/v1`.
  - State whether the generate and preset forms are plain forms or `hx-post`. If they use HTMX, errors come back as 200 with the re-rendered form, or `responseHandling` is configured for 422.

## Finding 5: `modal app list` only returns recently stopped apps, so a backend that has been stopped a while shows "unknown", not "stopped"
- **Severity:** Medium
- **Location:** Phase 6, section "Requirements → gpu.py" (`display_state`) and "Implementation Steps" step 1 (`parse_app_state`); live step 7.6
- **Flaw:**
  - "Stopped" is inferred from the app's row in `modal app list --json`, but the CLI lists only apps that are "running, deployed or recently stopped".
  - `parse_app_state` filters rows by `description`, prefers `deployed`, and otherwise takes the newest row (phase-06:178). It has no case for zero rows.
  - Once a stopped app ages out of the list, the behaviour is undefined: an exception, which the loop turns into `unknown`, or an index error.
  - `start_warm` is refused only when the state is `stopped` (phase-06:70, :251). Under `unknown`, warm is accepted and every ping fails.
  - The live check stops the app and looks within 60 s, while it is still "recent" (phase-06:200), so it cannot catch this.
- **Failure scenario:** The owner stops the Qwen app for a week to save money. The badge then shows "unknown" with an error instead of "stopped · needs `modal deploy`". Criterion 6 ("each backend shows deployed or stopped") fails in exactly the case it exists for.
- **Evidence:** modal SDK `cli/app.py:104` (`"""List Apps that are running, deployed or recently stopped."""`); phase-06:69, :70, :178, :199-201, :216, :251.
- **Suggested fix:** Treat "no row with this app name" as `stopped`, and add a parser test for it. Optionally cross-check with the SDK lookup error for an app that isn't deployed [UNVERIFIED offline].

## Finding 6: The backup CLIs are told both to avoid and to use the full `Settings`, and the engine never names the database file
- **Severity:** Medium
- **Location:** Phase 5, sections "Requirements → Settings independence" and "Implementation Steps" step 1; Phase 2, section "Requirements → Database"
- **Flaw:**
  - **Contradiction.** phase-05:60 says both CLIs read only `ATELIER_DATA_DIR` and never load the full `Settings`, so verify can run in a bare `docker run`. phase-05:186 says they "open the live DB at `settings.data_dir / "atelier.db"`".
  - **Why that breaks verify.** The full `Settings`:
    - defaults `ATELIER_ENV` to `production`;
    - then requires the AUD, the owner email and the plugin client ID;
    - and requires the volume sentinel (phase-02:65, :69, :73).

    The verify container (phase-05:138) has no environment and no `/data` mount.
  - **Undefined filename.** Phase 2 never names the database file; the string `atelier.db` appears nowhere in phase-02. Phase 5's backup scope, snapshot and restore runbook all hard-code it (phase-05:38, :186, :242).
- **Failure scenario:**
  - The implementer follows step 1 literally. Every Sunday, verify raises `ConfigError` and exits 3, and the badge turns red weekly.
  - The owner then either ignores the badge, which defeats criterion 11, or passes `/opt/atelier/.env` (with the Modal tokens) into a container that is supposed to need no secrets.
  - If phase 2 happens to pick another filename, the nightly snapshot backs up a missing path.
- **Evidence:** phase-05:38, :60, :138, :186, :242; phase-02:65, :69, :73, :74-76 (no filename).
- **Suggested fix:**
  - Define the database filename as a constant in phase 2's `db.py` and import it in `backup_db`.
  - Rewrite phase-05:186 to read `ATELIER_DATA_DIR` directly.
  - Add a test that runs `backup_db verify` with an empty environment.

## Finding 7: The rollback drill is unrequested, needs token scopes nobody grants, and its cleanup can delete the good image
- **Severity:** Medium
- **Location:** Phase 4, section "Implementation Steps" step 18, and "Success Criteria → Criterion 13"
- **Flaw:**
  - **Scope.** Criterion 13 asks for a deploy and a health check over SSH (contract:70). The brief asks `deploy.sh` to roll back automatically (brief:117-118). Neither asks for a live drill on Folio's production host, but the plan adds one and writes it into criterion 13 (phase-04:420).
  - **Token scopes.**
    - The drill pipes `gh auth token` into `deploy.sh`, whose `docker pull` of the private package needs `read:packages`. gh's default scopes are only `repo`, `read:org` and `gist`.
    - Pushing `FAKE` needs `write:packages`, and cleaning it up needs `delete:packages`.
    - So the owner must widen a long-lived laptop token just for this drill.
  - **Cleanup.** GHCR deletes *versions*, not tags. `FAKE` is a retag of the good image. If it lands on the same version, "Delete the `FAKE` tag from GHCR" also removes the good SHA and `:latest`.
- **Failure scenario:**
  - With default gh scopes, the pull at deploy.sh:190 is denied. Under `set -euo pipefail` (:172) the script exits before `up`, `serves` or the rollback branch.
  - The owner sees a non-zero exit but no "rolled back" line. The drill either "passes" without exercising rollback or gets debugged on production.
  - If the owner widens the scopes and then deletes the version, a later `workflow_dispatch` redeploy of that SHA (phase-04:73) cannot pull.
- **Evidence:**
  - Plan: phase-04:172, :190, :388-392, :409, :420; contract:70; brief:117-118.
  - gh: `gh auth login --help` ("The minimum required scopes for the token are: `repo`, `read:org`, and `gist`").
  - GitHub docs, "Deleting and restoring a package": deletion is "Delete version", and deleting through GraphQL needs `read:packages`, `delete:packages` and `repo`.
- **Suggested fix:** Ask the owner whether the drill is wanted.
  - If yes:
    - run the deploy leg with `gh workflow run deploy.yml -f sha=<drill sha>`, so the job's `GITHUB_TOKEN` does the pull;
    - build the drill image with `--build-arg ATELIER_VERSION=drill`, so it has its own digest and is safe to delete;
    - list the exact scopes the push needs.
  - If no: drop step 18 and phase-04:420.

## Finding 8: GPU status polling runs all the time on Folio's shared server, whether or not anyone is looking
- **Severity:** Medium
- **Location:** Phase 6, sections "Key Insights" (app state vs stats), "Requirements → Worker additions" and "Architecture"
- **Flaw:**
  - The contract asks for status "refreshed automatically" (contract:61), and the brief says "every 10 s, cached" (brief:88).
  - The plan instead runs a worker loop 24/7:
    - `get_current_stats()` every 10 s;
    - a `python -m modal app list --json` subprocess every 60 s.
  - The plan itself costs each subprocess at about 1 s of CPU and up to 150 MB. The container is capped at 768 MB and 1 CPU on a 4-vCPU, 7.7 GB host that Folio shares.
  - Polling on demand would give the same freshness: the header partial already polls every 10 s whenever an Atelier tab is open. With no tab open, the loop's output has no consumer; the pinger needs only `warm_until` and its own ping handle.
  - The UI also shows `input_headroom`, which the plan itself rates "Certain" to look wrong.
- **Failure scenario:** On days with no tab open, the loop makes about 1,440 CLI spawns and 8,640 stats RPCs for a display nobody reads. Each spawn briefly adds memory inside a container with a hard 768 MB limit. That OOM risk is the plan's own (phase-06:248), and its pre-decided response is to raise `mem_limit` on Folio's server.
- **Evidence:**
  - Contract and brief: contract:61; brief:88.
  - The loops: phase-06:52, :59, :65, :87-88, :151, :248, :250.
  - Existing polling and limits: phase-03:70, :222; phase-04:128-129; scout-02:7.
  - My measurement: `python -m modal app list --help` alone takes about 0.2 s and 68 MB RSS on the laptop, before any RPC. That is a lower bound for the real call.
- **Suggested fix:**
  - Compute status when it is read: cache stats for 10 s and the app state for 60 s.
  - Refresh on requests to `/gpu/panel`, the header, `/api/v1/gpu` and `start_warm`, and force a refresh after warm or stop.
  - Remove the `gpu_status_tick` loop and drop `input_headroom` from the UI.

## Finding 9: The tests behind criteria 14 and 10 do not cover what later phases add
- **Severity:** Medium
- **Location:** Phase 3, section "Success Criteria → Criterion 14"; phases 5–8, section "Related Code Files"; Phase 8, section "Key Insights" (plugin integration test)
- **Flaw:**
  - **Crawl test.**
    - `test_pages_hide_secrets` is written in phase 3 and "crawls every GET page and partial" (phase-03:261).
    - Phases 5–8 add GET pages and partials: `/gpu`, `/gpu/panel`, `/library`, `/workflows`, `/workflows/{id}/download`, `/jobs/{id}/graph.json` and `/api/v1/*`.
    - None of those phases' Modify lists touch the crawl test, and the plan never says the crawl enumerates `app.routes`.
    - Phase 8's acceptance row 14 still cites "the page crawl test is green" as evidence.
    - `/gpu` renders CLI error text (phase-06:81). Modal's auth errors don't echo tokens (SDK `client.py:313`), so there is no known leak; the gap is in the proof.
  - **Plugin integration test.**
    - It runs with `ATELIER_DEV_IDENTITY=service`, which returns an identity before any JWT check (phase-03:107-108).
    - That contradicts brief §5 ("They must not bypass verification", brief:102), and the plan doesn't flag the deviation.
    - `test_api_v1.py` does use real service JWTs (phase-08:171), so the functional risk is low. The issue is the unflagged contradiction.
- **Failure scenario:** A phase 6 panel or phase 7 page starts rendering something taken from `settings`. The crawl, still visiting only phase 3 URLs, stays green, and row 14 is signed off.
- **Evidence:** phase-03:186, :261; phase-05:175-178; phase-06:74, :81, :163-171; phase-07:142-149; phase-08:47, :159-164, :171, :189, :241; brief:102; SDK `client.py:313`.
- **Suggested fix:**
  - Make the crawl enumerate `app.routes` (GET only, IDs filled from fixtures) and fail on any GET route it didn't visit. Alternatively, add the crawl test to each later phase's Modify list.
  - In phase 8, state the dev-identity deviation from brief §5 explicitly.

---

## Contract verification

**Result: FAILED.** There are 7 consumer mismatches; the rest passes. The mismatches are covered by findings 1, 3, 4, 5, 6, 7 and 9, plus one stale README line.

**Modal method names (backend → app)**
- `run_workflow`:
  - defined at `qwen21_uc_app.py:147-150`;
  - consumed at phase-02:199 and phase-07:84.
  - **PASS**
- `ping`:
  - added in phase 1 (phase-01:42, :64-69), inserted after `:150`;
  - first consumed in phase 6 (phase-06:61, :118, :179);
  - redeployed by `deploy-modal.yml` (phase-04:74, :287);
  - never referenced before it exists.
  - **PASS**
- `generate`: untouched, and never called by the app (phase-02:53). **PASS**
- SDK line references, spot-checked against modal 1.5.5. All match; research-02's `_functions.py:2121` for `from_id` is stale, and phase 2's `:2271` is the correct line.
  - `exception.py:189`, `:209`, `:229`, `:233`
  - `_functions.py:319-334` (poll), `:2080` (stats), `:2257` (cancel), `:2271-2273` (`from_id`)
  - `grpc_utils.py:459`, `:461`
  - `cls.py:90`
  - `cli/container.py:41` (`--app-id`), `:318-335` (stop with `--yes`)
  - `cli/utils.py:194-199`
  - `cli/app.py:41-50`
- `modal app list` only returns running, deployed or recently stopped apps, but `parse_app_state` has no zero-row case. **FAILED** (Finding 5)

**Settings and environment variables** (`config.py` reads 13, phase-02:65-71)
- Production supplies all of them:
  - `.env`: 11 variables (phase-04:336-338);
  - compose `environment:`: 7 variables (phase-04:121-127);
  - the Dockerfile: `ATELIER_VERSION` (phase-04:156-157);
  - the timeout has a default, and the dev identity is unset.
  - No variable is defined without a consumer; `MODAL_ENVIRONMENT` is read by the SDK. **PASS**
- The backup CLIs (`ATELIER_DATA_DIR` only) versus `settings.data_dir`, and the database filename is undefined in phase 2. **FAILED** (Finding 6)
- `backup.env`: 5 variables (phase-05:215-217), consumed by restic's `--env-file` (phase-05:111). **PASS**
- Plugin: 4 environment variables match the 4 `userConfig` keys (phase-08:71, :103-106).
  - Names: **PASS**.
  - How the values get set: **FAILED** (Finding 1).
  - `title` and `description` are required on each option but not specified.

**CI secrets versus workflow references**
- `deploy.yml` uses `HETZNER_HOST`, `ATELIER_DEPLOY_SSH_KEY` and `HETZNER_KNOWN_HOSTS` (phase-04:227-229); all three are created at phase-04:347. **PASS**
- `deploy-modal.yml` uses `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` (phase-04:287), created at phase-04:347. **PASS**

**`deploy.sh` grammar** (phase-04:177-185), 3 consumers
- `deploy.yml` (phase-04:236-237): **PASS**
- `test_deploy_script.py` (phase-04:278-282): **PASS**
- Rollback drill (phase-04:391): the command grammar passes, but the token scope fails. **FAILED** (Finding 7)

**Backup scripts versus app and server**
- Container name `atelier` (phase-04:109) matches `docker exec atelier` (phase-05:102, :126). **PASS**
- `current-tag` (phase-04:200) matches the verify image tag (phase-05:138, :232). **PASS**
- The volume sentinel (phase-04:326) matches the preflight check (phase-05:106). **PASS**
- Both backup modules exist (phase-05:163-164). **PASS**
- The status file feeds the header badge (phase-05:176-177). **PASS**

**UI routes**
- Phase 3: 15 routes plus `/` (phase-03:56-71, :169).
- Phase 6: 4 routes (phase-06:74-76).
- Phase 7: 11 routes. The paths for "save preset" and "delete preset" are not specified (phase-07:54-55).
- The Stop route has two response contracts. **FAILED** (Finding 4)
- The crawl test's coverage. **FAILED** (Finding 9)

**`/api/v1` versus the plugin**
- 11 endpoints (phase-08:55-65) against 9 tools (phase-08:68).
- `GET /api/v1/workflows` has no tool, the `before=` parameter has no sender, the `{name}` path clashes with unvalidated names, and there is no seed mode. **FAILED** (Finding 3)

**Docs and CLI names**
- `README.md:9` (`modal run qwen21_uc_app.py::download_models`) is not in phase 1's edit, which changes only lines 8 and 10 (phase-01:143). It stays stale until phase 8. **FAILED** (minor)
- `claude plugin install <dir>` (phase-08:196, :199) doesn't work without a marketplace. **FAILED** (Finding 1)
- `claude plugin validate <path>` exists. **PASS**
- `ak plan status` exists (plan.md:41). **PASS**
- Reference-repo citations were re-read and match. One is off by one: `pg-dump.sh`'s logger is at `:19`, not `:18`. **PASS**
  - `folio/scripts/deploy/deploy-runner.sh:22-23`, `folio/scripts/deploy/wait-healthy.sh:17-33`, `folio/scripts/backup/pg-dump.sh:8-11`
  - `folio/folio-plugin/.claude-plugin/plugin.json`, `.mcp.json`, `mcp_servers/folio_mcp/server.py:2-12`
  - `learnflow/.claude/skills/learnflow/SKILL.md:153-157`, `learnflow/.github/workflows/deploy.yml:11`
- Phase 7's claim that user search text can never cause an FTS5 syntax error was checked by running its `fts_query` sketch verbatim against SQLite 3.54 FTS5 with 12 adversarial inputs (for example `"`, `-`, `(`, `a" OR -b NEAR(` and an emoji). None raised an error. **PASS**

## Unresolved questions

1. Should Claude hold GPU warm and stop at all? (Finding 2)
2. Is the production rollback drill wanted? (Finding 7)
3. Which client will the owner use for the plugin: Claude Code CLI, or Claude Desktop like Folio? This decides the install route and whether `userConfig` applies. (Finding 1)
4. After `modal app stop` and a redeploy (phase 6 step 7.6), does the per-process cached `Cls` handle (phase-02:199) keep working, or does it hold stale IDs until Atelier restarts? [UNVERIFIED offline]
5. The tunnel change deviates from the brief without being flagged. Not raised as a finding: the replica method is defensible for Folio's uptime.
   - The replica cutover (phase-04:350-374) replaces brief §6's plain restart (brief:129), but it is not flagged as a brief deviation, which brief:3 requires.
   - Line 367 says to copy the service's ExecStart flags, while lines 368-369 hard-code the replica command.
   - The owner should confirm the method.
