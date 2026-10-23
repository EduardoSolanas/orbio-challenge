"""Tests for scripts/check_backends.py (PC §5, ORB-16).

The operator smoke is exercised against per-role ``httpx.MockTransport``
handlers: no real host is contacted and no credential leaves the process. The
report shape, sanitization and nonzero exit on any role failure are asserted.
"""

from __future__ import annotations

import asyncio
import json
import sys
import wave
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_backends  # noqa: E402

from app.config import Settings  # noqa: E402

KNOWLEDGE_PATH = REPO_ROOT / "app" / "knowledge" / "northstar-faq.json"
MP3_BYTES = (REPO_ROOT / "tests" / "fixtures" / "reply.mp3").read_bytes()

EXTRACTION_PAYLOAD = {
    "intent": "provide_details",
    "has_question": False,
    "candidates": {
        "order_number": "NS-000123",
        "category": "damaged_item",
        "description": "arrived broken",
        "urgency": "high",
    },
    "explicit_corrections": [],
    "ambiguous_fields": [],
    "detected_language": "en",
    "requested_language": None,
}
RETURN_ANSWER_PAYLOAD = {
    "answerable": True,
    "answer": (
        "You can request a return within 30 calendar days of delivery for an "
        "unused item in its original packaging."
    ),
    "cited_ids": ["faq-returns"],
}
ABSTENTION_PAYLOAD = {"answerable": False, "answer": "", "cited_ids": []}


def build_settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        {
            "AI_PROVIDER": "openai_compatible",
            "LLM_BASE_URL": "http://llm.test:8001/v1",
            "LLM_MODEL": "llm-model",
            "LLM_AUTH_MODE": "none",
            "LLM_OUTPUT_MODE": "json_schema",
            "RAG_BASE_URL": "http://rag.test:8002/v1",
            "RAG_MODEL": "rag-model",
            "RAG_AUTH_MODE": "bearer",
            "RAG_API_KEY": "rag-key",
            "RAG_OUTPUT_MODE": "json_object",
            "STT_BASE_URL": "http://stt.test:8003/v1",
            "STT_MODEL": "stt-model",
            "STT_AUTH_MODE": "none",
            "TTS_BASE_URL": "http://tts.test:8004/v1",
            "TTS_MODEL": "tts-model",
            "TTS_AUTH_MODE": "bearer",
            "TTS_API_KEY": "tts-key",
            "TTS_VOICE": "alloy",
            "SESSION_DIR": str(tmp_path),
            "PROVIDER_MAX_CONCURRENCY": "2",
        }
    )


def streamed_json(payload: dict) -> httpx.Response:
    body = json.dumps(payload).encode("utf-8")
    return httpx.Response(
        200,
        stream=httpx.ByteStream(body),
        headers={"content-type": "application/json"},
    )


def chat_response(payload: dict) -> httpx.Response:
    return streamed_json(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(payload)},
                }
            ]
        }
    )


def make_wav() -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * 16000)
    return buffer.getvalue()


def make_transports(
    *, tts_status: int = 200, rag_always_supported: bool = False
) -> tuple[dict[str, httpx.AsyncBaseTransport], list[tuple[str, httpx.Request]]]:
    calls: list[tuple[str, httpx.Request]] = []

    def llm(request: httpx.Request) -> httpx.Response:
        calls.append(("llm", request))
        return chat_response(EXTRACTION_PAYLOAD)

    def rag(request: httpx.Request) -> httpx.Response:
        calls.append(("rag", request))
        if b"internationally" in request.content:
            if rag_always_supported:
                return chat_response(
                    {
                        "answerable": True,
                        "answer": "We ship internationally.",
                        "cited_ids": ["faq-delivery"],
                    }
                )
            return chat_response(ABSTENTION_PAYLOAD)
        return chat_response(RETURN_ANSWER_PAYLOAD)

    def stt(request: httpx.Request) -> httpx.Response:
        calls.append(("stt", request))
        return streamed_json({"text": "My order NS-000123 arrived broken"})

    def tts(request: httpx.Request) -> httpx.Response:
        calls.append(("tts", request))
        if tts_status != 200:
            return streamed_json({"error": {"message": "denied"}}).copy() if tts_status == 200 else httpx.Response(tts_status, stream=httpx.ByteStream(b"{\"error\":{\"message\":\"denied\"}}"), headers={"content-type": "application/json"})
        return httpx.Response(
            200,
            stream=httpx.ByteStream(MP3_BYTES),
            headers={"content-type": "audio/mpeg"},
        )

    return (
        {
            "llm": httpx.MockTransport(llm),
            "rag": httpx.MockTransport(rag),
            "stt": httpx.MockTransport(stt),
            "tts": httpx.MockTransport(tts),
        },
        calls,
    )


