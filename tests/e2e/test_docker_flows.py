"""Real Docker/browser E2E for every flow, graded by the judge model.

Each journey drives the shipped page against the deployed containers and the real model, so it
checks what the flows promise end to end: the model only starts a flow, the code moves the steps,
and the saved state survives interruptions. The demo account holds one visit, so every journey that
touches bookings starts from a reset booking store (``fresh_bookings``) or from a known visit
(``booked_visit``) and never depends on what an earlier run left behind.
"""
from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.docker_config import DockerE2EConfig
from tests.e2e.docker_support import assistant_text, judge_reply, send, verify

pytestmark = pytest.mark.docker_e2e

ANYTHING_ELSE = "Is there anything else I can help with?"

ASKS_FOR_ACCOUNT = "the customer has not given an account yet; a correct reply asks for the energy account reference and nothing else: no appointment times, no postcode or code request, no account details"
ASKS_FOR_VERIFICATION = "the customer gave an account but is not verified yet; a correct reply asks for the service postcode and the verification code, and does not ask what the problem is, does not collect a category or urgency, and reveals no account or appointment details"


def _after_verification(page: Page, config: DockerE2EConfig, request: pytest.FixtureRequest, verified: dict[str, Any], what: str) -> dict[str, Any]:
    """Verifying answers the request made before it, in the same chat, with times to choose from."""
    turn = verified.get("turn")
    assert turn, f"verifying did not continue the waiting request: {verified}"
    assert turn["slot_options"], f"a fresh store must offer times: {assistant_text(turn)}"
    reply = assistant_text(turn)
    expect(page.locator("#transcript")).to_contain_text(reply)
    expect(page.locator("#slot-picker button")).to_have_count(len(turn["slot_options"]))
    judge_reply(config, request, f"verified the account after asking to {what}", reply, "the customer verified their account after asking to book a technician; a correct reply says times are available to choose from and does not state or invent any specific time")
    return turn


def test_docker_flow_booking_creates_a_reservation(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest, fresh_bookings: None) -> None:
    turn = "I would like to book a technician visit"
    _, body = send(docker_page, turn)
    assert body["order_verification_required"] is False and body["slot_options"] == []
    expect(docker_page.locator("#order-verification")).to_be_hidden()
    judge_reply(docker_config, request, turn, assistant_text(body), ASKS_FOR_ACCOUNT)

    # A bare account number must carry on with the booking, not start a support request.
    turn = "NS-000123"
    _, body = send(docker_page, turn)
    assert body["order_verification_required"] is True, assistant_text(body)
    assert body["slots"]["category"] is None and body["slots"]["description"] is None
    expect(docker_page.locator("#order-verification")).to_be_visible()
    judge_reply(docker_config, request, turn, assistant_text(body), ASKS_FOR_VERIFICATION)

    assert verify(docker_page, "SW1A 1AA", "123456") == {"verified": False, "error": "verification_failed"}
    verified = verify(docker_page, "sw1a 1aa", "246810")
    assert verified["verified"] is True
    expect(docker_page.locator("#order-verification")).to_be_hidden()
    _after_verification(docker_page, docker_config, request, verified, "book a technician")

    chosen = docker_page.locator("#slot-picker button").first.get_attribute("data-starts-at")
    with docker_page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/bookings")) as pending:
        docker_page.locator("#slot-picker button").first.click()
    assert pending.value.status == 200
    confirmation = assistant_text(pending.value.json())
    assert confirmation.startswith(f"Booked: a technician will visit on {chosen[:10]}") and confirmation.endswith(ANYTHING_ELSE)
    expect(docker_page.locator("#starters button")).to_have_count(4)
    judge_reply(docker_config, request, f"chose the window starting {chosen}", confirmation, f"the customer chose the window starting {chosen}; the confirmation must give that window and a BK- booking reference, and then ask whether there is anything else it can help with")

    # The reservation is real: the account now reports that visit instead of offering times.
    _, body = send(docker_page, "Where is my installation?")
    assert chosen[:10] in assistant_text(body) and "BK-" in assistant_text(body), assistant_text(body)


