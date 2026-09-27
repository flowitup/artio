---
phase: 3
title: "Web UI & Access auth"
status: completed
priority: P1
effort: "11h"
dependencies: [2]
---

# Phase 3: Web UI & Access auth

## Context Links

- Contract criteria 1, 2, 4, 5, 7, 12 and 14, and the "app is private" constraint: [brainstorm report](../reports/brainstorm-260925-1507-atelier-image-studio-hetzner.md)
- Brief §4 (UI polling, remix) and §5 (auth, dev mode, CSRF): [architecture brief](./reports/architecture-brief.md)
- Access JWT facts (header then cookie, JWKS URL, `common_name` on service tokens, never require `email`): [research-01 §1–2](./research/researcher-01-cloudflare-access-tunnel-restic-r2.md)
- HTMX polling with a 286 stop, and classless CSS instead of a Tailwind build: [research-02 §7](./research/researcher-02-modal-sdk-app-plugin.md)
- Red-team evidence: [security](./reports/red-team-security-adversary.md) (Findings 4 and 8, fact checks 9 and 15), [scope](./reports/red-team-scope-complexity-critic.md) (Findings 4 and 9), [failure modes](./reports/red-team-failure-mode-analyst.md) (Finding 6).
- Verified library facts: PyJWT 2.14.0 `jwt/jwks_client.py:39-40` (`cache_jwk_set=True`, `lifespan=300`), `:146` `fetch_data()` and `:269` `get_signing_key_from_jwt()`; `jwt/exceptions.py:96` (`PyJWKClientError` subclasses `PyJWTError`).
- Engine from phase 2: `atelier/jobs.py`, `atelier/worker.py`, `atelier/storage.py`, `atelier/registry.py`, `atelier/config.py` and `atelier/db.py`.

## Overview

When this phase is done, the owner can use Atelier in a browser:
- pick a model and size preset;
- submit single or batch jobs with random or fixed seeds;
- remix an image;
- watch the queue update live;
- cancel, retry, browse the gallery grouped by batch and filtered by model;
- open an image's full settings, then download or delete it.

Every route except `/healthz` requires a verified Cloudflare Access JWT, and a request without one gets a 403. HTML routes serve the owner only; the plugin's service identity is limited to an explicit `/api/v1` allowlist, which phase 8 fills. Priority P1. Status pending.

## Key Insights

<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->

- **Auth must be a middleware.** Mounted `StaticFiles` apps bypass FastAPI dependencies, and the brief requires every route except `/healthz` to be protected, so auth is an HTTP middleware.
- **JWKS fetches block.** `PyJWKClient.get_signing_key_from_jwt()` can fetch the JWKS over blocking HTTPS once the 300 s cache expires, so call it through `asyncio.to_thread`. The default caching absorbs Cloudflare's key rotation, which keeps old keys valid for 7 days (research-01 §1).
- **Two identities, with authorization by route.**
  - A user JWT passes when `email` matches the owner (case-insensitive). A service-token JWT passes when `common_name` equals the plugin's client ID (`hmac.compare_digest`). `email` is never required, because service-token JWTs don't carry it (research-01 §1).
  - Authentication alone is not authorization. HTML routes are owner-only. The service identity may call only an explicit allowlist of `(method, /api/v1 path)` pairs and gets a 403 everywhere else, including static files, delete and upload routes.
  - Cloudflare's `aud` claim is a list, and PyJWT accepts a list audience.
- **CSRF: the Origin check is the real protection.**
  - Owner POSTs whose `Origin` (or `Referer`, when `Origin` is absent) is not `ATELIER_PUBLIC_ORIGIN` get a 403.
  - Access's `CF_Authorization` cookie uses the SameSite value set on the Access application. Cloudflare's default is None, so it blocks nothing by itself; phase 4 sets it to Lax as an owner action, as a second layer.
  - Service-identity calls come from the plugin process, not a browser, so they are exempt from the Origin check, and the route allowlist bounds what they can reach.
- **Bodies are limited before parsing.** A small ASGI middleware checks `Content-Length` and counts streamed bytes, so an oversized upload is refused before multipart parsing spools it. The default is 64 KB; phase 7 raises it for the workflow upload path only.
- **HTML routes always answer 200.**
  - A 4xx response is not swapped by htmx 2's default `responseHandling`, so a partial sent with a 409 or a 422 silently disappears.
  - HTML handlers return 200 with the re-rendered form or partial, and use `HX-Retarget`/`HX-Reswap` to put an error where it belongs. Status-code semantics live only in `/api/v1`.
