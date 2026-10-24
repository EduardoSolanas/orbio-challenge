"""IT14-01..IT14-10: startup validation, health, isolation and sanitized errors.

Each case runs the real `Settings` resolution and the real app lifespan
(IC §13.2 IT14). Offline validation, the writable-storage probe, the immutable
loaded knowledge base and the sanitized error surface are production code; only
the external AI service is doubled, and no case performs a model call during
startup or health.
"""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import AsyncExitStack

import httpx
import pytest

from app.errors import (
    CONFIGURATION_INVALID_MESSAGE,
    STORAGE_UNAVAILABLE_MESSAGE,
)
from app.main import create_app
from app.providers import OpenAICompatibleProvider
from tests.integration.support_api import (
    ScriptedProvider,
    read_session,
)
from tests.integration.test_it02_intake import PART_TEXT, start_session, turn_body
from tests.integration.test_it08_transcription import (
    RoleTransport,
    assert_error,
    snapshot,
    unexpected,
)
from tests.integration.test_it12_transport import (
    ROLES,
    apply_env,
    chat_valid,
    env,
    settings_from,
)
from tests.integration.test_it13_bounds import blocked_stream_responder

SENTINEL_KEY = "sk-live-provider-key-sentinel"
SENTINEL_CUSTOMER = "customer NS-000999 payment details"
INTERNAL_MESSAGE = "An internal error occurred."


def none_auth_env(session_dir, **overrides):
    values = env(
        session_dir,
        LLM_AUTH_MODE="none",
        LLM_API_KEY=None,
        RAG_AUTH_MODE="none",
        RAG_API_KEY=None,
        STT_AUTH_MODE="none",
        STT_API_KEY=None,
        TTS_AUTH_MODE="none",
        TTS_API_KEY=None,
        **overrides,
    )
    return values


async def expect_configuration_failure(values, monkeypatch):
    """Offline Settings validation and the real lifespan both fail before readiness."""

    with pytest.raises(RuntimeError) as excinfo:
        settings_from(values)
    assert str(excinfo.value) == CONFIGURATION_INVALID_MESSAGE

    apply_env(monkeypatch, values)
    app = create_app()
    with pytest.raises(RuntimeError) as excinfo:
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan reached readiness with invalid settings")
    assert str(excinfo.value) == CONFIGURATION_INVALID_MESSAGE
    assert getattr(app.state, "ready", False) is False
    assert not hasattr(app.state, "provider")
    assert not hasattr(app.state, "engine")
    return app


class RaisingProvider(ScriptedProvider):
    """Fake extraction raises an unexpected error carrying sensitive sentinels."""

    async def extract(self, context):
        self.calls.append(("extract", context))
        raise RuntimeError(
            f"{SENTINEL_KEY} while handling {SENTINEL_CUSTOMER}"
        )


class ClosingTransport(httpx.MockTransport):
    """Real mock transport that counts how often the client closes it."""

    def __init__(self, handler):
        super().__init__(handler)
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1
        await super().aclose()


def closing_provider(settings):
    transports = {}
    for role in ROLES:
        def handler(request, _role=role):
            raise AssertionError(f"unexpected {_role} wire call")

        transports[role] = ClosingTransport(handler)
    provider = OpenAICompatibleProvider(
        {
            "llm": settings.llm,
            "rag": settings.rag,
            "stt": settings.stt,
            "tts": settings.tts,
        },
        transports=transports,
    )
    return provider, transports


# IT14-01..IT14-04: offline configuration validation -------------------------


async def case_it14_01(session_dir, monkeypatch):
    values = env(session_dir, LLM_BASE_URL="ftp://llm.test/v1")
    await expect_configuration_failure(values, monkeypatch)


async def case_it14_02(session_dir, monkeypatch):
    values = env(
        session_dir,
        LLM_AUTH_MODE="bearer",
        LLM_API_KEY=None,
        OPENAI_API_KEY=SENTINEL_KEY,
    )
    # No developer/global key fallback: the bearer role stays invalid.
    await expect_configuration_failure(values, monkeypatch)


async def case_it14_03(session_dir, monkeypatch):
    values = env(session_dir, LLM_AUTH_MODE="none", LLM_API_KEY=SENTINEL_KEY)
    await expect_configuration_failure(values, monkeypatch)


async def case_it14_04(session_dir, monkeypatch):
    values = none_auth_env(session_dir, TTS_VOICE=None)
    await expect_configuration_failure(values, monkeypatch)


# IT14-11..IT14-13: TTS format/cap variants (PC §3) ---------------------------


