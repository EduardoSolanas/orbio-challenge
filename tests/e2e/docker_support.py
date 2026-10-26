"""Shared pieces of the real Docker E2E modules: fixtures, page helpers and the judge call.

Nothing here installs a provider fake; the modules that import it talk to the deployed stack.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.docker_config import DockerE2EConfig, load_config, probe, semantic_judge
from tests.e2e.judge_config import write_artifact

JUDGE_ARTIFACTS = Path("reports/e2e-docker/judge")


def judge_reply(config: DockerE2EConfig, request: pytest.FixtureRequest, turn: str, response: str, context: str) -> None:
    """Grade one reply against a written rule, keep the verdict, and fail the test if it did not pass."""
    artifact: dict[str, Any] = {"test_node_id": request.node.nodeid, "user_turn": turn, "response": response, "context": context, "model": config.judge.model, "prompt_version": config.judge.prompt_version}
    try:
        result = semantic_judge(config.judge, turn, response, context)
    except Exception as exc:
        artifact.update({"raw_output": None, "verdict": None, "error": str(exc)})
        write_artifact(JUDGE_ARTIFACTS, request.node.nodeid, artifact)
        raise
    artifact.update(result)
    write_artifact(JUDGE_ARTIFACTS, request.node.nodeid, artifact)
    assert result["verdict"]["pass"], result["verdict"]


def send(page: Page, text: str) -> tuple[Any, dict[str, Any]]:
    """Type a message and press Send. Ids, not names: the page labels its controls in Spanish after a Spanish turn."""
    page.locator("#message").fill(text)
    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/turns")) as pending:
        page.locator("#send").click()
    response = pending.value
    body = response.json()
    assert response.status == 200
    assert "reply" in body, body
    return response, body


def assistant_text(body: dict[str, Any]) -> str:
    return body["reply"]


def verify(page: Page, postcode: str, code: str) -> dict[str, Any]:
    """Fill the verification form and submit it; the body may carry the request that was waiting."""
    page.locator("#delivery-postcode").fill(postcode)
    page.locator("#verification-code").fill(code)
    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/order-verification")) as pending:
        page.locator("#verify-order").click()
    assert pending.value.status == 200
    return pending.value.json()


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
