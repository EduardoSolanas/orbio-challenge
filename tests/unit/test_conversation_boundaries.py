"""Unit tests for the conversation turn transaction (IC §3-§7, RAG §5-§6): boundaries."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.conversation import ConversationEngine
from app.errors import AppError
from app.prompts import (
    ABSTENTION,
    CAPABILITY,
    GREETING,
    NO_REVIEW_TO_REJECT,
    POLICY_QUESTION_SUFFIX,
    REJECT_PROMPT,
)
from app.schemas import ConfirmRequest, KnowledgeAnswer, TurnRequest
from app.storage import JsonSessionRepository
from tests.support.unit_fakes import (
    FakeKnowledgeBase,
    FakeProvider,
    SlowProvider,
    make_extraction,
    make_record,
    make_session,
    make_slots,
)
from tests.unit.conversation_support import (
    FULL_EXTRACTION,
    FULL_SLOTS,
    SUMMARY,
    assert_state,
    build_engine,
    confirm_request,
    rejection,
    turn_request,
)


def canonical_digest(endpoint, session_id, body):
    import hashlib
    import json

    payload = {"endpoint": endpoint, "session_id": str(session_id), **body}
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


async def test_turn_digest_is_canonical_json_with_endpoint_and_request_body():
    from app.conversation import turn_digest

    sid = UUID("8b9dc0ee-1111-4a4a-8a8a-0123456789ab")
    command = TurnRequest(
        request_id=UUID("11111111-2222-4333-8444-555555555555"),
        expected_revision=2,
        text="Café — naïve",
        input_mode="transcript",
    )
    expected = canonical_digest(
        "turn",
        sid,
        {
            "request_id": "11111111-2222-4333-8444-555555555555",
            "expected_revision": 2,
            "text": "Café — naïve",
            "input_mode": "transcript",
        },
    )

    assert turn_digest(sid, command) == expected


async def test_confirm_digest_is_canonical_json_with_endpoint_and_tokens():
    from app.conversation import confirm_digest

    sid = UUID("8b9dc0ee-1111-4a4a-8a8a-0123456789ab")
    command = ConfirmRequest(
        request_id=UUID("11111111-2222-4333-8444-555555555555"),
        expected_revision=3,
        review_revision=2,
    )
    expected = canonical_digest(
        "confirm",
        sid,
        {
            "request_id": "11111111-2222-4333-8444-555555555555",
            "expected_revision": 3,
            "review_revision": 2,
        },
    )

    assert confirm_digest(sid, command) == expected


async def test_unrelated_statement_without_facts_uses_capability_reply(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", make_extraction(intent="unrelated"))]
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("Hello there"))

    assert response.revision == 1
    assert response.status == "collecting"
    assert response.pending_review_revision is None
    assert response.summary is None
    assert response.reply == (
        "I can't help with that, but I can answer energy questions, look up a demo account after verification, "
        "and help report a service issue. I cannot change a real account or contact an energy network."
    )
    stored = await repository.load(created.session_id)
    assert stored.collection_turn_count == 1
    provider.assert_exhausted()


async def test_unrelated_statement_at_valid_review_preserves_summary_and_token(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", make_extraction(intent="unrelated"))],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Nice weather", revision=1))

    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 1
    assert response.summary == SUMMARY
    assert response.reply == (
        "I can't help with that, but I can answer energy questions, look up a demo account after verification, "
        "and help report a service issue. I cannot change a real account or contact an energy network."
    )
    provider.assert_exhausted()


async def test_empty_extraction_at_valid_review_redisplays_the_review(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", make_extraction()),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Hmm", revision=1))

    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 1
    assert response.summary == SUMMARY
    assert response.reply == SUMMARY + "\nPlease confirm these details or tell me what to change."
    provider.assert_exhausted()


async def test_empty_extraction_with_complete_slots_creates_a_fresh_review(
    tmp_path,
):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=2,
            status="collecting",
            slots=FULL_SLOTS,
            collection_turn_count=2,
        )
    )
    provider = FakeProvider([("extract", make_extraction())])
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    response = await engine.turn(sid, turn_request("Anything", revision=2))

    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 3
    assert response.summary == SUMMARY
    assert response.reply == SUMMARY + "\nPlease confirm these details or tell me what to change."
    provider.assert_exhausted()


async def test_reject_without_a_review_keeps_the_pending_issue_and_no_flag(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(
                    intent="reject", candidates={"urgency": "super-urgent"}
                ),
            )
        ],
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("No, it is super urgent"))

    assert response.status == "collecting"
    assert response.pending_review_revision is None
    assert response.summary is None
    assert response.reply == NO_REVIEW_TO_REJECT
    assert response.pending_clarifications["urgency"].code == "invalid"
    stored = await repository.load(created.session_id)
    assert stored.review_rejected is False
    provider.assert_exhausted()


async def test_pure_question_on_a_complete_session_without_review_has_no_follow_up(
    tmp_path,
):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=1,
            status="collecting",
            slots=FULL_SLOTS,
            collection_turn_count=1,
        )
    )
    engine = ConversationEngine(
        repository,
        FakeProvider([("extract", make_extraction(intent="question", has_question=True))]),
        FakeKnowledgeBase({"Which payment methods are accepted?": []}),
    )

    response = await engine.turn(
        sid, turn_request("Which payment methods are accepted?", revision=1)
    )

    assert response.reply == (
        "I do not have that information in Northstar Energy's help guides. "
        "I can still help with your energy question or service issue."
    )
    assert response.status == "collecting"


async def test_unrelated_question_without_facts_uses_the_capability_reply(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="unrelated", has_question=True)),
        ],
        kb_answers={"What can you do?": []},
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("What can you do?"))

    assert response.reply == CAPABILITY
    assert ABSTENTION not in response.reply
    assert POLICY_QUESTION_SUFFIX not in response.reply
    assert [method for method, _ in provider.calls] == ["extract"]
    provider.assert_exhausted()


async def test_pure_question_during_intake_has_no_follow_up(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(candidates={"order_number": "123"}),
            ),
            ("extract", make_extraction(intent="question", has_question=True)),
        ],
        kb_answers={"Do you ship internationally?": []},
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request("My order is 123"))

    response = await engine.turn(
        sid, turn_request("Do you ship internationally?", revision=1)
    )

    assert response.reply == (
        "I do not have that information in Northstar Energy's help guides. "
        "I can still help with your energy question or service issue."
    )
    assert response.pending_clarifications["order_number"].code == "invalid"
    provider.assert_exhausted()


async def test_question_with_only_corrections_is_not_a_pure_question(tmp_path):
    extraction = make_extraction(
        intent="unrelated",
        has_question=True,
        explicit_corrections=["urgency"],
    )
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", extraction)]
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("Why urgent?"))

    assert response.reply == (
        "What is your energy account reference? Use NS- followed by six digits, for example NS-123456. "
        "Please ask your policy question in a separate message."
    )
    provider.assert_exhausted()


async def test_question_with_only_ambiguities_is_not_a_pure_question(tmp_path):
    extraction = make_extraction(
        intent="unrelated",
        has_question=True,
        ambiguous_fields=["urgency"],
    )
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", extraction)]
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("Why urgent?"))

    assert response.reply == (
        "Please clarify your urgency. "
        "Please ask your policy question in a separate message."
    )
    provider.assert_exhausted()


async def test_one_character_answer_is_a_valid_grounded_answer(tmp_path):
    record = make_record()
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="question", has_question=True)),
            (
                "answer_knowledge",
                KnowledgeAnswer(answerable=True, answer="x", cited_ids=["faq-returns"]),
            ),
        ],
        kb_answers={"Can I return it?": [record]},
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("Can I return it?"))

    assert response.reply == "x"
    assert len(response.citations) == 1


async def test_answer_at_the_1200_character_boundary_is_accepted(tmp_path):
    record = make_record()
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="question", has_question=True)),
            (
                "answer_knowledge",
                KnowledgeAnswer(
                    answerable=True, answer="x" * 1200, cited_ids=["faq-returns"]
                ),
            ),
        ],
        kb_answers={"Can I return it?": [record]},
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("Can I return it?"))

    assert response.reply == "x" * 1200


async def test_two_distinct_cited_ids_are_accepted_and_snapshotted(tmp_path):
    first = make_record(record_id="faq-returns", title="Returns and refunds")
    second = make_record(
        record_id="faq-damaged",
        title="Damaged items",
        keywords=["damaged", "broken"],
        body="Keep the packaging and describe the damage.",
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="question", has_question=True)),
            (
                "answer_knowledge",
                KnowledgeAnswer(
                    answerable=True,
                    answer="Both policies apply.",
                    cited_ids=["faq-returns", "faq-damaged"],
                ),
            ),
        ],
        kb_answers={"Can I return a damaged item?": [first, second]},
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("Can I return a damaged item?")
    )

    assert [citation.source_id for citation in response.citations] == [
        "faq-returns",
        "faq-damaged",
    ]
    assert response.citations[1].title == "Damaged items"
    assert response.citations[1].excerpt == second.body


async def test_three_cited_ids_are_an_invalid_provider_response(tmp_path):
    records = [
        make_record(record_id=f"faq-{index}") for index in range(3)
    ]
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="question", has_question=True)),
            (
                "answer_knowledge",
                KnowledgeAnswer(
                    answerable=True,
                    answer="Grounded.",
                    cited_ids=["faq-0", "faq-1", "faq-2"],
                ),
            ),
        ],
        kb_answers={"Policies?": records},
    )
    created = await engine.create_session()

    with pytest.raises(AppError) as excinfo:
        await engine.turn(created.session_id, turn_request("Policies?"))

    assert excinfo.value.code == "invalid_provider_response"


async def rejected_after_full_review(tmp_path, script_tail):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", rejection()), *script_tail],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())
    await engine.turn(sid, turn_request("No", revision=1))
    return engine, repository, provider, sid


async def test_reaffirmation_with_an_explicit_correction_list_stays_rejected(
    tmp_path,
):
    extraction = make_extraction(
        intent="confirm",
        explicit_corrections=["urgency"],
    )
    engine, repository, provider, sid = await rejected_after_full_review(
        tmp_path, [("extract", extraction)]
    )

    response = await engine.turn(sid, turn_request("Yes", revision=2))

    await assert_state(
        repository, sid, response, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert response.reply == "What would you like to change?"
    provider.assert_exhausted()


async def test_reaffirmation_with_a_pending_issue_stays_rejected(tmp_path):
    extraction = make_extraction(
        intent="confirm", candidates={"urgency": "bogus"}
    )
    engine, repository, provider, sid = await rejected_after_full_review(
        tmp_path, [("extract", extraction)]
    )

    response = await engine.turn(sid, turn_request("Yes, bogus", revision=2))

    await assert_state(
        repository, sid, response, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert response.reply == "Please choose low, normal, or high urgency."
    provider.assert_exhausted()


async def test_reaffirmation_with_incomplete_slots_stays_rejected(tmp_path):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=2,
            status="collecting",
            slots=make_slots(order_number="NS-000123"),
            review_rejected=True,
            collection_turn_count=2,
        )
    )
    engine = ConversationEngine(
        repository,
        FakeProvider([("extract", make_extraction(intent="confirm"))]),
        FakeKnowledgeBase(),
    )

    response = await engine.turn(sid, turn_request("Yes", revision=2))

    await assert_state(
        repository, sid, response, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert response.reply == REJECT_PROMPT


async def test_reaffirmation_with_a_changed_candidate_stays_rejected(tmp_path):
    extraction = make_extraction(
        intent="confirm", candidates={"urgency": "low"}
    )
    engine, repository, provider, sid = await rejected_after_full_review(
        tmp_path, [("extract", extraction)]
    )

    response = await engine.turn(sid, turn_request("Actually low", revision=2))

    await assert_state(
        repository, sid, response, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert response.pending_clarifications["urgency"].code == "conflict"
    provider.assert_exhausted()


async def test_reaffirmation_with_an_invalid_accepted_slot_stays_rejected(
    tmp_path,
):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=2,
            status="collecting",
            slots=FULL_SLOTS.model_copy(update={"urgency": "bogus"}),
            review_rejected=True,
            collection_turn_count=2,
        )
    )
    engine = ConversationEngine(
        repository,
        FakeProvider([("extract", make_extraction(intent="confirm"))]),
        FakeKnowledgeBase(),
    )

    response = await engine.turn(sid, turn_request("Yes", revision=2))

    await assert_state(
        repository, sid, response, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert response.reply == "What would you like to change?"


async def test_reaffirmation_with_alias_equal_candidates_recovers(tmp_path):
    extraction = make_extraction(
        intent="confirm",
        candidates={"category": "Damaged Item", "urgency": "High"},
    )
    engine, repository, provider, sid = await rejected_after_full_review(
        tmp_path, [("extract", extraction)]
    )

    response = await engine.turn(sid, turn_request("Yes, that is right", revision=2))

    await assert_state(
        repository, sid, response, revision=3, status="awaiting_confirmation",
        rejected=False, token=3, summary=SUMMARY,
    )
    provider.assert_exhausted()


async def test_confirmation_with_a_new_pending_issue_does_not_complete(tmp_path):
    extraction = make_extraction(
        intent="confirm", candidates={"urgency": "bogus"}
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", extraction)],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Yes, bogus", revision=1))

    assert response.status == "collecting"
    assert response.pending_review_revision is None
    assert response.summary is None
    assert response.reply == "Please choose low, normal, or high urgency."
    stored = await repository.load(sid)
    assert stored.confirmation is None
    provider.assert_exhausted()


async def test_confirmation_with_alias_equal_candidates_completes(tmp_path):
    extraction = make_extraction(
        intent="confirm",
        candidates={
            "order_number": "NS-000123",
            "category": "Damaged Item",
            "description": "My order arrived broken",
            "urgency": "High",
        },
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", extraction)],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Yes", revision=1))

    assert response.status == "completed"
    assert response.summary == SUMMARY
    stored = await repository.load(sid)
    assert stored.confirmation is not None
    provider.assert_exhausted()


async def test_confirmation_revision_conflict_exposes_current_revision(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    with pytest.raises(AppError) as excinfo:
        await engine.confirm(sid, confirm_request(revision=0, review_revision=1))

    assert excinfo.value.code == "revision_conflict"
    assert excinfo.value.current_revision == 1


async def test_get_session_exposes_the_review_token_and_summary(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    fetched = await engine.get_session(sid)

    assert fetched.revision == 1
    assert fetched.status == "awaiting_confirmation"
    assert fetched.pending_review_revision == 1
    assert fetched.summary == SUMMARY
    assert len(fetched.messages) == 3
    provider.assert_exhausted()


async def test_engine_locks_are_per_session_not_global(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    first = await engine.create_session()
    second = await engine.create_session()

    async with repository.locked(first.session_id):
        fetched = await engine.get_session(second.session_id)
        assert fetched.session_id == second.session_id
        assert await engine.assistant_text(
            second.session_id, second.messages[0].id
        ) == GREETING
        turned = await engine.turn(second.session_id, turn_request())
        assert turned.revision == 1
        confirmed = await engine.confirm(
            second.session_id, confirm_request(revision=1, review_revision=1)
        )
        assert confirmed.status == "completed"
        created = await engine.create_session()
        assert created.revision == 0
    provider.assert_exhausted()


async def test_dedicated_confirmation_persists_the_canonical_request_digest(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())
    command = confirm_request()

    await engine.confirm(sid, command)

    stored = await repository.load(sid)
    expected = canonical_digest(
        "confirm",
        sid,
        {
            "request_id": str(command.request_id),
            "expected_revision": 1,
            "review_revision": 1,
        },
    )
    assert stored.requests[-1].input_digest == expected


async def test_accepted_equal_details_at_a_valid_review_create_a_fresh_review(
    tmp_path,
):
    repeated = make_extraction(
        candidates={
            "order_number": "NS-000123",
            "category": "damaged_item",
            "description": "My order arrived broken",
            "urgency": "high",
        }
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", repeated)],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Same details again", revision=1))

    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 2
    assert response.summary == SUMMARY
    provider.assert_exhausted()


async def test_confirmation_with_correction_flags_does_not_complete(tmp_path):
    extraction = make_extraction(
        intent="confirm",
        explicit_corrections=["urgency"],
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", extraction)],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Yes, but urgency", revision=1))

    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 1
    stored = await repository.load(sid)
    assert stored.confirmation is None
    provider.assert_exhausted()


async def test_engine_operations_on_other_sessions_do_not_wait_for_a_slow_turn(
    tmp_path,
):
    import asyncio
    import time

    repository = JsonSessionRepository(tmp_path)
    provider = SlowProvider(
        [("extract", FULL_EXTRACTION), ("extract", FULL_EXTRACTION)], delay=0.4
    )
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())
    slow = await engine.create_session()
    other = await engine.create_session()
    await engine.turn(other.session_id, turn_request())
    await engine.confirm(
        other.session_id, confirm_request(revision=1, review_revision=1)
    )
    fast_engine = ConversationEngine(
        repository,
        FakeProvider([("extract", FULL_EXTRACTION)]),
        FakeKnowledgeBase(),
    )
    fresh = await fast_engine.create_session()

    slow_turn = asyncio.create_task(
        engine.turn(slow.session_id, turn_request())
    )
    await asyncio.sleep(0.05)

    started = time.monotonic()
    await engine.get_session(other.session_id)
    await engine.assistant_text(other.session_id, other.messages[0].id)
    await fast_engine.turn(fresh.session_id, turn_request())
    await fast_engine.confirm(
        fresh.session_id, confirm_request(revision=1, review_revision=1)
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    await slow_turn


async def test_engine_never_uses_a_global_none_lock_key(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    first = await engine.create_session()
    second = await engine.create_session()
    await engine.turn(second.session_id, turn_request())

    async with repository.locked(None):
        fetched = await engine.get_session(first.session_id)
        assert fetched.session_id == first.session_id
        assert await engine.assistant_text(
            first.session_id, first.messages[0].id
        ) == GREETING
        created = await engine.create_session()
        assert created.revision == 0
        confirmed = await engine.confirm(
            second.session_id, confirm_request(revision=1, review_revision=1)
        )
        assert confirmed.status == "completed"
    provider.assert_exhausted()
