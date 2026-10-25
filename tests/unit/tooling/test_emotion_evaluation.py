"""Unit tests for the operator-invoked emotion evaluation script.

The script is exercised against a fake provider: the only doubled dependency is
the remote model boundary. Context construction, one-call-per-case accounting,
observed-label recording, sanitization and exit codes are asserted.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import evaluate_emotions  # noqa: E402

from backend.gateway.errors import app_error  # noqa: E402
from backend.gateway.providers.prompts import EMOTION_EVALUATION_CASES  # noqa: E402
from backend.gateway.schemas import Extraction, Slots  # noqa: E402

EMPTY_SLOTS = Slots(order_number=None, category=None, description=None, urgency=None)


def extraction(sentiment: str = "neutral", frustration: str = "none", detected_language: str = "en") -> Extraction:
    return Extraction(
        intent="unrelated",
        has_question=False,
        candidates=EMPTY_SLOTS,
        explicit_corrections=[],
        ambiguous_fields=[],
        detected_language=detected_language,
        requested_language=None,
        sentiment=sentiment,
        frustration=frustration,
    )


class FakeProvider:
    def __init__(self, script):
        self.script = list(script)
        self.contexts = []
        self.closed = False

    async def extract(self, context):
        self.contexts.append(context)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.closed = True


def case(case_id: str, message: str, sentiment: str, frustration: str, **extra) -> dict:
    return {
        "id": case_id,
        "message": message,
        "recent_messages": extra.get("recent_messages", ()),
        "frustration_active": extra.get("frustration_active", False),
        "sentiment": sentiment,
        "frustration": frustration,
    }


def test_build_context_uses_recent_committed_messages_and_the_active_flag():
    context = evaluate_emotions.build_context(EMOTION_EVALUATION_CASES[7])

    assert context.current_message == "I am still frustrated, but thank you for explaining."
    assert context.frustration_active is True
    assert [(message.role, message.text) for message in context.recent_messages] == [
        ("user", "I have asked three times. This is really frustrating."),
        (
            "assistant",
            "I understand this is frustrating. Can you share the energy account reference?",
        ),
    ]
    assert context.slots == EMPTY_SLOTS


async def test_evaluate_cases_records_observed_labels_with_one_call_per_case():
    cases = (
        case("EV01", "How long does delivery take?", "neutral", "none"),
        case("EV02", "Thank you, that solved it.", "positive", "none"),
        case("EV03", "I have asked three times. This is really frustrating.", "negative", "present"),
    )
    provider = FakeProvider(
        [
            extraction("neutral", "none"),
            extraction("positive", "none"),
            extraction("negative", "present"),
        ]
    )

    records = await evaluate_emotions.evaluate_cases(provider, cases)

    assert len(provider.contexts) == 3
    assert [context.current_message for context in provider.contexts] == [
        item["message"] for item in cases
    ]
    assert records == [
        {
            "id": "EV01",
            "message": "How long does delivery take?",
            "expected_sentiment": "neutral",
            "expected_frustration": "none",
            "observed_sentiment": "neutral",
            "observed_frustration": "none",
            "observed_detected_language": "en",
            "match": True,
            "error_code": None,
        },
        {
            "id": "EV02",
            "message": "Thank you, that solved it.",
            "expected_sentiment": "positive",
            "expected_frustration": "none",
            "observed_sentiment": "positive",
            "observed_frustration": "none",
            "observed_detected_language": "en",
            "match": True,
            "error_code": None,
        },
        {
            "id": "EV03",
            "message": "I have asked three times. This is really frustrating.",
            "expected_sentiment": "negative",
            "expected_frustration": "present",
            "observed_sentiment": "negative",
            "observed_frustration": "present",
            "observed_detected_language": "en",
            "match": True,
            "error_code": None,
        },
    ]


async def test_evaluate_cases_records_a_mismatch_without_failing_the_run():
    cases = (case("EV01", "Great. Just great.", "uncertain", "uncertain"),)
    provider = FakeProvider([extraction("positive", "none")])

    records = await evaluate_emotions.evaluate_cases(provider, cases)

    assert records[0]["match"] is False
    assert records[0]["observed_sentiment"] == "positive"
    assert records[0]["error_code"] is None
    assert evaluate_emotions.exit_code(records, error_code=None) == 0


async def test_evaluate_cases_records_a_provider_error_and_continues():
    cases = (
        case("EV01", "My item is broken.", "negative", "none"),
        case("EV02", "Thank you, that solved it.", "positive", "none"),
    )
    provider = FakeProvider([app_error("provider_timeout"), extraction("positive", "none")])

    records = await evaluate_emotions.evaluate_cases(provider, cases)

    assert records[0]["error_code"] == "provider_timeout"
    assert records[0]["observed_sentiment"] is None
    assert records[0]["match"] is False
    assert records[1]["match"] is True
    assert len(provider.contexts) == 2
    assert evaluate_emotions.exit_code(records, error_code=None) == 1


def test_build_report_is_sanitized_and_summarizes_the_outcome():
    records = [
        {
            "id": "EV01",
            "message": "How long does delivery take?",
            "expected_sentiment": "neutral",
            "expected_frustration": "none",
            "observed_sentiment": "neutral",
            "observed_frustration": "none",
            "observed_detected_language": "en",
            "match": True,
            "error_code": None,
        },
        {
            "id": "EV02",
            "message": "Great. Just great.",
            "expected_sentiment": "uncertain",
            "expected_frustration": "uncertain",
            "observed_sentiment": "positive",
            "observed_frustration": "none",
            "observed_detected_language": "en",
            "match": False,
            "error_code": None,
        },
    ]
    report = evaluate_emotions.build_report(
        model="local-model",
        output_mode="prompt_json",
        base_url="http://model.test:8080/v1",
        records=records,
        checked_at=datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc),
        source_revision="abc123",
        error_code=None,
    )

    assert report["checked_at"] == "2026-10-23T12:00:00Z"
    assert report["source_revision"] == "abc123"
    assert report["model"] == "local-model"
    assert report["output_mode"] == "prompt_json"
    assert report["endpoint"] == {
        "scheme": "http",
        "hostname": "model.test",
        "port": 8080,
    }
    assert report["cases"] == records
    assert report["matched"] == 1
    assert report["total"] == 2
    assert report["pass"] is True
    serialized = repr(report)
    assert "http://model.test:8080/v1" not in serialized
    assert "api_key" not in serialized.lower()


def test_build_report_with_a_configuration_error_has_no_cases():
    report = evaluate_emotions.build_report(
        model=None,
        output_mode=None,
        base_url=None,
        records=[],
        checked_at=datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc),
        source_revision="",
        error_code="configuration_invalid",
    )

    assert report["pass"] is False
    assert report["error_code"] == "configuration_invalid"
    assert report["endpoint"] is None
    assert report["cases"] == []


def test_evaluation_cases_cover_the_required_examples():
    messages = {item["message"] for item in EMOTION_EVALUATION_CASES}
    assert {
        "How long does delivery take?",
        "Thank you, that solved it.",
        "My item is broken.",
        "I have asked three times. This is really frustrating.",
        "I am not frustrated; I just need the policy.",
        "The example customer says ‘I am furious.’",
        "Great. Just great.",
    } <= messages
