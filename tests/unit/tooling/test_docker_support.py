"""The booking reset may only touch the dedicated deployment on this machine."""

from __future__ import annotations

import pytest

from tests.e2e import docker_support


@pytest.mark.parametrize(
    ("app_url", "published", "expected"),
    [
        ("http://127.0.0.1:8010", "127.0.0.1:8010", True),
        ("http://localhost:8010", "127.0.0.1:8010", True),
        ("http://127.0.0.1:8010", "0.0.0.0:8010", True),
        ("http://[::1]:8010", "[::]:8010", True),
        ("http://staging.example.com:8010", "127.0.0.1:8010", False),
        ("http://10.0.0.5:8010", "0.0.0.0:8010", False),
        ("http://127.0.0.1:9000", "127.0.0.1:8010", False),
        ("http://127.0.0.1:8010", "192.168.1.5:8010", False),
        ("http://127.0.0.1", "127.0.0.1:80", False),
        ("http://127.0.0.1:8010", "", False),
    ],
)
def test_an_origin_matches_only_this_machines_published_address(
    app_url, published, expected
) -> None:
    assert docker_support.is_published_at(app_url, published) is expected


@pytest.fixture
def docker_calls(monkeypatch):
    calls: list[tuple] = []

    def record(project, *args):
        calls.append((project, args))
        return "127.0.0.1:8010" if args[0] == "port" else ""

    monkeypatch.setattr(docker_support, "_compose", record)
    return calls


def test_a_remote_origin_is_refused_before_any_docker_call(
    monkeypatch, docker_calls
) -> None:
    monkeypatch.setenv("E2E_COMPOSE_PROJECT", "northstar-e2e")

    with pytest.raises(pytest.fail.Exception, match="not a loopback origin"):
        docker_support.reset_demo_bookings("http://staging.example.com:8010")

    assert docker_calls == []


def test_a_project_not_named_for_e2e_is_refused_before_any_docker_call(
    monkeypatch, docker_calls
) -> None:
    monkeypatch.setenv("E2E_COMPOSE_PROJECT", "northstar-demo")

    with pytest.raises(pytest.fail.Exception, match="dedicated deployment"):
        docker_support.reset_demo_bookings("http://127.0.0.1:8010")

    assert docker_calls == []


def test_an_origin_the_project_does_not_publish_is_not_reset(
    monkeypatch, docker_calls
) -> None:
    monkeypatch.setenv("E2E_COMPOSE_PROJECT", "northstar-e2e")

    with pytest.raises(pytest.fail.Exception, match="not the origin under test"):
        docker_support.reset_demo_bookings("http://127.0.0.1:9000")

    assert [args[0] for _, args in docker_calls] == ["port"]


def test_the_dedicated_deployments_own_origin_is_reset(
    monkeypatch, docker_calls
) -> None:
    monkeypatch.setenv("E2E_COMPOSE_PROJECT", "northstar-e2e")

    docker_support.reset_demo_bookings("http://127.0.0.1:8010")

    assert [args[0] for _, args in docker_calls] == ["port", "exec"]
    assert all(project == "northstar-e2e" for project, _ in docker_calls)
