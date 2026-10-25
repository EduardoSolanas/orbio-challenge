"""RAG sections 1-4: immutable knowledge base, lexical retrieval and citations."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from gateway.errors import INVALID_PROVIDER_RESPONSE, KNOWLEDGE_INVALID_MESSAGE, AppError
from gateway.knowledge.retrieval import (
    MAX_ANSWER_CHARS,
    MAX_KB_BYTES,
    ImmutableKnowledgeRecord,
    KnowledgeBase,
    KnowledgeInvalid,
    load_knowledge,
    tokenize,
    validate_knowledge_answer,
)
from gateway.schemas import Citation, KnowledgeAnswer

CONTRACT = Path(__file__).resolve().parents[4] / "docs" / "rag-contract.md"
SEED_KB = Path(__file__).resolve().parents[4] / "gateway" / "knowledge" / "northstar-faq.json"
KB_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "kb"
INVALID_FIXTURES = sorted(path.name for path in (KB_FIXTURES / "invalid").glob("*.json"))
VALID_FIXTURES = sorted(path.name for path in (KB_FIXTURES / "valid").glob("*.json"))


def _seed_block() -> bytes:
    text = CONTRACT.read_text(encoding="utf-8")
    match = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert match is not None
    return match.group(1).encode("utf-8")


def test_bundled_kb_is_byte_identical_to_the_contract_seed_corpus() -> None:
    assert SEED_KB.read_bytes() == _seed_block()


def test_load_reads_the_bundled_seed_kb_immutably() -> None:
    kb = load_knowledge(SEED_KB)
    assert kb.version == "2026-10-25"
    assert [record.id for record in kb.records] == [
        "faq-delivery",
        "faq-damaged",
        "faq-returns",
        "faq-urgency",
        "faq-standing-charge",
        "faq-installation",
        "faq-meter-readings",
        "faq-moving-home",
        "faq-bill-queries",
        "faq-power-cut",
        "faq-gas-safety",
        "faq-account-reference",
    ]
    assert isinstance(kb.records, tuple)
    assert kb.records[0].keywords == ["delivery", "shipping", "ship", "arrive", "arrives"]
    with pytest.raises(ValidationError):
        kb.records[0].id = "faq-other"


def test_bundled_kb_retrieves_energy_bill_guidance_for_supplier_site() -> None:
    kb = load_knowledge(SEED_KB)

    matches = kb.retrieve("What does a standing charge on my energy bill cover?")

    assert matches
    assert matches[0].id == "faq-standing-charge"
    assert "standing charge" in matches[0].body.casefold()


@pytest.mark.parametrize("name", VALID_FIXTURES)
def test_valid_fixture_loads(name: str) -> None:
    kb = load_knowledge(KB_FIXTURES / "valid" / name)
    assert kb.version
    assert kb.records


def test_boundary_fixture_accepts_every_documented_limit() -> None:
    kb = load_knowledge(KB_FIXTURES / "valid" / "boundary.json")
    record = kb.records[0]
    assert kb.version == "v" * 40
    assert record.id == "a" * 64
    assert record.title == "T" * 100
    assert record.body == "b" * 2000
    assert len(record.keywords) == 20


def test_minimum_length_values_are_accepted() -> None:
    kb = load_knowledge(KB_FIXTURES / "valid" / "single-char.json")
    assert kb.version == "v"
    record = kb.records[0]
    assert record.id == "a"
    assert record.title == "T"
    assert record.body == "b"
    assert record.keywords == ["k"]


def test_one_hundred_records_are_accepted() -> None:
    kb = load_knowledge(KB_FIXTURES / "valid" / "max-records.json")
    assert len(kb.records) == 100
    assert [record.id for record in kb.records] == [f"faq-{i}" for i in range(100)]


def test_directory_path_is_rejected_as_missing(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeInvalid) as excinfo:
        load_knowledge(tmp_path)
    assert excinfo.value.reason == "missing"


@pytest.mark.parametrize("name", INVALID_FIXTURES)
def test_invalid_fixture_is_rejected_with_the_exact_startup_message(name: str) -> None:
    with pytest.raises(KnowledgeInvalid) as excinfo:
        load_knowledge(KB_FIXTURES / "invalid" / name)
    assert str(excinfo.value) == KNOWLEDGE_INVALID_MESSAGE
    assert excinfo.value.reason == "invalid_document"


def test_missing_file_is_rejected() -> None:
    with pytest.raises(KnowledgeInvalid) as excinfo:
        load_knowledge(KB_FIXTURES / "invalid" / "does-not-exist.json")
    assert str(excinfo.value) == KNOWLEDGE_INVALID_MESSAGE
    assert excinfo.value.reason == "missing"


def test_oversize_file_is_rejected_before_parsing(tmp_path: Path) -> None:
    path = tmp_path / "oversize.json"
    path.write_bytes(b"{" + b" " * MAX_KB_BYTES)
    with pytest.raises(KnowledgeInvalid) as excinfo:
        load_knowledge(path)
    assert str(excinfo.value) == KNOWLEDGE_INVALID_MESSAGE
    assert excinfo.value.reason == "file_too_large"


def test_file_of_exactly_the_byte_cap_still_loads(tmp_path: Path) -> None:
    document = (KB_FIXTURES / "valid" / "minimal.json").read_bytes()
    path = tmp_path / "at-limit.json"
    path.write_bytes(document + b" " * (MAX_KB_BYTES - len(document)))
    assert path.stat().st_size == MAX_KB_BYTES
    assert load_knowledge(path).version == "2026-10-22"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("How long does delivery take?", {"long", "delivery", "take"}),
        ("Can I return an unused item?", {"return", "unused"}),
        ("What about damaged items?", {"about", "damaged"}),
        ("What does high urgency mean?", {"high", "urgency", "mean"}),
        ("Do you ship internationally?", {"ship", "internationally"}),
        ("Which payment methods are accepted?", {"which", "payment", "methods", "accepted"}),
        ("Ｄｅｌｉｖｅｒｙ", {"delivery"}),
        ("ﬁle", {"file"}),
        ("STRASSE", {"strasse"}),
        ("Straße", {"strasse"}),
        ("2-5 business-days!", {"2", "5", "business", "days"}),
        ("30 calendar days", {"30", "calendar", "days"}),
        ("the and of for in on at", set()),
        ("", set()),
    ],
)
def test_tokenize_normalises_casefolds_and_removes_stop_words(
    text: str, expected: set[str]
) -> None:
    tokens = tokenize(text)
    assert isinstance(tokens, set)
    assert tokens == expected


def test_tokenize_stop_word_list_is_exact() -> None:
    stop_words = (
        "a an the i my me we you your is are was were be been do does did can could "
        "would should will what how when where why who to of for in on at by with and "
        "or but it its this that as have has had order item items"
    )
    assert tokenize(stop_words) == set()


def _kb(records: list[dict[str, object]], version: str = "test-version") -> KnowledgeBase:
    return KnowledgeBase(
        version,
        tuple(ImmutableKnowledgeRecord(**record) for record in records),
    )


@pytest.mark.parametrize(
    ("question", "expected_ids"),
    [
        ("How long does delivery take?", ["faq-delivery"]),
        ("Can I return an unused item?", ["faq-returns"]),
        ("What about damaged items?", ["faq-damaged"]),
        ("What does high urgency mean?", ["faq-urgency"]),
        ("Do you ship internationally?", ["faq-delivery"]),
        ("Which payment methods are accepted?", []),
    ],
)
def test_seed_kb_returns_the_contract_examples(question: str, expected_ids: list[str]) -> None:
    kb = load_knowledge(SEED_KB)
    assert [record.id for record in kb.retrieve(question)] == expected_ids


def test_retrieval_requires_a_keyword_match_not_just_body_text() -> None:
    kb = load_knowledge(SEED_KB)
    assert kb.retrieve("Is a replacement possible?") == []
    assert kb.retrieve("How do I") == []


def test_retrieval_is_case_and_punctuation_insensitive() -> None:
    kb = load_knowledge(SEED_KB)
    assert [record.id for record in kb.retrieve("DELIVERY?!")] == ["faq-delivery"]


def test_ranking_weighs_keyword_hits_three_times_body_hits() -> None:
    kb = _kb(
        [
            {
                "id": "aaa",
                "title": "N1",
                "keywords": ["alpha"],
                "body": "alpha beta gamma",
            },
            {
                "id": "bbb",
                "title": "N2",
                "keywords": ["beta", "gamma"],
                "body": "nothing",
            },
            {
                "id": "ccc",
                "title": "N3",
                "keywords": ["alpha", "beta"],
                "body": "alpha",
            },
        ]
    )
    assert [record.id for record in kb.retrieve("alpha beta gamma")] == ["ccc", "aaa"]


def test_equal_scores_break_ties_by_ascending_id() -> None:
    kb = _kb(
        [
            {"id": "zzz", "title": "Same", "keywords": ["delivery"], "body": "same"},
            {"id": "aaa", "title": "Same", "keywords": ["delivery"], "body": "same"},
        ]
    )
    assert [record.id for record in kb.retrieve("delivery")] == ["aaa", "zzz"]


def test_retrieval_returns_at_most_two_records() -> None:
    kb = _kb(
        [
            {"id": "faq-3", "title": "T", "keywords": ["delivery"], "body": "b"},
            {"id": "faq-1", "title": "T", "keywords": ["delivery"], "body": "b"},
            {"id": "faq-2", "title": "T", "keywords": ["delivery"], "body": "b"},
        ]
    )
    assert [record.id for record in kb.retrieve("delivery")] == ["faq-1", "faq-2"]


def test_retrieval_returns_a_fresh_list_that_cannot_mutate_the_index() -> None:
    kb = load_knowledge(SEED_KB)
    first = kb.retrieve("How long does delivery take?")
    first.clear()
    assert [record.id for record in kb.retrieve("How long does delivery take?")] == [
        "faq-delivery"
    ]


DELIVERY_BODY = (
    "Standard delivery takes 2 to 5 business days after dispatch, "
    "for equipment such as a smart meter. "
    "This demo cannot check the status of an individual delivery."
)


def _answer(**overrides: object) -> KnowledgeAnswer:
    values: dict[str, object] = {
        "answerable": True,
        "answer": "Standard delivery takes 2 to 5 business days after dispatch.",
        "cited_ids": ["faq-delivery"],
    }
    values.update(overrides)
    return KnowledgeAnswer(**values)


def _delivery_retrieval() -> tuple[KnowledgeBase, list[ImmutableKnowledgeRecord]]:
    kb = load_knowledge(SEED_KB)
    return kb, kb.retrieve("How long does delivery take?")


def _assert_invalid(
    answer: KnowledgeAnswer, retrieved: list[ImmutableKnowledgeRecord]
) -> None:
    with pytest.raises(AppError) as excinfo:
        validate_knowledge_answer(answer, retrieved, "2026-10-22")
    assert excinfo.value.code == INVALID_PROVIDER_RESPONSE


def test_citation_snapshot_uses_the_whole_record_body_title_and_kb_version() -> None:
    kb, retrieved = _delivery_retrieval()
    citations = validate_knowledge_answer(_answer(), retrieved, kb.version)
    assert len(citations) == 1
    citation = citations[0]
    assert isinstance(citation, Citation)
    assert citation.source_id == "faq-delivery"
    assert citation.title == "Delivery times"
    assert citation.excerpt == DELIVERY_BODY
    assert citation.kb_version == "2026-10-25"


def test_two_unique_retrieved_ids_become_snapshots_in_cited_order() -> None:
    kb = _kb(
        [
            {"id": "faq-delivery", "title": "Delivery times", "keywords": ["delivery"], "body": "D1"},
            {"id": "faq-returns", "title": "Returns", "keywords": ["returns"], "body": "R1"},
        ]
    )
    retrieved = kb.retrieve("delivery returns")
    assert [record.id for record in retrieved] == ["faq-delivery", "faq-returns"]
    citations = validate_knowledge_answer(
        _answer(answer="Both policies apply.", cited_ids=["faq-returns", "faq-delivery"]),
        retrieved,
        kb.version,
    )
    assert [citation.source_id for citation in citations] == ["faq-returns", "faq-delivery"]
    assert [citation.excerpt for citation in citations] == ["R1", "D1"]


def test_rejects_unknown_cited_id() -> None:
    _, retrieved = _delivery_retrieval()
    _assert_invalid(_answer(cited_ids=["faq-payment"]), retrieved)


def test_rejects_record_that_exists_but_was_not_retrieved_this_turn() -> None:
    _, retrieved = _delivery_retrieval()
    _assert_invalid(_answer(cited_ids=["faq-damaged"]), retrieved)


def test_rejects_duplicate_cited_ids() -> None:
    _, retrieved = _delivery_retrieval()
    _assert_invalid(_answer(cited_ids=["faq-delivery", "faq-delivery"]), retrieved)


def test_rejects_more_than_two_cited_ids() -> None:
    kb = load_knowledge(SEED_KB)
    _assert_invalid(
        _answer(cited_ids=["faq-delivery", "faq-damaged", "faq-returns"]),
        list(kb.records[:3]),
    )


def test_rejects_answerable_true_with_no_citations() -> None:
    _, retrieved = _delivery_retrieval()
    _assert_invalid(_answer(cited_ids=[]), retrieved)


def test_rejects_blank_answer() -> None:
    _, retrieved = _delivery_retrieval()
    _assert_invalid(_answer(answer=""), retrieved)
    _assert_invalid(_answer(answer="   "), retrieved)


def test_rejects_answer_over_the_character_limit() -> None:
    _, retrieved = _delivery_retrieval()
    _assert_invalid(_answer(answer="x" * (MAX_ANSWER_CHARS + 1)), retrieved)


def test_accepts_an_answer_of_exactly_the_character_limit() -> None:
    _, retrieved = _delivery_retrieval()
    citations = validate_knowledge_answer(
        _answer(answer=" " + "x" * MAX_ANSWER_CHARS + " "), retrieved, "2026-10-22"
    )
    assert len(citations) == 1


def test_accepts_a_single_character_answer() -> None:
    _, retrieved = _delivery_retrieval()
    citations = validate_knowledge_answer(_answer(answer="x"), retrieved, "2026-10-22")
    assert len(citations) == 1


def test_answerable_false_returns_no_citations() -> None:
    _, retrieved = _delivery_retrieval()
    assert validate_knowledge_answer(
        _answer(answerable=False, answer="", cited_ids=[]), retrieved, "2026-10-22"
    ) == ()


def test_rejects_answerable_false_with_text() -> None:
    _, retrieved = _delivery_retrieval()
    _assert_invalid(_answer(answerable=False, answer="No answer.", cited_ids=[]), retrieved)
    _assert_invalid(_answer(answerable=False, answer=" ", cited_ids=[]), retrieved)


def test_rejects_answerable_false_with_cited_ids() -> None:
    _, retrieved = _delivery_retrieval()
    _assert_invalid(
        _answer(answerable=False, answer="", cited_ids=["faq-delivery"]), retrieved
    )


def test_answer_model_cannot_supply_titles_or_versions() -> None:
    with pytest.raises(ValidationError):
        KnowledgeAnswer(
            answerable=True,
            answer="x",
            cited_ids=["faq-delivery"],
            title="Model title",
            kb_version="1999-01-01",
        )


def test_snapshot_uses_the_entire_body_without_truncation() -> None:
    body = "b" * 2000
    kb = _kb([{"id": "faq-long", "title": "Long policy", "keywords": ["delivery"], "body": body}])
    retrieved = kb.retrieve("delivery")
    citations = validate_knowledge_answer(
        _answer(answer="Answer.", cited_ids=["faq-long"]), retrieved, kb.version
    )
    assert citations[0].excerpt == body
    assert len(citations[0].excerpt) == 2000
    assert citations[0].title == "Long policy"
    assert citations[0].kb_version == kb.version


