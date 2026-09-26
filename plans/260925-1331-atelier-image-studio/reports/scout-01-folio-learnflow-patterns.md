# Scout Report: Folio & LearnFlow Patterns for Atelier

## 1. Claude Plugin Structure

**Folio plugin** at `/Users/sweet-home/Works/folio/folio-plugin/`:

| Component | Pattern | File:Line | Reuse as |
|---|---|---|---|
| **Plugin metadata** | `name`, `version`, `description`, `keywords` in frontmatter | `plugin.json:2-18` | Atelier plugin.json template |
| **MCP server setup** | Command: `uv run --script mcp_servers/folio_mcp/server.py`; env vars passed via `.mcp.json` | `.mcp.json:4-9` | Atelier .mcp.json: pass env via `"env"` dict, use `uv run --script` |
| **PEP 723 header** | `requires-python=">=3.10"`, pinned mcp `<2` (mcp 2.x API change), httpx, pydantic | `server.py:2-12` | Atelier server.py: pin mcp to 1.x, include httpx + pydantic |
| **Auth state singleton** | `_Auth` class with async `get_access_token()`, auto-refresh 60s before expiry, retry on 401 | `server.py:124-187` | Atelier auth: singleton pattern for JWT refresh logic |
| **HTTP client** | `httpx.AsyncClient()` in `_request()`, with Bearer token in header, 401-retry auto-login | `server.py:193-235` | Atelier HTTP layer: use httpx.AsyncClient, retry on 401 |
| **Error handling** | `_format_error()` returns dict with status, method, url, body, `_error_hint()` with HTTP-status-specific guidance | `server.py:238-286` | Atelier errors: status-code-mapped hints in error dicts |
| **File upload/download** | `_read_upload()` validates size/mime, returns dict with `files`; `_download()` streams to disk, handles content-disposition | `server.py:328-342, 313-325` | Atelier file I/O: stream uploads via multipart, resolve save paths from headers |
| **Tool signatures** | Pydantic `BaseModel` per tool, `model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)`, annotations dict on `@mcp.tool()` | `server.py:414-427` | Atelier tools: strict Pydantic models, read-only/destructive/idempotent hints |
| **Full tool list** | 35 tools covering projects, invoices, payments, tasks, notes, tags, labor, billing, docs, library | `README.md:17-48` | Atelier capability scope: map to 10-20 core tools first |
| **Skill structure** | Frontmatter (name, description), numbered steps (research, extract, ask, create, verify), error handling | `invoice/SKILL.md:1-128` | Atelier skill: frontmatter → workflow steps → error cases |

**folio.plugin zip** at `/Users/sweet-home/Works/folio/folio.plugin`:
- Entries: `.claude-plugin/plugin.json`, `.mcp.json`, `mcp_servers/folio_mcp/server.py`, `skills/invoice/SKILL.md`, `README.md` | `unzip -l:Lines 2-10` | Atelier package: include plugin.json, .mcp.json, server.py, skills/, README |

## 2. Deploy Scripts (Folio)

**`/Users/sweet-home/Works/folio/scripts/deploy/`:**

| Pattern | File:Line | Reuse as |
|---|---|---|
| **SHA validation** | Whitelist 7-40 hex chars; guard against injection | `deploy-runner.sh:22-23` | Atelier deploy-runner: `[[ "$SHA" =~ ^[0-9a-f]{7,40}$ ]]` check |
| **Compose setup** | Load multiple compose files + env file; set `IMAGE_TAG` env var | `deploy-runner.sh:27` | Atelier deploy: `docker compose -f yml -f prod.yml --env-file` |
| **Pre-swap migrations** | Run `flask db upgrade` BEFORE swapping container (Y5 pattern: api & worker share image) | `deploy-runner.sh:42-47` | Atelier DB migrations: run before container swap |
| **Health polling** | Check `docker inspect` State.Status & Health.Status; retry 30× with 5s sleep; log final state+health | `wait-healthy.sh:17-33` | Atelier health check: query docker inspect, timeout+logs on failure |
| **Rollback chain** | Query Artifact Registry for previous SHA tags (skip latest/stable); auto-detect revision label; pull old image & swap both api+worker | `rollback.sh:26-58` | Atelier rollback: get prior SHA from AR labels, swap with --no-deps |

## 3. Docker Compose Security & Structure

**`docker-compose.yml` & `docker-compose.prod.yml`:**