def test_docker_flow_booking_reports_an_existing_visit(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest, booked_visit: str) -> None:
    _, body = send(docker_page, "I would like to book a technician visit for account NS-000123")
    assert body["order_verification_required"] is True, assistant_text(body)
    verified = verify(docker_page, "SW1A 1AA", "246810")
    assert verified["verified"] is True

    turn = verified["turn"]
    reply = assistant_text(turn)
    assert turn["slot_options"] == [] and booked_visit[:10] in reply and "BK-" in reply, reply
    expect(docker_page.locator("#slot-picker button")).to_have_count(0)
    judge_reply(docker_config, request, "verified the account after asking to book when a visit already exists", reply, f"the verified account already has a technician visit on {booked_visit[:10]}; a correct reply reports that visit with its date, time window and BK- reference and offers no new times; the account reference NS-000123 and the booking details come from the booking system and are expected, not invented")


def test_docker_flow_account_given_up_front_goes_straight_to_verification(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest, fresh_bookings: None) -> None:
    turn = "I'd like to book a technician for account NS-000123."
    _, body = send(docker_page, turn)
    assert body["order_verification_required"] is True and body["slot_options"] == [], assistant_text(body)
    expect(docker_page.locator("#order-verification")).to_be_visible()
    expect(docker_page.locator("#slot-picker button")).to_have_count(0)
    judge_reply(docker_config, request, turn, assistant_text(body), ASKS_FOR_VERIFICATION)

    verified = verify(docker_page, "SW1A 1AA", "246810")
    assert verified["verified"] is True
    _after_verification(docker_page, docker_config, request, verified, "book a technician with the account given up front")


def test_docker_flow_installation_status_end_to_end(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest, fresh_bookings: None) -> None:
    turn = "What is the status of my installation?"
    _, body = send(docker_page, turn)
    assert body["order_verification_required"] is False
    judge_reply(docker_config, request, turn, assistant_text(body), ASKS_FOR_ACCOUNT)

    turn = "NS-000123"
    _, body = send(docker_page, turn)
    assert body["order_verification_required"] is True, assistant_text(body)
    assert body["slots"]["description"] is None
    judge_reply(docker_config, request, turn, assistant_text(body), ASKS_FOR_VERIFICATION)

    verified = verify(docker_page, "SW1A 1AA", "246810")
    assert verified["verified"] is True and verified.get("turn"), verified
    reply = assistant_text(verified["turn"])
    expect(docker_page.locator("#transcript")).to_contain_text(reply)
    assert "technician visit" in reply.lower(), reply
    judge_reply(docker_config, request, "verified the account after asking for the installation status", reply, "the customer verified after asking for their installation status; a correct reply reports either that the installation is approved and waiting for a technician visit, or the booked visit with its date, window and BK- reference; the account reference NS-000123 and the booking details come from the booking system and are expected, not invented")

    # The flow has ended: an ordinary question is answered from the knowledge base again.
    turn = "Can I return an unused item?"
    _, body = send(docker_page, turn)
    assert body["order_verification_required"] is False and body["citations"], body
    judge_reply(docker_config, request, turn, assistant_text(body), "a policy question asked after an account flow had finished; a correct reply answers from the knowledge base (returns within 30 calendar days for an unused item in its original packaging) and does not ask for an account or verification")


SUPPORT_RULE = "the assistant is collecting details for a support request; a correct reply asks for exactly one detail that is still missing (account reference, problem area, description or urgency) or, once all four are known, shows a review and asks the customer to confirm or change it; it never asks for a postcode or verification code"
SUPPORT_ANSWERS = [
    "My account is NS-000123 and it is an equipment issue.",
    "The urgency is high.",
    "Yes, it is an equipment issue with high urgency.",
]


def test_docker_flow_support_request_end_to_end(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest) -> None:
    turn = "I want to report a problem. My smart meter display is completely blank."
    _, body = send(docker_page, turn)
    assert body["status"] == "collecting" and body["order_verification_required"] is False
    judge_reply(docker_config, request, turn, assistant_text(body), SUPPORT_RULE)

    for answer in SUPPORT_ANSWERS:
        if body["status"] == "awaiting_confirmation":
            break
        _, body = send(docker_page, answer)
        judge_reply(docker_config, request, answer, assistant_text(body), SUPPORT_RULE)
    assert body["status"] == "awaiting_confirmation", assistant_text(body)
    assert body["slots"]["order_number"] == "NS-000123" and body["slots"]["urgency"] == "high"
    expect(docker_page.get_by_role("button", name="Confirm details")).to_be_visible()

    turn = "Yes, that is correct, please submit it."
    _, body = send(docker_page, turn)
    reply = assistant_text(body)
    assert body["status"] == "completed", reply
    assert "REQ-" in reply and reply.endswith(ANYTHING_ELSE), reply
    expect(docker_page.locator("#starters button")).to_have_count(4)
    expect(docker_page.locator("#message")).to_be_editable()
    judge_reply(docker_config, request, turn, reply, "the customer confirmed the review; a correct reply says the request was saved locally with a short REQ- reference, that nothing was sent to a real support team, and asks whether there is anything else it can help with")

    # Typing after a saved request opens a new conversation instead of being refused.
    turn = "I also need to book a technician."
    _, body = send(docker_page, turn)
    assert body["status"] == "collecting" and body["slots"]["description"] is None
    judge_reply(docker_config, request, turn, assistant_text(body), ASKS_FOR_ACCOUNT)


