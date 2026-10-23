"""Unit test for the sanitized example session and the delivery README.

Covers BS section 12 deliverables, TC section 9 definition of done and the
ORB-17 acceptance criteria: ``examples/session.example.json`` must validate
against the stored ``Session`` schema and the README must contain the required
sections, the exact test commands and honest gate status.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.prompts import GREETING, RECEIPT_TEMPLATE, build_review_reply, build_summary
from app.schemas import Session

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PATH = ROOT / "examples" / "session.example.json"
README_PATH = ROOT / "README.md"
KB_PATH = ROOT / "app" / "knowledge" / "northstar-faq.json"

REQUIRED_HEADINGS = [
    "Fictional scenario",
    "Chosen models and provider profiles",
    "Setup and exact commands",
    "Architecture and data flow",
    "State and validation rules",
    "Example session JSON",
    "Voice workflow",
    "RAG corpus and citation display",
    "Failure behaviour",
    "Quality gates: commands and actual status",
    "Limitations",
    "Deferred bonuses and potential improvements",
    "Demo video",
]

REQUIRED_COMMANDS = [
    ".venv/bin/python -m pip check",
    ".venv/bin/python -m pytest tests/unit tests/integration -q",
    ".venv/bin/python -m pytest tests/unit tests/integration --cov=app --cov-branch --cov-context=test --cov-report=json:reports/coverage.json --cov-report=term-missing --cov-report=xml:reports/coverage.xml --junitxml=reports/unit-integration.xml",
    ".venv/bin/python -m pytest tests/e2e --browser chromium --tracing retain-on-failure --screenshot only-on-failure --video retain-on-failure --output reports/playwright --junitxml=reports/e2e.xml",
    ".venv/bin/python scripts/check_e2e_inventory.py --spec docs/implementation-contract.md --manifest tests/e2e/cases.json --collection reports/e2e-collection.json --results reports/e2e-results.json",
    ".venv/bin/python -m pytest tests/unit/test_example_session.py -q",
]


def _load_example() -> Session:
    return Session.model_validate_json(EXAMPLE_PATH.read_text(encoding="utf-8"))


def _readme() -> str:
    return README_PATH.read_text(encoding="utf-8")


def test_example_session_is_a_valid_completed_session():
    session = _load_example()
    assert session.schema_version == 1
    assert session.status == "completed"
    assert session.confirmation is not None
    assert session.confirmation.review_revision == 1
    assert session.pending_review_revision is None
    assert session.pending_clarifications == {}
    assert session.review_rejected is False
    assert session.collection_turn_count == 2
    assert session.slots.order_number == "NS-000123"
    assert session.slots.category == "damaged_item"
    assert session.slots.urgency == "high"
    assert session.summary == build_summary(session.slots)


def test_example_session_transcript_covers_intake_policy_question_and_confirmation():
    session = _load_example()
    assert [message.role for message in session.messages] == [
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert session.messages[0].text == GREETING
    assert session.messages[2].text == build_review_reply(session.slots)
    policy_answer = session.messages[4]
    assert "30 calendar days" in policy_answer.text
    assert "support review" in policy_answer.text
    assert "What is your order number" not in policy_answer.text
    assert session.messages[5].text == "Confirm reviewed intake"
    assert session.messages[6].text == RECEIPT_TEMPLATE.format(
        session_id=session.session_id
    )


def test_example_session_citation_matches_the_shipped_knowledge_base():
    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
    records = {record["id"]: record for record in kb["records"]}
    session = _load_example()
    citations = [
        citation for message in session.messages for citation in message.citations
    ]
    assert len(citations) == 1
    citation = citations[0]
    assert citation.source_id == "faq-returns"
    assert citation.title == records["faq-returns"]["title"]
    assert citation.excerpt == records["faq-returns"]["body"]
    assert citation.kb_version == kb["version"]


def test_example_session_replay_records_cover_all_three_requests():
    session = _load_example()
    assert len(session.requests) == 3
    assert [record.response.revision for record in session.requests] == [1, 2, 3]
    for record in session.requests:
        assert re.fullmatch(r"[0-9a-f]{64}", record.input_digest)


def test_example_session_is_sanitized():
    raw = EXAMPLE_PATH.read_text(encoding="utf-8")
    for token in (
        "sk-",
        "Bearer",
        "API_KEY",
        "password",
        "@",
        "http://",
        "https://",
        "/home/",
        "C:\\",
    ):
        assert token not in raw
    assert "NS-000123" in raw


@pytest.mark.parametrize("heading", REQUIRED_HEADINGS)
def test_readme_has_required_section(heading):
    assert re.search(rf"^## {re.escape(heading)}\s*$", _readme(), re.MULTILINE)


@pytest.mark.parametrize("command", REQUIRED_COMMANDS)
def test_readme_lists_the_exact_command(command):
    assert command in _readme()


def test_readme_documents_all_four_fictional_kb_records():
    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
    assert len(kb["records"]) == 4
    text = _readme()
    for record in kb["records"]:
        assert record["id"] in text
        assert record["title"] in text


def test_readme_marks_the_demo_video_as_pending_operator_recording():
    text = _readme()
    assert "Demo video: pending operator recording" in text
    assert "youtu" not in text.lower()
    assert "vimeo" not in text.lower()


def test_readme_states_unverified_gates_without_inventing_results():
    text = _readme()
    assert "not verified" in text
    assert "Mutation gate: incomplete" in text
    assert "Live provider smoke: not verified" in text


def test_readme_states_the_core_limitations():
    text = _readme().lower()
    for phrase in ("single process", "no authentication", "local only"):
        assert phrase in text
