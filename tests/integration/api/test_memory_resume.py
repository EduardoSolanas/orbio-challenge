"""Multi-turn memory and session resume.

Integration coverage for the acceptance criteria, against the real app
factory, lifespan, engine and JSON repository over an isolated temp
SESSION_DIR; only the AI service boundary is scripted (IC §13.1).

- resume-01: review state (transcript, token, summary, citations) survives a
  process restart via GET, and the preserved token still confirms.
- resume-02: accepted facts survive once early messages fall outside the
  12-message extraction window.
- resume-03: facts survive an FAQ detour followed by a correction.
"""

from __future__ import annotations

from uuid import uuid4

from gateway.providers.prompts import SOCIAL_ACKNOWLEDGMENT, serialize_context
from gateway.schemas import KnowledgeRecord, RecentMessage
from tests.integration.support.api import (
    ScriptedProvider,
    make_extraction,
    persisted_response,
    read_session,
    served_messages,
)
from tests.integration.support.confirmation import (
    equal_explicit_correction_extraction as urgency_correction_extraction,
)
from tests.integration.support.intake import (
    CATEGORY_TEXT,
    FULL_SLOTS,
    FULL_SUMMARY,
    FULL_TEXT,
    HIGH_TEXT,
    PART_TEXT,
    REVIEW_SUFFIX,
    category_extraction,
    full_extraction,
    high_extraction,
    part_extraction,
    start_session,
    turn_body,
)
from tests.integration.support.persistence import app_client
from tests.integration.support.rag import (
    RETURN_ANSWER_TEXT,
    RETURN_Q,
    grounded,
    question_extraction,
    seed_citation,
)

SOCIAL_TEXT = "Hello, how are you?"


def social_extraction():
    return make_extraction(intent="social", has_question=True)


def plain_social_extraction():
    return make_extraction(intent="social")


def order_correction_extraction():
    return make_extraction(
        intent="correction",
        candidates={"order_number": "NS-000456"},
        explicit_corrections=["order_number"],
    )


async def post_turn(client, provider, session_id, *, text, revision, extraction, answer=None):
    provider.script_extraction(extraction)
    if answer is not None:
        provider.script_answer(answer)
    request_id = str(uuid4())
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body(text, revision, request_id),
    )
    assert response.status_code == 200, response.text
    return request_id, response.json()


# resume-01 ------------------------------------------------------------------


