"""FR01-FR22: frustration episode behavior over the real HTTP application.

Every case runs the real app factory, lifespan, routes, engine, JSON repository
and bundled retriever; only the remote AI service is scripted (IC §13.1). Stable
FR case identifiers are asserted in the test metadata so report evidence can map
each required behavior to an executed test.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock
from uuid import uuid4

import pytest

from app.errors import app_error
from app.main import create_app
from app.schemas import KnowledgeAnswer
from tests.integration.support_api import (
    ScriptedProvider,
    make_extraction,
    read_session,
    running_client,
)
from tests.integration.test_it02_intake import (
    FULL_SLOTS,
    MISSING_CATEGORY,
    start_session,
    turn_body,
)

EN_PREFIX = "I understand this is frustrating."
ES_PREFIX = "Entiendo que esto es frustrante."
SPANISH_MISSING_CATEGORY = (
    "¿Se trata de la entrega, un artículo dañado, un artículo incorrecto, una "
    "devolución o reembolso, u otra cosa?"
)
UNSUPPORTED_NOTICE = (
    "This demo supports English and Spanish only. Please write in English or Spanish."
)
DELIVERY_QUESTION = "How long does delivery take?"
DELIVERY_ANSWER = "Standard delivery takes 2 to 5 business days after dispatch."
FULL_EXTRACTION = make_extraction(candidates=dict(FULL_SLOTS))


class FailingExtractionProvider(ScriptedProvider):
    """Extraction boundary fails after its bounded repair budget."""

    async def extract(self, context):
        self.calls.append(("extract", context))
        raise app_error("invalid_provider_response")


class FailingAnswerProvider(ScriptedProvider):
    """Grounded answer boundary fails after a valid extraction."""

    async def answer_knowledge(self, context):
        self.calls.append(("answer_knowledge", context))
        raise app_error("provider_unavailable")


def social_extraction(*, sentiment="neutral", frustration="none"):
    return make_extraction(intent="social", sentiment=sentiment, frustration=frustration)


def present_extraction(*, candidates=None, sentiment="negative"):
    return make_extraction(
        candidates=candidates or {},
        sentiment=sentiment,
        frustration="present",
    )


async def send_turn(
    client, provider, session_id, text_value, revision, extraction, request_id=None
):
    provider.script_extraction(extraction)
    body = turn_body(text_value, revision, request_id)
    response = await client.post(f"/api/sessions/{session_id}/turns", json=body)
    return response, body


def assert_turn(response, *, active, prefixed, prefix=EN_PREFIX):
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["frustration_active"] is active
    assert payload["reply"].startswith(prefix) is prefixed
    return payload


# FR01 ----------------------------------------------------------------------- #


async def test_fr01_new_session_has_a_false_flag_and_unlabelled_greeting(client, provider):
    created = await client.post("/api/sessions", json={})

    assert created.status_code == 201
    payload = created.json()
    assert payload["frustration_active"] is False
    assert payload["messages"][0]["sentiment"] is None
    assert payload["messages"][0]["frustration"] is None
    assert provider.methods() == []


# FR02 ----------------------------------------------------------------------- #


async def test_fr02_all_six_transition_rows_over_http(client, provider, session_dir):
    session_id = await start_session(client)

    response, _ = await send_turn(
        client, provider, session_id, "Hello.", 0, social_extraction(sentiment="positive")
    )
    assert_turn(response, active=False, prefixed=False)

    response, _ = await send_turn(
        client, provider, session_id, "Great. Just great.", 1,
        social_extraction(sentiment="uncertain", frustration="uncertain"),
    )
    assert_turn(response, active=False, prefixed=False)

    response, _ = await send_turn(
        client, provider, session_id, "I am really frustrated.", 2,
        present_extraction(sentiment="negative"),
    )
    assert_turn(response, active=True, prefixed=True)

    response, _ = await send_turn(
        client, provider, session_id, "Still frustrated.", 3, present_extraction()
    )
    assert_turn(response, active=True, prefixed=False)

    response, _ = await send_turn(
        client, provider, session_id, "Hmm, not sure.", 4,
        social_extraction(sentiment="uncertain", frustration="uncertain"),
    )
    assert_turn(response, active=True, prefixed=False)

    response, _ = await send_turn(
        client, provider, session_id, "Thanks, resolved.", 5,
        social_extraction(sentiment="positive", frustration="none"),
    )
    assert_turn(response, active=False, prefixed=False)

    disk = read_session(session_dir, session_id)
    assert disk["frustration_active"] is False
    assert sum(
        message["text"].startswith(EN_PREFIX)
        for message in disk["messages"]
        if message["role"] == "assistant"
    ) == 1


# FR03 ----------------------------------------------------------------------- #


async def test_fr03_new_frustrated_report_is_prefixed_once_and_keeps_urgency_empty(
    client, provider, session_dir
):
    session_id = await start_session(client)
    extraction = present_extraction(candidates={"order_number": "NS-000123"})

    response, _ = await send_turn(
        client, provider, session_id,
        "I have asked three times. This is really frustrating. Order NS-000123.", 0,
        extraction,
    )
    payload = assert_turn(response, active=True, prefixed=True)
    assert payload["reply"] == f"{EN_PREFIX}\n\n{MISSING_CATEGORY}"
    assert payload["slots"] == {
        "order_number": "NS-000123",
        "category": None,
        "description": None,
        "urgency": None,
    }
    disk = read_session(session_dir, session_id)
    assert disk["slots"]["urgency"] is None
    assert disk["messages"][-2]["frustration"] == "present"


# FR04 ----------------------------------------------------------------------- #


async def test_fr04_consecutive_present_turns_only_prefix_the_first(client, provider, session_dir):
    session_id = await start_session(client)

    first, _ = await send_turn(
        client, provider, session_id, "I am frustrated.", 0,
        present_extraction(candidates={"order_number": "NS-000123"}),
    )
    second, _ = await send_turn(
        client, provider, session_id, "Still frustrated. It was damaged.", 1,
        present_extraction(candidates={"category": "damaged_item"}),
    )

    assert_turn(first, active=True, prefixed=True)
    assert_turn(second, active=True, prefixed=False)
    disk = read_session(session_dir, session_id)
    assert disk["frustration_active"] is True
    assert sum(
        message["text"].startswith(EN_PREFIX)
        for message in disk["messages"]
        if message["role"] == "assistant"
    ) == 1


# FR05 ----------------------------------------------------------------------- #


async def test_fr05_present_none_present_starts_two_episodes(client, provider):
    session_id = await start_session(client)

    first, _ = await send_turn(
        client, provider, session_id, "I am frustrated.", 0,
        present_extraction(candidates={"order_number": "NS-000123"}),
    )
    second, _ = await send_turn(
        client, provider, session_id, "Thanks, that helps.", 1,
        social_extraction(sentiment="positive"),
    )
    third, _ = await send_turn(
        client, provider, session_id, "Still no update. Frustrating.", 2,
        present_extraction(candidates={"description": "Still no update after three days."}),
    )

    assert_turn(first, active=True, prefixed=True)
    assert_turn(second, active=False, prefixed=False)
    assert_turn(third, active=True, prefixed=True)


# FR06 ----------------------------------------------------------------------- #


async def test_fr06_present_uncertain_present_keeps_the_episode_without_a_new_prefix(
    client, provider
):
    session_id = await start_session(client)

    first, _ = await send_turn(
        client, provider, session_id, "I am frustrated.", 0,
        present_extraction(candidates={"order_number": "NS-000123"}),
    )
    second, _ = await send_turn(
        client, provider, session_id, "Hmm, not sure.", 1,
        social_extraction(sentiment="uncertain", frustration="uncertain"),
    )
    third, _ = await send_turn(
        client, provider, session_id, "It was damaged.", 2,
        present_extraction(candidates={"category": "damaged_item"}),
    )

    assert_turn(first, active=True, prefixed=True)
    assert_turn(second, active=True, prefixed=False)
    assert_turn(third, active=True, prefixed=False)


# FR07 ----------------------------------------------------------------------- #


async def test_fr07_negative_sentiment_with_none_gets_no_prefix_or_urgency(client, provider):
    session_id = await start_session(client)
    extraction = make_extraction(
        candidates={
            "order_number": "NS-000123",
            "category": "damaged_item",
            "description": "My item is broken.",
        },
        sentiment="negative",
        frustration="none",
    )

    response, _ = await send_turn(
        client, provider, session_id, "My item is broken.", 0, extraction
    )
    payload = assert_turn(response, active=False, prefixed=False)
    assert payload["slots"]["urgency"] is None


# FR08 ----------------------------------------------------------------------- #


async def test_fr08_uncertain_labels_are_valid_and_do_not_start_an_episode(client, provider):
    session_id = await start_session(client)

    response, _ = await send_turn(
        client, provider, session_id, "Great. Just great.", 0,
        social_extraction(sentiment="uncertain", frustration="uncertain"),
    )
    assert_turn(response, active=False, prefixed=False)
    view = await client.get(f"/api/sessions/{session_id}")
    assert view.json()["messages"][-2]["sentiment"] == "uncertain"
    assert view.json()["messages"][-2]["frustration"] == "uncertain"


# FR09 ----------------------------------------------------------------------- #


async def test_fr09_failed_extraction_is_safe_and_saves_nothing(settings, session_dir):
    provider = FailingExtractionProvider()
    app = create_app(settings=settings, provider=provider)
    async with running_client(app) as client:
        session_id = await start_session(client)
        path = session_dir / f"{session_id}.json"
        original = path.read_bytes()

        response = await client.post(
            f"/api/sessions/{session_id}/turns",
            json=turn_body("I am frustrated.", 0),
        )

        assert response.status_code == 502, response.text
        assert response.json()["error"]["code"] == "invalid_provider_response"
        assert path.read_bytes() == original
        disk = read_session(session_dir, session_id)
        assert disk["revision"] == 0
        assert disk["frustration_active"] is False
        assert len(disk["messages"]) == 1
        assert disk["requests"] == []


# FR10 ----------------------------------------------------------------------- #


async def test_fr10_quotation_and_negation_labels_pass_through(client, provider):
    session_id = await start_session(client)

    quoted, _ = await send_turn(
        client, provider, session_id, "The example customer says: I am furious.", 0,
        social_extraction(sentiment="neutral", frustration="none"),
    )
    negated, _ = await send_turn(
        client, provider, session_id, "I am not frustrated; I only need the policy.", 1,
        social_extraction(sentiment="neutral", frustration="none"),
    )

    assert_turn(quoted, active=False, prefixed=False)
    view = await client.get(f"/api/sessions/{session_id}")
    assert view.json()["messages"][-2]["frustration"] == "none"
    assert_turn(negated, active=False, prefixed=False)


# FR11 ----------------------------------------------------------------------- #


async def test_fr11_frustrated_faq_gets_prefix_and_cited_answer_without_intake(
    client, provider, session_dir
):
    session_id = await start_session(client)
    provider.script_extraction(
        make_extraction(
            intent="question",
            has_question=True,
            sentiment="negative",
            frustration="present",
        )
    )
    provider.script_answer(
        KnowledgeAnswer(answerable=True, answer=DELIVERY_ANSWER, cited_ids=["faq-delivery"])
    )

    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body(DELIVERY_QUESTION, 0),
    )

    payload = assert_turn(response, active=True, prefixed=True)
    assert payload["reply"] == f"{EN_PREFIX}\n\n{DELIVERY_ANSWER}"
    assert [citation["source_id"] for citation in payload["citations"]] == ["faq-delivery"]
    assert payload["slots"] == {
        "order_number": None,
        "category": None,
        "description": None,
        "urgency": None,
    }
    assert payload["pending_clarifications"] == {}
    assert payload["pending_review_revision"] is None
    assert "order number" not in payload["reply"].lower()
    disk = read_session(session_dir, session_id)
    assert disk["messages"][-1]["citations"] == payload["citations"]
    assert disk["messages"][-2]["frustration"] == "present"


# FR12 ----------------------------------------------------------------------- #


async def test_fr12_spanish_episode_start_uses_the_exact_spanish_prefix(client, provider):
    session_id = await start_session(client)
    extraction = make_extraction(
        detected_language="es",
        candidates={"order_number": "NS-000123"},
        sentiment="negative",
        frustration="present",
    )

    response, _ = await send_turn(
        client, provider, session_id,
        "Ya he preguntado tres veces y estoy muy frustrado.", 0, extraction,
    )
    payload = assert_turn(response, active=True, prefixed=True, prefix=ES_PREFIX)
    assert payload["reply_language"] == "es"
    assert payload["reply"] == f"{ES_PREFIX}\n\n{SPANISH_MISSING_CATEGORY}"
    assert EN_PREFIX not in payload["reply"]


# FR13 ----------------------------------------------------------------------- #


async def test_fr13_social_turn_while_active_uses_normal_routing_and_none_resets(
    client, provider
):
    session_id = await start_session(client)
    first, _ = await send_turn(
        client, provider, session_id, "I am frustrated.", 0, present_extraction()
    )
    assert_turn(first, active=True, prefixed=True)

    second, _ = await send_turn(
        client, provider, session_id, "Thanks.", 1,
        social_extraction(sentiment="positive", frustration="none"),
    )
    payload = assert_turn(second, active=False, prefixed=False)
    assert payload["reply"] == "Hello! How can I help with Northstar Shop?"
    assert "order number" not in payload["reply"].lower()


# FR14 ----------------------------------------------------------------------- #


async def test_fr14_failed_extraction_leaves_the_document_byte_identical(settings, session_dir):
    provider = FailingExtractionProvider()
    app = create_app(settings=settings, provider=provider)
    async with running_client(app) as client:
        session_id = await start_session(client)
        path = session_dir / f"{session_id}.json"
        original = path.read_bytes()
        before = read_session(session_dir, session_id)

        response = await client.post(
            f"/api/sessions/{session_id}/turns",
            json=turn_body("I am frustrated.", 0),
        )

        assert response.status_code == 502, response.text
        assert path.read_bytes() == original
        assert read_session(session_dir, session_id) == before


# FR15 ----------------------------------------------------------------------- #


async def test_fr15_failed_rag_does_not_publish_the_candidate_episode(settings, session_dir):
    provider = FailingAnswerProvider()
    provider.script_extraction(
        make_extraction(
            intent="question",
            has_question=True,
            sentiment="negative",
            frustration="present",
        )
    )
    app = create_app(settings=settings, provider=provider)
    async with running_client(app) as client:
        session_id = await start_session(client)
        path = session_dir / f"{session_id}.json"
        original = path.read_bytes()

        response = await client.post(
            f"/api/sessions/{session_id}/turns",
            json=turn_body(DELIVERY_QUESTION, 0),
        )

        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "provider_unavailable"
        assert path.read_bytes() == original
        disk = read_session(session_dir, session_id)
        assert disk["frustration_active"] is False
        assert disk["revision"] == 0
        assert len(disk["messages"]) == 1
        assert disk["requests"] == []


# FR16 ----------------------------------------------------------------------- #


async def test_fr16_replace_failure_rolls_back_and_the_retry_commits_once(
    settings, session_dir
):
    provider = ScriptedProvider()
    app = create_app(settings=settings, provider=provider)
    async with running_client(app) as client:
        session_id = await start_session(client)
        target = session_dir / f"{session_id}.json"
        original = target.read_bytes()
        request_id = str(uuid4())
        body = turn_body("I am frustrated.", 0, request_id)
        provider.script_extraction(present_extraction(candidates={"order_number": "NS-000123"}))

        real_replace = os.replace

        def scoped_replace(src, dst, *args, **kwargs):
            if Path(dst) == target:
                raise OSError("scoped os.replace failure")
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch("app.storage.os.replace", side_effect=scoped_replace):
            failed = await client.post(f"/api/sessions/{session_id}/turns", json=body)

        assert failed.status_code == 503, failed.text
        assert failed.json()["error"]["code"] == "storage_unavailable"
        assert target.read_bytes() == original
        assert sorted(session_dir.iterdir()) == [target]

        provider.script_extraction(present_extraction(candidates={"order_number": "NS-000123"}))
        retry = await client.post(f"/api/sessions/{session_id}/turns", json=body)

        payload = assert_turn(retry, active=True, prefixed=True)
        disk = read_session(session_dir, session_id)
        assert disk["revision"] == 1
        assert len(disk["messages"]) == 3
        assert len(disk["requests"]) == 1
        assert disk["requests"][0]["request_id"] == request_id
        assert disk["messages"][-1]["text"] == payload["reply"]
        assert payload["reply"].count(EN_PREFIX) == 1


# FR17 ----------------------------------------------------------------------- #


async def test_fr17_lost_response_then_exact_replay_returns_the_same_committed_turn(
    client, provider, session_dir
):
    session_id = await start_session(client)
    request_id = str(uuid4())
    response, body = await send_turn(
        client, provider, session_id, "I am frustrated.", 0,
        present_extraction(candidates={"order_number": "NS-000123"}), request_id,
    )
    first = assert_turn(response, active=True, prefixed=True)
    calls_after_first = list(provider.calls)

    replay = await client.post(f"/api/sessions/{session_id}/turns", json=body)
    second = assert_turn(replay, active=True, prefixed=True)

    assert second == first
    assert provider.calls == calls_after_first
    disk = read_session(session_dir, session_id)
    assert disk["revision"] == 1
    assert len(disk["messages"]) == 3
    assert len(disk["requests"]) == 1
    assert disk["messages"][-1]["text"].count(EN_PREFIX) == 1


# FR18 ----------------------------------------------------------------------- #


async def test_fr18_restart_while_active_suppresses_the_next_acknowledgment(
    settings, provider, session_dir
):
    app = create_app(settings=settings, provider=provider)
    async with running_client(app) as client:
        session_id = await start_session(client)
        first, _ = await send_turn(
            client, provider, session_id, "I am frustrated.", 0,
            present_extraction(candidates={"order_number": "NS-000123"}),
        )
        assert_turn(first, active=True, prefixed=True)

    restarted_provider = ScriptedProvider()
    restarted = create_app(settings=settings, provider=restarted_provider)
    async with running_client(restarted) as client:
        restarted_provider.script_extraction(
            present_extraction(candidates={"category": "damaged_item"})
        )
        response = await client.post(
            f"/api/sessions/{session_id}/turns",
            json=turn_body("Still frustrated. It was damaged.", 1),
        )

    payload = assert_turn(response, active=True, prefixed=False)
    assert payload["revision"] == 2
    disk = read_session(session_dir, session_id)
    assert disk["frustration_active"] is True
    assert sum(
        message["text"].startswith(EN_PREFIX)
        for message in disk["messages"]
        if message["role"] == "assistant"
    ) == 1


# FR19 ----------------------------------------------------------------------- #


async def test_fr19_separate_sessions_keep_independent_episodes(client, provider):
    first_id = await start_session(client)
    second_id = await start_session(client)

    first, _ = await send_turn(
        client, provider, first_id, "I am frustrated.", 0,
        present_extraction(candidates={"order_number": "NS-000123"}),
    )
    second, _ = await send_turn(
        client, provider, second_id, "I am frustrated too.", 0,
        present_extraction(candidates={"order_number": "NS-000456"}),
    )
    assert_turn(first, active=True, prefixed=True)
    assert_turn(second, active=True, prefixed=True)

    reset, _ = await send_turn(
        client, provider, second_id, "Thanks, resolved.", 1,
        social_extraction(sentiment="positive", frustration="none"),
    )
    assert_turn(reset, active=False, prefixed=False)

    still_active, _ = await send_turn(
        client, provider, first_id, "Still frustrated.", 1,
        present_extraction(candidates={"category": "damaged_item"}),
    )
    assert_turn(still_active, active=True, prefixed=False)
    assert [call[1].frustration_active for call in provider.calls] == [
        False,
        False,
        True,
        True,
    ]


# FR20 ----------------------------------------------------------------------- #


async def test_fr20_confirm_get_stt_and_tts_do_not_classify_or_transition(
    client, provider, session_dir
):
    from tests.fixtures.audio import gen_wav

    session_id = await start_session(client)
    turn, _ = await send_turn(
        client, provider, session_id, "I am frustrated.", 0, present_extraction()
    )
    payload = assert_turn(turn, active=True, prefixed=True)
    calls_before = list(provider.calls)

    view = await client.get(f"/api/sessions/{session_id}")
    assert view.status_code == 200
    assert view.json()["frustration_active"] is True
    assert provider.calls == calls_before

    provider.script_transcription("I am still frustrated.")
    transcription = await client.post(
        "/api/transcriptions",
        files={"file": ("recording.wav", gen_wav.pcm_wav(), "audio/wav")},
    )
    assert transcription.status_code == 200, transcription.text
    assert transcription.json() == {"text": "I am still frustrated."}
    assert provider.methods()[-1] == "transcribe"

    provider.script_speech(b"mp3-bytes", media_type="audio/mpeg")
    speech = await client.post(
        f"/api/sessions/{session_id}/speech",
        json={"message_id": payload["message_id"]},
    )
    assert speech.status_code == 200, speech.text
    assert provider.methods()[-1] == "synthesize"

    disk = read_session(session_dir, session_id)
    assert disk["frustration_active"] is True
    assert disk["revision"] == 1
    assert len(disk["messages"]) == 3
    assert [message["input_mode"] for message in disk["messages"]] == [
        "system",
        "text",
        "system",
    ]


# FR21 ----------------------------------------------------------------------- #


async def test_fr21_unsupported_turn_while_active_keeps_the_episode_and_localizes(
    client, provider, session_dir
):
    session_id = await start_session(client)
    first, _ = await send_turn(
        client, provider, session_id, "I am frustrated.", 0,
        present_extraction(candidates={"order_number": "NS-000123"}),
    )
    assert_turn(first, active=True, prefixed=True)

    unsupported, _ = await send_turn(
        client, provider, session_id, "Je suis très frustré.", 1,
        make_extraction(
            detected_language="unsupported",
            sentiment="negative",
            frustration="present",
        ),
    )
    payload = assert_turn(unsupported, active=True, prefixed=False)
    assert payload["reply"] == UNSUPPORTED_NOTICE
    disk = read_session(session_dir, session_id)
    assert disk["frustration_active"] is True
    assert disk["messages"][-2]["sentiment"] == "uncertain"
    assert disk["messages"][-2]["frustration"] == "uncertain"


# FR22 ----------------------------------------------------------------------- #


async def test_fr22_natural_language_confirmation_with_present_prefixes_the_receipt_once(
    client, provider, session_dir
):
    session_id = await start_session(client)
    review, _ = await send_turn(
        client, provider, session_id, "My order NS-000123 arrived broken.", 0,
        FULL_EXTRACTION,
    )
    assert review.status_code == 200, review.text
    assert review.json()["status"] == "awaiting_confirmation"

    request_id = str(uuid4())
    provider.script_extraction(
        make_extraction(intent="confirm", sentiment="negative", frustration="present")
    )
    body = turn_body("Yes, confirm it. I am still frustrated.", 1, request_id)
    response = await client.post(f"/api/sessions/{session_id}/turns", json=body)

    payload = assert_turn(response, active=True, prefixed=True)
    assert payload["status"] == "completed"
    receipt = (
        f"Saved locally as {session_id}. "
        "No request has been sent to a real support team."
    )
    assert payload["reply"] == f"{EN_PREFIX}\n\n{receipt}"

    replay = await client.post(f"/api/sessions/{session_id}/turns", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == payload
    disk = read_session(session_dir, session_id)
    assert disk["status"] == "completed"
    assert disk["frustration_active"] is True
    assert len(disk["requests"]) == 2
    assert disk["requests"][-1]["response"]["reply"].count(EN_PREFIX) == 1
