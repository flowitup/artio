# Brainstorm: Atelier, a private multi-model image studio on Hetzner

Date: 2026-09-25 · Status: contract accepted (decisions below were chosen by the owner over two rounds) · Next: `/ak:plan`, then `/ak:cook`

## Decisions

| Topic | Decision |
|---|---|
| Name | **Atelier**: `atelier.flowitup.com`, repo `flowitup/atelier`. The owner asked for a model-neutral name because more models will be added. |
| GPU | Stays on Modal, one backend per model. v1 ships one model, the existing `qwen21-uc` / `Qwen21UC` (L40S, scales to zero). |
| Host | `folio-prod-1` (Hetzner), the Folio production server, which also serves LearnFlow (confirmed by the owner). |
| Ingress | The server's existing Cloudflare Tunnel plus Cloudflare Access (team `flowitupteam`). No new public ports. |
| Stack | One Python service: FastAPI + HTMX + SQLite, one container, native Modal SDK. |
| Users | Only the owner. |
| v1 scope | Core (prompt form, job queue, gallery with settings, download, delete), a model registry, GPU status plus warm-up and stop buttons, batch and variations, prompt library, custom ComfyUI workflows, a Claude plugin like Folio's, and nightly backups. |

## Contract

### Outcome
Atelier is a private web app at `atelier.flowitup.com`. In it the owner:
- picks a model (Qwen-Image 2.1 UC in v1) and runs single or batch generations;
- watches the job queue;
- browses, searches, stars, tags, downloads, remixes and deletes results along with their full settings;
- sees whether each Modal GPU backend is warm, scaled to zero or stopped, and can warm it up or stop it;
- runs uploaded ComfyUI API-format workflows.

Claude can drive Atelier through a plugin. Data is backed up every night, encrypted and off-site. Pushing to `main` deploys to Hetzner automatically, and changes to a Modal script redeploy it on Modal.

