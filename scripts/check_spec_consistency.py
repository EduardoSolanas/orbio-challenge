#!/usr/bin/env python3
"""Specification consistency gate for the private Order API boundary.

This repository ships specifications and deployment templates only. This script
is the executable acceptance check for the two-service contract: it parses the
Compose template with Docker Compose itself and asserts that every contract
document describes the same gateway/private-Order-API boundary.

Exit status 0 means every check passed; 1 lists the failed checks.
Standard library only; requires the Docker CLI for the Compose parse.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docs" / "deployment" / "compose.yaml"
ENV_FILE = ROOT / "docs" / "deployment" / ".env"
ENV_EXAMPLE = ROOT / "docs" / "deployment" / ".env.example"

DOCS = {
    "implementation": ROOT / "docs" / "implementation-contract.md",
    "testing": ROOT / "docs" / "testing-contract.md",
    "startup": ROOT / "docs" / "startup.md",
    "build": ROOT / "docs" / "build-spec.md",
    "provider": ROOT / "docs" / "provider-compatibility.md",
    "architecture": ROOT / "docs" / "architecture.html",
}


def text(key: str) -> str:
    """Document text with runs of whitespace collapsed for line-wrap tolerance."""
    return re.sub(r"\s+", " ", DOCS[key].read_text(encoding="utf-8"))


def compose_config() -> dict:
    """Parse the Compose template through Docker Compose; needs a .env file."""
    created = False
    if not ENV_FILE.exists():
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        created = True
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=120,
        )
    finally:
        if created:
            ENV_FILE.unlink(missing_ok=True)
    if result.returncode != 0:
        raise AssertionError(f"docker compose config failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def check_compose(config: dict) -> None:
    services = config["services"]
    assert set(services) == {"app", "order-api"}, f"services are {sorted(services)}"
    gateway, order_api = services["app"], services["order-api"]

    assert not order_api.get("ports"), "private Order API must not publish a host port"
    assert set(order_api["networks"]) == {"internal"}, "Order API is internal-network only"
    assert config["networks"]["internal"]["internal"] is True, "internal network must be internal"
    assert "internal" in gateway["networks"], "gateway must join the internal network"
    published = [(p.get("host_ip"), p.get("published"), p.get("target")) for p in gateway["ports"]]
    assert published == [("127.0.0.1", "8000", 8000)], f"only the gateway is published, got {published}"

    depends = gateway.get("depends_on", {})
    assert depends.get("order-api", {}).get("condition") == "service_healthy", (
        "gateway must wait for a healthy Order API"
    )
    assert gateway["environment"]["ORDER_API_URL"] == "http://order-api:8001"
    assert gateway["environment"]["SESSION_DIR"] == "/data/sessions"
    assert order_api["environment"]["ORDER_API_TOKEN"], "Order API needs its shared secret"
    assert gateway["environment"]["ORDER_API_TOKEN"], "gateway needs the shared secret"

    compose_text = COMPOSE.read_text(encoding="utf-8")
    assert compose_text.count("- .env") == 2, "both services must load docs/deployment/.env"
    for name in ("app", "order-api"):
        assert services[name]["healthcheck"]["test"], f"{name} needs a healthcheck"
    assert "/health" in " ".join(order_api["healthcheck"]["test"])
    assert "/health" in " ".join(gateway["healthcheck"]["test"])
    assert config["volumes"]["sessions"] is not None, "session volume retained"


def check_dockerfile() -> None:
    dockerfile = (ROOT / "docs" / "deployment" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY .*order_api \./order_api$", dockerfile, re.M), (
        "Dockerfile must ship the order_api package"
    )
    assert "app.main:app" in dockerfile, "gateway CMD unchanged"
    assert "USER appuser" in dockerfile, "non-root runtime"


def check_env_example() -> None:
    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in ("ORDER_API_URL", "ORDER_API_TOKEN", "ORDER_API_TIMEOUT_SECONDS"):
        assert re.search(rf"^{key}=", example, re.M), f"{key} missing from .env.example"


def check_implementation_contract() -> None:
    doc = text("implementation")
    for endpoint in (
        "POST /internal/v1/order-verifications",
        "GET /internal/v1/orders/{order_number}",
    ):
        assert endpoint in doc, f"{endpoint} missing from implementation contract"
    for path in ("order_api/main.py", "order_api/catalog.py", "order_api/grants.py",
                 "app/order_client.py"):
        assert path in doc, f"{path} missing from the file-ownership table"
    assert "X-Verification-Grant" in doc, "grant header not specified"
    assert "X-Order-Api-Token" in doc, "internal shared-secret header not specified"
    for code in ("order_api_unavailable", "order_api_timeout",
                 "invalid_order_api_response", "order_api_configuration"):
        assert code in doc, f"gateway error code {code} missing"
    for code in ("invalid_order_number", "order_not_found",
                 "verification_required", "unauthorized"):
        assert code in doc, f"Order API error code {code} missing"
    assert "not_found" in doc, "not_found clarification code missing"
    assert "OrderApiClient" in doc, "gateway client signature missing"
    assert "ConversationEngine" in doc and "order_api: OrderApiClient" in doc, (
        "engine must take the Order API dependency"
    )
    assert "unreachable from the browser" in doc or "never reachable from the browser" in doc, (
        "browser unreachability rule missing"
    )
    assert "never verify an order" not in doc, "stale prompt rule still forbids verification"


def check_test_matrix() -> None:
    doc = text("implementation")
    assert "IT15" in doc, "IT15 Order API group missing"
    assert "J11" in doc, "outage-recovery journey missing"
    assert "199" in doc, "integration inventory total not updated to 199"
    assert "15 backend groups" in doc or "15 groups" in doc, "group total not updated"
    assert "eleven" in doc or "11 complete Playwright journeys" in doc, (
        "journey total not updated"
    )
    assert "J01–J11" in doc or "J01-J11" in doc, "journey ID range not updated"
    it15 = re.findall(r"IT15-\d\d", doc)
    assert len(set(it15)) == 14, f"expected 14 IT15 cases, found {len(set(it15))}"
    assert "order-verifications" in doc and "verification" in doc
    j01 = re.search(r"J01 \| (.*?)\|", doc)
    assert j01 and "verification" in j01.group(1).lower(), "J01 must cover verification"


def check_testing_contract() -> None:
    doc = text("testing")
    assert "199" in doc, "testing contract must state 199 integration cases"
    assert "IT01–IT15" in doc or "IT01-IT15" in doc, "testing contract group range stale"
    assert "J01–J11" in doc or "J01-J11" in doc, "testing contract journey range stale"
    assert 'source_paths = ["app/", "order_api/"]' in doc, "mutmut scope must include order_api"
    assert "order_api" in doc and "real" in doc.lower()
    assert "order-api" in doc, "E2E fixture must run the real Order API"


def check_startup() -> None:
    doc = text("startup")
    assert "order-api" in doc, "startup contract must describe the Order API service"
    assert "service_healthy" in doc, "readiness ordering missing"
    assert "no host port" in doc or "unpublished" in doc, "private port rule missing"
    assert "one service, `app`" not in doc, "single-service statement still present"
    assert "order_api.main:app" in doc, "local two-process startup missing"
    assert "ORDER_API_URL" in doc and "ORDER_API_TOKEN" in doc
    assert "order_api_unavailable" in doc, "readiness failure surface missing"


def check_build_spec() -> None:
    doc = text("build")
    assert "R14" in doc, "R14 private Order API requirement missing"
    assert "AT20" in doc, "AT20 acceptance test missing"
    assert "cannot verify order existence" not in doc, "stale capability statement"
    assert "private Order API" in doc.lower() or "private order api" in doc.lower()
    assert "not reachable from the browser" in doc.lower() or "unreachable" in doc.lower()


def check_architecture() -> None:
    doc = text("architecture")
    assert 'id="orderapi"' in doc, "architecture diagram lacks an Order API node"
    assert "<strong>2</strong> containers" in doc, "container fact not updated"
    assert "IT15" in doc, "architecture IT table lacks IT15"
    assert 'data-journey="J11"' in doc, "architecture lacks J11"
    assert "199" in doc, "architecture integration total stale"
    assert "11 complete Playwright journeys" in doc, "architecture journey count stale"


def check_no_single_service_claims() -> None:
    stale = re.compile(r"one service,? `app`|one container and worker|1</strong> container")
    offenders = []
    for key in ("implementation", "testing", "startup", "build", "provider", "architecture"):
        if stale.search(text(key)):
            offenders.append(DOCS[key].name)
    assert not offenders, f"single-service claims remain in {offenders}"


CHECKS = {
    "compose": lambda: check_compose(compose_config()),
    "dockerfile": check_dockerfile,
    "env-example": check_env_example,
    "implementation-contract": check_implementation_contract,
    "test-matrix": check_test_matrix,
    "testing-contract": check_testing_contract,
    "startup": check_startup,
    "build-spec": check_build_spec,
    "architecture": check_architecture,
    "no-single-service-claims": check_no_single_service_claims,
}


def main() -> int:
    failures = []
    for name, check in CHECKS.items():
        try:
            check()
        except AssertionError as exc:
            failures.append((name, str(exc)))
            print(f"FAIL {name}: {exc}")
        else:
            print(f"PASS {name}")
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
