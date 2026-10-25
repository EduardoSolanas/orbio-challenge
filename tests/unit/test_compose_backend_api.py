"""Compose topology for the split gateway/Backend API deployment.

The private Backend API must be a second service reachable only on the internal
network: no host port, its own health check, and the gateway must start only
after it reports healthy. These assertions pin the deployment contract that the
gateway-origin integration test cannot prove on its own.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_API_PORT = "8001"


def compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_compose_runs_the_gateway_and_the_private_backend_api() -> None:
    services = compose()["services"]

    assert set(services) == {"app", "backend-api"}


def test_backend_api_publishes_no_host_port_and_is_only_exposed_internally() -> None:
    backend_api = compose()["services"]["backend-api"]

    assert "ports" not in backend_api
    assert backend_api["expose"] == [BACKEND_API_PORT]
    command = backend_api["command"]
    assert "app.backend_api:app" in command
    assert command[command.index("--port") + 1] == BACKEND_API_PORT
    assert backend_api["healthcheck"]["test"][-1].endswith("/health', timeout=2).close()")


def test_gateway_waits_for_a_healthy_backend_api_and_calls_it_by_service_dns() -> None:
    app = compose()["services"]["app"]

    assert app["depends_on"] == {"backend-api": {"condition": "service_healthy"}}
    assert app["environment"]["BACKEND_API_BASE_URL"] == f"http://backend-api:{BACKEND_API_PORT}"
    assert "ports" in app
