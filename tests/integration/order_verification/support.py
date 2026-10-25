"""Shared helpers for the order verification tests."""

from __future__ import annotations

from uuid import uuid4

from gateway.localization import render, text
from backend.api import DEMO_VERIFICATION_CODE, FIXTURE_ORDER_NUMBER, FIXTURE_POSTCODE
from gateway.schemas import ToolRequest
from tests.integration.support.api import make_extraction


OTHER_ORDER = "NS-000999"

WRONG_CODE = "123456"

ORDER_STATUS_Q = "Where is my order?"

ORDER_NUMBER_TEXT = "The energy account reference is NS-000123."

RETURN_Q = "Can I return an unused item?"

RETURN_ANSWER_TEXT = (
    "You can request a return within 30 calendar days of delivery for an unused "
    "item in its original packaging. Refund eligibility requires support review."
)

VERIFICATION_PROMPT = text("auth.verification_prompt", "en")

ORDER_NUMBER_PROMPT = text("auth.order_number_prompt", "en")

ORDER_RESULT = render(
    "auth.order_result",
    "en",
    {
        "order_number": FIXTURE_ORDER_NUMBER,
        "first_name": "Casey",
        "item_names": "Electricity and gas supply",
        "status": text("auth.status_active", "en"),
    },
)


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def order_lookup(
    *,
    order_number: str | None = None,
    tool: bool = False,
    language: str = "en",
    frustration: str = "none",
):
    return make_extraction(
        intent="order_lookup",
        has_question=True,
        candidates={"order_number": order_number},
        detected_language=language,
        frustration=frustration,
        tool_request=(
            ToolRequest(name="lookup_verified_order", arguments={}) if tool else None
        ),
    )


def full_intake():
    return make_extraction(
        candidates={
            "order_number": FIXTURE_ORDER_NUMBER,
            "category": "damaged_item",
            "description": "My order arrived broken",
            "urgency": "high",
        }
    )


async def create_session(client) -> str:
    response = await client.post("/api/sessions", json={})
    assert response.status_code == 201
    return response.json()["session_id"]


async def send_turn(client, session_id: str, message: str, revision: int):
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json={
            "request_id": str(uuid4()),
            "expected_revision": revision,
            "text": message,
            "input_mode": "text",
        },
    )
    return response


async def verify(
    client,
    session_id: str,
    postcode: str = FIXTURE_POSTCODE,
    code: str = DEMO_VERIFICATION_CODE,
):
    return await client.post(
        f"/api/sessions/{session_id}/order-verification",
        json={"delivery_postcode": postcode, "verification_code": code},
    )


