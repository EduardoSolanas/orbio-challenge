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
    _judge(docker_config, request, turn, _assistant_text(first), "booking requested before verification; the assistant must ask for the service postcode and verification code and offer or confirm no appointment time")

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
    with docker_page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/bookings")) as pending:
        slots.first.click()
    assert pending.value.status == 200
    confirmation = _assistant_text(pending.value.json())
    assert confirmation.startswith(f"Booked: a technician will visit on {chosen[:10]}")
    _judge(docker_config, request, f"chose {chosen}", confirmation, f"confirmation of the technician visit the customer chose ({chosen}); it must match that time and include a booking reference")