| Pattern | File:Line | Reuse as |
|---|---|---|
| **Secret-required vars** | Use `${VAR:?required}` to FORCE failure if unset; no fallbacks in prod | `docker-compose.yml:10-30` | Atelier compose: all secrets must use `:?required` syntax |
| **Loopback-only ports** | Prod: bind to `127.0.0.1:PORT` only; dev uses `0.0.0.0` via dev override; cloudflared/SSH tunnel reaches them | `docker-compose.prod.yml:29, 82, 89` | Atelier prod compose: all services port `127.0.0.1:PORT` |
| **Service dependencies** | Use `depends_on: {db: {condition: service_healthy}}` to gate startup | `docker-compose.yml:31-37` | Atelier compose: gate api/worker on db+redis health |
| **Healthchecks** | API: HTTP check on /health; Postgres: `pg_isready`; Redis: `redis-cli ping`; MinIO: `curl /minio/health/live`; Worker: disable (no HTTP port) | `docker-compose.yml:38-43, 83-87, 96-100, 113-117, 53-54` | Atelier healthchecks: use built-in probes per service type |
| **Restart policy** | Prod compose uses implicit `always` (default); explicit in dev if needed | `docker-compose.prod.yml` (inherits defaults) | Atelier: rely on compose default restart=always for prod |
| **Network isolation** | db/redis ports removed entirely in prod via `ports: !reset []`; only docker network access | `docker-compose.prod.yml:82, 89` | Atelier network: remove non-essential service ports in prod |
| **Container users** | Postgres/Redis run as unprivileged (image defaults); no root escalation visible | `docker-compose.yml:70-118` | Atelier: use official images' default non-root users |
| **Logging** | Compose default is json-file; no explicit override needed in base (inherit docker daemon config) | `docker-compose.yml` (implicit) | Atelier: rely on docker daemon logging config for prod |
| **Env file loading** | Prod compose references `.env` via `--env-file /opt/folio/.env` (set by service-account-rendered template) | `docker-compose.prod.yml:1-19` | Atelier prod: load .env from Secret Manager template, never commit secrets |

## 4. Backup Scripts (Folio)

**`/Users/sweet-home/Works/folio/scripts/backup/`:**

| Pattern | File:Line | Reuse as |
|---|---|---|
| **Logging via journald** | All scripts use `/usr/bin/logger -t <tag>` to emit to journald; cron output goes to .log file + journald | `install-backup-cron.sh:19, pg-dump.sh:19` | Atelier backup: use `logger -t <job>` for dual file+journald logging |
| **Exit codes** | 0=success, 1=config/input error, 2=missing resource (container/file), 3=runtime failure (dump/restore), 4=upload failure | `pg-dump.sh:8-11` | Atelier backup exit codes: stratify by layer (config, resource, runtime, upload) |
| **Cron installation** | Install via `/etc/cron.d/folio-backups` with explicit PATH; logrotate configured in same script | `install-backup-cron.sh:15, 31-60` | Atelier cron: install to /etc/cron.d/, configure logrotate in same script |
| **Drift guard** | Compare source object count to last run; abort if < 95% threshold (prevents propagating corruption/wipe) | `minio-mirror.sh:20, 52-58` | Atelier backup guard: track last source count, refuse mirror on >5% drop |
| **Idempotent within day** | pg_dump uploads to `gs://bucket/pg-dumps/${DATE}.dump` (same DATE = overwrite via versioning) | `pg-dump.sh:38-39` | Atelier backup: use date-based keys, rely on GCS versioning for history |
| **Restore verification** | Weekly: download dump, spin sidecar Postgres, pg_restore, run SELECT 1, tear down; log failures for alerting | `verify-latest-dump.sh:1-90` | Atelier backup verify: weekly restore into sidecar, verify schema queryable |
| **Service account roles** | backup-sa is append-only (no read/list); vm-runtime-sa has reader role for verify step | `pg-dump.sh:29-31, verify-latest-dump.sh:30-36` | Atelier IAM: separate read-only SA for verification step |
| **Multipart streaming** | pg_dump → pipe → gcloud storage cp (no temp file); mc mirror S3 ↔ GCS directly (S3-compatible auth) | `pg-dump.sh:50-54, minio-mirror.sh:40-68` | Atelier backup: stream pipe for DB, use mc for S3↔GCS |

## 5. Deploy Workflow (Folio)

**`.github/workflows/deploy-backend.yml`:**

