"""Compose and nginx topology for the three-container deployment.

``web`` (nginx) is the only service with a host port: it serves the page and
forwards the API to the gateway. The gateway and the private Backend API are
reachable only on the internal network, each with its own health check, and each
starts only after the service behind it reports healthy. These assertions pin the
deployment contract that the in-process integration tests cannot prove.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB = "web"
GATEWAY = "gateway"
BACKEND = "backend-api"
GATEWAY_PORT = "8000"
BACKEND_API_PORT = "8001"
NGINX_CONF = (REPO_ROOT / "web" / "nginx.conf").read_text(encoding="utf-8")


def compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_compose_runs_the_web_proxy_the_gateway_and_the_private_backend_api() -> None:
    services = compose()["services"]

    assert set(services) == {WEB, GATEWAY, BACKEND}


def test_only_the_web_container_publishes_a_host_port() -> None:
    services = compose()["services"]

    assert "ports" in services[WEB]
    assert "ports" not in services[GATEWAY]
    assert "ports" not in services[BACKEND]
    assert services[GATEWAY]["expose"] == [GATEWAY_PORT]
    assert services[BACKEND]["expose"] == [BACKEND_API_PORT]


def test_each_service_builds_from_its_own_dockerfile() -> None:
    services = compose()["services"]

    for name, folder in ((WEB, "web"), (GATEWAY, "gateway"), (BACKEND, "backend_api")):
        dockerfile = services[name]["build"]["dockerfile"]
        assert dockerfile == f"{folder}/Dockerfile"
        assert (REPO_ROOT / dockerfile).is_file()


def test_each_service_waits_for_a_healthy_service_behind_it() -> None:
    services = compose()["services"]

    assert services[WEB]["depends_on"] == {GATEWAY: {"condition": "service_healthy"}}
    assert services[GATEWAY]["depends_on"] == {BACKEND: {"condition": "service_healthy"}}
    for name in (WEB, GATEWAY, BACKEND):
        assert services[name]["healthcheck"]["test"][-1].endswith(("/health", "/health', timeout=2).close()"))


def test_gateway_calls_the_backend_api_by_service_dns() -> None:
    gateway = compose()["services"][GATEWAY]

    assert gateway["environment"]["BACKEND_API_BASE_URL"] == f"http://{BACKEND}:{BACKEND_API_PORT}"


def test_dockerfiles_start_the_right_app_on_the_exposed_port() -> None:
    expected = {
        "gateway": ("gateway.main:app", GATEWAY_PORT),
        "backend_api": ("backend_api.main:app", BACKEND_API_PORT),
    }
    for folder, (target, port) in expected.items():
        text = (REPO_ROOT / folder / "Dockerfile").read_text(encoding="utf-8")
        assert target in text
        assert f'"--port", "{port}"' in text


def test_the_images_carry_only_their_own_packages() -> None:
    gateway = (REPO_ROOT / "gateway" / "Dockerfile").read_text(encoding="utf-8")
    backend_api = (REPO_ROOT / "backend_api" / "Dockerfile").read_text(encoding="utf-8")

    assert "gateway ./gateway" in gateway and "shared ./shared" in gateway
    assert "backend_api" not in gateway.replace("# Build from", "")
    assert "backend_api ./backend_api" in backend_api and "shared ./shared" in backend_api
    assert "gateway" not in backend_api


def test_nginx_forwards_only_the_api_and_health_to_the_gateway() -> None:
    proxied = re.findall(r"location ([^{]+)\{[^}]*proxy_pass (\S+);", NGINX_CONF)

    assert [(path.strip(), url) for path, url in proxied] == [
        ("/api/", f"http://{GATEWAY}:{GATEWAY_PORT}"),
        ("= /health", f"http://{GATEWAY}:{GATEWAY_PORT}"),
    ]
    assert BACKEND not in NGINX_CONF
    assert "/internal" not in NGINX_CONF


def test_nginx_serves_the_page_with_revalidation_and_denies_everything_else() -> None:
    assert re.search(r"location = / \{[^}]*try_files /index.html", NGINX_CONF)
    assert re.search(r"location /static/ \{[^}]*alias /usr/share/nginx/html/;", NGINX_CONF)
    assert NGINX_CONF.count('add_header Cache-Control "no-cache" always;') == 2
    assert re.search(r"location / \{\s*return 404;", NGINX_CONF)
    # The gateway enforces the audio limit; nginx must not reject a legal upload first.
    assert re.search(r"client_max_body_size 11m;", NGINX_CONF)


def test_the_page_markup_has_no_server_side_placeholder() -> None:
    page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert "__BOOTSTRAP" not in page
    assert 'data-i18n="actions.send"' in page
    assert ">Send<" not in page