async def case_it14_11(session_dir, monkeypatch):
    values = none_auth_env(session_dir, TTS_AUDIO_FORMAT="WAV")
    await expect_configuration_failure(values, monkeypatch)


async def case_it14_12(session_dir, monkeypatch):
    values = none_auth_env(
        session_dir, TTS_AUDIO_FORMAT="mp3", TTS_MAX_INPUT_CHARS="200"
    )
    await expect_configuration_failure(values, monkeypatch)


async def case_it14_13(session_dir, monkeypatch):
    values = none_auth_env(
        session_dir, TTS_AUDIO_FORMAT="wav", TTS_MAX_INPUT_CHARS="4001"
    )
    await expect_configuration_failure(values, monkeypatch)


# IT14-05: unwritable session storage ----------------------------------------


async def case_it14_05(session_dir, monkeypatch):
    settings = settings_from(none_auth_env(session_dir))
    provider = ScriptedProvider()
    app = create_app(settings=settings, provider=provider)

    def denied(*args, **kwargs):
        raise PermissionError("scoped probe denial")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", denied)

    with pytest.raises(RuntimeError) as excinfo:
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan reached readiness without writable storage")

    assert str(excinfo.value) == STORAGE_UNAVAILABLE_MESSAGE
    assert provider.calls == []
    # Resources constructed before the failure are closed exactly once.
    assert provider.close_count == 1
    assert getattr(app.state, "ready", False) is False
    assert not hasattr(app.state, "engine")


# IT14-06..IT14-07: real health probe ----------------------------------------


async def case_it14_06(session_dir, monkeypatch):
    settings = settings_from(none_auth_env(session_dir))
    real_named_temp = tempfile.NamedTemporaryFile
    probes: list[str] = []

    def counting(*args, **kwargs):
        probes.append("probe")
        return real_named_temp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", counting)

    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            assert len(probes) >= 2  # startup probe plus the health probe
            # The probe really wrote and removed its temporary file.
            assert list(session_dir.iterdir()) == []
            knowledge_base = app.state.knowledge_base
            assert knowledge_base.version == "2026-10-22"
            assert len(knowledge_base.records) == 5
            assert app.state.provider._in_use == 0

    assert app.state.ready is False


