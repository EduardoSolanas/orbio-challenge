"""Seed knowledge-base citations and scripted RAG answers."""

from __future__ import annotations

import json
from pathlib import Path

from gateway.schemas import KnowledgeAnswer
from tests.integration.support.api import make_extraction

SEED = json.loads(
    (Path(__file__).resolve().parents[3] / "gateway" / "knowledge" / "northstar-faq.json").read_text(
        encoding="utf-8"
    )
)

KB_VERSION = SEED["version"]

SEED_RECORDS = {record["id"]: record for record in SEED["records"]}

RETURN_Q = "Can I return an unused item?"

RETURN_ANSWER_TEXT = (
    "You can request a return within 30 calendar days of delivery for an unused item in "
    "its original packaging. Refund eligibility requires support review."
)


def seed_citation(record_id: str) -> dict:
    record = SEED_RECORDS[record_id]
    return {
        "source_id": record_id,
        "title": record["title"],
        "excerpt": record["body"],
        "kb_version": KB_VERSION,
    }


def question_extraction():
    return make_extraction(intent="question", has_question=True)


def grounded(answer: str, cited_ids: list[str]) -> KnowledgeAnswer:
    return KnowledgeAnswer(answerable=True, answer=answer, cited_ids=cited_ids)
