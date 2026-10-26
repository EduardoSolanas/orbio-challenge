"""FastAPI app factory and lifespan assembly.

`create_app(settings=None, provider=None, knowledge_path=None)` assembles the
real dependency graph on lifespan start (IC §3): settings validation, writable
session storage, repository, provider, immutable knowledge base, engine. The
routes live in ``app.routes`` and read everything from ``request.app.state``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway.backend_client import BackendApiClient
from gateway.config import Settings
from gateway.conversation.engine import ConversationEngine
from gateway.conversation.safety import (
    SafetyExamplesInvalid,
    SafetyIndex,
    load_examples,
)
from gateway.conversation.verification import VerificationRegistry
from gateway.errors import (
    KNOWLEDGE_INVALID_MESSAGE,
    LOCALIZATION_INVALID_MESSAGE,
    STORAGE_UNAVAILABLE_MESSAGE,
    AppError,
    app_error,
)
from gateway.knowledge.retrieval import KnowledgeInvalid, load_knowledge
from gateway.knowledge.semantic import SemanticIndex
from gateway.localization import LocalizationInvalid, load_locales
from gateway.providers.client import OpenAICompatibleProvider
from gateway.routes.asgi_guards import CorrelationIdMiddleware, JsonBodyGuardMiddleware
from gateway.routes.endpoints import router
from gateway.routes.http_support import error_payload, request_id_of
from gateway.storage import JsonSessionRepository

logger = logging.getLogger(__name__)

DEFAULT_KNOWLEDGE_PATH = Path(__file__).parent / "knowledge" / "northstar-faq.json"
DEFAULT_SAFETY_EXAMPLES_PATH = (
    Path(__file__).parent / "knowledge" / "safety-examples.json"
)


def _build_provider(settings: Settings) -> OpenAICompatibleProvider:
    profiles = {
        "llm": settings.llm,
        "rag": settings.rag,
        "stt": settings.stt,
        "tts": settings.tts,
    }
    if settings.tts_es is not None:
        profiles["tts_es"] = settings.tts_es
    if settings.embed is not None:
        profiles["embed"] = settings.embed
    return OpenAICompatibleProvider(
        profiles, capacity=settings.provider_max_concurrency
    )


async def _open_repository(settings: Settings, provider) -> JsonSessionRepository:
    """Create and probe the session directory, closing an injected provider on failure."""

    session_dir = Path(settings.session_dir)
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        repository = JsonSessionRepository(session_dir)
        await repository.check_writable()
    except (OSError, AppError):
        # Resources already constructed for this lifespan (an injected
        # provider) are closed before the startup failure surfaces.
        if provider is not None:
            await provider.close()
        raise RuntimeError(STORAGE_UNAVAILABLE_MESSAGE) from None
    return repository


def create_app(
    settings: Settings | None = None,
    provider=None,
    knowledge_path: Path | None = None,
    locales_dir: Path | None = None,
    verification_registry: VerificationRegistry | None = None,
    backend_api_client: BackendApiClient | None = None,
    semantic_index: SemanticIndex | None = None,
    safety_index: SafetyIndex | None = None,
) -> FastAPI:
    """Build the app; dependencies are assembled on lifespan start (IC §3)."""

    kb_path = DEFAULT_KNOWLEDGE_PATH if knowledge_path is None else Path(knowledge_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        active_settings = Settings.from_env() if settings is None else settings
        repository = await _open_repository(active_settings, provider)
        active_provider = (
            provider if provider is not None else _build_provider(active_settings)
        )
        try:
            locales = load_locales(locales_dir)
        except LocalizationInvalid:
            await active_provider.close()
            raise RuntimeError(LOCALIZATION_INVALID_MESSAGE) from None
        try:
            knowledge_base = load_knowledge(kb_path)
        except KnowledgeInvalid:
            await active_provider.close()
            raise RuntimeError(KNOWLEDGE_INVALID_MESSAGE) from None

        speech_languages = frozenset({"en"})
        if active_settings.tts_es is not None:
            speech_languages = speech_languages | {"es"}

        verification = (
            verification_registry
            if verification_registry is not None
            else VerificationRegistry()
        )
        customer_data = (
            backend_api_client
            if backend_api_client is not None
            else BackendApiClient(
                active_settings.backend_api_base_url,
                timeout_seconds=active_settings.backend_api_timeout_seconds,
            )
        )

        active_index = semantic_index
        if active_index is None and active_settings.embed is not None:
            active_index = SemanticIndex(
                knowledge_base.records,
                active_provider.embed,
                min_similarity=active_settings.embed_min_similarity,
            )

        active_safety = safety_index
        if active_safety is None and active_settings.embed is not None:
            try:
                active_safety = SafetyIndex(
                    load_examples(DEFAULT_SAFETY_EXAMPLES_PATH), active_provider.embed
                )
            except SafetyExamplesInvalid:
                await active_provider.close()
                raise RuntimeError(
                    "The safety examples are missing or invalid."
                ) from None
            active_safety.warm()

        app.state.settings = active_settings
        app.state.locales = locales
        app.state.repository = repository
        app.state.provider = active_provider
        app.state.knowledge_base = knowledge_base
        app.state.speech_languages = speech_languages
        app.state.verification = verification
        app.state.customer_data = customer_data
        app.state.engine = ConversationEngine(
            repository,
            active_provider,
            knowledge_base,
            speech_languages=speech_languages,
            customer_data=customer_data,
            verification=verification,
            semantic_index=active_index,
            safety_index=active_safety,
        )
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            if active_safety is not None:
                await active_safety.aclose()
            await customer_data.aclose()
            await active_provider.close()

    app = FastAPI(title="Northstar Energy Customer Support", lifespan=lifespan)
    app.add_middleware(JsonBodyGuardMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):
        request_id = request_id_of(request)
        logger.info(
            "request_id=%s error_code=%s status=%s", request_id, exc.code, exc.status
        )
        return JSONResponse(error_payload(exc, request_id), status_code=exc.status)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        request_id = request_id_of(request)
        logger.error("request_id=%s exception=%s", request_id, type(exc).__name__)
        return JSONResponse(
            error_payload(app_error("internal_error"), request_id),
            status_code=500,
            headers={"x-request-id": request_id},
        )

    app.include_router(router)
    return app


app = create_app()
