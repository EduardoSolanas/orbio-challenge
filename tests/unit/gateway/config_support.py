"""Shared helpers extracted from test_config."""

from __future__ import annotations

import pytest

from gateway.config import Settings

SESSION_DIR = "/tmp/orbio-sessions"


def valid_environ():
    return {
        "AI_PROVIDER": "openai_compatible",
        "LLM_BASE_URL": "http://host.docker.internal:8001/v1",
        "LLM_MODEL": "extraction-model",
        "LLM_AUTH_MODE": "none",
        "LLM_API_KEY": "",
        "LLM_OUTPUT_MODE": "json_schema",
        "STT_BASE_URL": "http://host.docker.internal:8002/v1",
        "STT_MODEL": "transcription-model",
        "STT_AUTH_MODE": "none",
        "STT_API_KEY": "",
        "TTS_BASE_URL": "http://host.docker.internal:8003/v1",
        "TTS_MODEL": "speech-model",
        "TTS_AUTH_MODE": "none",
        "TTS_API_KEY": "",
        "TTS_VOICE": "alloy",
        "SESSION_DIR": SESSION_DIR,
        "PROVIDER_MAX_CONCURRENCY": "4",
        "BACKEND_API_BASE_URL": "http://backend-api.test",
    }


def complete_rag_environ():
    return {
        "RAG_BASE_URL": "http://rag.internal:9001/v1",
        "RAG_MODEL": "rag-model",
        "RAG_AUTH_MODE": "none",
        "RAG_API_KEY": "",
        "RAG_OUTPUT_MODE": "json_object",
    }


CONFIGURATION_INVALID = (
    "configuration_invalid: Check required provider and model settings."
)


def assert_configuration_invalid(environ):
    with pytest.raises(RuntimeError) as excinfo:
        Settings.from_env(environ)

    assert str(excinfo.value) == CONFIGURATION_INVALID