async def test_run_checks_passes_every_role_with_controlled_transport(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    wav = tmp_path / "spoken.wav"
    wav.write_bytes(make_wav())
    transports, calls = make_transports()

    checks = await check_backends.run_checks(
        settings, transports=transports, wav_path=wav, knowledge_path=KNOWLEDGE_PATH
    )

    assert check_backends.exit_code(checks) == 0
    assert all(check.passed for check in checks)
    assert {check.role for check in checks} == {"llm", "rag", "stt", "tts"}
    rag_checks = [check for check in checks if check.role == "rag"]
    assert {check.check for check in rag_checks} == {"rag_supported", "rag_unsupported"}
    assert {role for role, _ in calls} == {"llm", "rag", "stt", "tts"}


async def test_report_is_sanitized_and_has_every_contract_field(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    wav = tmp_path / "spoken.wav"
    wav.write_bytes(make_wav())
    transports, _ = make_transports()
    checks = await check_backends.run_checks(
        settings, transports=transports, wav_path=wav, knowledge_path=KNOWLEDGE_PATH
    )

    report = check_backends.build_report(
        settings,
        checks,
        checked_at=datetime(2026, 10, 23, 3, 0, tzinfo=timezone.utc),
        source_revision="abc123",
    )

    assert report["checked_at"] == "2026-10-23T03:00:00Z"
    assert report["source_revision"] == "abc123"
    assert report["pass"] is True
    assert len(report["checks"]) == 5
    for entry in report["checks"]:
        assert set(entry) == {
            "role",
            "check",
            "model",
            "scheme",
            "hostname",
            "port",
            "auth_mode",
            "output_mode",
            "pass",
            "error_code",
            "latency_ms",
        }
        assert isinstance(entry["latency_ms"], int)
        assert entry["latency_ms"] >= 0
        assert entry["pass"] is True
        assert entry["error_code"] is None

    rag_entry = next(entry for entry in report["checks"] if entry["role"] == "rag")
    assert rag_entry["auth_mode"] == "bearer"
    assert rag_entry["output_mode"] == "json_object"
    assert rag_entry["hostname"] == "rag.test"
    assert rag_entry["port"] == 8002
    tts_entry = next(entry for entry in report["checks"] if entry["role"] == "tts")
    assert tts_entry["output_mode"] is None

    dumped = json.dumps(report)
    assert "rag-key" not in dumped
    assert "tts-key" not in dumped
    assert "http://" not in dumped
    assert "/v1" not in dumped


async def test_any_role_failure_makes_exit_nonzero(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    wav = tmp_path / "spoken.wav"
    wav.write_bytes(make_wav())
    transports, _ = make_transports(tts_status=401)

    checks = await check_backends.run_checks(
        settings, transports=transports, wav_path=wav, knowledge_path=KNOWLEDGE_PATH
    )

    tts_check = next(check for check in checks if check.role == "tts")
    assert tts_check.passed is False
    assert tts_check.error_code == "provider_configuration"
    assert check_backends.exit_code(checks) == 1


async def test_unsupported_question_must_abstain(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    wav = tmp_path / "spoken.wav"
    wav.write_bytes(make_wav())
    transports, _ = make_transports(rag_always_supported=True)

    checks = await check_backends.run_checks(
        settings, transports=transports, wav_path=wav, knowledge_path=KNOWLEDGE_PATH
    )

    unsupported = next(check for check in checks if check.check == "rag_unsupported")
    assert unsupported.passed is False
    assert unsupported.error_code == "fixture_mismatch"
    assert check_backends.exit_code(checks) == 1


async def test_missing_spoken_fixture_fails_stt_without_calling_the_host(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path)
    transports, calls = make_transports()

    checks = await check_backends.run_checks(
        settings,
        transports=transports,
        wav_path=tmp_path / "absent.wav",
        knowledge_path=KNOWLEDGE_PATH,
    )

    stt_check = next(check for check in checks if check.role == "stt")
    assert stt_check.passed is False
    assert stt_check.error_code == "fixture_missing"
    assert not [role for role, _ in calls if role == "stt"]
    assert check_backends.exit_code(checks) == 1


def test_mp3_decodability_accepts_real_audio_and_rejects_garbage() -> None:
    assert check_backends.mp3_decodable(MP3_BYTES)
    assert not check_backends.mp3_decodable(b"not-an-mp3" * 20)
    assert not check_backends.mp3_decodable(b"")


def test_sanitized_endpoint_drops_credentials_and_path() -> None:
    endpoint = check_backends.sanitized_endpoint(
        "https://operator:secret@example.test:8443/v1"
    )
    assert endpoint == {"scheme": "https", "hostname": "example.test", "port": 8443}
    assert "secret" not in json.dumps(endpoint)
    assert "/v1" not in json.dumps(endpoint)
