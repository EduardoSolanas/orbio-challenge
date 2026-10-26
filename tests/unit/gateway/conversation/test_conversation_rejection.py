"""Unit tests for the conversation turn transaction (IC §3-§7, RAG §5-§6): rejection."""

from __future__ import annotations

from gateway.localization import request_reference
from gateway.providers.prompts import CAPABILITY
from tests.support.unit_fakes import make_extraction
from tests.unit.gateway.conversation.conversation_support import (
    FULL_EXTRACTION,
    SUMMARY,
    assert_state,
    build_engine,
    confirm_equal,
    rejection,
    turn_request,
)


async def test_reject_then_valid_correction_emits_fresh_review_then_confirm(
    tmp_path,
):
    correction = make_extraction(
        intent="correction",
        candidates={"urgency": "low"},
        explicit_corrections=["urgency"],
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", rejection()),
            ("extract", correction),
            ("extract", confirm_equal("low")),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository,
        sid,
        first,
        revision=1,
        status="awaiting_confirmation",
        rejected=False,
        token=1,
        summary=SUMMARY,
    )

    second = await engine.turn(sid, turn_request("No, that's wrong", revision=1))
    await assert_state(
        repository,
        sid,
        second,
        revision=2,
        status="collecting",
        rejected=True,
        token=None,
        summary=None,
    )
    assert second.reply == "What would you like to change?"

    third = await engine.turn(sid, turn_request("Make the urgency low", revision=2))
    corrected_summary = SUMMARY.replace("Urgency: high", "Urgency: low")
    await assert_state(
        repository,
        sid,
        third,
        revision=3,
        status="awaiting_confirmation",
        rejected=False,
        token=3,
        summary=corrected_summary,
    )
    assert (
        third.reply
        == corrected_summary
        + "\nPlease confirm these details or tell me what to change."
    )

    fourth = await engine.turn(sid, turn_request("Yes, confirm", revision=3))
    await assert_state(
        repository,
        sid,
        fourth,
        revision=4,
        status="completed",
        rejected=False,
        token=None,
        summary=corrected_summary,
    )
    assert (
        fourth.reply
        == f"Saved locally as {request_reference(sid)}. No request has been sent to a real support team. Is there anything else I can help with?"
    )
    stored = await repository.load(sid)
    assert stored.collection_turn_count == 3
    assert stored.confirmation is not None
    assert stored.confirmation.review_revision == 3
    provider.assert_exhausted()


async def test_reject_then_equal_value_reaffirmation_recovers_and_confirms(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", rejection()),
            ("extract", confirm_equal()),
            ("extract", confirm_equal()),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository,
        sid,
        first,
        revision=1,
        status="awaiting_confirmation",
        rejected=False,
        token=1,
        summary=SUMMARY,
    )
    rejected = await engine.turn(sid, turn_request("That is not right", revision=1))
    await assert_state(
        repository,
        sid,
        rejected,
        revision=2,
        status="collecting",
        rejected=True,
        token=None,
        summary=None,
    )

    reaffirmed = await engine.turn(sid, turn_request("It is correct", revision=2))
    await assert_state(
        repository,
        sid,
        reaffirmed,
        revision=3,
        status="awaiting_confirmation",
        rejected=False,
        token=3,
        summary=SUMMARY,
    )

    confirmed = await engine.turn(sid, turn_request("Yes", revision=3))
    await assert_state(
        repository,
        sid,
        confirmed,
        revision=4,
        status="completed",
        rejected=False,
        token=None,
        summary=SUMMARY,
    )
    provider.assert_exhausted()


async def test_reject_then_no_candidate_reaffirmation_recovers_and_confirms(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", rejection()),
            ("extract", make_extraction(intent="confirm")),
            ("extract", make_extraction(intent="confirm")),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository,
        sid,
        first,
        revision=1,
        status="awaiting_confirmation",
        rejected=False,
        token=1,
        summary=SUMMARY,
    )
    rejected = await engine.turn(sid, turn_request("No", revision=1))
    await assert_state(
        repository,
        sid,
        rejected,
        revision=2,
        status="collecting",
        rejected=True,
        token=None,
        summary=None,
    )

    reaffirmed = await engine.turn(sid, turn_request("Correct", revision=2))
    await assert_state(
        repository,
        sid,
        reaffirmed,
        revision=3,
        status="awaiting_confirmation",
        rejected=False,
        token=3,
        summary=SUMMARY,
    )

    confirmed = await engine.turn(sid, turn_request("Yes, that is right", revision=3))
    await assert_state(
        repository,
        sid,
        confirmed,
        revision=4,
        status="completed",
        rejected=False,
        token=None,
        summary=SUMMARY,
    )
    provider.assert_exhausted()


