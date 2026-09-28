"""The FastAPI application factory: settings, middleware stack, routers, templates and the worker.

Single uvicorn worker only (see the deployment runbook): the Worker's dispatcher and poller loops live
in this process's memory, so a second worker process would run them twice.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from artio import __version__, db, gpu
from artio.auth import AccessVerifier, SecurityHeadersMiddleware, access_guard
from artio.config import ConfigError, Settings, load_settings
from artio.modal_gateway import ModalGateway, ModalSdkGateway
from artio.registry import DEFAULT_REGISTRY, Registry
from artio.request_limits import BodySizeLimitMiddleware
from artio.routes import api_v1, chat, generate, health, images, jobs, library, pages, workflows
from artio.routes import gpu as gpu_routes
from artio.worker import Worker

# Uploaded workflow graphs are read whole into memory before validate_api_graph's own 2 MB
# file-size check runs, so the request body itself is capped a little higher (3 MB) to leave room
# for the surrounding multipart framing and the name/backend form fields.
_WORKFLOW_UPLOAD_LIMIT_BYTES = 3 * 1024 * 1024

_JWT_EXECUTOR_WORKERS = 2

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _localtime_filter(tz: ZoneInfo) -> Callable[[float | None, str], str]:
    def localtime(value: float | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
        if value is None:
            return "—"
        return datetime.fromtimestamp(value, tz=tz).strftime(fmt)

    return localtime


def create_app(
    settings: Settings | None = None,
    *,
    registry: Registry | None = None,
    gateway: ModalGateway | None = None,
    start_worker: bool = True,
) -> FastAPI:
    """Builds the Artio app. Loads settings from the real environment when none is given, so an
    invalid production configuration (a ConfigError) fails at startup rather than at the first request.
    registry/gateway/start_worker exist so tests can inject a fake gateway and drive the worker
    themselves instead of letting its background loops run. start_worker=False is itself refused
    outside test/development, so a production deploy can never accidentally run with dead loops."""
    settings = settings or load_settings(os.environ)
    if not start_worker and settings.env not in ("test", "development"):
        raise ConfigError("start_worker=False is only allowed when ARTIO_ENV is 'test' or 'development'")
    registry = registry or DEFAULT_REGISTRY
    gateway = gateway or ModalSdkGateway()
    worker = Worker(settings, registry, gateway)

    # A dedicated pool for JWT verification (see AccessVerifier), separate from the loop's default
    # executor that storage.save_result() uses, so the two kinds of blocking work never contend.
    jwt_executor = ThreadPoolExecutor(max_workers=_JWT_EXECUTOR_WORKERS, thread_name_prefix="artio-jwt")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        db.migrate(settings)
        if start_worker:
            # A window still open from before a restart (crash, or a plain redeploy) resumes its
            # pinger: warm-up must never silently stop just because Artio itself restarted. One
            # that had already expired while Artio was down is cleared right away instead of
            # sitting there as a stale "warm until" nobody's pinger will ever reach and clear.
            now = time.time()
            with db.session(settings) as conn:
                for backend_id in registry.backends:
                    until, _ = gpu.warm_state(conn, backend_id)
                    if until is not None and until > now:
                        worker.ensure_pinger(backend_id)
                    elif until is not None:
                        gpu.clear_expired_warm(conn, backend_id, until)
            await worker.run()
        try:
            yield
        finally:
            await worker.stop()
            jwt_executor.shutdown(wait=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.registry = registry
    app.state.gateway = gateway
    app.state.worker = worker
    app.state.verifier = AccessVerifier(settings, executor=jwt_executor)

    templates = Jinja2Templates(directory=_TEMPLATES_DIR)
    templates.env.filters["localtime"] = _localtime_filter(ZoneInfo(settings.timezone))
    templates.env.globals["artio_release"] = __version__
    # Appended to every /static URL as ?v=, so each deploy (a new commit SHA) gets fresh URLs and no
    # browser or Cloudflare cache can pair new pages with an old stylesheet or script.
    templates.env.globals["asset_version"] = settings.version
    app.state.templates = templates

    # Registration order controls middleware nesting (Starlette wraps the most-recently-added
    # middleware outermost): access_guard first, then the body-size limiter, then the security-headers
    # wrapper last, so the limiter runs before auth even sees the request, and the security headers
    # land on every response -- including the limiter's own 413 and the guard's own 403.
    app.middleware("http")(access_guard)
    app.add_middleware(
        BodySizeLimitMiddleware, per_path={"/workflows": _WORKFLOW_UPLOAD_LIMIT_BYTES}
    )
    app.add_middleware(SecurityHeadersMiddleware)

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    app.include_router(pages.router)
    app.include_router(chat.router)
    app.include_router(generate.router)
    app.include_router(jobs.router)
    app.include_router(images.router)
    app.include_router(gpu_routes.router)
    app.include_router(library.router)
    app.include_router(workflows.router)
    app.include_router(health.router)
    api_v1.register(app)

    return app