- **HTMX polling stops with a 286.** A 286 response cancels polling after a final swap. Cancel and retry therefore return the **whole** queue panel, including its `hx-trigger`, so polling restarts after a retry.
- **`/healthz` proves the engine is alive.** It reports the age of the last completed dispatcher and poller ticks and returns 503 when either is stale, so a release whose loops are broken fails the deploy health check (phase 4).
- **No CSS build step.** Research-02 §7 settles the "CSS build" in brief §9: a vendored, pinned `pico.min.css` plus a small hand-written `app.css`, with nothing else to run.
- **Single uvicorn worker.** uvicorn runs with `--factory atelier.main:create_app` and one worker, so the background loops exist exactly once. The factory takes `registry`, `gateway` and `start_worker` for tests.

## Requirements

<!-- Updated: Red Team 2026-09-25 - F4 error handling -->
<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->
<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

Functional:
- **Auth** (`auth.py` plus the middleware in `main.py`):
  - Read the token from `Cf-Access-Jwt-Assertion`, falling back to the `CF_Authorization` cookie.
  - Verify RS256 with keys from `{team}/cdn-cgi/access/certs`, plus `aud`, `iss` (the team domain) and `exp`, with 30 s leeway.
  - Allow only the owner email or the plugin client ID. Anything else gets a plain 403 "Forbidden"; the reason is logged, never echoed.
  - The service identity may call only the pairs in `SERVICE_ROUTES` (empty in this phase; phase 8 adds the criterion-10 API set). HTML routes and static files are owner-only.
  - `/healthz` is public.
  - A dev identity (`owner` or `service`) is honoured only when `ATELIER_ENV=development`.
- **Request size** (`request_limits.py`): bodies over 64 KB, or over a per-path limit, get a 413 with a one-line message before any parser runs.
- **Generate** (`GET /generate`, `GET /generate/params?model=`, `POST /generate`):
  - The form has a model picker from the registry, prompt, negative, size preset or custom width and height (multiples of 16, within bounds), steps, cfg, seed mode (random or fixed) with a seed field, and count N from 1 to 8.
  - It is a plain HTML form, not `hx-post`. The model picker swaps the parameter fields with `hx-get="/generate/params"`.
  - `?from=<image_id>` prefills the form from that image's job params with fixed seed mode (remix).
  - Validation and disk-guard errors re-render the page with the message and a 200 status.
  - Success redirects with a 303 to `/queue?batch=<id>`.
- **Queue** (`GET /queue`, `GET /queue/rows`, `POST /jobs/{id}/cancel`, `POST /jobs/{id}/retry`):
  - Rows show status (`queued`, "running" for `submitted`, `done`, `failed`, `cancelled`), model, prompt excerpt, seed, elapsed time, a thumbnail when done, the error text for failed jobs, and "waiting: <reason>" for queued jobs that have one.
  - The rows partial polls every 2 s and returns 286 when no job is queued or submitted.
  - Cancel and retry are `hx-post` buttons that return the whole panel with a 200.
  - Cancelling a running job marks it cancelled in the DB only. The row then says: "Cancelled. The GPU may still finish this render; its result will be discarded."
- **Gallery** (`GET /gallery?model=&page=`, `GET /batches/{id}`): batches newest first, each group headed by prompt excerpt, model, count and date, with its thumbnails. A model filter comes from the registry. 48 images per page.
- **Image** (`GET /images/{id}`, `GET /images/{id}/file`, `GET /images/{id}/thumb`, `POST /images/{id}/delete`):
  - The page shows model, prompt, negative, seed, width × height, steps, cfg, duration, estimated cost, created time (in `ATELIER_TIMEZONE`) and batch link.
  - Download returns the PNG with `Content-Disposition: attachment; filename="atelier-<id>-<seed>.png"`.
  - Remix links to `/generate?from=<id>`.
  - Delete is an `hx-post` with `hx-confirm="Delete this image? It is removed from Atelier now, with its prompt and job record; nightly backups keep a copy for up to 6 months."`.
    - It deletes the image row, its job row, and the batch if the batch has no job left, in one transaction, then the files.
    - On success it answers 200 with `HX-Redirect: /gallery`.