async def test_reject_then_invalid_correction_stays_rejected(tmp_path):
    invalid = make_extraction(
        intent="correction",
        candidates={"urgency": "super-urgent"},
        explicit_corrections=["urgency"],
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", rejection()),
            ("extract", invalid),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository,
        sid,
        first,
        revision=1,
        status="awaiting_confirmation",
        rejected=False,
        token=1,
        summary=SUMMARY,
    )
    second = await engine.turn(sid, turn_request("No", revision=1))
    await assert_state(
        repository,
        sid,
        second,
        revision=2,
        status="collecting",
        rejected=True,
        token=None,
        summary=None,
    )
    third = await engine.turn(sid, turn_request("Make it super urgent", revision=2))

    await assert_state(
        repository,
        sid,
        third,
        revision=3,
        status="collecting",
        rejected=True,
        token=None,
        summary=None,
    )
    assert third.reply == "Please choose low, normal, or high urgency."
    provider.assert_exhausted()


async def test_reject_then_valid_correction_with_invalid_field_clears_flag_but_waits(
    tmp_path,
):
    mixed = make_extraction(
        intent="correction",
        candidates={"urgency": "low", "category": "mystery"},
        explicit_corrections=["urgency"],
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", rejection()),
            ("extract", mixed),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository,
        sid,
        first,
        revision=1,
        status="awaiting_confirmation",
        rejected=False,
        token=1,
        summary=SUMMARY,
    )
    second = await engine.turn(sid, turn_request("No", revision=1))
    await assert_state(
        repository,
        sid,
        second,
        revision=2,
        status="collecting",
        rejected=True,
        token=None,
        summary=None,
    )
    third = await engine.turn(
        sid, turn_request("Make urgency low, category mystery", revision=2)
    )

    await assert_state(
        repository,
        sid,
        third,
        revision=3,
        status="collecting",
        rejected=False,
        token=None,
        summary=None,
    )
    assert (
        third.reply
        == "Choose a bill, supply issue, meter reading, moving home, or other energy issue."
    )
    provider.assert_exhausted()


async def test_reject_then_unrelated_statement_stays_rejected(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", rejection()),
            ("extract", make_extraction(intent="unrelated")),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository,
        sid,
        first,
        revision=1,
        status="awaiting_confirmation",
        rejected=False,
        token=1,
        summary=SUMMARY,
    )
    second = await engine.turn(sid, turn_request("No", revision=1))
    await assert_state(
        repository,
        sid,
        second,
        revision=2,
        status="collecting",
        rejected=True,
        token=None,
        summary=None,
    )
    third = await engine.turn(sid, turn_request("The weather is nice", revision=2))

    await assert_state(
        repository,
        sid,
        third,
        revision=3,
        status="collecting",
        rejected=True,
        token=None,
        summary=None,
    )
    assert third.reply == CAPABILITY
    provider.assert_exhausted()


async def test_reject_then_pure_question_stays_rejected(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", rejection()),
            ("extract", make_extraction(intent="question", has_question=True)),
        ],
        kb_answers={"Do you ship internationally?": []},
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository,
        sid,
        first,
        revision=1,
        status="awaiting_confirmation",
        rejected=False,
        token=1,
        summary=SUMMARY,
    )
    second = await engine.turn(sid, turn_request("No", revision=1))
    await assert_state(
        repository,
        sid,
        second,
        revision=2,
        status="collecting",
        rejected=True,
        token=None,
        summary=None,
    )
    third = await engine.turn(
        sid, turn_request("Do you ship internationally?", revision=2)
    )

    await assert_state(
        repository,
        sid,
        third,
        revision=3,
        status="collecting",
        rejected=True,
        token=None,
        summary=None,
    )
    assert third.reply == (
        "I do not have that information in Northstar Energy's help guides. "
        "I can still help with your energy question or service issue."
    )
    assert third.citations == []
    provider.assert_exhausted()


async def test_reject_then_question_with_facts_stays_rejected_and_appends_suffix(
    tmp_path,
):
    question_with_facts = make_extraction(
        intent="provide_details",
        has_question=True,
        candidates={"order_number": "NS-000123"},
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", rejection()),
            ("extract", question_with_facts),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository,
        sid,
        first,
        revision=1,
        status="awaiting_confirmation",
        rejected=False,
        token=1,
        summary=SUMMARY,
    )
    second = await engine.turn(sid, turn_request("No", revision=1))
    await assert_state(
        repository,
        sid,
        second,
        revision=2,
        status="collecting",
        rejected=True,
        token=None,
        summary=None,
    )
    third = await engine.turn(
        sid, turn_request("NS-000123 — can I return it?", revision=2)
    )

    await assert_state(
        repository,
        sid,
        third,
        revision=3,
        status="collecting",
        rejected=True,
        token=None,
        summary=None,
    )
    assert third.reply == (
        "What would you like to change? "
        "Please ask your policy question in a separate message."
    )
    provider.assert_exhausted()
