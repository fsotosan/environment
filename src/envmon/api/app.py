"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from envmon import __version__
from envmon.api.routes import router
from envmon.config import Settings, get_settings
from envmon.db import open_database
from envmon.llm import DailySummaryService
from envmon.maintenance import MaintenanceScheduler
from envmon.repository import Repository
from envmon.sampler import Sampler
from envmon.sensors.manager import SensorManager

logger = logging.getLogger(__name__)

API_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = API_DIR / "templates"
STATIC_DIR = API_DIR / "static"


def create_app(
    settings: Settings | None = None,
    *,
    session_factory=None,
    manager: SensorManager | None = None,
    start_background: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    engine = None
    if session_factory is None:
        engine, session_factory = open_database(settings)

    repository = Repository(session_factory)
    manager = manager or SensorManager(settings)
    sampler = Sampler(settings, repository, manager=manager)
    summary_service = DailySummaryService(settings, repository)
    maintenance = MaintenanceScheduler(settings, repository)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if start_background:
            if settings.sampling.enabled:
                await sampler.start()
            await maintenance.start()
            logger.info(
                "envmon %s started (mode=%s, db=%s)",
                __version__,
                settings.sensors.mode,
                "sqlite" if settings.database.is_sqlite else "external",
            )
        try:
            yield
        finally:
            if start_background:
                await maintenance.stop()
            await sampler.stop()
            if engine is not None:
                engine.dispose()

    app = FastAPI(
        title=f"{settings.web.title} API",
        version=__version__,
        description="Indoor environment monitoring station API",
        lifespan=lifespan,
        root_path=settings.web.root_path,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.repository = repository
    app.state.manager = manager
    app.state.sampler = sampler
    app.state.summary_service = summary_service
    app.state.maintenance = maintenance
    app.state.engine = engine

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            # ECharts' UMD bundle uses Function(); the asset is self-hosted.
            "script-src 'self' 'unsafe-eval'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'",
        )
        return response

    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.get("/", include_in_schema=False)
    def dashboard(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "title": settings.web.title,
                "station_name": settings.station_name,
                "version": __version__,
                "refresh_seconds": settings.web.refresh_seconds,
            },
        )

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon():
        from fastapi.responses import FileResponse

        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    return app