- **Header status** (`GET /partials/header-status`, polled every 10 s):
  - The disk badge shows "used / cap · free on volume", in red with the refusal reason when the guard trips.
  - Each backend's paused reason and permanent-error alert from the worker appear as a banner.
  - Phases 5 and 6 add their badges to this same partial.
- **Health:** `GET /healthz`:
  - 200 with `{"status":"ok","version":"<ATELIER_VERSION>","loops":"ok"}` when the DB answers `SELECT 1`, the volume sentinel exists in production, and both the dispatcher and poller completed a tick in the last 30 s.
  - 503 with `{"status":"degraded", …}` and `"loops":"stale"` otherwise.
  - When the worker was not started, which happens in tests only, `"loops"` is `"disabled"` and the status stays 200.

Non-functional:
- All routes are `async def`, and DB access goes through `db.session()`.
- Route path parameters use specific names (`image_id`, `batch_id`, `job_id`, and later `workflow_id`, `preset_id` and `backend_id`), so the route-enumerating crawl can fill each one from the `route_ids` fixture. This plan abbreviates them as `{id}`.
- Jinja2 autoescaping stays on. No inline `<script>`. htmx and Pico are vendored with pinned versions recorded in `atelier/static/VERSIONS.txt`.
- Pages never render any credential: Modal tokens, the Access AUD, the client ID or any backup secret.
- HTML handlers never return a 4xx or 5xx for an expected condition. `/api/v1` owns status-code semantics.

## Architecture

<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->

```
Browser ─► Cloudflare Access (login) ─► tunnel ─► 127.0.0.1:8090 ─► uvicorn (1 worker)
  body_size_limit (Content-Length + streamed count) ─► access_guard: verify JWT ─► identity
     ─► service identity? only SERVICE_ROUTES, else 403 ─► owner POST? Origin/Referer check ─► routers
  routers (pages, generate, jobs, images, health) ─► jobs / library / storage services ─► SQLite (WAL) + /data/images
  lifespan: db.migrate() ─► app.state.worker.run()  …  shutdown: worker.stop()
```

Auth (sketch):

```python
@dataclass(frozen=True)
class Identity:
    kind: Literal["owner", "service"]
    subject: str

SERVICE_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = ()   # the /api/v1 endpoints the plugin may call

class AccessVerifier:
    def __init__(self, s: Settings, jwks: PyJWKClient | None = None):
        self.issuer = s.cf_team_domain.rstrip("/")
        self.jwks = jwks or PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs")   # default 300 s JWK-set cache
        self.s = s

    def _decode(self, token: str) -> dict:
        key = self.jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256"], audience=self.s.cf_aud, issuer=self.issuer,
                          leeway=30, options={"require": ["exp", "iss", "aud"]})

    async def identify(self, request: Request) -> Identity:
        if self.s.dev_identity:                               # only loadable when ATELIER_ENV=development
            return Identity(self.s.dev_identity, "dev")
        token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization")
        if not token:
            raise AccessDenied("missing Access JWT")
        try:
            claims = await asyncio.to_thread(self._decode, token)
        except jwt.PyJWTError as exc:                         # includes PyJWKClientError (fetch/kid failures)
            raise AccessDenied(f"invalid Access JWT: {type(exc).__name__}") from None
        email, cn = claims.get("email"), claims.get("common_name")
        if email and email.casefold() == self.s.owner_email.casefold():
            return Identity("owner", email)
        if cn and hmac.compare_digest(cn, self.s.plugin_client_id):
            return Identity("service", cn)
        raise AccessDenied("identity not allowed")

def service_may_call(method: str, path: str) -> bool:
    return any(method == m and pattern.fullmatch(path) for m, pattern in SERVICE_ROUTES)

@app.middleware("http")
async def access_guard(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    try:
        identity = await request.app.state.verifier.identify(request)
    except AccessDenied as exc:
        log.warning("access denied path=%s reason=%s", request.url.path, exc.reason)
        return PlainTextResponse("Forbidden", status_code=403)
    if identity.kind == "service" and not service_may_call(request.method, request.url.path):
        log.warning("service identity refused path=%s", request.url.path)
        return PlainTextResponse("Forbidden", status_code=403)
    if identity.kind == "owner" and request.method not in ("GET", "HEAD", "OPTIONS") \
            and not same_origin(request, request.app.state.settings.public_origin):
        return PlainTextResponse("Forbidden", status_code=403)
    request.state.identity = identity
    return await call_next(request)
```

