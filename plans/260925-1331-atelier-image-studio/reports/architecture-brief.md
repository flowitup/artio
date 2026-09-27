# Architecture brief for the Atelier plan (orchestrator → planner)

This brief turns the accepted contract (`plans/reports/brainstorm-260925-1507-atelier-image-studio-hetzner.md`) into a concrete design and phase outline. Where it says "per research-0X", take the final detail from that report. Where research contradicts this brief, flag it in the plan's risk notes rather than silently switching.

## 1. Fixed facts
- **Local folder:** `/Users/sweet-home/Works/atelier`. It becomes the git repo for `flowitup/atelier`. Don't rename the folder, because the Claude memory path depends on it. It currently holds `qwen21_uc_app.py`, `test_deployed.py`, `README.md`, `out/` (generated images, private), `logs/` (may contain token output, **never commit**) and `plans/`.
- **Server:** `folio-prod-1`, Folio's production box (see `reports/scout-02-folio-prod-1-live-state.md`).
  - The root disk has 21 GB free, so Atelier data goes on a dedicated Hetzner Volume.
  - Ingress is a locally-managed cloudflared config. LearnFlow is `learn → localhost:8080` (Caddy).
  - Atelier gets `atelier.flowitup.com → http://localhost:8090`.
- **Modal:** app `qwen21-uc`, class `Qwen21UC`. `generate()` and `run_workflow()` return PNG bytes. `max_containers=1`, `max_inputs=4`, `scaledown_window=60`. A cold start takes 68 s and a warm image about 16 s. The L40S costs $1.95/h.

## 2. Repository layout (target)
```
.gitignore            # out/, logs/, data/, .env*, __pycache__/, .venv/, *.plugin build output
pyproject.toml        # uv project "atelier"; deps: fastapi, uvicorn[standard], jinja2, python-multipart,
                      #   modal (pin 1.5.x), pyjwt[crypto], pillow; dev: pytest, ruff, httpx
uv.lock
README.md             # rewritten: what Atelier is, local dev, commands, links to docs
Dockerfile            # multi-stage; CSS build per research-02 §7; python:3.12-slim; uid 10001; no shell tools needed
compose.yaml          # service `atelier` (see §6)
modal/qwen21_uc_app.py   # moved unchanged + one `ping()` method
modal/smoke_test.py      # was test_deployed.py (manual, costs GPU time)
atelier/
  __init__.py
  main.py             # app factory, lifespan starts/stops background tasks, mounts static
  config.py           # env settings (dataclass), validation, dev-mode guard
  auth.py             # Cloudflare Access JWT dependency (owner email or service-token client id)
  db.py               # sqlite3 connection (WAL, foreign_keys), migration runner
  migrations/0001_init.sql (+ later numbered files)
  registry.py         # BACKENDS + MODELS (model-neutral); size presets; param schema; cost rate
  workflows/qwen_image_21.py  # build_graph(params) — the logic of today's build_workflow()
  modal_gateway.py    # thin wrapper: spawn run_workflow, poll call, cancel, stats, app state, ping, stop containers
  jobs.py             # job/batch service: create batch, retry, cancel, state transitions
  worker.py           # asyncio loops: dispatcher, poller, gpu-status, warm pinger
  storage.py          # save PNG, WebP thumbnail (Pillow), delete, disk guard, byte accounting
  library.py          # presets, stars, tags, FTS search
  custom_workflows.py # upload validation (API format), seed override, run
  backup_status.py    # read backup-status.json written by the backup job
  routes/ pages.py generate.py jobs.py images.py library.py workflows.py gpu.py api_v1.py health.py
  templates/ base.html, generate.html, queue.html, gallery.html, image.html, batch.html, library.html,
             workflows.html, partials/*.html (job row, gpu badge, disk/backup badge, tag editor, …)
  static/ htmx.min.js (vendored, pinned), app.css (built), minimal app.js if needed
tests/                # pytest: unit + api; fakes only for the Modal gateway boundary; opt-in live test marker
plugin/               # Claude plugin (mirrors folio-plugin layout, see scout-01)
  .claude-plugin/plugin.json  .mcp.json  mcp_servers/atelier_mcp/server.py (PEP 723)  skills/atelier/SKILL.md  README.md  build.sh
deploy/
  deploy.sh           # server-side forced-command target (validate sha → pull → up → health → rollback → prune)
  backup/atelier-backup.sh  backup/atelier-backup-verify.sh
  systemd/atelier-backup.{service,timer}  atelier-backup-verify.{service,timer}
  cloudflared-ingress-rule.yml   # the exact rule to insert
.github/workflows/ ci.yml  deploy.yml  deploy-modal.yml
docs/ deployment-guide.md (server setup, owner actions, deploy, rollback, backup/restore runbook)  system-architecture.md
```

