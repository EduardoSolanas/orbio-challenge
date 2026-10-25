"""Unit tests for the engine's order-verification endpoint path.

The gateway keeps no catalog and no demo code: the engine delegates to
``BackendApiClient`` and only records the grant or the failure. A protocol-faithful
``httpx.MockTransport`` stands in for the external Backend API so the real client,
response models and registry run without a network.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.conversation import ConversationEngine
from app.backend_client import BackendApiClient
from app.schemas import OrderVerificationRequest
from app.storage import JsonSessionRepository
from app.verification import VerificationRegistry
from tests.support.unit_fakes import (
    FakeKnowledgeBase,
    FakeProvider,
    make_session,
    make_slots,
)

ORDER = "NS-000123"
POSTCODE = "SW1A 1AA"
CODE = "246810"


def build_engine(tmp_path, *, customer_data=None):
    repository = JsonSessionRepository(tmp_path)
    engine = ConversationEngine(
        repository,
        FakeProvider([]),
        FakeKnowledgeBase(),
        customer_data=customer_data,
        verification=VerificationRegistry(),
    )
    return engine, repository


def request() -> OrderVerificationRequest:
    return OrderVerificationRequest(delivery_postcode=POSTCODE, verification_code=CODE)


async def test_verification_without_an_order_records_one_failure(tmp_path):
    engine, repository = build_engine(tmp_path)
    session = make_session(slots=make_slots())
    await repository.save(session)
    before = (tmp_path / f"{session.session_id}.json").read_bytes()

    response = await engine.verify_order(session.session_id, request())

    assert response.verified is False
    assert response.error == "verification_failed"
    assert engine.verification.failures(session.session_id) == 1
    assert engine.verification.grant(session.session_id) is None
    assert (tmp_path / f"{session.session_id}.json").read_bytes() == before


async def test_verification_without_a_client_fails_without_calling_anything(tmp_path):
    engine, repository = build_engine(tmp_path, customer_data=None)
    session = make_session(slots=make_slots(order_number=ORDER))
    await repository.save(session)

    response = await engine.verify_order(session.session_id, request())

    assert response.verified is False
    assert response.error == "verification_failed"
    assert engine.verification.failures(session.session_id) == 1


async def test_successful_verification_records_the_grant_through_the_client(tmp_path):
    seen: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return httpx.Response(200, json={"verified": True, "error": None})

    client = BackendApiClient(
        "http://backend-api.test", timeout_seconds=1.0, transport=httpx.MockTransport(handler)
    )
    engine, repository = build_engine(tmp_path, customer_data=client)
    session = make_session(slots=make_slots(order_number=ORDER))
    await repository.save(session)

    response = await engine.verify_order(session.session_id, request())

    assert response.verified is True
    assert response.error is None
    assert engine.verification.grant(session.session_id) == ORDER
    assert engine.verification.failures(session.session_id) == 0
    assert [str(request.url) for request in seen] == [
        "http://backend-api.test/internal/v1/order-verifications"
    ]


async def test_completed_sessions_cannot_be_verified(tmp_path):
    engine, repository = build_engine(tmp_path)
    session = make_session(status="completed", slots=make_slots(order_number=ORDER))
    await repository.save(session)

    with pytest.raises(Exception) as raised:
        await engine.verify_order(session.session_id, request())

    assert getattr(raised.value, "code", None) == "session_completed"
