"""English/Spanish conversation support over the real HTTP app.

Every case runs the real app factory, lifespan, routes, engine and JSON
repository; only the remote AI service is scripted (IC §13.1). Language is
carried by the strict wire schema, so these checks assert stored state, provider
context and citation identity, not just rendered text.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient

from gateway.localization import request_reference
from gateway.config import Settings
from gateway.localization import build_review_reply, build_summary, text
from gateway.main import create_app
from gateway.schemas import Slots
from tests.integration.support.api import (
    ScriptedProvider,
    build_settings,
    make_extraction,
    read_session,
    running_client,
)
from tests.integration.support.intake import (
    FULL_SLOTS,
    FULL_SUMMARY,
    start_session,
    turn_body,
)


SPANISH_TEXT = "Mi pedido NS-000123 llegó dañado, urgencia alta"
SPANISH_SLOTS = {
    "order_number": "NS-000123",
    "category": "damaged_item",
    "description": "Mi pedido llegó dañado",
    "urgency": "high",
}
SPANISH_SUMMARY = build_summary(Slots(**SPANISH_SLOTS), "es")
SPANISH_EXTRACTION = make_extraction(
    detected_language="es",
    candidates={
        "order_number": "NS-000123",
        "category": "artículo dañado",
        "description": "Mi pedido llegó dañado",
        "urgency": "alta",
    },
)


def wav_bytes() -> bytes:
    from tests.fixtures.audio import gen_wav

    return gen_wav.pcm_wav()


def spanish_summary_of_english_facts() -> str:
    from gateway.localization import build_summary as build
    from gateway.schemas import Slots

    return build(Slots(**FULL_SLOTS), "es")


def english_review() -> str:
    from gateway.localization import build_review_reply as build
    from gateway.schemas import Slots

    return build(Slots(**FULL_SLOTS), "en")


def spanish_review(slots: dict | None = None) -> str:
    from gateway.localization import build_review_reply as build
    from gateway.schemas import Slots

    return build(Slots(**(slots or SPANISH_SLOTS)), "es")


async def post_turn(client, session_id, *, text_value, revision, input_mode="text"):
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json={
            "request_id": str(uuid4()),
            "expected_revision": revision,
            "text": text_value,
            "input_mode": input_mode,
        },
    )
    return response


async def collect_full_english_intake(client, provider):
    session_id = await start_session(client)
    provider.script_extraction(
        make_extraction(
            candidates={
                "order_number": "NS-000123",
                "category": "damaged_item",
                "description": "My order arrived broken",
                "urgency": "high",
            }
        )
    )
    response = await post_turn(
        client, session_id, text_value="NS-000123 arrived broken and urgent", revision=0
    )
    assert response.status_code == 200, response.text
    return session_id, response.json()


async def test_new_session_starts_in_english_with_a_playable_greeting(client):
    created = await client.post("/api/sessions", json={})

    assert created.status_code == 201
    body = created.json()
    assert body["reply_language"] == "en"
    assert body["messages"][0]["language"] == "en"
    assert body["messages"][0]["speech_available"] is True
    assert body["messages"][0]["text"] == text("greeting", "en")


async def test_spanish_turn_localizes_the_reply_and_keeps_canonical_values(
    client, provider, session_dir
):
    session_id = await start_session(client)
    provider.script_extraction(SPANISH_EXTRACTION)

    response = await post_turn(client, session_id, text_value=SPANISH_TEXT, revision=0)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply_language"] == "es"
    assert body["slots"] == SPANISH_SLOTS
    assert body["summary"] == SPANISH_SUMMARY
    assert body["reply"] == spanish_review()
    assert body["speech_available"] is False

    stored = read_session(session_dir, session_id)
    assert stored["reply_language"] == "es"
    assert stored["slots"] == SPANISH_SLOTS
    assert stored["messages"][-2]["language"] == "es"
    assert stored["messages"][-1]["language"] == "es"
    assert stored["messages"][-1]["text"] == body["reply"]
    assert "speech_available" not in stored["messages"][-1]

    fetched = await client.get(f"/api/sessions/{session_id}")
    assert fetched.json()["reply_language"] == "es"
    assert fetched.json()["messages"][-1]["speech_available"] is False


async def test_explicit_switch_both_ways_redisplays_the_same_review(client, provider):
    session_id, review = await collect_full_english_intake(client, provider)
    assert review["reply_language"] == "en"
    assert review["reply"] == english_review()

    provider.script_extraction(
        make_extraction(intent="unrelated", detected_language="en", requested_language="es")
    )
    spanish = await post_turn(
        client, session_id, text_value="Please continue in Spanish", revision=1
    )
    assert spanish.status_code == 200, spanish.text
    spanish_body = spanish.json()
    assert spanish_body["reply_language"] == "es"
    assert spanish_body["reply"] == spanish_review(FULL_SLOTS)
    assert spanish_body["status"] == "awaiting_confirmation"
    assert spanish_body["pending_review_revision"] == review["pending_review_revision"]
    assert spanish_body["summary"] == spanish_summary_of_english_facts()
    assert spanish_body["slots"] == FULL_SLOTS

    provider.script_extraction(
        make_extraction(intent="unrelated", detected_language="es", requested_language="en")
    )
    english = await post_turn(
        client, session_id, text_value="Continuemos en inglés", revision=2
    )
    assert english.status_code == 200, english.text
    english_body = english.json()
    assert english_body["reply_language"] == "en"
    assert english_body["reply"] == english_review()
    assert english_body["summary"] == FULL_SUMMARY
    assert english_body["slots"] == FULL_SLOTS


async def test_language_switch_does_not_confirm_the_review(client, provider, session_dir):
    session_id, review = await collect_full_english_intake(client, provider)
    provider.script_extraction(
        make_extraction(intent="unrelated", requested_language="es", detected_language="en")
    )
    await post_turn(client, session_id, text_value="En español", revision=1)

    stored = read_session(session_dir, session_id)
    assert stored["status"] == "awaiting_confirmation"
    assert stored["confirmation"] is None
    assert stored["pending_review_revision"] == review["pending_review_revision"]

    confirmed = await client.post(
        f"/api/sessions/{session_id}/confirm",
        json={
            "request_id": str(uuid4()),
            "expected_revision": 2,
            "review_revision": review["pending_review_revision"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["reply"] == text("receipt", "es").format(reference=request_reference(session_id))
    assert confirmed.json()["reply_language"] == "es"
    stored_after = read_session(session_dir, session_id)
    assert stored_after["messages"][-2]["text"] == text("review.confirmed", "es")
    assert stored_after["messages"][-1]["text"] == text("receipt", "es").format(
        reference=request_reference(session_id)
    )


async def test_unsupported_language_returns_a_localized_notice_and_no_facts(
    client, provider, session_dir
):
    session_id = await start_session(client)
    provider.script_extraction(
        make_extraction(
            detected_language="unsupported",
            candidates={
                "order_number": "NS-999999",
                "category": "damaged_item",
                "description": "Bonjour mon colis est cassé",
                "urgency": "high",
            },
        )
    )

    response = await post_turn(
        client, session_id, text_value="Bonjour mon colis est cassé", revision=0
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"] == text("unsupported_language", "en")
    assert body["reply_language"] == "en"
    assert body["slots"] == {
        "order_number": None,
        "category": None,
        "description": None,
        "urgency": None,
    }
    assert body["status"] == "collecting"
    stored = read_session(session_dir, session_id)
    assert stored["slots"]["order_number"] is None


async def test_unsupported_input_with_an_explicit_switch_applies_only_the_switch(
    client, provider, session_dir
):
    session_id = await start_session(client)
    provider.script_extraction(
        make_extraction(
            detected_language="unsupported",
            requested_language="es",
            candidates={
                "order_number": "NS-999999",
                "description": "Bonjour mon colis est cassé",
            },
        )
    )

    response = await post_turn(
        client, session_id, text_value="Switch to Spanish: bonjour", revision=0
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply_language"] == "es"
    assert body["reply"] == text("language_switch_restate", "es")
    assert body["slots"]["order_number"] is None
    assert read_session(session_dir, session_id)["reply_language"] == "es"


async def test_mixed_language_correction_keeps_canonical_values(client, provider):
    session_id = await start_session(client)
    provider.script_extraction(
        make_extraction(
            candidates={
                "order_number": "NS-000123",
                "category": "damaged_item",
                "description": "My order arrived broken",
                "urgency": "low",
            }
        )
    )
    first = await post_turn(
        client, session_id, text_value="NS-000123 arrived broken, low urgency", revision=0
    )
    assert first.status_code == 200, first.text

    provider.script_extraction(
        make_extraction(
            intent="correction",
            detected_language="es",
            candidates={"category": "artículo incorrecto", "urgency": "alta"},
            explicit_corrections=["category", "urgency"],
        )
    )
    corrected = await post_turn(
        client,
        session_id,
        text_value="Perdón, era el artículo incorrecto y es urgente",
        revision=1,
    )

    assert corrected.status_code == 200, corrected.text
    body = corrected.json()
    assert body["reply_language"] == "es"
    assert body["slots"]["order_number"] == "NS-000123"
    assert body["slots"]["category"] == "wrong_item"
    assert body["slots"]["urgency"] == "high"
    assert body["slots"]["description"] == "My order arrived broken"


async def test_numeric_only_reply_retains_the_spanish_reply_language(client, provider):
    session_id = await start_session(client)
    provider.script_extraction(
        make_extraction(
            detected_language="es",
            candidates={"description": "Mi pedido llegó dañado"},
        )
    )
    await post_turn(client, session_id, text_value="Mi pedido llegó dañado", revision=0)

    provider.script_extraction(
        make_extraction(detected_language="es", candidates={"order_number": "000042"})
    )
    response = await post_turn(client, session_id, text_value="000042", revision=1)

    body = response.json()
    assert body["reply_language"] == "es"
    assert body["slots"]["order_number"] == "NS-000042"


async def test_spanish_faq_answer_keeps_english_source_citations(client, provider):
    session_id = await start_session(client)
    provider.script_extraction(
        make_extraction(intent="question", has_question=True, detected_language="es")
    )
    from gateway.schemas import KnowledgeAnswer

    provider.script_answer(
        KnowledgeAnswer(
            answerable=True,
            answer="La entrega estándar tarda de 2 a 5 días laborables.",
            cited_ids=["faq-delivery"],
        )
    )

    response = await post_turn(
        client, session_id, text_value="¿Cuánto tarda la entrega?", revision=0
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply_language"] == "es"
    assert body["reply"] == "La entrega estándar tarda de 2 a 5 días laborables."
    assert [citation["source_id"] for citation in body["citations"]] == ["faq-delivery"]
    citation = body["citations"][0]
    assert citation["title"] == "Delivery times"
    assert citation["kb_version"] == "2026-10-25"
    assert "Standard delivery takes 2 to 5 business days" in citation["excerpt"]

    method, context = provider.calls[-1]
    assert method == "answer_knowledge"
    assert context.reply_language == "es"


async def test_spanish_abstention_is_localized_without_citations(client, provider):
    session_id = await start_session(client)
    provider.script_extraction(
        make_extraction(intent="question", has_question=True, detected_language="es")
    )

    response = await post_turn(
        client, session_id, text_value="¿Puedo pagar con tarjeta?", revision=0
    )

    body = response.json()
    assert body["reply"] == text("abstention", "es")
    assert body["citations"] == []


async def test_spanish_transcription_enters_the_same_language_path(
    client, provider, session_dir
):
    session_id = await start_session(client)
    provider.script_transcription("Mi pedido NS-000123 llegó dañado, urgencia alta")

    uploaded = await client.post(
        "/api/transcriptions", files={"file": ("speech.wav", wav_bytes(), "audio/wav")}
    )
    assert uploaded.status_code == 200, uploaded.text
    transcript = uploaded.json()["text"]

    provider.script_extraction(SPANISH_EXTRACTION)
    response = await post_turn(
        client, session_id, text_value=transcript, revision=0, input_mode="transcript"
    )

    assert response.status_code == 200, response.text
    assert response.json()["reply_language"] == "es"
    stored = read_session(session_dir, session_id)
    assert stored["messages"][-2]["input_mode"] == "transcript"
    assert stored["messages"][-2]["language"] == "es"


async def test_speech_uses_the_saved_message_language(client, provider):
    session_id = await start_session(client)
    greeting = (await client.get(f"/api/sessions/{session_id}")).json()["messages"][0]

    provider.script_speech(b"\xff\xfbmp3", "audio/mpeg")
    response = await client.post(
        f"/api/sessions/{session_id}/speech", json={"message_id": greeting["id"]}
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "audio/mpeg"
    assert provider.speech_languages == ["en"]


async def test_spanish_speech_without_a_spanish_profile_fails_clearly(
    client, provider, session_dir
):
    session_id = await start_session(client)
    provider.script_extraction(SPANISH_EXTRACTION)
    turn = await post_turn(client, session_id, text_value=SPANISH_TEXT, revision=0)
    reply = turn.json()
    assert reply["speech_available"] is False

    response = await client.post(
        f"/api/sessions/{session_id}/speech", json={"message_id": reply["message_id"]}
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "provider_configuration"
    assert "synthesize" not in provider.methods()


async def test_spanish_speech_uses_the_spanish_profile_when_configured(session_dir):
    provider = ScriptedProvider()
    settings = build_settings(session_dir, tts_es=True)
    app = create_app(settings=settings, provider=provider)
    async with running_client(app) as client:
        session_id = await start_session(client)
        provider.script_extraction(SPANISH_EXTRACTION)
        turn = await post_turn(client, session_id, text_value=SPANISH_TEXT, revision=0)
        reply = turn.json()
        assert reply["speech_available"] is True

        provider.script_speech(b"RIFFwav", "audio/wav")
        response = await client.post(
            f"/api/sessions/{session_id}/speech", json={"message_id": reply["message_id"]}
        )

        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "audio/wav"
        assert provider.speech_languages == ["es"]


async def test_old_english_messages_remain_english_after_a_switch(client, provider):
    session_id, review = await collect_full_english_intake(client, provider)
    provider.script_extraction(
        make_extraction(intent="unrelated", requested_language="es", detected_language="en")
    )
    await post_turn(client, session_id, text_value="En español", revision=1)

    view = (await client.get(f"/api/sessions/{session_id}")).json()
    assistant = [message for message in view["messages"] if message["role"] == "assistant"]
    assert assistant[0]["language"] == "en"
    assert assistant[0]["text"] == text("greeting", "en")
    assert assistant[-2]["language"] == "en"
    assert assistant[-2]["text"] == english_review()
    assert assistant[-1]["language"] == "es"
    assert assistant[-1]["text"] == spanish_review(FULL_SLOTS)
    assert [message["speech_available"] for message in assistant] == [True, True, False]


async def test_replay_returns_the_stored_response_and_language(client, provider):
    session_id = await start_session(client)
    provider.script_extraction(
        make_extraction(detected_language="en", candidates={"order_number": "NS-000123"})
    )
    request = {
        "request_id": str(uuid4()),
        "expected_revision": 0,
        "text": "NS-000123",
        "input_mode": "text",
    }
    first = await client.post(f"/api/sessions/{session_id}/turns", json=request)
    assert first.status_code == 200, first.text

    provider.script_extraction(
        make_extraction(intent="unrelated", requested_language="es", detected_language="en")
    )
    await post_turn(client, session_id, text_value="En español", revision=1)

    replay = await client.post(f"/api/sessions/{session_id}/turns", json=request)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert replay.json()["reply_language"] == "en"


async def test_resume_after_restart_restores_language_and_transcript(session_dir):
    first_provider = ScriptedProvider()
    app = create_app(settings=build_settings(session_dir), provider=first_provider)
    async with running_client(app) as client:
        session_id = await start_session(client)
        first_provider.script_extraction(SPANISH_EXTRACTION)
        await post_turn(client, session_id, text_value=SPANISH_TEXT, revision=0)

    second_provider = ScriptedProvider()
    app = create_app(settings=build_settings(session_dir), provider=second_provider)
    async with running_client(app) as client:
        view = (await client.get(f"/api/sessions/{session_id}")).json()

    assert view["reply_language"] == "es"
    assert len(view["messages"]) == 3
    assert view["messages"][-1]["language"] == "es"
    assert view["messages"][-1]["text"] == spanish_review()
    assert view["messages"][-1]["speech_available"] is False


async def test_error_codes_and_messages_stay_canonical(client):
    session_id = await start_session(client)

    response = await post_turn(client, session_id, text_value="Hello", revision=7)

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "revision_conflict"
    assert body["error"]["message"] == "This session changed. Refresh before sending again."
    assert body["error"]["current_revision"] == 0