## 3. Data model (SQLite, WAL)
- **`batches`**: id, created_at, model_id, kind (`generate` or `workflow`), base_params_json, count.
- **`jobs`**:
  - Identity and parameters: id, batch_id, model_id, backend_id, kind, params_json (prompt, negative, width, height, steps, cfg, seed), graph_json (the exact graph sent to Modal), workflow_id (nullable).
  - State: status (`queued`, `submitted`, `done`, `failed` or `cancelled`), call_id, error, attempt, retry_of.
  - Timing: created_at, submitted_at, finished_at, duration_s, est_cost_usd.
- **`images`**: id, job_id (unique), model_id, file_png, file_thumb, width, height, bytes, sha256, seed, prompt, negative, starred, created_at. Search uses FTS5 `images_fts(prompt, negative, tags)` kept in sync by triggers or the service.
- **`tags`**: id, name (unique, normalized). **`image_tags`**: image_id, tag_id.
- **`presets`**: id, name, model_id, params_json, created_at, updated_at.
- **`workflows`**: id, name, backend_id, graph_json, created_at. Uploaded API-format JSON.
- **`backend_state`**: backend_id, warm_until (nullable). This lets the warm window survive restarts.
- **`schema_version`**.

**Delete** is a hard delete of the row and its files, after confirmation. Backups keep deleted images only within their retention window.

## 4. Key flows
- **Generate.** The form collects model, prompt, negative, size preset (from the registry) or custom width and height (multiples of 16), steps, cfg, seed mode (random or fixed) and count N (1–8).
  - One batch and N queued jobs are created, each with a distinct seed. The app always chooses the seed.
  - The graph comes from the registry template, and the app calls the backend's existing `run_workflow`. Nothing else in the Modal script changes; `generate()` stays for backward compatibility.
- **Dispatcher** (every 1 s). For each backend, while in-flight jobs number fewer than `max_inflight` (4, equal to `max_inputs`) and queued jobs exist:
  1. Check the disk guard.
  2. Call `run_workflow.spawn.aio(graph)`.
  3. Store the call_id and set status `submitted`.
- **Poller** (every 2 s). For each submitted job, poll per research-02 §1:
  - Not ready: keep polling. Mark the job failed after `JOB_TIMEOUT` (1800 s, the Modal class timeout plus margin).
  - Result: write the PNG and thumbnail, insert the image row, and mark the job `done` with duration and cost.
  - Error: mark `failed` with the ComfyUI message.
  - On startup, polling of submitted jobs resumes because call_id is persisted.
- **Cancel.** A queued job becomes `cancelled`. A submitted job gets `FunctionCall.cancel()` and then `cancelled`. Note that ComfyUI may finish rendering anyway, and that result is discarded.
- **Retry** creates a new job with the same params and seed (`retry_of`).
- **Remix** opens `/generate?from=<image_id>` with the form prefilled.
- **UI polling.** HTMX partials poll every 2 s while jobs are active and stop (HTTP 286) when none are. The GPU badge polls every 10 s.
- **GPU status** (every 10 s, cached). Collect `get_current_stats()` (runners, running inputs, backlog) and the app state (deployed or stopped) per research-02 §2.
- **Warm-up.** `POST /gpu/{backend}/warm` with minutes set to 5, 15 or 30 stores `warm_until`. The pinger calls `ping()` about every 30 s until `warm_until`, with one ping in flight at most.
  - The UI shows "warming → warm (until hh:mm)" and a running cost estimate.
  - The pinger resumes after a restart, and it fails safe: if Atelier stops, the GPU scales to zero within 60 s.
- **Stop.** `POST /gpu/{backend}/stop` clears `warm_until`.
  - If jobs are submitted, it requires confirmation and cancels them first.
  - It then stops the backend's containers (per research-02 §2; the CLI `modal container stop` via subprocess if there is no public SDK call) and refreshes the status.
- **Disk guard.** New jobs are refused when the volume's free space falls below the floor (for example 10% or 5 GB) or when Atelier's image bytes exceed the cap (a setting). The UI shows usage.
- **Backup status.** `backup-status.json` in the data dir holds the last run, the last success, the last verify and any error. The UI banner warns when the last success is older than 36 h.

