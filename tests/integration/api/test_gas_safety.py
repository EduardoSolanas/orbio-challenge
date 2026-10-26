"""A report of a gas leak gets the safety answer first, whatever the model made of it.

Real app, route, engine, retriever and repository. The AI service is scripted, and the
embedding service is a small stand-in that puts anything mentioning a smell next to the
emergency example.
"""

from __future__ import annotations

from gateway.conversation.safety import SafetyExamples, SafetyIndex
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
EXAMPLES = SafetyExamples(emergency=("gas smell here",), ordinary=("my bill is high",))


async def smell_embed(texts: list[str]) -> list[list[float]]:
    return [[1.0, 0.1] if "smell" in text.lower() else [0.1, 1.0] for text in texts]


async def embedding_down(texts: list[str]) -> list[list[float]]:
    raise app_error("provider_unavailable")


class ModelDown(ScriptedProvider):
    """Every model call fails, as in an outage."""

    async def extract(self, context):
        self.calls.append(("extract", context))
        raise app_error("provider_unavailable")

    async def answer_knowledge(self, context):
        self.calls.append(("answer_knowledge", context))
        raise app_error("provider_unavailable")


async def turn(settings, provider, embed, text, *, expect=200):
    """Send one message as the first turn of a fresh session; return the body and ledger."""

    app = create_app(
        settings=settings, provider=provider, safety_index=SafetyIndex(EXAMPLES, embed)
    )
    async with running_client(app) as client:
        session_id = await start_session(client)
        _, response = await send_turn(client, session_id, text=text, revision=0)
    assert response.status_code == expect, response.text
    return response.json(), session_id


async def test_the_embedding_check_catches_a_report_the_model_labelled_as_intake(
    settings, provider, session_dir
):
    provider.script_extraction(
        make_extraction(intent="start_intake", has_question=False)
    )
    provider.script_answer(grounded(ADVICE, ["faq-gas-safety"]))

    body, session_id = await turn(
        settings, provider, smell_embed, "my kitchen smells funny"
    )

    assert body["reply"] == ADVICE
    assert body["citations"] == [seed_citation("faq-gas-safety")]
    context = provider.calls[-1][1]
    assert "gas" in context.question and "leak" in context.question
    assert read_session(session_dir, session_id)["slots"] == EMPTY


async def test_the_models_flag_catches_what_the_embedding_check_does_not(
    settings, provider, session_dir
):
    provider.script_extraction(
        make_extraction(intent="start_intake", has_question=False, safety_concern=True)
    )
    provider.script_answer(grounded(ADVICE, ["faq-gas-safety"]))

    body, session_id = await turn(
        settings, provider, smell_embed, "it hisses near the meter"
    )

    assert body["reply"] == ADVICE
    assert read_session(session_dir, session_id)["slots"] == EMPTY


async def test_the_flag_still_works_when_the_embedding_service_is_down(
    settings, provider
):
    provider.script_extraction(
        make_extraction(intent="start_intake", safety_concern=True)
    )
    provider.script_answer(grounded(ADVICE, ["faq-gas-safety"]))

    body, _ = await turn(settings, provider, embedding_down, "it hisses near the meter")

    assert body["reply"] == ADVICE


async def test_a_gas_bill_question_is_not_a_safety_turn(settings, provider):
    provider.script_extraction(
        make_extraction(intent="start_intake", has_question=False)
    )

    body, _ = await turn(
        settings, provider, smell_embed, "My bill is much higher than last month"
    )

    assert provider.methods() == ["extract"]
    assert body["citations"] == []


async def test_an_angry_customer_with_a_gas_smell_gets_the_advice_not_a_handoff(
    settings, provider
):
    provider.script_extraction(
        make_extraction(intent="start_intake", frustration="present")
    )
    provider.script_answer(grounded(ADVICE, ["faq-gas-safety"]))

    body, _ = await turn(
        settings, provider, smell_embed, "I am furious, the house smells wrong"
    )

    assert body["reply"] == ADVICE


async def test_with_the_model_down_a_flagged_customer_still_gets_the_record(settings):
    provider = ModelDown()

    body, _ = await turn(settings, provider, smell_embed, "something smells strange")

    assert body["reply"] == SEED_RECORDS["faq-gas-safety"]["body"]
    assert body["citations"] == [seed_citation("faq-gas-safety")]
    assert provider.methods() == ["extract", "answer_knowledge"]


async def test_with_the_model_down_an_ordinary_message_is_still_an_error(settings):
    body, _ = await turn(
        settings, ModelDown(), smell_embed, "what is my balance", expect=503
    )

    assert body["error"]["code"] == "provider_unavailable"


async def test_without_any_safety_index_the_model_flag_is_the_only_check(
    settings, provider
):
    """No embedding service configured: the app builds no index and relies on the model."""

    provider.script_extraction(make_extraction(intent="start_intake"))
    app = create_app(settings=settings, provider=provider)
    async with running_client(app) as client:
        session_id = await start_session(client)
        _, response = await send_turn(
            client, session_id, text="my kitchen smells funny", revision=0
        )

    assert response.status_code == 200, response.text
    assert provider.methods() == ["extract"]
