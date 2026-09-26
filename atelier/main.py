"""The FastAPI application factory: settings, middleware stack, routers, templates and the worker.

Single uvicorn worker only (see the deployment runbook): the Worker's dispatcher and poller loops live
in this process's memory, so a second worker process would run them twice.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from atelier import db
from atelier.auth import AccessVerifier, SecurityHeadersMiddleware, access_guard
from atelier.config import ConfigError, Settings, load_settings
from atelier.modal_gateway import ModalGateway, ModalSdkGateway
from atelier.registry import DEFAULT_REGISTRY, Registry
from atelier.request_limits import BodySizeLimitMiddleware
from atelier.routes import generate, health, images, jobs, pages
from atelier.worker import Worker

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
    """Builds the Atelier app. Loads settings from the real environment when none is given, so an
    invalid production configuration (a ConfigError) fails at startup rather than at the first request.
    registry/gateway/start_worker exist so tests can inject a fake gateway and drive the worker
    themselves instead of letting its background loops run. start_worker=False is itself refused
    outside test/development, so a production deploy can never accidentally run with dead loops."""
    settings = settings or load_settings(os.environ)
    if not start_worker and settings.env not in ("test", "development"):
        raise ConfigError("start_worker=False is only allowed when ATELIER_ENV is 'test' or 'development'")
    registry = registry or DEFAULT_REGISTRY
    gateway = gateway or ModalSdkGateway()
    worker = Worker(settings, registry, gateway)

    # A dedicated pool for JWT verification (see AccessVerifier), separate from the loop's default
    # executor that storage.save_result() uses, so the two kinds of blocking work never contend.
    jwt_executor = ThreadPoolExecutor(max_workers=_JWT_EXECUTOR_WORKERS, thread_name_prefix="atelier-jwt")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        db.migrate(settings)
        if start_worker:
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
    app.state.templates = templates

    # Registration order controls middleware nesting (Starlette wraps the most-recently-added
    # middleware outermost): access_guard first, then the body-size limiter, then the security-headers
    # wrapper last, so the limiter runs before auth even sees the request, and the security headers
    # land on every response -- including the limiter's own 413 and the guard's own 403.
    app.middleware("http")(access_guard)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    app.include_router(pages.router)
    app.include_router(generate.router)
    app.include_router(jobs.router)
    app.include_router(images.router)
    app.include_router(health.router)

    return app
