#!/usr/bin/env python3
"""Operator-invoked live backend compatibility smoke .

This is never a startup hook and never runs in automatic CI: an operator runs it
explicitly against the profiles resolved from the process environment. It uses
the same ``Settings`` and ``OpenAICompatibleProvider`` adapter as the app, so a
pass here is evidence about the real transport, parsing and citation path.

It writes sanitized ``reports/backend-compatibility.json``: UTC ``checked_at``,
source revision, and one entry per check with role, model, scheme/hostname/port
(never the key or the full URL), auth mode, output mode where applicable,
pass/fail, error code and observed latency. It exits nonzero when any role
fails, including a missing real spoken WAV fixture or a mismatch between the
model output and the seeded policy expectations.

Usage:
    python scripts/check_backends.py [--report PATH] [--wav PATH] [--knowledge PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Awaitable, Callable, Mapping
from urllib.parse import urlsplit

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
# Running a script puts its own directory on sys.path, not the repository root.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.config import ServiceProfile, Settings
from gateway.errors import AppError
from gateway.knowledge.retrieval import load_knowledge
from gateway.providers.client import OpenAICompatibleProvider
from gateway.schemas import (
    Extraction,
    ExtractionContext,
    KnowledgeAnswer,
    KnowledgeContext,
    KnowledgeRecord,
    Slots,
)

DEFAULT_REPORT = REPO_ROOT / "reports" / "backend-compatibility.json"
DEFAULT_WAV = REPO_ROOT / "tests" / "fixtures" / "audio" / "spoken.wav"
DEFAULT_KNOWLEDGE = REPO_ROOT / "gateway" / "knowledge" / "northstar-faq.json"

EXTRACTION_MESSAGE = (
    "My order NS-000123 arrived broken. It is a damaged item and high urgency."
)
SUPPORTED_QUESTION = "Can I return an unused item?"
UNSUPPORTED_QUESTION = "Do you ship internationally?"
TTS_TEXT = "Your issue has been recorded for support review."
SUPPORTED_ANSWER_MARKER = "30"

MAX_ANSWER_CHARS = 1200
MAX_TRANSCRIPT_CHARS = 4000
MAX_TTS_BYTES = 5 * 1024 * 1024

ROLES = ("llm", "rag", "stt", "tts")

# Layer III bitrate tables in kbps (index 1..14); version bits 3=MPEG1,
# 2=MPEG2, 0=MPEG2.5 (1 is reserved). Layer bits 1 = Layer III.
_MP3_BITRATES = {
    3: (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    0: (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MP3_SAMPLE_RATES = {
    3: (44100, 48000, 32000),
    2: (22050, 24000, 16000),
    0: (11025, 12000, 8000),
}


class CheckFailure(Exception):
    """A compatibility expectation failed with a fixed, sanitized code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CheckResult:
    role: str
    check: str
    model: str
    scheme: str
    hostname: str | None
    port: int | None
    auth_mode: str
    output_mode: str | None
    passed: bool
    error_code: str | None
    latency_ms: int

    def as_report_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "check": self.check,
            "model": self.model,
            "scheme": self.scheme,
            "hostname": self.hostname,
            "port": self.port,
            "auth_mode": self.auth_mode,
            "output_mode": self.output_mode,
            "pass": self.passed,
            "error_code": self.error_code,
            "latency_ms": self.latency_ms,
        }


def sanitized_endpoint(base_url: str) -> dict[str, object]:
    """Scheme/hostname/port only: never a credential or the full URL."""

    parts = urlsplit(base_url)
    return {"scheme": parts.scheme, "hostname": parts.hostname, "port": parts.port}


def _id3v2_offset(data: bytes) -> int:
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return min(10 + size, len(data))


def mp3_decodable(data: bytes) -> bool:
    """True when ``data`` holds at least one complete MPEG Layer III frame."""

    offset = _id3v2_offset(data)
    while offset + 4 <= len(data):
        first, second, third, fourth = data[offset : offset + 4]
        if first != 0xFF or (second & 0xE0) != 0xE0:
            offset += 1
            continue
        version = (second >> 3) & 0x03
        layer = (second >> 1) & 0x03
        bitrate_index = (third >> 4) & 0x0F
        rate_index = (third >> 2) & 0x03
        padding = (third >> 1) & 0x01
        if version == 1 or layer != 1:
            offset += 1
            continue
        if bitrate_index in (0, 15) or rate_index == 3:
            offset += 1
            continue
        bitrate = _MP3_BITRATES[version][bitrate_index - 1] * 1000
        sample_rate = _MP3_SAMPLE_RATES[version][rate_index]
        coefficient = 144 if version == 3 else 72
        frame_length = (coefficient * bitrate) // sample_rate + padding
        if frame_length >= 4 and offset + frame_length <= len(data):
            return True
        offset += 1
    return False


