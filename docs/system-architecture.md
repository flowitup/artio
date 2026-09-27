# Artio — system architecture

Artio is one FastAPI service (a single uvicorn worker), HTMX for the owner's browser UI, SQLite for
all state, and Modal for GPU rendering. It runs in a confined Docker container on `folio-prod-1`
(shared with Folio, cdn and LearnFlow) and is published at `artio.flowitup.com` through Cloudflare's
existing tunnel and Access. There are no backups: the data volume holds the only copy of the images
and the database (owner decision, 2026-09-27).

## Components

```
Owner's browser ──┐                                    ┌─► ComfyUI on Modal GPU (per model backend)
Claude (plugin) ───┼─► Cloudflare Access ─► FastAPI app ─┤
curl (blocked) ────┘         (JWT)              │        └─► SQLite (artio.db)
                                                 └─► static files, Jinja2 templates
```

- **`artio/main.py`** -- the app factory: builds `Settings`, wires the middleware stack, the
  routers and the `Worker`, and runs migrations at startup.
- **`artio/auth.py`** -- verifies the Cloudflare Access JWT and maps it to one of exactly two
  identities (below); the one auth middleware every request (except `/healthz`) passes through.
- **`artio/routes/`** -- HTML routes (`pages`, `generate`, `jobs`, `images`, `gpu`, `library`,
  `workflows`) plus the versioned JSON API (`api_v1`). Every route calls the same service layer.
- **`artio/jobs.py`, `artio/library.py`, `artio/custom_workflows.py`, `artio/gpu.py`,
  `artio/storage.py`** -- the service layer: batches and jobs, the gallery/search/preset/tag
  library, custom ComfyUI workflow storage and validation, GPU status and warm/stop, and PNG/thumbnail
  storage plus the disk guard. Both the HTML routes and the JSON API call these directly -- there is
  exactly one implementation of "create a batch" or "search images", never two.
- **`artio/worker.py`** -- the background engine: one dispatcher loop (spawns queued jobs onto
  Modal, respecting each backend's `max_inflight`) and one poller loop (reads finished/failed results
  and stores them), plus the warm-up pinger and the Stop sequence. Runs in-process; a restart just
  means both loops re-derive their state from the database.
- **`artio/modal_gateway.py`** -- the only module that talks to Modal (`modal.Cls.from_name`,
  `.spawn()`, `.get(timeout=0)`, `app list`, `container list`). Every other module reaches Modal only
  through this boundary, which is exactly what tests replace with `FakeModalGateway`.
- **`artio/registry.py`** -- the model-neutral registry: which backends and models exist, their
  parameter bounds and size presets, and which Modal app/class backs each one. Adding a model is a
  registry entry plus a Modal backend, never a migration.
- **SQLite (`artio.db`)** -- `batches`, `jobs`, `images`, `tags`/`image_tags`, `presets`,
  `workflows`, `backend_state` and an FTS5 index (`images_fts`) over prompt, negative prompt and tags.
  One connection per request (`db.session`), WAL mode, `busy_timeout`.

## Auth and route authorization

The origin verifies the Access JWT itself (signature, `aud`, `iss`, `exp`, RS256 only) via
`AccessVerifier`, then maps its claims to one of exactly two identities:

- **owner** -- the JWT's `email` matches `ARTIO_OWNER_EMAIL` (case-insensitive).
- **service** -- the JWT's `common_name` matches `ARTIO_PLUGIN_CLIENT_ID` (the Cloudflare Access
  service token's Client ID), compared with `hmac.compare_digest`.

