"""Real Docker/browser E2E. This module never installs a provider fake."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.docker_config import DockerE2EConfig, load_config, probe, semantic_judge


pytestmark = pytest.mark.docker_e2e


def _artifact_path(nodeid: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", nodeid)
    path = Path("reports/e2e-docker/judge") / f"{safe}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _judge(config: DockerE2EConfig, request: pytest.FixtureRequest, turn: str, response: str, context: str) -> None:
    artifact: dict[str, Any] = {"test_node_id": request.node.nodeid, "user_turn": turn, "response": response, "context": context, "model": config.judge.model, "prompt_version": config.judge.prompt_version}
    try:
        result = semantic_judge(config.judge, turn, response, context)
    except Exception as exc:
        artifact.update({"raw_output": None, "verdict": None, "error": str(exc)})
        _artifact_path(request.node.nodeid).write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        raise
    artifact.update(result)
    _artifact_path(request.node.nodeid).write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert result["verdict"]["pass"], json.dumps(result["verdict"], ensure_ascii=False)


def _send(page: Page, text: str) -> tuple[Any, dict[str, Any]]:
    page.get_by_role("textbox", name="Message").fill(text)
    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/turns")) as pending:
        page.get_by_role("button", name="Send", exact=True).click()
    response = pending.value
    body = response.json()
    assert response.status == 200
    assert isinstance(body, dict)
    assert isinstance(body.get("messages"), list) or isinstance(body.get("message"), dict) or "reply" in body
    return response, body


def _assistant_text(body: dict[str, Any]) -> str:
    message = body.get("message")
    if isinstance(message, dict):
        return str(message.get("text") or message.get("content") or "")
    if isinstance(body.get("reply"), str):
        return body["reply"]
    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        for item in reversed(messages):
            if isinstance(item, dict) and item.get("role") == "assistant":
                return str(item.get("text") or item.get("content") or "")
    return ""


@pytest.fixture(scope="session")
def docker_config() -> DockerE2EConfig:
    config = load_config()
    probe(config)
    return config


@pytest.fixture
def docker_page(page: Page, docker_config: DockerE2EConfig) -> Page:
    response = page.goto(f"{docker_config.app_url}/")
    assert response is not None and response.status == 200
    assert page.url == f"{docker_config.app_url}/"
    expect(page.locator("#transcript")).not_to_be_empty()
    return page


def test_docker_browser_to_provider_to_browser(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest) -> None:
    """A real shipped UI request must traverse Docker and return to the browser."""
    turn = "Hello"
    _, body = _send(docker_page, turn)
    text = _assistant_text(body)
    expect(docker_page.locator("#transcript")).to_contain_text(text)
    assert text.strip()
    _judge(docker_config, request, turn, text, "social greeting; remain brief and do not start intake or retrieval")


def test_docker_multi_turn_rag_preserves_state_and_citations(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest) -> None:
    fact = "My order NS-000123 arrived broken."
    supported_one = "Can I return an unused item?"
    supported_two = "How long do I have to request that return?"
    _, fact_body = _send(docker_page, fact)
    first_session = docker_page.evaluate("() => localStorage.getItem('northstar.session_id')")
    assert first_session
    _, first_body = _send(docker_page, supported_one)
    first_text = _assistant_text(first_body)
    first_citations = first_body.get("citations", [])
    assert first_text.strip()
    assert isinstance(first_citations, list) and first_citations
    assert first_body.get("slots") == fact_body.get("slots") or first_body.get("revision", 0) > fact_body.get("revision", -1)
    first_context = json.dumps(
        {"retrieved_citations": first_citations}, ensure_ascii=False
    )
    _judge(
        docker_config,
        request,
        supported_one,
        first_text,
        f"Supported policy answer; evaluate grounding against these retrieved citations: {first_context}",
    )

    _, second_body = _send(docker_page, supported_two)
    second_text = _assistant_text(second_body)
    second_citations = second_body.get("citations", [])
    assert second_text.strip()
    assert isinstance(second_citations, list) and second_citations
    # The follow-up is answered from the same record and gives the 30-day window.
    # Both questions can legitimately retrieve one record and quote it identically.
    assert {c["source_id"] for c in second_citations} == {"faq-returns"}
    assert "30" in second_text
    assert second_body.get("revision", 0) > first_body.get("revision", -1)
    second_context = json.dumps(
        {
            "previous_grounded_answer": first_text,
            "retrieved_citations": second_citations,
        },
        ensure_ascii=False,
    )
    _judge(
        docker_config,
        request,
        supported_two,
        second_text,
        f"Follow-up policy question; use the previous answer and these retrieved citations: {second_context}",
    )

    docker_page.reload()
    expect(docker_page.locator("#transcript")).to_contain_text(first_text)
    expect(docker_page.locator("#transcript")).to_contain_text(second_text)
    restored = docker_page.evaluate("() => localStorage.getItem('northstar.session_id')")
    assert restored == first_session


def test_docker_unsupported_question_abstains_without_citations(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest) -> None:
    turn = "Which payment methods are accepted?"
    _, body = _send(docker_page, turn)
    text = _assistant_text(body)
    assert text.strip()
    assert body.get("citations") == []
    _judge(docker_config, request, turn, text, "unsupported policy question; clear abstention with no invented facts")


def _verify(page: Page, postcode: str, code: str) -> dict[str, Any]:
    page.locator("#delivery-postcode").fill(postcode)
    page.locator("#verification-code").fill(code)
    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/order-verification")) as pending:
        page.locator("#verify-order").click()
    assert pending.value.status == 200
    return pending.value.json()


def test_docker_booking_asks_for_verification_first_then_books(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest) -> None:
    """The real model must route booking requests, and the app must gate them on verification.

    The demo account holds one visit in the Backend API volume. On a rerun the account already has
    its visit, so the second request is answered with that visit instead of new times; reset with
    ``docker compose down --volumes`` to book again.
    """
    slots = docker_page.locator("#slot-picker button")
    turn = "I'd like to book a technician for account NS-000123."
    _, first = _send(docker_page, turn)
    assert first["order_verification_required"] is True
    assert first["slot_options"] == []
    expect(docker_page.locator("#order-verification")).to_be_visible()
    expect(slots).to_have_count(0)
    _judge(docker_config, request, turn, _assistant_text(first), "the customer asked to book a technician but is not verified yet; a correct reply asks for the service postcode and verification code and does not offer, state or confirm any appointment time")

    assert _verify(docker_page, "SW1A 1AA", "123456") == {"verified": False, "error": "verification_failed"}
    expect(docker_page.locator("#order-verification")).to_be_visible()
    expect(slots).to_have_count(0)
    assert _verify(docker_page, "SW1A 1AA", "246810") == {"verified": True, "error": None}
    expect(docker_page.locator("#order-verification")).to_be_hidden()

    turn = "Please book a technician for me now."
    _, second = _send(docker_page, turn)
    reply = _assistant_text(second)
    if not second["slot_options"]:
        assert "Booking reference: BK-" in reply, reply
        _judge(docker_config, request, turn, reply, "the verified account already has a technician visit; the assistant must report that existing visit and its reference and offer no new times")
        return
    assert len(second["slot_options"]) >= 1
    expect(slots).to_have_count(len(second["slot_options"]))
    _judge(docker_config, request, turn, reply, "verified customer asked to book a technician; the assistant must say that times are available to choose from and must not state or invent any specific time")
    chosen = slots.first.get_attribute("data-starts-at")
    clicked = slots.first.inner_text()
    with docker_page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/bookings")) as pending:
        slots.first.click()
    assert pending.value.status == 200
    confirmation = _assistant_text(pending.value.json())
    assert confirmation.startswith(f"Booked: a technician will visit on {chosen[:10]}")
    _judge(docker_config, request, f"clicked the time button labelled '{clicked}' (window starting {chosen})", confirmation, f"the customer clicked the button '{clicked}' for the window starting {chosen}; the confirmation must give that same window, including its end time as shown on the button, and a booking reference")


SINGLE_TURN_INTENTS = [
    ("gratitude", "Thank you so much, that was really helpful.", "the customer is thanking the assistant; a correct reply is a brief polite acknowledgement that does not ask for issue details or verification and does not give a policy answer"),
    ("capability", "What can you help me with?", "the customer asks what the assistant can do; a correct reply describes what it can help with, such as energy questions, a verified demo account or reporting a service issue, and does not start collecting issue details"),
    ("unrelated", "Tell me a joke about pizza.", "the customer makes an off-topic request; a correct reply says it cannot help with that request, restates what the assistant can help with and does not tell a joke"),
    ("start_intake", "I want to report a problem.", "the customer wants to report a problem without saying what it is; a correct reply starts collecting by asking for one detail, such as the account reference or what happened, and does not ask for verification codes or give a policy answer"),
]


@pytest.mark.parametrize(("name", "turn", "rule"), SINGLE_TURN_INTENTS, ids=[case[0] for case in SINGLE_TURN_INTENTS])
def test_docker_single_turn_intents(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest, name: str, turn: str, rule: str) -> None:
    _, body = _send(docker_page, turn)
    reply = _assistant_text(body)
    assert reply.strip()
    assert body["status"] == "collecting"
    assert body["order_verification_required"] is False
    _judge(docker_config, request, turn, reply, rule)


def test_docker_intake_correction_rejection_and_confirmation(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest) -> None:
    turn = "My account NS-000123 has a cracked smart meter casing. It is an equipment issue and the urgency is high."
    _, body = _send(docker_page, turn)
    assert body["status"] == "awaiting_confirmation", body["reply"]
    assert body["slots"]["order_number"] == "NS-000123" and body["slots"]["urgency"] == "high"
    _judge(docker_config, request, turn, _assistant_text(body), "a review of the collected details: it must show account reference NS-000123, an equipment issue, a cracked smart meter casing and high urgency, and ask the customer to confirm or change them")

    turn = "Actually, make the urgency low."
    _, body = _send(docker_page, turn)
    assert body["status"] == "awaiting_confirmation" and body["slots"]["urgency"] == "low", body["reply"]
    _judge(docker_config, request, turn, _assistant_text(body), "the customer changed the urgency to low; the updated review must show low urgency, keep the other details and ask to confirm or change")

    turn = "No."
    _, body = _send(docker_page, turn)
    assert body["status"] == "collecting"
    _judge(docker_config, request, turn, _assistant_text(body), "the customer rejected the review; a correct reply asks what they would like to change and does nothing else")

    turn = "Make the urgency normal."
    _, body = _send(docker_page, turn)
    assert body["status"] == "awaiting_confirmation" and body["slots"]["urgency"] == "normal", body["reply"]
    _judge(docker_config, request, turn, _assistant_text(body), "the customer corrected the urgency to normal after rejecting; the new review must show normal urgency with the other details unchanged")

    with docker_page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/confirm")) as pending:
        docker_page.get_by_role("button", name="Confirm details").click()
    assert pending.value.status == 200
    confirmed = pending.value.json()
    assert confirmed["status"] == "completed"
    _judge(docker_config, request, "confirmed the review with the Confirm button", _assistant_text(confirmed), "the customer confirmed; a correct reply says the details were saved locally and that no request was sent to a real support team, and does not claim a real ticket or agent")


def test_docker_order_status_asks_for_verification_first(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest) -> None:
    turn = "What is the status of my account NS-000123?"
    _, first = _send(docker_page, turn)
    assert first["order_verification_required"] is True
    reply = _assistant_text(first)
    assert "Casey" not in reply and "Active" not in reply
    _judge(docker_config, request, turn, reply, "the customer asked for account status but is not verified yet; a correct reply asks for the service postcode and verification code and reveals no account details")
    assert _verify(docker_page, "SW1A 1AA", "123456") == {"verified": False, "error": "verification_failed"}
    assert _verify(docker_page, "SW1A 1AA", "246810") == {"verified": True, "error": None}

    turn = "Please show me the details of my account."
    _, second = _send(docker_page, turn)
    reply = _assistant_text(second)
    assert "NS-000123" in reply and "Casey" in reply, reply
    _judge(docker_config, request, turn, reply, "a verified customer asked for their account details; a correct reply gives the account reference NS-000123, the first name Casey, the service and the status, and no other customer data such as a postcode, verification code, email or address")


def test_docker_installation_status_asks_for_verification_first(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest) -> None:
    turn = "What is the status of my smart meter installation for account NS-000123?"
    _, first = _send(docker_page, turn)
    assert first["order_verification_required"] is True
    reply = _assistant_text(first)
    assert "technician visit" not in reply.lower()
    _judge(docker_config, request, turn, reply, "the customer asked for installation status but is not verified yet; a correct reply asks for the service postcode and verification code and reveals no installation details")
    assert _verify(docker_page, "SW1A 1AA", "246810") == {"verified": True, "error": None}

    turn = "Any update on my installation now?"
    _, second = _send(docker_page, turn)
    reply = _assistant_text(second)
    assert "technician visit" in reply.lower(), reply
    _judge(docker_config, request, turn, reply, "a verified customer asked about their installation; a correct reply reports either that the installation is approved and waiting for a technician visit, with a hint to book one, or the booked visit with its date, two-hour window and BK- reference; the account reference NS-000123 and these booking details are expected and not invented")


ANGRY_CUSTOMERS = [
    ("english", "This is ridiculous, I'm so pissed off. I want to talk to a human right now!", "simulates a transfer"),
    ("spanish", "Estoy harto de este bot, quiero hablar con una persona.", "simula la transferencia"),
]


@pytest.mark.parametrize(("name", "turn", "marker"), ANGRY_CUSTOMERS, ids=[case[0] for case in ANGRY_CUSTOMERS])
def test_docker_angry_customer_is_handed_to_a_simulated_agent(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest, name: str, turn: str, marker: str) -> None:
    _, body = _send(docker_page, turn)
    reply = _assistant_text(body)
    assert body["status"] == "handed_off", reply
    assert marker in reply
    expect(docker_page.locator("#message")).to_have_js_property("readOnly", True)
    _judge(docker_config, request, turn, reply, "the customer is angry and asks for a human; a correct reply shows empathy and clearly says the human handoff is simulated, that no live agent is connected and that no real support request was sent; it does not keep collecting details or claim a real transfer happened")


def test_docker_angry_customer_in_the_middle_of_an_intake_is_handed_off(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest) -> None:
    _, first = _send(docker_page, "My account NS-000123 has a cracked smart meter casing.")
    assert first["status"] == "collecting"
    turn = "I have asked three times and nobody helps me. I'm furious, get me a real person!"
    _, body = _send(docker_page, turn)
    reply = _assistant_text(body)
    assert body["status"] == "handed_off", reply
    assert "simulates a transfer" in reply
    _judge(docker_config, request, turn, reply, "the customer was in the middle of reporting an issue and became furious; a correct reply shows empathy and clearly says the human handoff is simulated, no live agent is connected and no real request was sent; it does not ask for more issue details")


def test_docker_nervous_customer_stays_in_the_chat(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest) -> None:
    turn = "I'm a bit nervous, my meter is showing a strange number."
    _, body = _send(docker_page, turn)
    reply = _assistant_text(body)
    assert body["status"] != "handed_off", reply
    assert "simulates a transfer" not in reply
    _judge(docker_config, request, turn, reply, "the customer is nervous but not angry; a correct reply stays calm and helpful, for example by asking one detail to start a report or offering help, and does not hand the customer to a human agent or claim a transfer")