def wav_decodable(data: bytes) -> bool:
    """True when ``data`` is a complete PCM WAV in the accepted speech window."""

    try:
        with wave.open(BytesIO(data)) as reader:
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            rate = reader.getframerate()
            frame_count = reader.getnframes()
            frames = reader.readframes(frame_count)
    except (EOFError, wave.Error):
        return False
    return (
        1 <= channels <= 2
        and width == 2
        and 8000 <= rate <= 48000
        and frame_count > 0
        and len(frames) == frame_count * channels * 2
    )


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def source_revision(root: Path = REPO_ROOT) -> str:
    """Resolve git HEAD for the evidence record; empty when unavailable."""

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def build_report(
    settings: Settings,
    checks: list[CheckResult],
    *,
    checked_at: datetime,
    source_revision: str,
    error_code: str | None = None,
) -> dict[str, object]:
    passed = (
        error_code is None and bool(checks) and all(check.passed for check in checks)
    )
    return {
        "checked_at": _utc_text(checked_at),
        "source_revision": source_revision,
        "ai_provider": settings.ai_provider,
        "pass": passed,
        "error_code": error_code,
        "checks": [check.as_report_dict() for check in checks],
    }


def exit_code(checks: list[CheckResult]) -> int:
    return 0 if checks and all(check.passed for check in checks) else 1


def _profile(settings: Settings, role: str) -> ServiceProfile:
    return getattr(settings, role)


async def _record(
    role: str,
    check: str,
    profile: ServiceProfile,
    action: Callable[[], Awaitable[None]],
) -> CheckResult:
    endpoint = sanitized_endpoint(profile.base_url)
    started = time.monotonic()
    error_code: str | None = None
    try:
        await action()
    except (AppError, CheckFailure) as exc:
        error_code = exc.code
    except Exception:
        # Never leak exception text, paths, request bodies or credentials.
        error_code = "unexpected_error"
    latency_ms = max(0, int(round((time.monotonic() - started) * 1000)))
    return CheckResult(
        role=role,
        check=check,
        model=profile.model,
        scheme=str(endpoint["scheme"]),
        hostname=endpoint["hostname"]
        if isinstance(endpoint["hostname"], str)
        else None,
        port=endpoint["port"] if isinstance(endpoint["port"], int) else None,
        auth_mode=profile.auth_mode,
        output_mode=getattr(profile, "output_mode", None),
        passed=error_code is None,
        error_code=error_code,
        latency_ms=latency_ms,
    )


def _knowledge_context(knowledge, question: str) -> KnowledgeContext:
    sources = [
        KnowledgeRecord(
            id=record.id,
            title=record.title,
            keywords=list(record.keywords),
            body=record.body,
        )
        for record in knowledge.retrieve(question)
    ]
    return KnowledgeContext(
        question=question, kb_version=knowledge.version, sources=sources
    )