async def case_it14_07(session_dir, monkeypatch):
    settings = settings_from(none_auth_env(session_dir))
    provider, transports = closing_provider(settings)
    app = create_app(settings=settings, provider=provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (await client.get("/health")).status_code == 200

            calls: list[str] = []

            def denied(*args, **kwargs):
                calls.append("probe")
                raise PermissionError("scoped probe denial")

            monkeypatch.setattr(tempfile, "NamedTemporaryFile", denied)
            response = await client.get("/health")

            assert_error(response, status=503, code="storage_unavailable")
            assert calls == ["probe"]  # the real repository probe was invoked
            assert all(transport.close_count == 0 for transport in transports.values())


# IT14-08: lifespan exit closes all four clients ------------------------------


async def case_it14_08(session_dir):
    # RAG profile fully absent: the inherited RAG client is a distinct client.
    settings = settings_from(
        env(
            session_dir,
            LLM_AUTH_MODE="none",
            LLM_API_KEY=None,
            RAG_AUTH_MODE=None,
            RAG_API_KEY=None,
            STT_AUTH_MODE="none",
            STT_API_KEY=None,
            TTS_AUTH_MODE="none",
            TTS_API_KEY=None,
            RAG_BASE_URL=None,
            RAG_MODEL=None,
            RAG_OUTPUT_MODE=None,
        )
    )
    assert settings.rag == settings.llm
    provider, transports = closing_provider(settings)
    clients = list(provider._clients.values())
    assert len({id(client) for client in clients}) == 4

    app = create_app(settings=settings, provider=provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            assert all(transport.close_count == 0 for transport in transports.values())

    for role in ROLES:
        assert transports[role].close_count == 1
    assert all(client.is_closed for client in clients)
    # close() is idempotent: a second call does not close anything again.
    await provider.close()
    for role in ROLES:
        assert transports[role].close_count == 1


# IT14-09: two independent app instances --------------------------------------


async def case_it14_09(tmp_path):
    dir_a = tmp_path / "a" / "sessions"
    dir_b = tmp_path / "b" / "sessions"
    settings_a = settings_from(none_auth_env(dir_a))
    settings_b = settings_from(none_auth_env(dir_b))

    gate = asyncio.Event()
    started = asyncio.Event()
    responder_a = blocked_stream_responder(gate, started)
    responder_b = chat_valid()

    provider_a = OpenAICompatibleProvider(
        {
            "llm": settings_a.llm,
            "rag": settings_a.rag,
            "stt": settings_a.stt,
            "tts": settings_a.tts,
        },
        transports={
            role: RoleTransport(
                role, responder_a if role == "llm" else unexpected(role)
            ).transport
            for role in ROLES
        },
        capacity=1,
    )
    provider_b = OpenAICompatibleProvider(
        {
            "llm": settings_b.llm,
            "rag": settings_b.rag,
            "stt": settings_b.stt,
            "tts": settings_b.tts,
        },
        transports={
            role: RoleTransport(
                role, responder_b if role == "llm" else unexpected(role)
            ).transport
            for role in ROLES
        },
        capacity=1,
    )

    app_a = create_app(settings=settings_a, provider=provider_a)
    app_b = create_app(settings=settings_b, provider=provider_b)

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app_a.router.lifespan_context(app_a))
        await stack.enter_async_context(app_b.router.lifespan_context(app_b))
        client_a = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_a), base_url="http://testserver"
            )
        )
        client_b = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_b), base_url="http://testserver"
            )
        )

        session_a1 = await start_session(client_a)
        session_a2 = await start_session(client_a)
        held = asyncio.create_task(
            client_a.post(
                f"/api/sessions/{session_a1}/turns", json=turn_body(PART_TEXT, 0)
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)

        # App A's provider capacity is exhausted; its second session cannot use
        # app B's independent semaphore.
        exhausted = await client_a.post(
            f"/api/sessions/{session_a2}/turns", json=turn_body(PART_TEXT, 0)
        )
        assert_error(exhausted, status=429, code="provider_busy")

        # App B keeps working with its own settings, store and semaphore.
        session_b1 = await start_session(client_b)
        completed = await client_b.post(
            f"/api/sessions/{session_b1}/turns", json=turn_body(PART_TEXT, 0)
        )
        assert completed.status_code == 200, completed.text

        # Independent stores: app A's session does not exist in app B.
        missing = await client_b.get(f"/api/sessions/{session_a1}")
        assert_error(missing, status=404, code="session_not_found")

        gate.set()
        finished = await asyncio.wait_for(held, timeout=10)
        assert finished.status_code == 200, finished.text
        assert read_session(dir_a, session_a1)["revision"] == 1


# IT14-10: sanitized unexpected error on the real turn route ------------------


async def case_it14_10(session_dir, caplog):
    settings = settings_from(none_auth_env(session_dir))
    provider = RaisingProvider()
    app = create_app(settings=settings, provider=provider)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            session_id = await start_session(client)
            before = snapshot(session_dir, session_id)
            with caplog.at_level("DEBUG"):
                response = await client.post(
                    f"/api/sessions/{session_id}/turns",
                    json=turn_body(PART_TEXT, 0),
                )

            assert response.status_code == 500
            body = response.json()
            assert body["error"] == {
                "code": "internal_error",
                "message": INTERNAL_MESSAGE,
                "retryable": False,
            }
            assert body["request_id"] == response.headers["x-request-id"]
            assert "RuntimeError" in caplog.text
            for sentinel in (SENTINEL_KEY, SENTINEL_CUSTOMER, "NS-000999", "payment"):
                assert sentinel not in response.text
                assert sentinel not in caplog.text
            assert snapshot(session_dir, session_id) == before
            assert provider.methods() == ["extract"]


CASES = {
    "IT14-01": case_it14_01,
    "IT14-02": case_it14_02,
    "IT14-03": case_it14_03,
    "IT14-04": case_it14_04,
    "IT14-11": case_it14_11,
    "IT14-12": case_it14_12,
    "IT14-13": case_it14_13,
    "IT14-05": case_it14_05,
    "IT14-06": case_it14_06,
    "IT14-07": case_it14_07,
    "IT14-08": case_it14_08,
    "IT14-09": case_it14_09,
    "IT14-10": case_it14_10,
}


@pytest.mark.parametrize("case_id", sorted(CASES))
async def test_it14_startup(case_id, session_dir, tmp_path, monkeypatch, caplog):
    case = CASES[case_id]
    names = case.__code__.co_varnames[: case.__code__.co_argcount]
    kwargs = {}
    if "session_dir" in names:
        kwargs["session_dir"] = session_dir
    if "tmp_path" in names:
        kwargs["tmp_path"] = tmp_path
    if "monkeypatch" in names:
        kwargs["monkeypatch"] = monkeypatch
    if "caplog" in names:
        kwargs["caplog"] = caplog
    await case(**kwargs)
