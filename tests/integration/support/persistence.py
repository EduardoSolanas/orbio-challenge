"""App-client helper shared by the persistence and memory tests."""

from __future__ import annotations

from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient

from backend.gateway.main import create_app


@asynccontextmanager
async def app_running(settings, provider):
    app = create_app(settings=settings, provider=provider)
    async with app.router.lifespan_context(app):
        yield app


@asynccontextmanager
async def app_client(settings, provider):
    """One real app instance over `settings.session_dir`."""

    async with app_running(settings, provider) as app:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client