async def run_checks(
    settings: Settings,
    *,
    transports: Mapping[str, httpx.AsyncBaseTransport] | None = None,
    wav_path: Path = DEFAULT_WAV,
    knowledge_path: Path = DEFAULT_KNOWLEDGE,
) -> list[CheckResult]:
    """Run the four-role smoke through the shipped adapter and return one entry per check."""

    profiles = {role: _profile(settings, role) for role in ROLES}
    provider = OpenAICompatibleProvider(
        profiles, transports=transports, capacity=settings.provider_max_concurrency
    )
    knowledge = load_knowledge(knowledge_path)
    results: list[CheckResult] = []

    async def extraction() -> None:
        context = ExtractionContext(
            slots=Slots(
                order_number=None, category=None, description=None, urgency=None
            ),
            pending_clarifications={},
            recent_messages=[],
            current_message=EXTRACTION_MESSAGE,
            status="collecting",
            intake_started=False,
            frustration_active=False,
        )
        extraction_result: Extraction = await provider.extract(context)
        candidates = extraction_result.candidates
        observed = (
            candidates.order_number,
            candidates.category,
            candidates.urgency,
        )
        if observed != ("NS-000123", "damaged_item", "high"):
            raise CheckFailure("fixture_mismatch")

    async def rag_supported() -> None:
        context = _knowledge_context(knowledge, SUPPORTED_QUESTION)
        if not context.sources:
            raise CheckFailure("fixture_mismatch")
        answer: KnowledgeAnswer = await provider.answer_knowledge(context)
        retrieved_ids = {source.id for source in context.sources}
        if not answer.answerable:
            raise CheckFailure("fixture_mismatch")
        if not 1 <= len(answer.answer.strip()) <= MAX_ANSWER_CHARS:
            raise CheckFailure("fixture_mismatch")
        if not answer.cited_ids or not set(answer.cited_ids) <= retrieved_ids:
            raise CheckFailure("citation_mismatch")
        if SUPPORTED_ANSWER_MARKER not in answer.answer:
            raise CheckFailure("fixture_mismatch")

    async def rag_unsupported() -> None:
        context = _knowledge_context(knowledge, UNSUPPORTED_QUESTION)
        if not context.sources:
            raise CheckFailure("fixture_mismatch")
        answer: KnowledgeAnswer = await provider.answer_knowledge(context)
        if answer.answerable or answer.answer != "" or answer.cited_ids:
            raise CheckFailure("fixture_mismatch")

    async def transcription() -> None:
        path = Path(wav_path)
        if not path.is_file():
            raise CheckFailure("fixture_missing")
        text = await provider.transcribe(path.read_bytes())
        if not isinstance(text, str) or not text.strip():
            raise CheckFailure("fixture_mismatch")
        if len(text) > MAX_TRANSCRIPT_CHARS:
            raise CheckFailure("fixture_mismatch")

    async def synthesis() -> None:
        result = await provider.synthesize(TTS_TEXT)
        if not 0 < len(result.data) <= MAX_TTS_BYTES:
            raise CheckFailure("fixture_mismatch")
        decodable = (
            mp3_decodable(result.data)
            if result.media_type == "audio/mpeg"
            else wav_decodable(result.data)
        )
        if not decodable:
            raise CheckFailure("undecodable_audio")

    try:
        results.append(await _record("llm", "extraction", profiles["llm"], extraction))
        results.append(
            await _record("rag", "rag_supported", profiles["rag"], rag_supported)
        )
        results.append(
            await _record("rag", "rag_unsupported", profiles["rag"], rag_unsupported)
        )
        results.append(
            await _record("stt", "transcription", profiles["stt"], transcription)
        )
        results.append(await _record("tts", "synthesis", profiles["tts"], synthesis))
    finally:
        await provider.close()
    return results


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Operator-invoked live OpenAI-compatible backend compatibility smoke."
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--wav", type=Path, default=DEFAULT_WAV)
    parser.add_argument("--knowledge", type=Path, default=DEFAULT_KNOWLEDGE)
    args = parser.parse_args(argv)

    checked_at = datetime.now(timezone.utc)
    revision = source_revision()
    try:
        # Only the four model roles are exercised; storage and Backend API values
        # are required by Settings but unused here, so they default.
        settings = Settings.from_env(
            {
                "SESSION_DIR": "data/sessions",
                "BACKEND_API_BASE_URL": "http://127.0.0.1:8001",
                **os.environ,
            }
        )
    except RuntimeError:
        _write_report(
            args.report,
            {
                "checked_at": _utc_text(checked_at),
                "source_revision": revision,
                "ai_provider": "openai_compatible",
                "pass": False,
                "error_code": "configuration_invalid",
                "checks": [],
            },
        )
        print("backend compatibility: configuration_invalid", file=sys.stderr)
        return 1

    checks: list[CheckResult] = []
    error_code: str | None = None
    try:
        checks = asyncio.run(
            run_checks(settings, wav_path=args.wav, knowledge_path=args.knowledge)
        )
    except (AppError, CheckFailure) as exc:
        error_code = exc.code
    except Exception:
        error_code = "unexpected_error"

    report = build_report(
        settings,
        checks,
        checked_at=checked_at,
        source_revision=revision,
        error_code=error_code,
    )
    _write_report(args.report, report)

    for check in checks:
        status = "pass" if check.passed else "fail"
        detail = f" ({check.error_code})" if check.error_code else ""
        print(f"{check.role}/{check.check}: {status}{detail}")
    if error_code is not None:
        print(f"backend compatibility: {error_code}", file=sys.stderr)
        return 1
    return exit_code(checks)


if __name__ == "__main__":
    sys.exit(main())