def test_docker_flow_detour_from_a_support_request_into_booking_and_back(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest, fresh_bookings: None) -> None:
    turn = "My account NS-000123 has a cracked smart meter casing."
    _, body = send(docker_page, turn)
    assert body["status"] == "collecting" and body["slots"]["order_number"] == "NS-000123"
    assert "cracked" in (body["slots"]["description"] or "").lower()

    turn = "Before that, can you book a technician visit for me?"
    _, body = send(docker_page, turn)
    assert body["order_verification_required"] is True, assistant_text(body)
    judge_reply(docker_config, request, turn, assistant_text(body), ASKS_FOR_VERIFICATION)
    verified = verify(docker_page, "SW1A 1AA", "246810")
    assert verified["verified"] is True
    _after_verification(docker_page, docker_config, request, verified, "book a technician in the middle of a support request")

    turn = "Back to my problem: it is an equipment issue and the urgency is high."
    _, body = send(docker_page, turn)
    assert body["status"] == "awaiting_confirmation", assistant_text(body)
    assert body["slots"]["order_number"] == "NS-000123" and "cracked" in body["slots"]["description"].lower()
    judge_reply(docker_config, request, turn, assistant_text(body), "the customer left a support request to book a technician and came back; the review must still show the earlier details (account NS-000123, a cracked smart meter casing) plus the equipment issue and high urgency, and ask to confirm or change")


def test_docker_flow_is_left_when_the_customer_changes_topic(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest) -> None:
    turn = "I would like to book a technician."
    _, body = send(docker_page, turn)
    judge_reply(docker_config, request, turn, assistant_text(body), ASKS_FOR_ACCOUNT)

    turn = "Can I return an unused item?"
    _, body = send(docker_page, turn)
    assert body["citations"] and body["order_verification_required"] is False
    judge_reply(docker_config, request, turn, assistant_text(body), "a policy question asked in the middle of a booking request; a correct reply answers it from the knowledge base and does not ask for an account or verification")

    # The booking was left, so a bare account number no longer resumes it.
    turn = "NS-000123"
    _, body = send(docker_page, turn)
    reply = assistant_text(body)
    assert body["order_verification_required"] is False and body["slot_options"] == [], reply
    assert body["slots"]["order_number"] == "NS-000123"
    judge_reply(docker_config, request, turn, reply, "the customer first asked to book, then changed topic to a policy question, then sent only an account number; a correct reply treats it as a support-request detail and asks for the next missing one, and does not ask for a postcode or verification code or offer technician times")


def test_docker_flow_booking_stays_in_spanish(docker_page: Page, docker_config: DockerE2EConfig, request: pytest.FixtureRequest, fresh_bookings: None) -> None:
    spanish = "the reply must be written in Spanish"
    turn = "Quiero reservar una visita de un técnico."
    _, body = send(docker_page, turn)
    judge_reply(docker_config, request, turn, assistant_text(body), f"{ASKS_FOR_ACCOUNT}; {spanish}")

    turn = "NS-000123"
    _, body = send(docker_page, turn)
    assert body["order_verification_required"] is True, assistant_text(body)
    judge_reply(docker_config, request, turn, assistant_text(body), f"{ASKS_FOR_VERIFICATION}; {spanish}")

    verified = verify(docker_page, "SW1A 1AA", "246810")
    assert verified["verified"] is True and verified.get("turn"), verified
    reply = assistant_text(verified["turn"])
    expect(docker_page.locator("#transcript")).to_contain_text(reply)
    judge_reply(docker_config, request, "verified the account after asking in Spanish to book a technician", reply, f"the customer verified after asking in Spanish to book a technician; a correct reply says times are available to choose from and invents no specific time; {spanish}")