### Constraints
- **Model-neutral design.** Models are entries in a registry: label, Modal app and class, workflow template, parameter schema with size presets and defaults, and GPU $/h. Jobs and images record their model. Adding another ComfyUI-based model means adding a registry entry and a backend, with no database migration. The app builds workflows from the registry template (the logic of today's `build_workflow`) and calls the backend's existing `run_workflow`.
- **The GPU stays on Modal.** Hetzner Cloud has no GPU server types, and the GEX dedicated GPU servers cost €184+ a month.
- **Folio's production server is shared.**
  - Atelier runs as its own compose project and network. It is not root inside the container, has no Folio volumes or env, has no Docker socket, and is bound to 127.0.0.1.
  - Its data sits under its own directory with a hard size cap and a free-disk floor, so it can never fill the disk that Folio's Postgres and MinIO use. If the disk turns out to be small, the data moves to a Hetzner Volume.
  - Every change on this server needs the owner's explicit go-ahead at deploy time: Docker, cron, users, and the tunnel config.
  - The tunnel config must be backed up and validated before cloudflared restarts, because a broken config takes folio.flowitup.com offline.
- **The app is private.** Cloudflare Access lets in the owner's email and one service token (for the Claude plugin). The app also verifies the `Cf-Access-Jwt-Assertion` JWT (team certs plus app AUD) and returns 403 without it.
- **Secrets stay on the server.** Modal, R2 and restic credentials live only in the server env file and GitHub secrets. The restic password is also kept outside the server (in the owner's password manager), because backups are unrecoverable without it.
- **Jobs are asynchronous.** A cold start takes 68 s and Cloudflare times out origin requests at 100 s. The app calls `spawn()`, stores the call ID in SQLite, polls `FunctionCall.from_id(id).get(timeout=0)`, and resumes polling after a restart. It keeps at most 4 calls in flight per backend (`max_inputs=4`; the GPU renders one image at a time, about 16 s warm).
- **The app always chooses the seed.** `generate(seed=-1)` does not return the seed it picked.
- **Warm-up must fail safe.**
  - "Keep warm for N minutes" sends a tiny `ping()` to the backend every 30–45 s. If Atelier stops, the container scales down within `scaledown_window` (60 s).
  - Atelier does not use `min_containers=1`, which would keep billing at $1.95 an hour if Atelier crashed.
  - "Stop" cancels or blocks in-flight jobs first, then terminates the backend's containers (`modal container stop`). Terminating mid-job would otherwise just reschedule the job and start a new cold container.
- **Backups run nightly** at 04:30 UTC, after Folio's 03:00–04:00 jobs. A consistent `sqlite3 .backup` plus the image directory are sent to Cloudflare R2 with restic (encrypted, deduplicated). Retention is 7 daily, 4 weekly and 6 monthly snapshots. The backup is restore-tested every week, and the last backup's status shows in the UI.
- **License:** the Qwen Research License is non-commercial, so this stays a personal tool.

### Non-goals
- Adding a second model in v1 (the registry makes it possible later), video generation, and running models on Hetzner hardware.
- Public access, sign-up, multiple users, or payments.
- Changing the Qwen model, workflow or sampler. The only change to the Modal script is a no-op `ping()` method for warm-up.
- Changing Folio's app, data or backups, or LearnFlow, apart from adding one ingress rule to the shared tunnel config.

### Acceptance criteria
1. **Access:** opening `atelier.flowitup.com` without a session redirects to the `flowitupteam` login. A request that reaches the container without a valid Access JWT gets a 403.
2. **Jobs:** a job goes queued → running → done. A cold-start job finishes without a 524. Every image keeps its model, prompt, negative prompt, seed, size, steps, cfg, duration and estimated cost. Images survive a reload and a container restart, including a restart in the middle of a job.
3. **Determinism:** the same model, seed and settings give a visually identical image.
4. **Errors:** a failed ComfyUI run shows its error and can be retried. A queued or running job can be cancelled.
5. **Models:** the model picker lists the registry, and the gallery can filter by model. A test registers a second model entry without any schema change.
6. **GPU status:**
   - Status: each backend shows deployed or stopped (from `modal app list`), warm containers, running inputs and backlog (from `get_current_stats()`), refreshed automatically.
   - Warm up: "warm up" makes the backend show as warm within about 70 s and keeps it warm for the chosen period, then it scales to zero within about 60 s. Killing Atelier while it is warm also lets it scale to zero.
   - Stop: "stop" takes the backend to 0 containers within seconds, after confirmation if jobs are running.
7. **Batch and variations:** one submit with N seeds and a size preset queues N jobs with distinct seeds, all finish, and they are grouped in the gallery. "Remix" fills the form from an image.
8. **Prompt library:** you can save, load and delete presets. You can star and tag images and search them by prompt text.
9. **Custom workflows:** you can upload an API-format JSON, store it under a name for a chosen backend, and run it. The result is saved with the workflow attached. An invalid graph shows ComfyUI's validation error.
10. **Claude plugin:** from Claude you can list models, generate (and wait), check status, list or search images, fetch an image to a local file, run a stored workflow, and see GPU status. It authenticates with the Access service token, and calls without it are rejected.
11. **Backups:** the nightly snapshot appears in R2. The weekly check restores the database to a temporary directory and verifies the image count against it. A documented full restore has been tested once. A failure shows in the UI.
12. **Disk guard:** once the data cap or free-disk floor is reached, new jobs are refused with a clear message. Disk usage is visible in the UI.
13. **Deploy:** pushing to `main` builds the image and deploys to `folio-prod-1`, and a health check over SSH passes. folio.flowitup.com, cdn.flowitup.com and learn.flowitup.com still respond after the tunnel change. Changes under `modal/` run `modal deploy`.
14. **Secrets:** no credential appears in the repo, the image or any page.

### Trade-offs
- **Web stack.**
  - *One Python service (chosen)* relies on the UI staying at the level of forms, a queue and a gallery. It breaks down first if a rich editor is needed (masks, canvas).
  - *Next.js + shadcn* relies on Folio's look being worth two languages and HTTP endpoints on Modal.
  - *A Folio-style split* relies on many users or heavy volume, and breaks down first on how much there is to run for a single-user tool.
- **Warm-up mechanism.**
  - *Pings (chosen)* rely on Atelier running while you want the GPU warm. They fail safe: they stop when Atelier stops.
  - *`update_autoscaler(min_containers=1)`* needs no Modal change, but a crash or failed revert leaves the GPU billing at $1.95 an hour until someone notices.
- **Backup target.**
  - *restic → R2 (chosen)* relies on one new R2 bucket and token. It is independent of Folio's GCP project and encrypted on the server before upload. It breaks down first if the restic password is lost.
  - *Reusing Folio's GCS path* relies on how gcloud authenticates on this Hetzner box, which is unverified. It also couples Atelier to Folio's backup identity.
- **Data location.**
  - *Server disk with a cap (chosen)* relies on the disk having room. It breaks down first on a small disk.
  - *A Hetzner Volume* isolates the data completely, but needs Folio's Hetzner project, which is not in the local hcloud contexts.

### Better approaches
Keeping the GPU on Modal is better than porting the script to Hetzner hardware, for the cost and memory reasons already given. For ingress, the Cloudflare Tunnel already on the box is better than Caddy on public ports: nothing new is exposed.

## Evidence
- **Modal:**
  - `Cls.from_name("qwen21-uc","Qwen21UC")().generate.get_current_stats()` returns `FunctionStats(backlog=0, num_total_runners=0, num_running_inputs=0, input_headroom=0)`.
  - `modal app list --json` shows state `deployed` with 0 tasks.
  - SDK 1.5.5 has `Obj.update_autoscaler`, `FunctionCall.from_id` and `.cancel`.
  - `modal container stop` terminates a container, and its running inputs are rescheduled.
- **`qwen21_uc_app.py`:**
  - `generate()` at lines 140–145 hides the seed it picks.
  - `run_workflow()` at 147–150 returns the first image only.
  - Lines 99–100 set `max_containers=1`, `max_inputs=4` and `scaledown_window=60`.
  - Weights are 7.26 + 9.35 + 0.68 GB.
  - A cold render took 68.3 s and a warm one 15.6 s.
- **Folio on Hetzner since 2026-07-15** (`folio/impl/*.html`):
  - The tunnel config is `infra/cloudflare/cloudflared-config.yml`, installed at `/etc/cloudflared/config.yml`. It routes folio.flowitup.com to :5000 and :3000 and cdn.flowitup.com to :9000, ending in a 404 catch-all. The repo copy has no `learn.flowitup.com` rule, so the live file has drifted and must be read before editing.
  - The backups are `scripts/backup/*`: a root cron job at 03:00–04:00 UTC doing a Postgres dump and a MinIO mirror to GCS through an append-only `backup-sa`.
- **LearnFlow:**
  - It deploys to `folio-prod-1` (owner). Its secrets `HETZNER_HOST`/`USER` were updated on 2026-09-04, and deploys have succeeded since.
  - It sits behind Access: requests get a 302 to `flowitupteam.cloudflareaccess.com`.
- **Folio's Claude plugin** is a local stdio MCP server run with `uv run --script`.
- **Hetzner:**
  - Cloud has no GPU types (`hcloud server-type list`).
  - GEX44 costs €184–234 a month.
  - The `learnflow` hcloud context contains no servers.

## Owner actions (prerequisites)
1. **Cloudflare:**
   - Create the Access application for `atelier.flowitup.com` with an email policy plus a Service Auth policy, and note its AUD tag.
   - Create the service token for the plugin.
   - Create the R2 bucket and an API token scoped to that bucket.
2. **Modal:** create a dedicated token for the server.
3. **GitHub:** approve creating `flowitup/atelier` and set its deploy secrets. The server host is the same one LearnFlow uses.
4. **Server:** approve each change on `folio-prod-1`: the deploy user and Docker access, the tunnel ingress rule plus its DNS route, the backup timer, and the env file.
5. **Restic:** store the restic password in the password manager.

## Open items for the plan (not blocking)
- Check the live disk size and free space on `folio-prod-1`. Decide the data cap, or whether to use a Volume.
- Check whether the deploy user can run Docker, and whether LearnFlow's route runs through the tunnel or through Caddy.
- Decide how images are shipped: GHCR pull (needs a read token on the server) or a build on the server.
- Unrelated to Atelier: the SSH alias `dev-deploy` (46.224.60.209) now presents a new host key. Check it separately.

## Amendments (2026-09-25, after the red-team review; owner decisions)
The plan `plans/260925-1331-atelier-image-studio/` is the execution authority. These amendments keep this contract consistent with it.
1. **Data location.** The fallback applies. `folio-prod-1` has 21 GB free on `/`, so Atelier's data lives on a dedicated Hetzner Volume (50 GB default, confirmed in validation).
2. **LearnFlow.** The non-goal is narrowed. LearnFlow's deploy key will be restricted to rsync into `/var/www/learnflow` with a forced command, and its deploy action pinned by commit, after a read-only audit of root's SSH keys. This is needed because any unrestricted root key on the shared host can read Atelier's data.
3. **Modal access.** Stay in the same workspace, with separate tokens for the server and CI, a workspace spend limit with alerts, and an inventory of the legacy endpoint's proxy-auth tokens.
4. **Plugin.**
   - Thumbnails are off by default and opt-in per call.
   - The plugin has no GPU warm-up or stop, which stays within criterion 10.
   - Its service token reaches only an allowlist of `/api/v1` endpoints.
   - Saved files stay inside one private folder.
5. **Cancel semantics.** Cancelling a running job marks it cancelled in Atelier and discards its result. The GPU may still finish that render. Calling Modal's cancel on this backend would shut down the shared container and restart its other jobs from a cold start. Criterion 4 is unchanged.
6. **Timeout.** Cloudflare's documented proxy read timeout is 125 s, not 100 s. The asynchronous job design is unchanged.
7. **Tunnel change.** A replica cutover replaces the plain restart, so Folio, cdn and LearnFlow stay up during the change.
8. **Modal script (validation).** The non-goal is narrowed. Besides the new `ping()`, two small changes are allowed. `ping()` checks ComfyUI's `/system_stats`, so "warm" means ComfyUI is ready. The unused legacy public `api` web endpoint is removed. The model, workflow and sampler stay unchanged.
9. **Defaults (validation).**
   - A 50 GB volume, a 40 GB image cap and a 5 GB free-space floor.
   - Size presets: 9:16 (1088×1920), 16:9 (1920×1088) and 1:1 (1328×1328). Custom sizes are always possible.
   - The Access service token lasts 1 year, with a rotation reminder, and is revoked immediately if it leaks.
