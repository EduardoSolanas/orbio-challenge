"""Spanish titles and bodies on the knowledge records: loaded, validated, shown, cited."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from gateway.conversation.engine import ConversationEngine
from gateway.knowledge.retrieval import KnowledgeInvalid, load_knowledge
from gateway.schemas import Citation, KnowledgeAnswer, TurnRequest
from gateway.storage import JsonSessionRepository
from tests.support.unit_fakes import FakeProvider, make_extraction

FAQ = Path(__file__).resolve().parents[4] / "gateway/knowledge/northstar-faq.json"

RECORD = {
    "id": "faq-delivery",
    "title": "Delivery times",
    "keywords": ["delivery"],
    "body": "Standard delivery takes 2 to 5 business days.",
}
SPANISH = {"title": "Plazos de entrega", "body": "La entrega tarda de 2 a 5 días."}


def write_kb(tmp_path, record) -> Path:
    path = tmp_path / "kb.json"
    document = {"schema_version": 1, "version": "v1", "records": [record]}
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_every_bundled_record_has_a_spanish_version() -> None:
    kb = load_knowledge(FAQ)

    for record in kb.records:
        spanish = kb.localize(record, "es")
        assert spanish.id == record.id
        assert spanish.keywords == record.keywords
        assert (spanish.title, spanish.body) != (record.title, record.body)
        assert kb.localize(record, "en") == record


def test_a_record_without_a_translation_is_shown_as_it_is(tmp_path) -> None:
    kb = load_knowledge(write_kb(tmp_path, RECORD))

    assert kb.localize(kb.records[0], "es") == kb.records[0]


def test_a_translation_changes_only_the_title_and_body(tmp_path) -> None:
    kb = load_knowledge(write_kb(tmp_path, {**RECORD, "es": SPANISH}))
    english = kb.records[0]

    spanish = kb.localize(english, "es")

    assert (spanish.title, spanish.body) == (SPANISH["title"], SPANISH["body"])
    assert (spanish.id, spanish.keywords) == (english.id, english.keywords)
    assert kb.localize(english, "en") == english
    assert english.title == "Delivery times"


@pytest.mark.parametrize(
    "translation",
    [
        {"title": "Plazos"},
        {"body": "Cuerpo"},
        {**SPANISH, "extra": "x"},
        {"title": "", "body": "Cuerpo"},
        {"title": "Plazos", "body": ""},
        {"title": "x" * 101, "body": "Cuerpo"},
        {"title": "Plazos", "body": "x" * 2001},
        "texto",
        None,
    ],
)
def test_a_malformed_translation_makes_the_knowledge_base_invalid(
    tmp_path, translation
) -> None:
    with pytest.raises(KnowledgeInvalid):
        load_knowledge(write_kb(tmp_path, {**RECORD, "es": translation}))


def test_an_unknown_record_key_is_still_invalid(tmp_path) -> None:
    with pytest.raises(KnowledgeInvalid):
        load_knowledge(write_kb(tmp_path, {**RECORD, "fr": SPANISH}))


def test_ranking_ignores_the_translation(tmp_path) -> None:
    kb = load_knowledge(write_kb(tmp_path, {**RECORD, "es": SPANISH}))

    assert [r.id for r in kb.retrieve("delivery")] == ["faq-delivery"]
    assert kb.retrieve("plazos") == []


@pytest.mark.parametrize(
    ("language", "title", "body"),
    [
        ("es", SPANISH["title"], SPANISH["body"]),
        ("en", RECORD["title"], RECORD["body"]),
    ],
)
async def test_the_model_and_the_citation_get_the_reply_language(
    tmp_path, language, title, body
) -> None:
    kb = load_knowledge(write_kb(tmp_path, {**RECORD, "es": SPANISH}))
    answer = KnowledgeAnswer(
        answerable=True, answer="Respuesta.", cited_ids=["faq-delivery"]
    )
    provider = FakeProvider(
        [
            (
                "extract",
                make_extraction(
                    intent="question", has_question=True, detected_language=language
                ),
            ),
            ("answer_knowledge", answer),
        ]
    )
    engine = ConversationEngine(JsonSessionRepository(tmp_path), provider, kb)
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id,
        TurnRequest(
            request_id=uuid4(),
            expected_revision=0,
            text="delivery?",
            input_mode="text",
        ),
    )

    _, context = provider.calls[-1]
    assert [(r.title, r.body) for r in context.sources] == [(title, body)]
    assert response.citations == [
        Citation(source_id="faq-delivery", title=title, excerpt=body, kb_version="v1")
    ]
