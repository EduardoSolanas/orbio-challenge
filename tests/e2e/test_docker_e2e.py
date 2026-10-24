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
    _judge(docker_config, request, supported_one, first_text, "supported policy question; answer with grounded context")

    _, second_body = _send(docker_page, supported_two)
    second_text = _assistant_text(second_body)
    second_citations = second_body.get("citations", [])
    assert second_text.strip()
    assert isinstance(second_citations, list) and second_citations
    assert second_citations != first_citations or second_text != first_text
    assert second_body.get("revision", 0) > first_body.get("revision", -1)
    _judge(docker_config, request, supported_two, second_text, f"follow-up policy question after: {first_text}")

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
