"""Unit tests for Spanish lexical query normalization in retrieval."""

from __future__ import annotations

import pytest

from app.retrieval import load_knowledge, tokenize

KB_PATH = "app/knowledge/northstar-faq.json"


@pytest.fixture(scope="module")
def knowledge():
    return load_knowledge(KB_PATH)


@pytest.mark.parametrize(
    ("question", "record_id"),
    [
        ("¿Cuánto tarda la entrega?", "faq-delivery"),
        ("Cuando tarda el envio", "faq-delivery"),
        ("Mi paquete tiene un artículo dañado", "faq-damaged"),
        ("El producto llegó roto", "faq-damaged"),
        ("¿Puedo hacer una devolución?", "faq-returns"),
        ("Quiero un reembolso", "faq-returns"),
        ("¿Cuál es la urgencia recomendada?", "faq-urgency"),
        ("Necesito prioridad alta", "faq-urgency"),
    ],
)
def test_spanish_questions_retrieve_the_canonical_english_records(
    knowledge, question, record_id
):
    records = knowledge.retrieve(question)

    assert records, question
    assert record_id in [record.id for record in records]


def test_accented_and_unaccented_spanish_forms_retrieve_the_same_record(knowledge):
    accented = knowledge.retrieve("artículo dañado")
    unaccented = knowledge.retrieve("articulo danado")

    assert [record.id for record in accented] == [record.id for record in unaccented]
    assert accented[0].id == "faq-damaged"


def test_irrelevant_spanish_words_retrieve_nothing(knowledge):
    assert knowledge.retrieve("hola, ¿cómo estás?") == []
    assert knowledge.retrieve("quiero hablar con una persona") == []


def test_unsupported_regional_or_payment_questions_do_not_infer_policy(knowledge):
    assert knowledge.retrieve("¿Puedo pagar con tarjeta de crédito?") == []
    assert knowledge.retrieve("¿Tienen una tienda en México?") == []


def test_spanish_token_mapping_does_not_rewrite_other_tokens():
    assert tokenize("Entrega urgente") == {"entrega", "urgente"}
