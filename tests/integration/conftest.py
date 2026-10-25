"""Integration fixtures: real lifespan app, isolated temp SESSION_DIR, scripted provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.main import create_app
from tests.integration.support.api import (
    ScriptedProvider,
    build_settings,
    running_client,
)


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    return tmp_path / "sessions"


@pytest.fixture
def settings(session_dir: Path):
    return build_settings(session_dir)


@pytest.fixture
def provider() -> ScriptedProvider:
    return ScriptedProvider()


@pytest.fixture
def app(settings, provider):
    return create_app(settings=settings, provider=provider)


@pytest.fixture
async def client(app):
    async with running_client(app) as active_client:
        yield active_client
