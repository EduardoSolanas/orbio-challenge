"""IC §5 canonical intent table and mixed-intent rules at the real boundary.

Every test runs the real app factory, real lifespan, the real
`/api/sessions/{id}/turns` route, the real `ConversationEngine`, the real lexical
retriever over the bundled knowledge base and the real JSON repository in a
temporary directory. Only the external AI service is a deterministic ledger
(`ScriptedProvider`): E = one `extract` call, A = one `answer_knowledge` call.

Rows already owned by IT02 (provide_details/correction), IT04 (reject/recovery/
review token), IT05 (limits), IT07 (idempotency) and IT10 (supported/unsupported
questions, mixed facts+question, question at review) are mapped in
`reports/integration-matrix.d/ORB-33.json`; this module adds the HTTP-level
regression coverage for every remaining table row and mixed-intent rule.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.prompts import (
    ABSTENTION,
    CAPABILITY,
    CAPABILITY_REPLY,
    INVALID_ORDER,
    MISSING_CATEGORY,
    MISSING_ORDER,
    NO_REVIEW_TO_REJECT,
    NOTHING_TO_CONFIRM,
    POLICY_QUESTION_SUFFIX,
    REJECT_PROMPT,
    SOCIAL_ACKNOWLEDGMENT,
)
from app.schemas import KnowledgeAnswer
from tests.integration.support_api import (
    make_extraction,
    read_session,
)
from tests.integration.test_it02_intake import (
    EMPTY_SLOTS,
    FULL_SLOTS,
    FULL_SUMMARY,
    PART_TEXT,
    REVIEW_SUFFIX,
    part_extraction,
    post_turn,
    start_session,
    turn_body,
)
from tests.integration.test_it04_state import (
    NO_TEXT,
    YES_TEXT,
    assert_error,
    assert_ledger,
    assert_session_state,
    assert_turn_commit,
    post_confirm,
    receipt_for,
    rejected_session,
    review_session,
    send_turn,
)
from tests.integration.test_it05_limits import (
    AT_CAP,
    PARTIAL_SLOTS,
    TURN_LIMIT_MESSAGE,
    raw_bytes,
    seed_session,
)

SOCIAL_GREETING = "Hello, how are you?"
SOCIAL_THANKS = "Thanks."
CAPABILITY_Q = "What can you help me with?"
UNRELATED_TEXT = "Tell me a joke"
START_TEXT = "I want to report an issue"
RESUME_TEXT = "Continue my issue"
RETURN_Q = "Can I return an unused item?"
PAYMENT_Q = "Which payment methods are accepted?"
RETURN_ANSWER_TEXT = (
    "You can request a return within 30 calendar days of delivery for an unused item in "
    "its original packaging. Refund eligibility requires support review."
)
PART_SLOTS = {
    "order_number": "NS-000123",
    "category": None,
    "description": "My order arrived broken",
    "urgency": None,
}


def social_extraction(has_question: bool = False):
    return make_extraction(intent="social", has_question=has_question)


def capability_extraction(has_question: bool = False):
    return make_extraction(intent="capability", has_question=has_question)


def start_intake_extraction(has_question: bool = False):
    return make_extraction(intent="start_intake", has_question=has_question)


def question_extraction():
    return make_extraction(intent="question", has_question=True)


def grounded_return_answer() -> KnowledgeAnswer:
    return KnowledgeAnswer(
        answerable=True, answer=RETURN_ANSWER_TEXT, cited_ids=["faq-returns"]
    )


def assert_no_policy_suffix(reply: str) -> None:
    assert POLICY_QUESTION_SUFFIX not in reply


# social --------------------------------------------------------------------


async def test_social_greeting_returns_only_the_exact_acknowledgment(
    client, provider, session_dir
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(social_extraction(has_question=True))

    request_id, response = await send_turn(
        client, session_id, text=SOCIAL_GREETING, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == SOCIAL_ACKNOWLEDGMENT
    assert ABSTENTION not in payload["reply"]
    assert_no_policy_suffix(payload["reply"])
    assert payload["citations"] == []
    assert_session_state(
        payload,
        revision=1,
        status="collecting",
        slots=EMPTY_SLOTS,
        summary=None,
        token=None,
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=SOCIAL_GREETING,
        counter_delta=1,
    )
    assert disk["review_rejected"] is False


async def test_social_thanks_preserves_accepted_facts_and_pending_state(
    client, provider, session_dir
):
    session_id = await start_session(client)
    _, part = await post_turn(
        client,
        provider,
        session_id,
        text=PART_TEXT,
        revision=0,
        extraction=part_extraction(),
    )
    assert part["reply"] == MISSING_CATEGORY
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(social_extraction())

    request_id, response = await send_turn(
        client, session_id, text=SOCIAL_THANKS, revision=part["revision"]
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == SOCIAL_ACKNOWLEDGMENT
    assert MISSING_CATEGORY not in payload["reply"]
    assert_no_policy_suffix(payload["reply"])
    assert_session_state(
        payload,
        revision=part["revision"] + 1,
        status="collecting",
        slots=PART_SLOTS,
        summary=None,
        token=None,
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=SOCIAL_THANKS,
        counter_delta=1,
    )


async def test_social_at_a_valid_review_preserves_the_token_summary_and_status(
    client, provider, session_dir
):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    assert before["pending_review_revision"] == 1
    calls_before = len(provider.calls)
    provider.script_extraction(social_extraction(has_question=True))

    request_id, response = await send_turn(
        client, session_id, text=SOCIAL_GREETING, revision=1
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == SOCIAL_ACKNOWLEDGMENT
    assert REVIEW_SUFFIX not in payload["reply"]
    assert_session_state(
        payload,
        revision=2,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=1,
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=SOCIAL_GREETING,
        counter_delta=1,
    )
    assert disk["review_rejected"] is False
    assert disk["pending_review_revision"] == 1


async def test_social_while_rejected_preserves_rejection_without_the_reject_prompt(
    client, provider, session_dir
):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    assert before["review_rejected"] is True
    calls_before = len(provider.calls)
    provider.script_extraction(social_extraction())

    request_id, response = await send_turn(
        client, session_id, text=SOCIAL_THANKS, revision=2
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == SOCIAL_ACKNOWLEDGMENT
    assert payload["reply"] != REJECT_PROMPT
    assert_session_state(
        payload,
        revision=3,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=SOCIAL_THANKS,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True


async def test_social_while_an_issue_is_pending_preserves_the_clarification(
    client, provider, session_dir
):
    session_id = await start_session(client)
    _, invalid = await post_turn(
        client,
        provider,
        session_id,
        text="My order is 123.",
        revision=0,
        extraction=make_extraction(candidates={"order_number": "123"}),
    )
    assert invalid["reply"] == INVALID_ORDER
    assert invalid["pending_clarifications"]["order_number"]["code"] == "invalid"
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(social_extraction(has_question=True))

    request_id, response = await send_turn(
        client, session_id, text=SOCIAL_GREETING, revision=invalid["revision"]
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == SOCIAL_ACKNOWLEDGMENT
    assert INVALID_ORDER not in payload["reply"]
    assert payload["pending_clarifications"]["order_number"]["code"] == "invalid"
    assert_session_state(
        payload,
        revision=invalid["revision"] + 1,
        status="collecting",
        slots=invalid["slots"],
        summary=None,
        token=None,
        pending={"order_number": payload["pending_clarifications"]["order_number"]},
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=SOCIAL_GREETING,
        counter_delta=1,
    )


@pytest.mark.parametrize(
    ("intent", "customer_text"),
    [
        ("social", SOCIAL_GREETING),
        ("capability", CAPABILITY_Q),
        ("unrelated", UNRELATED_TEXT),
        ("start_intake", START_TEXT),
    ],
)
async def test_non_confirmation_intents_are_refused_at_the_turn_limit(
    client, provider, session_dir, intent, customer_text
):
    session_id = await seed_session(
        session_dir,
        revision=AT_CAP,
        status="collecting",
        slots=PARTIAL_SLOTS,
        summary=None,
        review_revision=None,
        collection_turn_count=AT_CAP,
    )
    before_bytes = raw_bytes(session_dir, session_id)
    calls_before = len(provider.calls)

    _, response = await send_turn(
        client, session_id, text=customer_text, revision=AT_CAP
    )

    await assert_error(
        response, status=409, code="turn_limit", message=TURN_LIMIT_MESSAGE
    )
    # The guard runs before extraction: no model work and no mutation.
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    assert raw_bytes(session_dir, session_id) == before_bytes


# capability ----------------------------------------------------------------


async def test_capability_returns_the_exact_offer_without_retrieval_or_state_change(
    client, provider, session_dir
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(capability_extraction(has_question=True))

    request_id, response = await send_turn(
        client, session_id, text=CAPABILITY_Q, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == CAPABILITY_REPLY
    assert ABSTENTION not in payload["reply"]
    assert_no_policy_suffix(payload["reply"])
    assert payload["citations"] == []
    assert_session_state(
        payload,
        revision=1,
        status="collecting",
        slots=EMPTY_SLOTS,
        summary=None,
        token=None,
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=CAPABILITY_Q,
        counter_delta=1,
    )


async def test_capability_at_a_valid_review_preserves_the_token(
    client, provider, session_dir
):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(capability_extraction(has_question=True))

    request_id, response = await send_turn(
        client, session_id, text=CAPABILITY_Q, revision=1
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == CAPABILITY_REPLY
    assert REVIEW_SUFFIX not in payload["reply"]
    assert_session_state(
        payload,
        revision=2,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=1,
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=CAPABILITY_Q,
        counter_delta=1,
    )


# questions -----------------------------------------------------------------


async def test_supported_policy_question_during_intake_returns_only_the_cited_answer(
    client, provider, session_dir
):
    session_id = await start_session(client)
    _, part = await post_turn(
        client,
        provider,
        session_id,
        text=PART_TEXT,
        revision=0,
        extraction=part_extraction(),
    )
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(question_extraction())
    provider.script_answer(grounded_return_answer())

    request_id, response = await send_turn(
        client, session_id, text=RETURN_Q, revision=part["revision"]
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=1)
    assert payload["reply"] == RETURN_ANSWER_TEXT
    assert MISSING_CATEGORY not in payload["reply"]
    assert payload["reply"] != part["reply"]
    assert [citation["source_id"] for citation in payload["citations"]] == ["faq-returns"]
    assert_session_state(
        payload,
        revision=part["revision"] + 1,
        status="collecting",
        slots=PART_SLOTS,
        summary=None,
        token=None,
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=RETURN_Q,
        counter_delta=1,
    )
    assert disk["messages"][-1]["citations"] == payload["citations"]
    assert provider.calls[-1][1].question == RETURN_Q


async def test_unsupported_policy_question_during_intake_abstains_without_generation(
    client, provider, session_dir
):
    session_id = await start_session(client)
    _, part = await post_turn(
        client,
        provider,
        session_id,
        text=PART_TEXT,
        revision=0,
        extraction=part_extraction(),
    )
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(question_extraction())

    request_id, response = await send_turn(
        client, session_id, text=PAYMENT_Q, revision=part["revision"]
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == ABSTENTION
    assert MISSING_CATEGORY not in payload["reply"]
    assert payload["citations"] == []
    assert_session_state(
        payload,
        revision=part["revision"] + 1,
        status="collecting",
        slots=PART_SLOTS,
        summary=None,
        token=None,
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=PAYMENT_Q,
        counter_delta=1,
    )


async def test_unsupported_policy_question_at_review_preserves_the_token(
    client, provider, session_dir
):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(question_extraction())

    request_id, response = await send_turn(
        client, session_id, text=PAYMENT_Q, revision=1
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == ABSTENTION
    assert REVIEW_SUFFIX not in payload["reply"]
    assert_session_state(
        payload,
        revision=2,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=1,
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=PAYMENT_Q,
        counter_delta=1,
    )

    # The preserved token still confirms the same review once.
    _, confirmed = await post_confirm(
        client, session_id, expected_revision=2, review_revision=1
    )
    assert confirmed.status_code == 200, confirmed.text
    confirmed_payload = confirmed.json()
    assert confirmed_payload["status"] == "completed"
    assert confirmed_payload["reply"] == receipt_for(session_id)
    assert read_session(session_dir, session_id)["confirmation"]["review_revision"] == 1


# start_intake / resume -----------------------------------------------------


async def test_explicit_start_intake_asks_the_first_missing_field(
    client, provider, session_dir
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(start_intake_extraction(has_question=False))

    request_id, response = await send_turn(
        client, session_id, text=START_TEXT, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == MISSING_ORDER
    assert_no_policy_suffix(payload["reply"])
    assert_session_state(
        payload,
        revision=1,
        status="collecting",
        slots=EMPTY_SLOTS,
        summary=None,
        token=None,
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=START_TEXT,
        counter_delta=1,
    )


async def test_explicit_resume_during_intake_reasks_the_pending_missing_field(
    client, provider, session_dir
):
    session_id = await start_session(client)
    _, part = await post_turn(
        client,
        provider,
        session_id,
        text=PART_TEXT,
        revision=0,
        extraction=part_extraction(),
    )
    assert part["reply"] == MISSING_CATEGORY
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(start_intake_extraction())

    request_id, response = await send_turn(
        client, session_id, text=RESUME_TEXT, revision=part["revision"]
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == MISSING_CATEGORY
    assert_session_state(
        payload,
        revision=part["revision"] + 1,
        status="collecting",
        slots=PART_SLOTS,
        summary=None,
        token=None,
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=RESUME_TEXT,
        counter_delta=1,
    )


async def test_explicit_resume_at_review_redisplays_the_current_review(
    client, provider, session_dir
):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(start_intake_extraction())

    request_id, response = await send_turn(
        client, session_id, text=RESUME_TEXT, revision=1
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == FULL_SUMMARY + REVIEW_SUFFIX
    assert_session_state(
        payload,
        revision=2,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=1,
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=RESUME_TEXT,
        counter_delta=1,
    )


async def test_explicit_resume_while_rejected_keeps_the_rejection_prompt(
    client, provider, session_dir
):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(start_intake_extraction())

    request_id, response = await send_turn(
        client, session_id, text=RESUME_TEXT, revision=2
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == REJECT_PROMPT
    assert_session_state(
        payload,
        revision=3,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=RESUME_TEXT,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True


# unrelated / confirm / reject without state --------------------------------


async def test_unrelated_statement_uses_the_fixed_capability_reply(
    client, provider, session_dir
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(make_extraction(intent="unrelated", has_question=True))

    request_id, response = await send_turn(
        client, session_id, text=UNRELATED_TEXT, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == CAPABILITY
    assert MISSING_ORDER not in payload["reply"]
    assert_no_policy_suffix(payload["reply"])
    assert payload["citations"] == []
    assert_session_state(
        payload,
        revision=1,
        status="collecting",
        slots=EMPTY_SLOTS,
        summary=None,
        token=None,
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=UNRELATED_TEXT,
        counter_delta=1,
    )


async def test_confirm_without_a_review_explains_and_never_completes(
    client, provider, session_dir
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(make_extraction(intent="confirm"))

    request_id, response = await send_turn(
        client, session_id, text=YES_TEXT, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == NOTHING_TO_CONFIRM
    assert_session_state(
        payload,
        revision=1,
        status="collecting",
        slots=EMPTY_SLOTS,
        summary=None,
        token=None,
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=YES_TEXT,
        counter_delta=1,
    )
    assert disk["confirmation"] is None


async def test_reject_without_a_review_does_not_fabricate_rejection(
    client, provider, session_dir
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(make_extraction(intent="reject"))

    request_id, response = await send_turn(
        client, session_id, text=NO_TEXT, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == NO_REVIEW_TO_REJECT
    assert_session_state(
        payload,
        revision=1,
        status="collecting",
        slots=EMPTY_SLOTS,
        summary=None,
        token=None,
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=NO_TEXT,
        counter_delta=1,
    )
    assert disk["review_rejected"] is False


# mixed intent --------------------------------------------------------------


@pytest.mark.parametrize("has_question", [False, True])
async def test_social_preface_with_facts_starts_intake(
    client, provider, session_dir, has_question
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(
        make_extraction(
            intent="social",
            has_question=has_question,
            candidates={
                "order_number": "NS-000123",
                "description": "My order arrived broken",
            },
        )
    )
    customer_text = "Hello, my order NS-000123 arrived broken."

    request_id, response = await send_turn(
        client, session_id, text=customer_text, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    # Real facts outrank the social preface: intake reply, never the acknowledgment.
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert SOCIAL_ACKNOWLEDGMENT not in payload["reply"]
    assert payload["reply"].startswith(MISSING_CATEGORY)
    assert (POLICY_QUESTION_SUFFIX in payload["reply"]) is has_question
    assert_session_state(
        payload,
        revision=1,
        status="collecting",
        slots=PART_SLOTS,
        summary=None,
        token=None,
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=customer_text,
        counter_delta=1,
    )


async def test_confirmation_with_a_policy_question_redisplays_the_review(
    client, provider, session_dir
):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(make_extraction(intent="confirm", has_question=True))

    request_id, response = await send_turn(
        client, session_id, text="Yes, can I return it?", revision=1
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == (
        f"{FULL_SUMMARY}{REVIEW_SUFFIX} {POLICY_QUESTION_SUFFIX}"
    )
    assert_session_state(
        payload,
        revision=2,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=1,
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text="Yes, can I return it?",
        counter_delta=1,
    )
    assert disk["confirmation"] is None


# guards --------------------------------------------------------------------


async def test_completed_session_refuses_a_new_social_intent(
    client, provider, session_dir
):
    session_id = await review_session(client, provider)
    _, confirmed = await post_confirm(
        client, session_id, expected_revision=1, review_revision=1
    )
    assert confirmed.status_code == 200, confirmed.text
    completed = read_session(session_dir, session_id)
    assert completed["status"] == "completed"
    before_bytes = raw_bytes(session_dir, session_id)
    calls_before = len(provider.calls)

    _, response = await send_turn(
        client, session_id, text=SOCIAL_GREETING, revision=completed["revision"]
    )

    await assert_error(
        response,
        status=409,
        code="session_completed",
        message="This intake is complete. Start a new intake to make changes.",
    )
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    assert raw_bytes(session_dir, session_id) == before_bytes


async def test_revision_conflict_precedes_intent_routing(
    client, provider, session_dir
):
    session_id = await start_session(client)
    before_bytes = raw_bytes(session_dir, session_id)
    calls_before = len(provider.calls)

    _, response = await send_turn(
        client, session_id, text=SOCIAL_GREETING, revision=7
    )

    await assert_error(
        response,
        status=409,
        code="revision_conflict",
        message="This session changed. Refresh before sending again.",
        current_revision=0,
    )
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    assert raw_bytes(session_dir, session_id) == before_bytes


async def test_replay_of_a_social_turn_returns_the_stored_response(
    client, provider, session_dir
):
    session_id = await start_session(client)
    request_id = str(uuid4())
    body = turn_body(SOCIAL_GREETING, 0, request_id)
    provider.script_extraction(social_extraction(has_question=True))

    first = await client.post(f"/api/sessions/{session_id}/turns", json=body)
    assert first.status_code == 200, first.text
    original = first.json()
    calls_after_first = len(provider.calls)
    before_bytes = raw_bytes(session_dir, session_id)

    replay = await client.post(f"/api/sessions/{session_id}/turns", json=body)

    assert replay.status_code == 200, replay.text
    assert replay.json() == original
    assert len(provider.calls) == calls_after_first
    assert raw_bytes(session_dir, session_id) == before_bytes


async def test_reused_request_id_with_a_changed_social_message_is_a_conflict(
    client, provider, session_dir
):
    session_id = await start_session(client)
    request_id = str(uuid4())
    provider.script_extraction(social_extraction(has_question=True))
    first = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body(SOCIAL_GREETING, 0, request_id),
    )
    assert first.status_code == 200, first.text
    before_bytes = raw_bytes(session_dir, session_id)
    calls_before = len(provider.calls)

    conflict = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body(SOCIAL_THANKS, 0, request_id),
    )

    await assert_error(
        conflict,
        status=409,
        code="request_id_conflict",
        message="This request ID was already used for different input.",
    )
    assert len(provider.calls) == calls_before
    assert raw_bytes(session_dir, session_id) == before_bytes
