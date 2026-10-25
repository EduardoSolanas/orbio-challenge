"""A report of a gas leak gets the safety answer first, whatever the model made of it.

Real app, route, engine, retriever and repository; only the AI service is scripted.
"""

from __future__ import annotations

from gateway.errors import app_error
from gateway.main import create_app
from tests.integration.support.api import (
    ScriptedProvider,
    make_extraction,
    read_session,
    running_client,
)
from tests.integration.support.confirmation import send_turn
from tests.integration.support.intake import start_session
from tests.integration.support.rag import SEED_RECORDS, grounded, seed_citation

ADVICE = "Do not use switches or flames, open the windows and call 0800 111 999."
EMPTY = {"order_number": None, "category": None, "description": None, "urgency": None}


class ModelDown(ScriptedProvider):
    """Every model call fails, as in an outage."""

    async def extract(self, context):
        self.calls.append(("extract", context))
        raise app_error("provider_unavailable")

    async def answer_knowledge(self, context):
        self.calls.append(("answer_knowledge", context))
        raise app_error("provider_unavailable")


async def test_an_obvious_report_is_answered_without_the_extraction_call(
    client, provider, session_dir
):
    session_id = await start_session(client)
    provider.script_answer(grounded(ADVICE, ["faq-gas-safety"]))

    _, response = await send_turn(client, session_id, text="i smell gas", revision=0)

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["reply"] == ADVICE
    assert body["citations"] == [seed_citation("faq-gas-safety")]
    assert provider.methods() == ["answer_knowledge"]
    assert read_session(session_dir, session_id)["slots"] == EMPTY


async def test_the_models_flag_catches_wording_the_rule_cannot_know(
    client, provider, session_dir
):
    session_id = await start_session(client)
    provider.script_extraction(
        make_extraction(intent="start_intake", has_question=False, safety_concern=True)
    )
    provider.script_answer(grounded(ADVICE, ["faq-gas-safety"]))

    _, response = await send_turn(
        client, session_id, text="my kitchen smells funny", revision=0
    )

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["reply"] == ADVICE
    assert body["citations"] == [seed_citation("faq-gas-safety")]
    context = provider.calls[-1][1]
    assert "gas" in context.question and "leak" in context.question
    assert read_session(session_dir, session_id)["slots"] == EMPTY


async def test_an_angry_customer_with_a_gas_smell_gets_the_advice_not_a_handoff(
    client, provider
):
    session_id = await start_session(client)
    provider.script_extraction(
        make_extraction(
            intent="start_intake",
            frustration="present",
            safety_concern=True,
        )
    )
    provider.script_answer(grounded(ADVICE, ["faq-gas-safety"]))

    _, response = await send_turn(
        client, session_id, text="I am furious, the house stinks, something is wrong", revision=0
    )

    assert response.json()["reply"] == ADVICE


async def test_a_gas_bill_question_is_not_a_safety_turn(client, provider):
    session_id = await start_session(client)
    provider.script_extraction(
        make_extraction(intent="start_intake", has_question=False, safety_concern=False)
    )

    _, response = await send_turn(
        client, session_id, text="My gas bill is much higher than last month", revision=0
    )

    assert response.status_code == 200, response.text
    assert provider.methods() == ["extract"]
    assert response.json()["citations"] == []


async def test_with_the_model_down_the_customer_still_gets_the_record(settings, session_dir):
    provider = ModelDown()
    app = create_app(settings=settings, provider=provider)
    async with running_client(app) as client:
        session_id = await start_session(client)

        _, response = await send_turn(client, session_id, text="i smell gas", revision=0)

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["reply"] == SEED_RECORDS["faq-gas-safety"]["body"]
    assert body["citations"] == [seed_citation("faq-gas-safety")]
    assert provider.methods() == ["answer_knowledge"]
