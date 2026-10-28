"""Durable replay and cancellation guarantees missed by the historical mutation run."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gateway.conversation.flows import FlowsInvalid, load_flows
from gateway.localization import text
from gateway.schemas import (
    BookingRequest,
    ConfirmRequest,
    KnowledgeAnswer,
    Session,
    TurnRequest,
)
from tests.support.unit_fakes import make_extraction, make_record
from tests.unit.gateway.conversation.conversation_support import (
    build_engine,
    turn_request,
)

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "replay_v2_survivors.json"


@pytest.mark.parametrize(
    "case", json.loads(FIXTURE.read_text("utf-8")), ids=lambda case: case["operation"]
)
async def test_a_saved_v2_request_replays_after_restart_without_repeating_work(
    tmp_path, case
):
    engine, repository, provider = build_engine(tmp_path)
    session = Session.model_validate(case["session"])
    await repository.save(session)
    before = (tmp_path / f"{session.session_id}.json").read_bytes()
    request_type = {
        "turn": TurnRequest,
        "confirm": ConfirmRequest,
        "book_slot": BookingRequest,
    }[case["operation"]]

    response = await getattr(engine, case["operation"])(
        session.session_id, request_type.model_validate(case["command"])
    )

    assert response.model_dump(mode="json") == case["response"]
    assert (tmp_path / f"{session.session_id}.json").read_bytes() == before
    provider.assert_exhausted()


async def test_cancelling_a_turn_while_safety_waits_cancels_its_extraction(tmp_path):
    engine, _, _ = build_engine(tmp_path)
    created = await engine.create_session()
    started, cancelled = asyncio.Event(), asyncio.Event()
    pending = []

    class WaitingProvider:
        async def extract(self, context):
            pending.append(asyncio.current_task())
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    class WaitingSafety:
        async def concerning(self, message):
            await asyncio.Event().wait()

    engine.provider = WaitingProvider()
    engine.safety_index = WaitingSafety()
    turn = asyncio.create_task(
        engine.turn(created.session_id, turn_request("A question"))
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await asyncio.wait_for(cancelled.wait(), 2)
    finally:
        turn.cancel()
        for task in pending:
            task.cancel()
        await asyncio.gather(turn, *pending, return_exceptions=True)


@pytest.mark.parametrize("retrieved", [False, True])
async def test_spanish_abstention_keeps_the_customers_language(tmp_path, retrieved):
    question = "¿Qué formas de pago aceptan?"
    script = [
        (
            "extract",
            make_extraction(
                intent="question", has_question=True, detected_language="es"
            ),
        )
    ]
    if retrieved:
        script.append(
            (
                "answer_knowledge",
                KnowledgeAnswer(answerable=False, answer="", cited_ids=[]),
            )
        )
    engine, _, provider = build_engine(
        tmp_path,
        script=script,
        kb_answers={question: [make_record()] if retrieved else []},
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request(question))

    assert response.reply_language == "es"
    assert response.reply == text("abstention", "es")
    provider.assert_exhausted()


@pytest.mark.parametrize("duplicate", [False, True])
def test_invalid_flow_configuration_reports_the_actual_startup_problem(
    tmp_path, duplicate
):
    account = {
        "intent": "book_technician",
        "steps": ["ask_account", "verify_account", "offer_windows"],
    }
    config = (
        {
            "support_request": {
                "intent": "start_intake",
                "steps": ["collect_details", "review_details"],
            },
            "first": account,
            "second": account,
        }
        if duplicate
        else {}
    )
    path = tmp_path / "flows.json"
    path.write_text(json.dumps(config), "utf-8")

    with pytest.raises(FlowsInvalid) as caught:
        load_flows(path)

    assert str(caught.value) == (
        "two flows start on the same intent"
        if duplicate
        else "the flows file must be a non-empty object of flows"
    )