async def test_resume_01_review_state_resumes_across_restart(settings, provider, session_dir):
    async with app_client(settings, provider) as first:
        session_id = await start_session(first)
        _, reviewed = await post_turn(
            first, provider, session_id,
            text="My order NS-000123 arrived broken. It is a damaged item and high urgency.",
            revision=0, extraction=full_extraction(),
        )
        assert reviewed["revision"] == 1
        assert reviewed["status"] == "awaiting_confirmation"
        assert reviewed["pending_review_revision"] == 1
        assert reviewed["summary"] == FULL_SUMMARY

        _, answered = await post_turn(
            first, provider, session_id,
            text=RETURN_Q, revision=1,
            extraction=question_extraction(),
            answer=grounded(RETURN_ANSWER_TEXT, ["faq-returns"]),
        )
        assert answered["revision"] == 2
        assert answered["status"] == "awaiting_confirmation"
        assert answered["reply"] == RETURN_ANSWER_TEXT
        assert answered["citations"] == [seed_citation("faq-returns")]
        assert answered["slots"] == FULL_SLOTS
        assert answered["summary"] == FULL_SUMMARY
        assert answered["pending_review_revision"] == 1
        committed = read_session(session_dir, session_id)
        calls_before_restart = len(provider.calls)

    async with app_client(settings, provider) as second:
        resumed = await second.get(f"/api/sessions/{session_id}")
        assert resumed.status_code == 200, resumed.text
        view = resumed.json()
        assert view["session_id"] == session_id
        assert view["revision"] == 2 == committed["revision"]
        assert view["status"] == "awaiting_confirmation"
        assert view["slots"] == FULL_SLOTS
        assert served_messages(view["messages"]) == committed["messages"]
        assert [m["role"] for m in view["messages"]] == [
            "assistant", "user", "assistant", "user", "assistant",
        ]
        assert view["messages"][-1]["citations"] == [seed_citation("faq-returns")]
        assert view["pending_clarifications"] == {}
        assert view["pending_review_revision"] == 1
        assert view["summary"] == FULL_SUMMARY
        assert len(provider.calls) == calls_before_restart

        confirm_id = str(uuid4())
        confirmed = await second.post(
            f"/api/sessions/{session_id}/confirm",
            json={
                "request_id": confirm_id,
                "expected_revision": 2,
                "review_revision": 1,
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        receipt = confirmed.json()
        assert receipt["status"] == "completed"
        assert receipt["revision"] == 3
        assert receipt["slots"] == FULL_SLOTS
        assert receipt["summary"] == FULL_SUMMARY
        assert receipt["pending_review_revision"] is None

        disk = read_session(session_dir, session_id)
        assert disk["status"] == "completed"
        assert disk["revision"] == 3
        assert disk["confirmation"] == {
            "confirmed_at": disk["confirmation"]["confirmed_at"],
            "review_revision": 1,
        }
        assert disk["messages"] == served_messages(view["messages"]) + disk["messages"][-2:]
        assert disk["requests"][-1]["request_id"] == confirm_id
        assert disk["requests"][-1]["response"] == persisted_response(receipt)

        replayed = await second.post(
            f"/api/sessions/{session_id}/confirm",
            json={
                "request_id": confirm_id,
                "expected_revision": 2,
                "review_revision": 1,
            },
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json() == receipt
        assert read_session(session_dir, session_id)["revision"] == 3


# resume-02 ------------------------------------------------------------------


async def test_resume_02_facts_survive_beyond_twelve_message_window(
    settings, provider, session_dir
):
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        _, partial = await post_turn(
            client, provider, session_id,
            text=PART_TEXT, revision=0, extraction=part_extraction(),
        )
        assert partial["revision"] == 1

        revision = 1
        for index in range(8):
            if index % 2 == 0:
                _, detour = await post_turn(
                    client, provider, session_id,
                    text=SOCIAL_TEXT, revision=revision,
                    extraction=social_extraction(),
                )
                assert detour["reply"] == SOCIAL_ACKNOWLEDGMENT
            else:
                _, detour = await post_turn(
                    client, provider, session_id,
                    text=RETURN_Q, revision=revision,
                    extraction=question_extraction(),
                    answer=grounded(RETURN_ANSWER_TEXT, ["faq-returns"]),
                )
                assert detour["reply"] == RETURN_ANSWER_TEXT
            assert detour["slots"] == {
                "order_number": "NS-000123",
                "category": None,
                "description": "My order arrived broken",
                "urgency": None,
            }
            assert detour["status"] == "collecting"
            revision = detour["revision"]

        disk = read_session(session_dir, session_id)
        assert len(disk["messages"]) == 19
        last_extract = next(
            context
            for method, context in reversed(provider.calls)
            if method == "extract"
        )
        assert len(last_extract.recent_messages) == 12
        assert last_extract.slots.model_dump() == {
            "order_number": "NS-000123",
            "category": None,
            "description": "My order arrived broken",
            "urgency": None,
        }
        assert PART_TEXT not in [
            message.text for message in last_extract.recent_messages
        ]

        _, categorized = await post_turn(
            client, provider, session_id,
            text=CATEGORY_TEXT, revision=revision,
            extraction=category_extraction(),
        )
        _, urgent = await post_turn(
            client, provider, session_id,
            text=HIGH_TEXT, revision=categorized["revision"],
            extraction=high_extraction(),
        )
        assert urgent["status"] == "awaiting_confirmation"
        assert urgent["slots"] == FULL_SLOTS
        assert urgent["summary"] == FULL_SUMMARY
        assert urgent["pending_review_revision"] == urgent["revision"]
        assert urgent["reply"] == FULL_SUMMARY + REVIEW_SUFFIX


# resume-03 ------------------------------------------------------------------


async def test_resume_03_faq_detour_then_correction_keeps_sibling_facts(
    settings, provider, session_dir
):
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        _, partial = await post_turn(
            client, provider, session_id,
            text=PART_TEXT, revision=0, extraction=part_extraction(),
        )
        assert partial["revision"] == 1

        _, answered = await post_turn(
            client, provider, session_id,
            text=RETURN_Q, revision=1,
            extraction=question_extraction(),
            answer=grounded(RETURN_ANSWER_TEXT, ["faq-returns"]),
        )
        assert answered["reply"] == RETURN_ANSWER_TEXT
        assert answered["slots"]["order_number"] == "NS-000123"
        assert answered["slots"]["description"] == "My order arrived broken"

        _, corrected = await post_turn(
            client, provider, session_id,
            text="Actually the order is NS-000456.", revision=2,
            extraction=order_correction_extraction(),
        )
        assert corrected["slots"]["order_number"] == "NS-000456"
        assert corrected["slots"]["description"] == "My order arrived broken"
        assert corrected["pending_clarifications"] == {}


# context ---------------------------------------------------------------------
#
# acceptance: the extraction context carries the authoritative session
# state and exactly the latest 12 committed messages; persisted JSON proves the
# stored state is never shortened or mutated by provider code.

AMBIGUOUS_TEXT = "My order NS-000123 has some problem."
AMBIGUOUS_CATEGORY = {
    "code": "ambiguous",
    "candidate": "other",
    "question": "Please clarify your energy support area.",
}
CORRECTION_TEXT = "Make the urgency high."


def ambiguous_extraction():
    return make_extraction(
        candidates={"order_number": "NS-000123", "category": "other"},
        ambiguous_fields=["category"],
    )



def last_extract_context(provider):
    return next(
        context for method, context in reversed(provider.calls) if method == "extract"
    )


async def test_context_01_window_matches_persisted_history_and_keeps_the_old_order(
    settings, provider, session_dir
):
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        _, partial = await post_turn(
            client, provider, session_id,
            text=PART_TEXT, revision=0, extraction=part_extraction(),
        )
        revision = partial["revision"]
        for index in range(12):
            if index % 2 == 0:
                _, detour = await post_turn(
                    client, provider, session_id,
                    text=SOCIAL_TEXT, revision=revision,
                    extraction=social_extraction(),
                )
                assert detour["reply"] == SOCIAL_ACKNOWLEDGMENT
            else:
                _, detour = await post_turn(
                    client, provider, session_id,
                    text=RETURN_Q, revision=revision,
                    extraction=question_extraction(),
                    answer=grounded(RETURN_ANSWER_TEXT, ["faq-returns"]),
                )
                assert detour["reply"] == RETURN_ANSWER_TEXT
            assert detour["slots"]["order_number"] == "NS-000123"
            revision = detour["revision"]

        before = read_session(session_dir, session_id)
        assert len(before["messages"]) == 27
        assert before["slots"]["order_number"] == "NS-000123"

        provider.script_extraction(urgency_correction_extraction())
        corrected = await client.post(
            f"/api/sessions/{session_id}/turns",
            json=turn_body(CORRECTION_TEXT, revision, str(uuid4())),
        )
        assert corrected.status_code == 200, corrected.text
        body = corrected.json()
        assert body["slots"]["order_number"] == "NS-000123"
        assert body["slots"]["description"] == "My order arrived broken"
        assert body["slots"]["urgency"] == "high"

        context = last_extract_context(provider)
        assert context.current_message == CORRECTION_TEXT
        assert len(context.recent_messages) == 12
        assert [message.text for message in context.recent_messages] == [
            message["text"] for message in before["messages"][-12:]
        ]
        assert PART_TEXT not in [message.text for message in context.recent_messages]
        assert context.slots.model_dump() == {
            "order_number": "NS-000123",
            "category": None,
            "description": "My order arrived broken",
            "urgency": None,
        }
        assert context.status == "collecting"
        assert context.intake_started is True
        assert context.reply_language == "en"
        assert context.frustration_active is False

        after = read_session(session_dir, session_id)
        assert len(after["messages"]) == 29
        assert after["messages"][:-2] == before["messages"]
        assert after["slots"] == body["slots"]
        assert after["pending_clarifications"] == {}


async def test_context_02_pending_ambiguity_survives_faq_detours(
    settings, provider, session_dir
):
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        _, ambiguous = await post_turn(
            client, provider, session_id,
            text=AMBIGUOUS_TEXT, revision=0, extraction=ambiguous_extraction(),
        )
        assert ambiguous["pending_clarifications"] == {"category": AMBIGUOUS_CATEGORY}
        assert ambiguous["slots"]["order_number"] == "NS-000123"
        assert ambiguous["status"] == "collecting"

        revision = ambiguous["revision"]
        for _ in range(2):
            _, answered = await post_turn(
                client, provider, session_id,
                text=RETURN_Q, revision=revision,
                extraction=question_extraction(),
                answer=grounded(RETURN_ANSWER_TEXT, ["faq-returns"]),
            )
            assert answered["reply"] == RETURN_ANSWER_TEXT
            assert answered["pending_clarifications"] == {
                "category": AMBIGUOUS_CATEGORY
            }
            assert answered["slots"]["order_number"] == "NS-000123"
            assert answered["status"] == "collecting"
            revision = answered["revision"]

        disk = read_session(session_dir, session_id)
        assert disk["pending_clarifications"] == {"category": AMBIGUOUS_CATEGORY}
        assert disk["slots"]["order_number"] == "NS-000123"
        context = last_extract_context(provider)
        assert context.pending_clarifications["category"].model_dump() == (
            AMBIGUOUS_CATEGORY
        )
        assert context.intake_started is True

        _, resolved = await post_turn(
            client, provider, session_id,
            text="It is a delivery problem.", revision=revision,
            extraction=make_extraction(candidates={"category": "delivery"}),
        )
        assert resolved["pending_clarifications"] == {}
        assert resolved["slots"]["order_number"] == "NS-000123"
        assert resolved["slots"]["category"] == "delivery"
        disk = read_session(session_dir, session_id)
        assert disk["pending_clarifications"] == {}
        assert disk["slots"]["order_number"] == "NS-000123"


class MutatingScriptedProvider(ScriptedProvider):
    """Scripted provider that tries to corrupt the context it is handed."""

    async def extract(self, context):
        context.slots.order_number = "NS-999999"
        context.slots.urgency = "high"
        context.pending_clarifications.clear()
        context.recent_messages.append(RecentMessage(role="user", text="injected"))
        context.recent_messages[0].text = "rewritten"
        context.status = "completed"
        context.frustration_active = True
        context.intake_started = False
        context.reply_language = "es"
        return await super().extract(context)


async def test_context_03_provider_context_mutation_cannot_alter_stored_json(
    settings, session_dir
):
    provider = MutatingScriptedProvider()
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        _, ambiguous = await post_turn(
            client, provider, session_id,
            text=AMBIGUOUS_TEXT, revision=0, extraction=ambiguous_extraction(),
        )
        assert ambiguous["pending_clarifications"] == {"category": AMBIGUOUS_CATEGORY}

        provider.script_extraction(question_extraction())
        provider.script_answer(grounded(RETURN_ANSWER_TEXT, ["faq-returns"]))
        answered = await client.post(
            f"/api/sessions/{session_id}/turns",
            json=turn_body(RETURN_Q, ambiguous["revision"], str(uuid4())),
        )
        assert answered.status_code == 200, answered.text
        body = answered.json()
        assert body["reply"] == RETURN_ANSWER_TEXT
        assert body["slots"] == ambiguous["slots"]
        assert body["pending_clarifications"] == {"category": AMBIGUOUS_CATEGORY}
        assert body["status"] == "collecting"
        assert body["reply_language"] == "en"
        assert body["frustration_active"] is False

        disk = read_session(session_dir, session_id)
        assert disk["slots"] == ambiguous["slots"]
        assert disk["slots"]["order_number"] == "NS-000123"
        assert disk["slots"]["urgency"] is None
        assert disk["pending_clarifications"] == {"category": AMBIGUOUS_CATEGORY}
        assert disk["status"] == "collecting"
        assert disk["reply_language"] == "en"
        assert disk["frustration_active"] is False
        assert len(disk["messages"]) == 5
        assert disk["messages"][0]["text"] != "rewritten"
        assert [message["text"] for message in disk["messages"]][-2:] == [
            RETURN_Q,
            RETURN_ANSWER_TEXT,
        ]


async def test_context_04_rag_context_carries_only_its_question_sources_and_language(
    settings, provider, session_dir
):
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        _, partial = await post_turn(
            client, provider, session_id,
            text=PART_TEXT, revision=0, extraction=part_extraction(),
        )
        assert partial["slots"]["order_number"] == "NS-000123"

        spanish_question = make_extraction(
            intent="question",
            has_question=True,
            detected_language="es",
            requested_language="es",
        )
        _, answered = await post_turn(
            client, provider, session_id,
            text=RETURN_Q, revision=partial["revision"],
            extraction=spanish_question,
            answer=grounded(RETURN_ANSWER_TEXT, ["faq-returns"]),
        )
        assert answered["reply"] == RETURN_ANSWER_TEXT
        assert answered["reply_language"] == "es"
        assert answered["slots"]["order_number"] == "NS-000123"

        method, context = next(
            call for call in reversed(provider.calls) if call[0] == "answer_knowledge"
        )
        assert method == "answer_knowledge"
        assert context.question == RETURN_Q
        assert [source.id for source in context.sources] == ["faq-returns"]
        assert [source.title for source in context.sources] == [
            seed_citation("faq-returns")["title"]
        ]
        assert answered["citations"] == [seed_citation("faq-returns")]
        disk = read_session(session_dir, session_id)
        assert disk["messages"][-1]["citations"] == [seed_citation("faq-returns")]
        assert context.reply_language == "es"
        assert context.kb_version == seed_citation("faq-returns")["kb_version"]
        assert set(context.model_dump()) == {
            "question",
            "kb_version",
            "sources",
            "reply_language",
        }
        serialized = serialize_context(context)
        assert "NS-000123" not in serialized
        assert PART_TEXT not in serialized
        assert "recent_messages" not in serialized
        assert "slots" not in serialized


class InjectingAnswerScriptedProvider(ScriptedProvider):
    """Scripted provider that appends a fabricated source and cites it."""

    async def answer_knowledge(self, context):
        context.sources.append(
            KnowledgeRecord(
                id="faq-injected",
                title="Injected source",
                keywords=["injected"],
                body="An injected source the knowledge base never supplied.",
            )
        )
        return await super().answer_knowledge(context)


async def test_context_05_injected_rag_source_cannot_become_a_citation(
    settings, session_dir
):
    provider = InjectingAnswerScriptedProvider()
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        _, partial = await post_turn(
            client, provider, session_id,
            text=PART_TEXT, revision=0, extraction=part_extraction(),
        )

        provider.script_extraction(question_extraction())
        provider.script_answer(
            grounded("An injected answer.", ["faq-injected"])
        )
        injected = await client.post(
            f"/api/sessions/{session_id}/turns",
            json=turn_body(RETURN_Q, partial["revision"], str(uuid4())),
        )

        assert injected.status_code == 502, injected.text
        assert injected.json()["error"]["code"] == "invalid_provider_response"
        disk = read_session(session_dir, session_id)
        assert disk["revision"] == partial["revision"]
        assert len(disk["messages"]) == 3
        assert disk["slots"] == partial["slots"]
        assert len(disk["requests"]) == 1
        assert disk["requests"][-1]["response"]["revision"] == partial["revision"]


async def test_context_06_policy_and_social_detours_preserve_review_state(
    settings, provider, session_dir
):
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        _, reviewed = await post_turn(
            client, provider, session_id,
            text=FULL_TEXT, revision=0, extraction=full_extraction(),
        )
        assert reviewed["status"] == "awaiting_confirmation"
        assert reviewed["pending_review_revision"] == 1
        assert reviewed["slots"] == FULL_SLOTS
        assert reviewed["summary"] == FULL_SUMMARY

        _, social = await post_turn(
            client, provider, session_id,
            text=SOCIAL_TEXT, revision=reviewed["revision"],
            extraction=social_extraction(),
        )
        assert social["reply"] == SOCIAL_ACKNOWLEDGMENT
        assert social["status"] == "awaiting_confirmation"
        assert social["pending_review_revision"] == 1
        assert social["slots"] == FULL_SLOTS
        assert social["summary"] == FULL_SUMMARY

        _, answered = await post_turn(
            client, provider, session_id,
            text=RETURN_Q, revision=social["revision"],
            extraction=question_extraction(),
            answer=grounded(RETURN_ANSWER_TEXT, ["faq-returns"]),
        )
        assert answered["reply"] == RETURN_ANSWER_TEXT
        assert answered["status"] == "awaiting_confirmation"
        assert answered["pending_review_revision"] == 1
        assert answered["slots"] == FULL_SLOTS
        assert answered["summary"] == FULL_SUMMARY

        context = last_extract_context(provider)
        assert context.status == "awaiting_confirmation"
        assert context.slots.model_dump() == reviewed["slots"]
        assert context.intake_started is True
        assert context.reply_language == "en"
        assert context.frustration_active is False

        disk = read_session(session_dir, session_id)
        assert disk["slots"] == FULL_SLOTS
        assert disk["pending_review_revision"] == 1
        assert disk["summary"] == FULL_SUMMARY
        assert disk["status"] == "awaiting_confirmation"
        assert len(disk["messages"]) == 7