Any other JWT, or none, is refused with a 403 before any route runs -- plain text for an HTML route,
the same `{"error": {"code","message"}}` envelope as every other `/api/v1` error for an API path (an
owner decision: a JSON client should never have to special-case one status code's body shape). The
body-size limiter's 413 follows the same rule. Authorization is then route-based, not identity-based
alone:

- **HTML routes and static files are owner-only.** The service identity gets 403 on every one of
  them, proven by the route-enumerating crawl (`test_service_identity_is_refused_outside_the_api_allowlist`).
- **The service identity may call only `auth.SERVICE_ROUTES`**, exactly the nine `/api/v1` endpoints
  in the table below. Nothing else -- no warm, no stop, no upload, no delete, no HTML page.
- **The owner identity is not specially restricted from `/api/v1`.** A GET works there exactly like
  any other owner GET; a state-changing call still needs the same same-origin (CSRF) check every
  owner POST already requires, API included. There is no separate rule for the API here: it is simply
  never blocked, the same as any other route the owner identity can already reach.
- **CSRF:** every owner POST/PUT/PATCH/DELETE must have an `Origin` (or, failing that, a `Referer`)
  that matches `ARTIO_PUBLIC_ORIGIN`. Service-identity calls come from the plugin process, not a
  browser, so they carry no such header and are not a CSRF vector; the origin check applies only to
  the owner identity.

### `/api/v1` -- criterion 10's nine endpoints

| Method and path | Calls |
|---|---|
| `GET /api/v1/models` | `registry.models` |
| `POST /api/v1/generate` | `jobs.create_batch` |
| `GET /api/v1/jobs?ids=` | `jobs` table, by id |
| `GET /api/v1/images` | `library.search` |
| `GET /api/v1/images/{image_id}` | `library.image_detail` |
| `GET /api/v1/images/{image_id}/file` | `storage.resolve_under`, confined to `data_dir/images/` |
| `GET /api/v1/workflows` | one query over `workflows`, parsed off the event loop |
| `POST /api/v1/workflows/{workflow_id}/run` | `jobs.create_workflow_batch` |
| `GET /api/v1/gpu` | `worker.status.get` (the same on-read status the owner panel uses) |

Every error is `{"error": {"code", "message"}}`, mapped from the service layer's own exceptions:
`ValidationError`-shaped input (pydantic, `extra="forbid"`) and domain errors (`UnknownModel`,
`InvalidParams`, a bad `seed_mode`/`count`) become 422; `DiskGuardError` becomes 507; an unknown image
or workflow id becomes 404. `GET /api/v1/gpu` is deliberately read-only: there is no `POST` route
under `/api/v1/gpu` at all, so the plugin can see status but can never warm up or stop a backend.

## Job lifecycle

A batch (`generate`, or a stored workflow's `run`) inserts one `batches` row and one `jobs` row per
seed, all `queued`. The dispatcher spawns up to `max_inflight` queued jobs per backend onto Modal
(`spawn_workflow`, an async call id) and marks each `submitted`. The poller reads every submitted job's
call id with `get(timeout=0)`; the pending signal is the **builtin** `TimeoutError`, not
`modal.exception.TimeoutError`. A finished result is decoded, stored as a PNG plus a WebP thumbnail,
and recorded (`done`) with an `images` row; a failed one is recorded `failed` with ComfyUI's own error
text. A user cancel marks a job `cancelled` in the database only -- it never touches Modal, so a
render already running is simply discarded on completion (rowcount-checked, so a late result can never
resurrect a cancelled job). Every state transition is a single `UPDATE ... WHERE status IN (...)`,
so two writers racing the same row can never both believe they made the transition.

## GPU control

Status is computed **on read**, never polled in the background: a 10 s cache for Modal's function
stats and a 60 s cache for the app's deployment state, at most one real refresh in flight per backend.
"Warm" is shown only after a successful ping (not merely because a warm-up window is open); a failed
ping marks the backend unhealthy and feeds a circuit breaker that recycles a backend whose ComfyUI died
(three first-poll connection failures, or a failed ping). Warm-up runs a pinger task only while a
window is open (5/15/30 minutes, extendable, never shortened by a smaller click); Stop cancels every
tracked call at once (the warm-up ping first), stops the backend's containers, and waits up to about a
minute for its runner and backlog counts to reach zero. The plugin's `/api/v1/gpu` reads this same
status; it has no path to warm or stop anything.

## Storage, disk guard and backups

Images live under `data_dir/images/YYYY/MM/job-<id>.png` (as Modal returned them) plus a normalized
WebP thumbnail; paths are always resolved back under `data_dir` before any read, write or delete
(`storage.resolve_under`), so a corrupted row or a planted symlink can never escape it. The disk guard
(`storage.disk_status`) refuses a new batch when the volume's free space drops under a floor
(`ARTIO_MIN_FREE_GB`) or stored image bytes reach a cap (`ARTIO_DATA_CAP_GB`), both configurable
and shown in the header on every page.

**There are no backups.** The owner decided on 2026-09-27 that Artio keeps no copy beyond its own
data volume: the originally planned restic-to-R2 pipeline, weekly verify, restore runbook and purge
procedure were dropped along with that phase. If the volume is lost or deleted, the images and the
database are gone; anything worth keeping must be downloaded from the gallery ahead of time.

## Deploy

Every push to `main` builds a digest-pinned image and deploys it to `folio-prod-1` through a single,
purpose-restricted SSH key (`restrict,command="/opt/artio/deploy.sh"`). The deploy step validates the
pulled image's revision label, declared volumes and size before touching anything running, applies it
inside a transient systemd unit (so a dropped connection can't interrupt it), and requires `/healthz`
to report the right version and both worker loops ticking before committing -- rolling back
automatically otherwise. `deploy.sh stop | start | rollback | status` is the only supported manual path.
See `docs/deployment-guide.md` for the full setup, the credential table, and the routine-operations
runbook (GPU control, disk usage, backend-unhealthy recovery, token rotation).