`same_origin()` returns true when `Origin == public_origin`, or, if `Origin` is absent, when `Referer` starts with `public_origin + "/"`.

Test fixture (real RS256; the only patch is the JWKS fetch):

```python
@pytest.fixture(scope="session")
def access_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "test-kid", "alg": "RS256"}
    return key, {"keys": [jwk]}

@pytest.fixture(autouse=True)
def jwks_without_network(monkeypatch, access_key):
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: access_key[1])

def mint(key, *, kid="test-kid", **claims) -> str:
    now = int(time.time())
    body = {"aud": [AUD], "iss": TEAM, "iat": now, "nbf": now, "exp": now + 300, "type": "app"} | claims
    return jwt.encode(body, key, algorithm="RS256", headers={"kid": kid})
```

Pages and partials: `base.html` (nav, header-status slot, flash area), `generate.html`, `partials/gen_params.html` (model-specific fields swapped by `hx-get` when the model changes), `queue.html`, `partials/job_rows.html`, `gallery.html`, `partials/image_card.html`, `batch.html`, `image.html`, `partials/header_status.html`, `partials/flash.html` (the inline error target for `hx-post` handlers).

## Related Code Files

<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

Create:
- `/Users/sweet-home/Works/artio/atelier/main.py` (`create_app` factory, lifespan, middlewares, static mount, templates)
- `/Users/sweet-home/Works/artio/atelier/auth.py` (verifier, `SERVICE_ROUTES`, `service_may_call`, `same_origin`)
- `/Users/sweet-home/Works/artio/atelier/request_limits.py` (the body-size ASGI middleware)
- `/Users/sweet-home/Works/artio/atelier/library.py` (gallery listing, image detail and image delete queries; phase 7 extends it)
- `/Users/sweet-home/Works/artio/atelier/routes/__init__.py`
- `/Users/sweet-home/Works/artio/atelier/routes/pages.py` (`/`, `/gallery`, `/batches/{id}`, `/partials/header-status`)
- `/Users/sweet-home/Works/artio/atelier/routes/generate.py`
- `/Users/sweet-home/Works/artio/atelier/routes/jobs.py`
- `/Users/sweet-home/Works/artio/atelier/routes/images.py`
- `/Users/sweet-home/Works/artio/atelier/routes/health.py`
- `/Users/sweet-home/Works/artio/atelier/templates/base.html`, `generate.html`, `queue.html`, `gallery.html`, `batch.html`, `image.html`
- `/Users/sweet-home/Works/artio/atelier/templates/partials/gen_params.html`, `job_rows.html`, `image_card.html`, `header_status.html`, `flash.html`
- `/Users/sweet-home/Works/artio/atelier/static/htmx.min.js` (vendored, pinned 2.x)
- `/Users/sweet-home/Works/artio/atelier/static/pico.min.css` (vendored, pinned 2.x)
- `/Users/sweet-home/Works/artio/atelier/static/app.css`
- `/Users/sweet-home/Works/artio/atelier/static/VERSIONS.txt` (upstream URL, version and SHA-256 of each vendored file)
- `/Users/sweet-home/Works/artio/tests/test_auth.py`
- `/Users/sweet-home/Works/artio/tests/test_request_limits.py`
- `/Users/sweet-home/Works/artio/tests/test_ui_generate.py`
- `/Users/sweet-home/Works/artio/tests/test_ui_queue.py`
- `/Users/sweet-home/Works/artio/tests/test_ui_gallery.py`
- `/Users/sweet-home/Works/artio/tests/test_ui_image.py`
- `/Users/sweet-home/Works/artio/tests/test_health.py`
- `/Users/sweet-home/Works/artio/tests/test_pages_hide_secrets.py`

Modify:
- `/Users/sweet-home/Works/artio/tests/conftest.py`: add the `access_key`, `jwks_without_network` and `mint` helpers; an `app_client` fixture that builds `create_app(settings, gateway=fake_gateway, start_worker=False)` inside `TestClient`; `owner_headers` and `service_headers` helpers (a minted JWT, plus `Origin` for the owner); and a `route_ids` fixture mapping each path parameter name to a real row ID.
- `/Users/sweet-home/Works/artio/atelier/jobs.py`: read helpers for the queue view, if they are not already there.

Delete: none.

## Implementation Steps

<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->
<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

