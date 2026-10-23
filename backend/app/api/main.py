from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from sqlalchemy import text

from app.api import admin, auth, me, webhooks
from app.config import get_settings
from app.logconfig import configure_logging
from app.services import Services


def create_app(services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = services is None
        if owned:
            settings = get_settings()
            configure_logging(settings.log_level)
            app.state.services = await Services.create(settings)
        else:
            app.state.services = services
        yield
        if owned:
            await app.state.services.close()

    app = FastAPI(title="Calendar Sync API", version="0.1.0", lifespan=lifespan)
    if services is not None:
        app.state.services = services

    app.include_router(webhooks.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(admin.router)
    app.mount("/metrics", make_asgi_app())

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        s: Services = app.state.services
        try:
            async with s.sessionmaker() as session:
                await session.execute(text("SELECT 1"))
            await s.redis.ping()
        except Exception as exc:  # noqa: BLE001 - report any dependency failure
            return JSONResponse({"status": "unavailable", "error": str(exc)}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app


app = create_app()
