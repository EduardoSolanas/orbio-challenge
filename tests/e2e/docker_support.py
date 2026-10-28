"""Shared pieces of the real Docker E2E modules: page helpers and the judge call (the fixtures are in conftest.py).

Nothing here installs a provider fake; the modules that import it talk to the deployed stack.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from playwright.sync_api import Page

from tests.e2e.docker_config import DockerE2EConfig, semantic_judge
from tests.e2e.judge_config import write_artifact

JUDGE_ARTIFACTS = Path("reports/e2e-docker/judge")


def judge_reply(
    config: DockerE2EConfig,
    request: pytest.FixtureRequest,
    turn: str,
    response: str,
    context: str,
) -> None:
    """Grade one reply against a written rule, keep the verdict, and fail the test if it did not pass."""
    artifact: dict[str, Any] = {
        "test_node_id": request.node.nodeid,
        "user_turn": turn,
        "response": response,
        "context": context,
        "model": config.judge.model,
        "prompt_version": config.judge.prompt_version,
    }
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
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/turns")
        )
    ) as pending:
        page.locator("#send").click()
    response = pending.value
    body = response.json()
    assert response.status == 200
    assert "reply" in body, body
    return response, body


RESET_BOOKINGS = (
    "import sqlite3; db = sqlite3.connect('/data/backend/bookings.db'); "
    "db.execute('UPDATE slots SET order_number = NULL, reference = NULL'); db.commit()"
)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _compose(project: str, *args: str) -> str:
    completed = subprocess.run(
        ["docker", "compose", "-p", project, *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout.strip()


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
WILDCARD_HOSTS = {"0.0.0.0", "::", ""}


def is_published_at(app_url: str, published: str) -> bool:
    """Does ``app_url`` name this machine's published address (``host:port`` from ``compose port``)?

    Only a loopback origin can be this machine's own deployment, and it has to be on the port the
    project publishes, on loopback or on every interface. A remote host on the same port is not.
    """
    target = urlparse(app_url)
    host, _, port = published.strip().rpartition(":")
    if (
        target.hostname not in LOOPBACK_HOSTS
        or target.port is None
        or str(target.port) != port
    ):
        return False
    return host.strip("[]") in LOOPBACK_HOSTS | WILDCARD_HOSTS


def reset_demo_bookings(app_url: str) -> None:
    """Free every technician window, in the dedicated test deployment and nowhere else.

    The reset empties every account's booking, so it only runs against a loopback origin served by a
    Compose project whose name ends in ``-e2e`` (``E2E_COMPOSE_PROJECT``) and whose published address
    is that origin. A remote origin, or any other project, stops the test before anything is touched.
    """
    project = os.environ.get("E2E_COMPOSE_PROJECT", "")
    if not project.endswith("-e2e"):
        pytest.fail(
            "booking journeys reset the booking store, so they need a dedicated deployment: set "
            "E2E_COMPOSE_PROJECT to a Compose project named like northstar-e2e (see docs/TESTING.md)"
        )
    if urlparse(app_url).hostname not in LOOPBACK_HOSTS:
        pytest.fail(
            f"booking journeys only reset a deployment on this machine; {app_url} is not a loopback origin"
        )
    try:
        published = _compose(project, "port", "web", "8080")
        if not is_published_at(app_url, published):
            pytest.fail(
                f"compose project {project} publishes {published}, which is not the origin under test {app_url}"
            )
        _compose(project, "exec", "-T", "backend-api", "python", "-c", RESET_BOOKINGS)
    except (OSError, subprocess.SubprocessError) as error:
        pytest.fail(
            f"cannot reset the booking store of compose project {project}: {error}"
        )


def book_demo_visit(app_url: str) -> str:
    """Give the demo account its one visit through the public API; returns the booked window."""

    def call(path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{app_url}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())

    session_id = call("/api/sessions", {})["session_id"]
    first = call(
        f"/api/sessions/{session_id}/turns",
        {
            "request_id": str(uuid4()),
            "expected_revision": 0,
            "text": "I'd like to book a technician for account NS-000123.",
            "input_mode": "text",
        },
    )
    assert first["order_verification_required"] is True, first["reply"]
    verified = call(
        f"/api/sessions/{session_id}/order-verification",
        {"delivery_postcode": "SW1A 1AA", "verification_code": "246810"},
    )
    turn = verified["turn"]
    window = turn["slot_options"][0]["starts_at"]
    call(
        f"/api/sessions/{session_id}/bookings",
        {
            "request_id": str(uuid4()),
            "expected_revision": turn["revision"],
            "starts_at": window,
        },
    )
    return window


def assistant_text(body: dict[str, Any]) -> str:
    return body["reply"]


def try_verify(page: Page, postcode: str, code: str) -> tuple[int, dict[str, Any]]:
    """Fill the verification form and submit it; returns the status as well, for refused attempts."""
    page.locator("#delivery-postcode").fill(postcode)
    page.locator("#verification-code").fill(code)
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/order-verification")
        )
    ) as pending:
        page.locator("#verify-order").click()
    return pending.value.status, pending.value.json()


def verify(page: Page, postcode: str, code: str) -> dict[str, Any]:
    """Verify with the form; the body may carry the request that was waiting."""
    status, body = try_verify(page, postcode, code)
    assert status == 200
    return body