1. **`auth.py`.** Write `Identity`, `AccessDenied` (with `reason`), `AccessVerifier`, `SERVICE_ROUTES` (empty), `service_may_call()` and `same_origin()` as sketched.
2. **`request_limits.py`.** Write a pure ASGI middleware:
   - A request whose `Content-Length` exceeds the limit for its path gets a 413 at once.
   - Otherwise it wraps `receive`, counts body bytes, and answers 413 as soon as the count passes the limit. This covers chunked bodies.
   - The default is 64 KB, and `per_path` holds exact-path overrides.
3. **`main.py`.**
   - `create_app(settings=None, *, registry=None, gateway=None, start_worker=True)` loads settings (fail fast on `ConfigError`).
   - It stores `settings`, `registry`, `gateway` (default `ModalSdkGateway()`), `verifier` and `templates` (Jinja2, with a `localtime` filter using `ZoneInfo(settings.timezone)`) on `app.state`.
   - It always creates the `Worker` and stores it on `app.state.worker`. Routes use it for status, paused reasons and alerts.
   - It adds the body-size middleware (outermost) and `access_guard`, mounts `/static` and includes the routers.
   - The lifespan runs `db.migrate()` and, only if `start_worker` is set, `worker.run()`, then stops the worker on shutdown. Tests pass `start_worker=False` and drive `dispatch_once()` and `poll_once()` themselves.
4. **Vendor the static assets.**
   - Download pinned htmx 2.x `htmx.min.js` and Pico 2.x `pico.min.css` from their official release URLs.
   - Record the URL, version and `shasum -a 256` of each in `VERSIONS.txt`.
   - Add `<meta name="htmx-config" content='{"includeIndicatorStyles":false}'>` so no inline styles are needed.
5. **Generate routes and templates.**
   - The model change triggers `hx-get="/generate/params"` with `hx-target="#gen-params"`.
   - `POST` builds a `GenerateRequest` and calls `jobs.create_batch`.
     - `ValidationError` and `DiskGuardError` re-render the page with the message and a 200.
     - Success redirects with a 303.
   - Remix is `?from=`: load the image and its job params and set the seed mode to fixed.
6. **Queue routes.**
   - The rows partial carries `hx-get="/queue/rows" hx-trigger="every 2s" hx-swap="outerHTML"` on the `#jobs-panel` wrapper and returns 286 when nothing is active.
   - Cancel calls `jobs.cancel_job` only; it never calls the gateway. A running job's row then shows the "GPU may still finish" note.
   - Retry calls `jobs.retry_job`.
   - Both re-render `#jobs-panel` with a 200. An unexpected condition, such as retrying a done job, returns the panel plus an inline message in `#flash` through `HX-Retarget`.
7. **Gallery.** Write the `library.py` queries:
   - `list_batches(conn, model_id, page)` returns batch groups with their images; `batch_detail`; `image_detail` (images join jobs).
   - `delete_image(conn, data_dir, id)` deletes, in one transaction, the image row, its job row, and the batch if it has no job left, then deletes the files. `retry_of` references are set to NULL by the schema.
8. **Image routes.** Serve files through `FileResponse` from paths resolved under `data_dir`: resolve the path and assert it is still inside `data_dir`.
9. **Header status.**
   - Write `partials/header_status.html` and its route: disk badge from `storage.disk_status()`, plus `worker.paused` and `worker.alerts` per backend.
   - `base.html` embeds it with `hx-get="/partials/header-status" hx-trigger="load, every 10s"`.
10. **`/healthz`.** Build it from DB `SELECT 1`, the sentinel in production, and the worker's `last_dispatch_tick` and `last_poll_tick`: stale after 30 s, and "disabled" when the worker wasn't started.
11. **Tests.** Write them as listed in the Todo list.
    - `test_pages_hide_secrets` enumerates `app.routes`, meaning every route with `GET`, plus one static asset. It fills path parameters from the `route_ids` fixture, requests each route with the owner identity (with the service identity for `/api/v1` paths), and asserts that no secret sentinel appears.
    - It **fails** when a GET route was not visited, or when a path parameter has no fixture value. Later phases' new GET routes are therefore covered automatically.
12. **Lint and test.** Run `uv run ruff check` and `uv run pytest -q`.
13. **Local look (optional).** Run the app in development:

    ```bash
    ATELIER_ENV=development ATELIER_DEV_IDENTITY=owner ATELIER_DATA_DIR=./data \
    ATELIER_PUBLIC_ORIGIN=http://127.0.0.1:8090 uv run uvicorn --factory atelier.main:create_app --port 8090
    ```

    Browsing is free. **[OWNER-GATED]** Submitting a job from this local run spends real GPU money through the owner's `~/.modal.toml`, so ask first.
