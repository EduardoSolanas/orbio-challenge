"""IT11-01..IT11-08: knowledge-base startup, immutability and ownership.

Every case runs the real app factory and the real lifespan: invalid corpora must
fail startup with the exact RAG §1 string before readiness, and a valid corpus is
loaded once and drives real retrieval through the real turn route (IC §13.2 IT11).
The KB fixtures under `tests/fixtures/kb/it11/` have distinct filenames from the
unit fixtures in `tests/fixtures/kb/invalid|valid`.

`oversize-valid-it11.json` is exactly 256 KiB + 1 bytes and its document is
schema-valid (100 records, 20 keywords of 24 characters, 2000-character bodies),
so only the bounded pre-parse size rejection can fail startup - a parse-first
implementation would accept it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.main as main_module
from app.errors import KNOWLEDGE_INVALID_MESSAGE
from app.main import create_app
from app.prompts import ABSTENTION
from app.schemas import KnowledgeAnswer
from tests.integration.support_api import (
    ScriptedProvider,
    make_extraction,
    read_session,
    running_client,
)
from tests.integration.test_it02_intake import MISSING_ORDER, start_session, turn_body

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kb" / "it11"
CORRUPT = FIXTURES / "corrupt-it11.json"
EMPTY_RECORDS = FIXTURES / "empty-records-it11.json"
DUPLICATE_IDS = FIXTURES / "duplicate-ids-it11.json"
OVERSIZE_VALID = FIXTURES / "oversize-valid-it11.json"
LITERAL_MARKUP = FIXTURES / "literal-markup-it11.json"
KB_LIMIT = 256 * 1024

SEED_PATH = Path(main_module.__file__).parent / "knowledge" / "northstar-faq.json"
SEED = json.loads(SEED_PATH.read_text(encoding="utf-8"))
SEED_RECORDS = {record["id"]: record for record in SEED["records"]}

RETURN_Q = "Can I return an unused item?"
RETURN_ANSWER_TEXT = (
    "You can request a return within 30 calendar days of delivery for an unused item in "
    "its original packaging. Refund eligibility requires support review."
)
EDITED_RETURNS_BODY = (
    "An unused item in its original packaging can be requested for return within 45 "
    "calendar days of delivery. Refund eligibility requires a support review; this demo "
    "cannot approve or issue a refund."
)
EDITED_VERSION = "2026-10-23"


def seed_citation(record_id: str) -> dict:
    record = SEED_RECORDS[record_id]
    return {
        "source_id": record_id,
        "title": record["title"],
        "excerpt": record["body"],
        "kb_version": SEED["version"],
    }


def question_extraction():
    return make_extraction(intent="question", has_question=True)


def grounded(answer: str, cited_ids: list[str]) -> KnowledgeAnswer:
    return KnowledgeAnswer(answerable=True, answer=answer, cited_ids=cited_ids)


async def post(client, session_id: str, text: str, revision: int):
    return await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body(text, revision)
    )


async def expect_startup_failure(settings, knowledge_path: Path) -> ScriptedProvider:
    """Lifespan must raise the exact RAG §1 error before readiness and close clients."""

    provider = ScriptedProvider()
    app = create_app(settings=settings, provider=provider, knowledge_path=knowledge_path)
    with pytest.raises(RuntimeError) as excinfo:
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan reached readiness with an invalid knowledge base")

    assert str(excinfo.value) == KNOWLEDGE_INVALID_MESSAGE
    assert provider.close_count == 1
    assert getattr(app.state, "ready", False) is False
    assert not hasattr(app.state, "engine")
    assert not hasattr(app.state, "knowledge_base")
    return provider


# IT11-01..IT11-05: invalid corpora fail startup ----------------------------


async def case_it11_01(settings, provider, session_dir, tmp_path):
    missing = tmp_path / "missing-it11.json"
    assert not missing.exists()
    await expect_startup_failure(settings, missing)


async def case_it11_02(settings, provider, session_dir, tmp_path):
    await expect_startup_failure(settings, CORRUPT)


async def case_it11_03(settings, provider, session_dir, tmp_path):
    await expect_startup_failure(settings, EMPTY_RECORDS)


async def case_it11_04(settings, provider, session_dir, tmp_path):
    await expect_startup_failure(settings, DUPLICATE_IDS)


async def case_it11_05(settings, provider, session_dir, tmp_path):
    # The fixture is schema-valid JSON of exactly 256 KiB + 1 bytes: startup can
    # only fail through the bounded pre-parse size guard, never a parse error.
    raw = OVERSIZE_VALID.read_bytes()
    assert len(raw) == KB_LIMIT + 1
    document = json.loads(raw)
    assert len(document["records"]) == 100
    await expect_startup_failure(settings, OVERSIZE_VALID)


# IT11-06..IT11-08: valid corpus, immutability across requests, literal sources --


async def case_it11_06(settings, provider, session_dir, tmp_path):
    app = create_app(settings=settings, provider=provider)
    async with running_client(app) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        knowledge_base = app.state.knowledge_base
        assert knowledge_base.version == SEED["version"]
        assert [record.model_dump() for record in knowledge_base.records] == SEED["records"]
        assert len(knowledge_base.records) == 4
        # Startup and health never probe the AI service.
        assert provider.calls == []

    assert provider.close_count == 1
    # The packaged seed path is the production default.
    assert main_module.DEFAULT_KNOWLEDGE_PATH == SEED_PATH
    assert SEED_PATH.is_file()


async def case_it11_07(settings, provider, session_dir, tmp_path):
    kb_path = tmp_path / "kb-it11-07.json"
    kb_path.write_text(json.dumps(SEED, ensure_ascii=False), encoding="utf-8")

    first = ScriptedProvider()
    first_app = create_app(settings=settings, provider=first, knowledge_path=kb_path)
    async with running_client(first_app) as client:
        assert (await client.get("/health")).status_code == 200
        session_id = await start_session(client)

        # Edit the valid KB file after the lifespan loaded it.
        edited = json.loads(json.dumps(SEED))
        edited["version"] = EDITED_VERSION
        edited["records"][2]["body"] = EDITED_RETURNS_BODY
        kb_path.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")

        first.script_extraction(question_extraction())
        first.script_answer(grounded(RETURN_ANSWER_TEXT, ["faq-returns"]))
        response = await post(client, session_id, RETURN_Q, 0)

        assert response.status_code == 200, response.text
        assert response.json()["citations"] == [seed_citation("faq-returns")]
        assert first_app.state.knowledge_base.version == SEED["version"]
        disk = read_session(session_dir, session_id)
        assert disk["messages"][-1]["citations"] == [seed_citation("faq-returns")]

    # A new lifespan re-reads the file: the edited version drives generation.
    second = ScriptedProvider()
    second_app = create_app(settings=settings, provider=second, knowledge_path=kb_path)
    async with running_client(second_app) as client:
        assert (await client.get("/health")).status_code == 200
        second.script_extraction(question_extraction())
        second.script_answer(grounded(RETURN_ANSWER_TEXT, ["faq-returns"]))
        response = await post(client, session_id, RETURN_Q, 1)

        assert response.status_code == 200, response.text
        assert response.json()["citations"] == [
            {
                "source_id": "faq-returns",
                "title": SEED_RECORDS["faq-returns"]["title"],
                "excerpt": EDITED_RETURNS_BODY,
                "kb_version": EDITED_VERSION,
            }
        ]
        assert second_app.state.knowledge_base.version == EDITED_VERSION


async def case_it11_08(settings, provider, session_dir, tmp_path):
    fixture = json.loads(LITERAL_MARKUP.read_text(encoding="utf-8"))
    record = fixture["records"][0]
    answer_text = "The markup policy record is stored exactly as written."

    markup_provider = ScriptedProvider()
    app = create_app(
        settings=settings, provider=markup_provider, knowledge_path=LITERAL_MARKUP
    )
    async with running_client(app) as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        session_id = await start_session(client)

        markup_provider.script_extraction(question_extraction())
        markup_provider.script_answer(grounded(answer_text, ["faq-markup"]))
        response = await post(client, session_id, "What does the markup policy say?", 0)

        assert response.status_code == 200, response.text
        body = response.json()
        expected = {
            "source_id": "faq-markup",
            "title": record["title"],
            "excerpt": record["body"],
            "kb_version": fixture["version"],
        }
        assert body["citations"] == [expected]
        assert body["reply"] == answer_text

        disk = read_session(session_dir, session_id)
        assert disk["messages"][-1]["citations"] == [expected]
        # Stored as data: no HTML/entity escaping and no escaped code points.
        text = (session_dir / f"{session_id}.json").read_text(encoding="utf-8")
        assert "<script>alert('title')</script>" in text
        assert "<b>Bold</b>" in text
        assert "&lt;" not in text
        assert "&gt;" not in text
        assert "&amp;" not in text
        assert "\\u003c" not in text
        assert json.loads(text)["messages"][-1]["citations"][0]["excerpt"] == record["body"]


CASES = {
    "IT11-01": case_it11_01,
    "IT11-02": case_it11_02,
    "IT11-03": case_it11_03,
    "IT11-04": case_it11_04,
    "IT11-05": case_it11_05,
    "IT11-06": case_it11_06,
    "IT11-07": case_it11_07,
    "IT11-08": case_it11_08,
}


@pytest.mark.parametrize("case_id", sorted(CASES))
async def test_it11_kb_startup(case_id, settings, provider, session_dir, tmp_path):
    await CASES[case_id](settings, provider, session_dir, tmp_path)