## 5. Auth
- Every route except `/healthz` requires `Cf-Access-Jwt-Assertion`, validated against the team JWKS with the AUD tag and issuer, per research-01 §1–2.
- Only two identities are allowed: the owner's email, or `common_name` equal to the plugin's service-token client ID. Everything else gets a 403.
- **Dev mode** (`ATELIER_DEV_IDENTITY`) is allowed only when `ATELIER_ENV=development`. Startup fails if dev mode is set together with `ATELIER_ENV=production`.
- **Tests** mint real RS256 JWTs with a test key and a patched JWKS fetcher. They must not bypass verification.
- **CSRF:** state-changing forms use POST. Rely on Access's SameSite cookie and additionally check the `Origin` header on POST requests (it must be the app host). The JSON API is used by the plugin with service-token headers (no cookies).

## 6. Runtime and deploy
- **`compose.yaml`:**
  - image `ghcr.io/flowitup/atelier:${ATELIER_TAG:?required}`, `ports: ["127.0.0.1:8090:8000"]`.
  - `volumes: ["/mnt/atelier-data:/data"]`, `env_file: /opt/atelier/.env`, with `${VAR:?required}` for critical settings.
  - `read_only: true` with a tmpfs `/tmp`, `user: "10001:10001"`, `cap_drop: [ALL]`, `security_opt: ["no-new-privileges:true"]`.
  - Resource limits: `mem_limit: 768m`, `cpus: 1.0`.
  - Healthcheck on `/healthz`; json-file logging with max-size 10m and max-file 3; `restart: unless-stopped`.
  - Run uvicorn with a **single worker** so the background loops aren't duplicated.
- **Server layout:** `/opt/atelier/` holds `compose.yaml`, `.env` (0600, root), `deploy.sh`, `current-tag`, `previous-tag` and `backup/`. Data lives at `/mnt/atelier-data` (the Hetzner Volume, mounted in fstab by UUID, owned by 10001:10001).
- **Deploy identity:** a new ed25519 key in root's `authorized_keys`, restricted to `restrict,command="/opt/atelier/deploy.sh"`.
  - `deploy.sh` reads the requested SHA from `SSH_ORIGINAL_COMMAND` and only accepts a 40-hex SHA.
  - It logs in to GHCR per research-02 §8 (preferably with a short-lived token over stdin, then `docker logout`), pulls, and runs `up -d`.
  - It waits up to 60 s for the container to become healthy, and rolls back to `previous-tag` if it doesn't.
  - Afterwards it prunes old `ghcr.io/flowitup/atelier` tags, keeping 3.
- **CI:**
  - `ci.yml` runs `uv sync`, `ruff check` and `pytest` on PRs and pushes.
  - `deploy.yml` runs on `main`: test, then build and push `:<sha>` and `:latest` with OCI labels, then SSH with the restricted key, then verify. It uses the concurrency group `deploy-atelier`. It verifies through the job result and a health check on the server, never the live URL (Access blocks it, as LearnFlow already notes).
  - `deploy-modal.yml` runs when `modal/**` changes on `main`: `modal deploy modal/qwen21_uc_app.py` with `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` secrets.
- **Tunnel change** (runbook, owner-approved at execution time):
  1. Back up `/etc/cloudflared/config.yml`.
  2. Insert the Atelier rule before the catch-all.
  3. Run `cloudflared tunnel ingress validate`, then `cloudflared tunnel ingress rule` for folio, cdn, learn and atelier.
  4. Create the **Access application before the DNS route**.
  5. Run `cloudflared tunnel route dns <tunnel> atelier.flowitup.com`.
  6. Restart cloudflared.
  7. Verify that folio `/health` returns 200 and that learn and atelier return the Access 302.
  8. If anything fails, restore the backup and restart.