14. **Commit** as `feat: web UI with Access JWT verification, queue, gallery and image pages`.

## Todo List

- [x] `auth.py` and the middleware; auth tests pass with real RS256 JWTs; the service identity is refused outside `SERVICE_ROUTES`
- [x] Body-size middleware with Content-Length and streamed-count checks
- [x] `create_app` factory and lifespan (migrate, start and stop the worker; `app.state.worker` always present)
- [x] Vendored htmx and Pico with recorded checksums, plus `app.css`
- [x] Generate form (plain form, 200 re-render on errors): model picker, presets, custom size, seed modes, count 1–8, remix
- [x] Queue with 2 s polling and a 286 stop; DB-only cancel with the "GPU may still finish" note; retry restarts polling; waiting reasons on queued rows
- [x] Gallery grouped by batch with a model filter; batch page
- [x] Image page with full settings; download and remix; delete removes image, job and empty batch, with the backup-retention confirmation text
- [x] Header disk badge, paused reasons and backend alerts; `/healthz` with loop liveness
- [x] Route-enumerating crawl proves no secret sentinel is ever rendered

## Success Criteria

<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->
<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

- **Criterion 1** (origin half), in `tests/test_auth.py`:
  - These return 403: missing token; a garbage token; a valid signature with the wrong `aud`; the wrong `iss`; an expired token; a token signed by another key under the same `kid`; an unknown `kid`; an HS256 token; a non-owner email; a service token with another `common_name`; `/static/app.css` without a token; an owner POST with a foreign `Origin`; an owner POST with neither `Origin` nor `Referer`.
  - `test_service_identity_is_refused_outside_the_api_allowlist`: a valid service JWT gets 403 on every HTML route, on static files and on `POST /images/{id}/delete`.
  - These succeed: an owner JWT in the header; the owner JWT in the `CF_Authorization` cookie; `/healthz` without a token.
  - `create_app` raises `ConfigError` for a dev identity with `ATELIER_ENV=production`.
- **Criterion 2** (UI side):
  - A job shows `queued → running → done` across successive `/queue/rows` responses as the fake gateway advances.
  - A done image's page lists model, prompt, negative prompt, seed, size, steps, cfg, duration and estimated cost.
  - A second client, simulating a reload, sees the same image.
- **Criterion 4** (UI side):
  - A failed row shows the ComfyUI error text and a Retry button that creates a new queued job.
  - Cancel works on queued and running rows. The running one shows the "GPU may still finish" note, and the fake gateway's `calls` log shows no cancel.
  - A queued row shows its "waiting: <reason>".
- **Criterion 5** (UI side): the picker lists every registry model. With a two-model test registry, `/gallery?model=<second>` shows only that model's images.
- **Criterion 7** (UI side): a POST with count 4 and a preset creates 4 queue rows with distinct seeds. The gallery renders them as one batch group. `/generate?from=<id>` prefills prompt, negative, size, steps, cfg and the fixed seed.
- **Criterion 12** (UI side): with the cap set below current usage, the POST re-renders with the refusal message (status 200) and no job is created. The header badge shows usage.
- **Criterion 14** (UI side): `test_pages_hide_secrets` sets sentinel values for `MODAL_TOKEN_SECRET`, `ATELIER_CF_AUD` and `ATELIER_PLUGIN_CLIENT_ID`. It visits every GET route in `app.routes` and asserts that no sentinel appears and that no GET route went unvisited.
- `test_delete_removes_image_job_and_empty_batch` passes, and the confirmation text names the 6-month backup retention.
- `test_healthz_reports_stale_loops_as_503` and `test_oversized_body_is_rejected_before_parsing` (a Content-Length case and a chunked case) pass.

## Verification

```bash
cd /Users/sweet-home/Works/artio
uv run ruff check
uv run pytest -q
uv run pytest -q tests/test_auth.py tests/test_request_limits.py tests/test_health.py -v
uv run pytest -q tests/test_ui_generate.py tests/test_ui_queue.py tests/test_ui_gallery.py tests/test_ui_image.py tests/test_pages_hide_secrets.py
# Optional local look (browsing only; generating is [OWNER-GATED] because it spends GPU money)
ATELIER_ENV=development ATELIER_DEV_IDENTITY=owner ATELIER_DATA_DIR=./data ATELIER_PUBLIC_ORIGIN=http://127.0.0.1:8090 \
  uv run uvicorn --factory atelier.main:create_app --port 8090
curl -s http://127.0.0.1:8090/healthz        # {"status":"ok","version":"dev","loops":"ok"}
```