| Pattern | File:Line | Reuse as |
|---|---|---|
| **Concurrency group** | `concurrency: {group: deploy-prod-backend, cancel-in-progress: false}` ensures sequential deploys | `deploy-backend.yml:39-41` | Atelier workflow: use concurrency group to prevent parallel deploys |
| **Permission split** | build job has `contents: read` only; separate bump-pointer job has `contents: write`, gated on build+smoke success | `deploy-backend.yml:63-65, 271-275` | Atelier CI: split write-scoped jobs from untrusted build steps |
| **Tag verification** | Verify version↔SHA tag exists; short-match SHAs (7-40 chars can match full commit or tag) | `deploy-backend.yml:107-131` | Atelier deploy: verify release tag before building |
| **WIF auth** | Use Workload Identity Federation (OIDC) instead of JSON keys; `google-github-actions/auth` | `deploy-backend.yml:133-139` | Atelier deploy: use WIF for keyless GCP auth in workflows |
| **Build + push** | docker/build-push-action: tag with both SHA and `:latest`; set OCI labels (revision, version, source) for AR introspection | `deploy-backend.yml:148-162` | Atelier image: tag SHA + latest, set org.opencontainers labels |
| **Health gating** | Smoke test `/health` endpoint; retry 5×; check JSON shape to catch CF error pages (200 with HTML) | `deploy-backend.yml:225-243` | Atelier smoke: test /health 5× with JSON shape validation |
| **Smoke timeout** | `--max-time 10` per curl; total 25s timeout for 5 tries | `deploy-backend.yml:231` | Atelier smoke: short timeout (10s) per try, overall ~25s budget |
| **Submodule strategy** | Checkout parent, checkout submodule at SHA, verify tag, build, push, deploy, smoke, then bump parent's submodule pointer | `deploy-backend.yml:86-223` | Atelier deploy: if using submodules, bump pointer only after smoke success |

## 6. LearnFlow Deployment

**`/Users/sweet-home/Works/learnflow/.github/workflows/deploy.yml`:**

| Pattern | File:Line | Reuse as |
|---|---|---|
| **Simple push-to-deploy** | On every push to main: npm ci → build → rsync dist/ to Hetzner | `deploy.yml:3-26` | Atelier deploy: simple workflow for static assets (npm build → rsync) |
| **rsync-deployments action** | Use `burnett01/rsync-deployments@v9` with SSH key; switches: `-az --delete` | `deploy.yml:18-26` | Atelier static deploy: rsync for asset sync, --delete for cleanup |

**LearnFlow `SKILL.md`:**

| Pattern | File:Line | Reuse as |
|---|---|---|
| **Deploy verification** | Verify via `gh run list --limit 1` checking for "completed success"; do NOT fetch live URL (Cloudflare Access returns 302 to login) | `SKILL.md:150-157` | Atelier skill: verify deploy via gh CLI, not live URL checks behind auth |
| **Skills as explicit guides** | Frontmatter + numbered workflow steps; ALWAYS ship (no "files written" without publish) | `SKILL.md:1-170` | Atelier skill: structure as step-by-step workflow with final publish gate |

**LearnFlow `README.md`:**

| Pattern | File:Line | Reuse as |
|---|---|---|
| **Server setup** | One-time: Caddy install, web root, Caddyfile copy; Caddy handles auto-HTTPS (no manual cert) | `README.md:54-65` | Atelier production: Caddy for reverse proxy + auto-HTTPS |
| **GitHub secrets** | HETZNER_HOST, HETZNER_USER, SSH_PRIVATE_KEY (public key in server ~/.ssh/authorized_keys) | `README.md:67-73` | Atelier CI secrets: Host, user, SSH key for rsync deploy |

## 7. Python Backend Conventions (Folio)

**`docs/code-standards-backend.md`:**

| Pattern | Lines | Reuse as |
|---|---|---|
| **Hexagonal layout** | `app/{api,application,domain,infrastructure}` with `wiring.py` DI container | Lines 61-72 | Atelier backend: domain → application → infrastructure → api layer split |
| **Token security** | Generate via `secrets.token_urlsafe(32)`, store SHA-256 hash, compare via `hmac.compare_digest()`, raw token returned once only | Lines 40 | Atelier tokens: hash + hmac pattern for invite/reset tokens |
| **Test split** | `tests/{unit,integration,api}` mirrors code layers; pytest for test runner | Inferred from code standards | Atelier tests: pytest with unit/integration/api directories |
| **Lint & type** | `ruff check .` + `mypy app` (Folio's choices from CLAUDE.md) | CLAUDE.md:43 | Atelier Python: ruff for lint, mypy for type checking |
| **Migration tool** | Flask-Migrate (Alembic) via `flask db migrate` / `flask db upgrade` | deploy-runner.sh:46 | Atelier migrations: flask db upgrade in deploy-runner before swap |
| **Naming conventions** | Use kebab-case for file names; CamelCase for classes; snake_case for functions | Inferred | Atelier Python: follow Python conventions (PEP 8) |

---

**Summary:** Atelier should copy Folio's **modular plugin + MCP pattern** (PEP 723 header, httpx async auth, file streaming), **Folio's deploy CI/CD** (WIF, concurrent concurrency, smoke testing, submodule pointer bump), **Folio's security posture** (loopback-only ports, required env vars, separate backup SA), **backup + restore verification logic** (drift guards, weekly verify), and **Folio's hexagonal Python backend** if Atelier has a backend component.

