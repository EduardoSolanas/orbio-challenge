"""Seed knowledge-base citations and scripted RAG answers."""

from __future__ import annotations

import json
from pathlib import Path

from gateway.schemas import KnowledgeAnswer
from tests.integration.support.api import make_extraction  # noqa: F401
from tests.support.extraction_builders import question_extraction  # noqa: F401  (re-exported to the tests)

SEED = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "gateway"
        / "knowledge"
        / "northstar-faq.json"
    ).read_text(encoding="utf-8")
)

KB_VERSION = SEED["version"]

# The English records as the model receives them for an English reply (no "es" translation).
SEED_RECORDS = {
    record["id"]: {key: value for key, value in record.items() if key != "es"}
    for record in SEED["records"]
}

RETURN_Q = "Can I return an unused item?"

RETURN_ANSWER_TEXT = (
    "You can request a return within 30 calendar days of delivery for an unused item in "
    "its original packaging. Refund eligibility requires support review."
)


SPANISH_RECORDS = {
    record["id"]: record["es"] for record in SEED["records"] if "es" in record
}


def seed_citation(record_id: str, language: str = "en") -> dict:
    """The citation for a record, from its Spanish version when the reply is Spanish."""

    record = SPANISH_RECORDS[record_id] if language == "es" else SEED_RECORDS[record_id]
    return {
        "source_id": record_id,
        "title": record["title"],
        "excerpt": record["body"],
        "kb_version": KB_VERSION,
    }


def grounded(answer: str, cited_ids: list[str]) -> KnowledgeAnswer:
    return KnowledgeAnswer(answerable=True, answer=answer, cited_ids=cited_ids)
