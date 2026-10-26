#!/usr/bin/env python3
"""Operator-invoked local evaluation of emotion classification (FEAT-01).

This is never a startup hook and never runs in automatic CI. An operator runs it
explicitly with the configured LLM environment exported. It sends one existing
extraction call per reviewed case through the same ``Settings`` and
``OpenAICompatibleProvider`` adapter as the application, then records how the
configured model labelled each message.

The report is sanitized: model id, output mode, scheme/hostname/port and the
observed labels only. It never contains an API key or the full base URL. A label
mismatch is recorded as evaluation data; the process exits nonzero only for a
configuration error or a failed provider call, not for imperfect accuracy.

Usage:
    python scripts/evaluate_emotions.py [--report PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
# Running a script puts its own directory on sys.path, not the repository root.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.config import Settings
from gateway.errors import AppError
from gateway.providers.client import OpenAICompatibleProvider
from gateway.providers.prompts import EMOTION_EVALUATION_CASES
from gateway.schemas import ExtractionContext, RecentMessage, Slots

DEFAULT_REPORT = REPO_ROOT / "reports" / "emotion-evaluation.json"

ROLES = ("llm", "rag", "stt", "tts")


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_context(case: dict[str, object]) -> ExtractionContext:
    """Build the same extraction context the engine builds for a committed turn."""

    return ExtractionContext(
        slots=Slots(
            order_number=None, category=None, description=None, urgency=None
        ),
        pending_clarifications={},
        recent_messages=[
            RecentMessage(role=message["role"], text=message["text"])
            for message in case["recent_messages"]
        ],
        current_message=case["message"],
        status="collecting",
        intake_started=False,
        frustration_active=case["frustration_active"],
    )


async def evaluate_cases(provider, cases) -> list[dict[str, object]]:
    """One extraction call per case; a failed call is recorded, not retried here."""

    records: list[dict[str, object]] = []
    for case in cases:
        record: dict[str, object] = {
            "id": case["id"],
            "message": case["message"],
            "expected_sentiment": case["sentiment"],
            "expected_frustration": case["frustration"],
            "observed_sentiment": None,
            "observed_frustration": None,
            "observed_detected_language": None,
            "match": False,
            "error_code": None,
        }
        try:
            result = await provider.extract(build_context(case))
        except AppError as exc:
            record["error_code"] = exc.code
        else:
            record["observed_sentiment"] = result.sentiment
            record["observed_frustration"] = result.frustration
            record["observed_detected_language"] = result.detected_language
            record["match"] = (
                result.sentiment == case["sentiment"]
                and result.frustration == case["frustration"]
            )
        records.append(record)
    return records


def sanitized_endpoint(base_url: str | None) -> dict[str, object] | None:
    """Scheme/hostname/port only: never a credential or the full URL."""

    if base_url is None:
        return None
    parts = urlsplit(base_url)
    return {"scheme": parts.scheme, "hostname": parts.hostname, "port": parts.port}


def build_report(
    *,
    model: str | None,
    output_mode: str | None,
    base_url: str | None,
    records: list[dict[str, object]],
    checked_at: datetime,
    source_revision: str,
    error_code: str | None,
) -> dict[str, object]:
    matched = sum(1 for record in records if record["match"])
    calls_succeeded = bool(records) and all(
        record["error_code"] is None for record in records
    )
    return {
        "checked_at": _utc_text(checked_at),
        "source_revision": source_revision,
        "model": model,
        "output_mode": output_mode,
        "endpoint": sanitized_endpoint(base_url),
        "pass": error_code is None and calls_succeeded,
        "error_code": error_code,
        "total": len(records),
        "matched": matched,
        "cases": records,
    }


def exit_code(records: list[dict[str, object]], *, error_code: str | None) -> int:
    if error_code is not None or not records:
        return 1
    return 0 if all(record["error_code"] is None for record in records) else 1


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


async def run_evaluation(settings: Settings) -> list[dict[str, object]]:
    profiles = {role: getattr(settings, role) for role in ROLES}
    provider = OpenAICompatibleProvider(
        profiles, capacity=settings.provider_max_concurrency
    )
    try:
        return await evaluate_cases(provider, EMOTION_EVALUATION_CASES)
    finally:
        await provider.close()


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Operator-invoked local emotion classification evaluation."
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)

    checked_at = datetime.now(timezone.utc)
    revision = source_revision()
    try:
        settings = Settings.from_env()
    except RuntimeError:
        _write_report(
            args.report,
            build_report(
                model=None,
                output_mode=None,
                base_url=None,
                records=[],
                checked_at=checked_at,
                source_revision=revision,
                error_code="configuration_invalid",
            ),
        )
        print("emotion evaluation: configuration_invalid", file=sys.stderr)
        return 1

    error_code: str | None = None
    records: list[dict[str, object]] = []
    try:
        records = asyncio.run(run_evaluation(settings))
    except AppError as exc:
        error_code = exc.code
    except Exception:
        # Never leak exception text, request bodies or credentials.
        error_code = "unexpected_error"

    report = build_report(
        model=settings.llm.model,
        output_mode=settings.llm.output_mode,
        base_url=settings.llm.base_url,
        records=records,
        checked_at=checked_at,
        source_revision=revision,
        error_code=error_code,
    )
    _write_report(args.report, report)

    for record in records:
        detail = f" ({record['error_code']})" if record["error_code"] else ""
        status = "match" if record["match"] else "mismatch"
        print(
            f"{record['id']}: {status}{detail} "
            f"expected={record['expected_sentiment']}/{record['expected_frustration']} "
            f"observed={record['observed_sentiment']}/{record['observed_frustration']}"
        )
    if error_code is not None:
        print(f"emotion evaluation: {error_code}", file=sys.stderr)
        return 1
    return exit_code(records, error_code=None)


if __name__ == "__main__":
    sys.exit(main())