## 7. Backups
- **When:** a systemd timer at 04:30 UTC daily. Verification runs Sunday at 05:00 UTC (Folio's jobs run at 03:00 and 03:30, and the stock scrubs on Sunday around 03:10).
- **Backup job:**
  1. `docker exec atelier python -m atelier.backup_db /data/backup/atelier.db` takes an online backup through `sqlite3.Connection.backup`, since sqlite3 isn't installed on the host.
  2. `docker run --rm restic/restic:<pinned>` backs up `/data/images` and `/data/backup/atelier.db` to R2, per research-01 §5. The live `atelier.db*` files are excluded.
  3. `forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune`.
  4. The job writes `backup-status.json` and logs through `logger -t atelier-backup`, using stratified exit codes (Folio's pattern).
- **Weekly verification:**
  1. `restic check --read-data-subset=5%`.
  2. Restore the latest DB snapshot into a temporary directory.
  3. Run `PRAGMA integrity_check` and compare the image row count with the files in the snapshot.
  4. Update the status.
- **Credentials:** the R2 key pair and `RESTIC_PASSWORD` live in `/opt/atelier/backup.env` (0600, root). The owner also stores the restic password in their password manager.
- **Restore runbook** in `docs/deployment-guide.md`, tested once end to end on the server into a temporary directory.

## 8. Claude plugin
- Mirror the layout of `folio-plugin` (scout-01). The server is a PEP 723 script run with `uv run --script`, built on the MCP Python SDK (FastMCP) and httpx.
- Env: `ATELIER_BASE_URL`, `ATELIER_CF_CLIENT_ID`, `ATELIER_CF_CLIENT_SECRET`, sent as the `CF-Access-Client-Id`/`CF-Access-Client-Secret` headers.
- Tools:
  - `list_models`
  - `generate(model, prompt, …, count, wait_seconds)`: wait up to about 50 s, then return job IDs (research-02 §6 decides between progress and a job ID)
  - `job_status(ids)`
  - `list_images(query, tag, starred, model, limit)`
  - `get_image(id, save_to)`: saves the PNG locally and returns a small WebP thumbnail
  - `run_workflow(name, seed, count)`
  - `gpu_status`, `gpu_warm(minutes)`, `gpu_stop(confirm)`
- The JSON API lives under `/api/v1` and uses the same auth dependency.
- `skills/atelier/SKILL.md` explains when and how to use it. `build.sh` zips the plugin into `atelier.plugin`, as Folio does.

## 9. Phase outline (planner expands; keep this order and these boundaries)
Tie every acceptance criterion from the contract to at least one phase's success criteria.
1. **Repo bootstrap and Modal backend.** Git init, `.gitignore` (logs/ and out/ excluded), uv project with ruff and pytest, move the Modal script to `modal/` and add `ping()`, move the smoke test.
   - Owner-approved: `modal deploy` plus one `ping` smoke call (a cold start costs about $0.04).
2. **Engine.** Config, DB and migrations, registry (Qwen entry plus presets), graph builder (parity test against the old `build_workflow` output), Modal gateway, job service, worker loops (dispatcher and poller, including restart resume), storage with thumbnails, disk guard.
   - Unit tests use a fake gateway. One opt-in live test (`-m live`) does a real generation.
3. **Web UI and Access auth.** Auth dependency and tests. Generate form (model picker, presets, batch N, seed modes, remix), queue view with polling, gallery (grid, model filter, batch grouping), image detail (metadata, download, delete, retry, cancel), CSS build, `/healthz`.
4. **Container, CI/CD and first production rollout.** Dockerfile, compose, `deploy.sh`, the three workflows. Server setup runbook: volume mount, `/opt/atelier`, restricted key, `.env`. Cloudflare Access app and service token. Tunnel rule and DNS route with validation and rollback.
   - First deploy, then prove that Access works, the origin rejects requests without a JWT, and Folio, cdn and LearnFlow are unaffected.
   - Owner actions are gated here.
5. **Backups and restore.** Backup and verify scripts, systemd units, `backup_db` module, R2 bucket and token plus `restic init` (owner), first backup, restore test, UI backup badge, runbook.
6. **GPU status, warm-up and stop.** Status loop, the warm pinger with persisted `warm_until`, stop with cancel-first confirmation, and UI controls with a cost hint. Live verification against Modal (owner-approved GPU time).
7. **Prompt library and custom workflows.** Presets CRUD and load into the form, stars, tags, FTS search. Workflow upload with API-format validation (reject UI-format with a hint), seed override on KSampler-family nodes, run N times.
8. **JSON API, Claude plugin and final acceptance.** `/api/v1`, the plugin with its skill and build script, docs (`deployment-guide.md`, `system-architecture.md`, README), and a full acceptance run of criteria 1–14 recorded in the plan.

## 10. Constraints the planner must keep
- **Owner approval:** do nothing on `folio-prod-1`, Cloudflare, R2, GitHub (repo creation, secrets) or Modal (deploy, GPU calls) without an explicit owner go-ahead. Mark each such step with **[OWNER-GATED]**.
- **Secrets:** never print or commit them. `logs/token_new.log` must stay untracked.
- **Tests:** don't weaken them. Fakes are allowed only at the Modal gateway boundary in unit tests. Real JWT verification is required in auth tests.
- **Code artifacts:** no plan IDs, phase numbers or finding codes in code comments, migration names, test names or commits.
- **Folio's server:** never touch Folio's containers, compose project, env, cron, volumes or Caddy. The one exception is the tunnel ingress rule insertion.
