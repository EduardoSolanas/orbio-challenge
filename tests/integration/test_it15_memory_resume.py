"""ORB-39: multi-turn memory and session resume.

Integration coverage for the acceptance criteria, against the real app
factory, lifespan, engine and JSON repository over an isolated temp
SESSION_DIR; only the AI service boundary is scripted (IC §13.1).

- ORB39-01: review state (transcript, token, summary, citations) survives a
  process restart via GET, and the preserved token still confirms.
- ORB39-02: accepted facts survive once early messages fall outside the
  12-message extraction window.
- ORB39-03: facts survive an FAQ detour followed by a correction.
"""

from __future__ import annotations

from uuid import uuid4

from app.prompts import SOCIAL_ACKNOWLEDGMENT
from tests.integration.support_api import (
    make_extraction,
    persisted_response,
    read_session,
    served_messages,
)
from tests.integration.test_it02_intake import (
    FULL_SLOTS,
    FULL_SUMMARY,
    REVIEW_SUFFIX,
    CATEGORY_TEXT,
    HIGH_TEXT,
    PART_TEXT,
    category_extraction,
    full_extraction,
    high_extraction,
    part_extraction,
    start_session,
    turn_body,
)
from tests.integration.test_it06_persistence import app_client
from tests.integration.test_it10_rag import (
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


# ORB39-01 ------------------------------------------------------------------


async def test_orb39_01_review_state_resumes_across_restart(settings, provider, session_dir):
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


# ORB39-02 ------------------------------------------------------------------


async def test_orb39_02_facts_survive_beyond_twelve_message_window(
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


# ORB39-03 ------------------------------------------------------------------


async def test_orb39_03_faq_detour_then_correction_keeps_sibling_facts(
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