## Risk Assessment

<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F14 interfaces and tests -->

| Risk | Likelihood × impact | Observable signal | Pre-decided response |
|---|---|---|---|
| Access does not forward `Cf-Access-Jwt-Assertion` for some request type | Low × High | Live 403s in phase 4 with log reason `missing Access JWT` while the browser holds a session | The cookie fallback already covers browsers. If service-token calls lack the header, stop and ask the owner to check the Access application settings. Do not weaken verification. |
| A browser omits both `Origin` and `Referer` on a same-origin HTMX POST | Low × Medium | Live 403s on clicks, logged as denied at the origin check | Also accept `Sec-Fetch-Site: same-origin` for owner POSTs, and add a test for it. Adjust within the plan. |
| A JWKS fetch blocks or fails when the cache expires | Low × Medium | Latency spikes every 5 minutes, or 403s with `PyJWKClientError` | `to_thread` already isolates the blocking call. On a Cloudflare outage, requests fail closed with 403, which is the intended behavior. |
| An HTML handler returns a 4xx and htmx drops the partial | Low × Medium | A click shows nothing, while the server logged a 4xx | Fix the handler to return 200 with `HX-Retarget`; add a test asserting 200 for that path. |
| The 64 KB default body limit is too small for a legitimate form | Low × Low | A 413 on a normal submit | Raise the default in `request_limits.py` with a test; keep uploads on their own per-path limit. |
| Time zone data is missing in the slim image | Low × Low | `ZoneInfoNotFoundError` at startup | The `tzdata` dependency, added in phase 1, provides it. |

**Rollback:** local code only, so `git revert`. Nothing is deployed until phase 4.

## Security Considerations

<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->

- The origin verifies signature, `aud`, `iss` and `exp` on every non-health request, and fails closed. The 403 body never includes the reason.
- The identity allowlist has exactly two entries, and authorization is by route: the service identity can reach only the explicit `/api/v1` allowlist. The service-token check uses a constant-time comparison.
- Dev identity cannot load unless `ATELIER_ENV=development`; this is tested.
- State changes use POST only. The owner Origin/Referer check blocks CSRF; the Access cookie's SameSite setting (Lax, set in phase 4) is only a second layer. Every GET is side-effect free.
- Request bodies are size-limited before parsing.
- Delete removes the image, its prompt and job record, and an empty batch. Copies remain in nightly backups for up to 6 months (the confirmation says so; phase 5 documents a purge procedure), and Modal keeps outputs for 7 days.
- File responses resolve under `data_dir` only. Jinja autoescapes prompts, negatives and error text.
- Secrets never reach templates. The route-enumerating crawl test enforces this.

## Next Steps

Phase 4 containerizes this app, deploys it to `folio-prod-1` behind the tunnel and Access, and proves criterion 1 end to end. Its deploy health check requires the `"loops":"ok"` field defined here.

## Verification notes (2026-09-26)
- **Gates:** `uv run ruff check` is clean, and `uv run pytest -q` passes 285 tests (1 live test deselected). Every success-criteria test exists and passes. They were mutation-proven: the service allowlist, email match, CSRF check, body limit, `/healthz` staleness, delete cascade and the secret crawl.
- **Independent security review:** 6 Medium and 11 Low findings, no Critical or High. All were accepted and fixed before the commit. The main fixes:
  - queue polling now stops (the timer is only emitted while jobs are active);
  - `/healthz` reports a started-but-silent worker as stale (503);
  - anti-framing and nosniff headers are sent on every response;
  - a dedicated JWKS thread pool with a 5 s timeout;
  - path containment is enforced before every file serve and unlink;
  - the tests that couldn't fail were rewritten.
  See [code-review-web-ui-auth.md](./reports/code-review-web-ui-auth.md).
- **FastAPI 0.141 behavior:** included routers are wrapped as `_IncludedRouter`, so route crawls use `fastapi.routing.iter_route_contexts()` with a minimum-visited floor.
- **Local generation:** the optional owner-approved local run (step 13) was skipped. Generation is proven end to end in the phase 4 live acceptance on production.
