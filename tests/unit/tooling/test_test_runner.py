"""Unit tests for the portable test runner.

``scripts/test.py`` is loaded by path so the tests exercise the real module
without adding ``scripts/`` to ``sys.path``. Process execution is the external
boundary: ``run`` is a callable that starts a child process, and every test
injects a recorder instead of launching suites. Docker reachability is likewise
an external boundary and is injected where a test must pass validation without
a live deployment.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = REPO_ROOT / "scripts" / "test.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("orbio_test_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["orbio_test_runner"] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


class Recorder:
    """Records the command lines the runner would execute."""

    def __init__(self, codes: list[int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.codes = list(codes or [])

    def __call__(self, command: list[str], root: Path) -> int:
        self.calls.append(list(command))
        return self.codes.pop(0) if self.codes else 0


def _main(argv, *, root=REPO_ROOT, run=None, probe=None, which=None):
    output = io.StringIO()
    code = runner.main(
        argv,
        root=root,
        run=run or Recorder(),
        probe=probe or (lambda config: None),
        which=which or (lambda name: "/usr/bin/docker"),
        wait_health=lambda url, timeout: None,
        stream=output,
    )
    return code, output.getvalue()


def _write_dotenv(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8"
    )


def _judge_env() -> dict[str, str]:
    return {
        "E2E_JUDGE_BASE_URL": "http://127.0.0.1:8888/v1",
        "E2E_JUDGE_MODEL": "local-judge",
    }


def _docker_env() -> dict[str, str]:
    return {
        "E2E_DOCKER_BASE_URL": "http://127.0.0.1:8000",
        "E2E_JUDGE_BASE_URL": "http://127.0.0.1:8888/v1",
        "E2E_JUDGE_MODEL": "local-judge",
        "E2E_JUDGE_TIMEOUT_SECONDS": "30",
        "E2E_JUDGE_MAX_TOKENS": "256",
        "E2E_JUDGE_TEMPERATURE": "0",
        "E2E_JUDGE_PROMPT_VERSION": "intent-judge-v1",
    }


@pytest.fixture(autouse=True)
def _no_judge_environment(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("E2E_JUDGE_"):
            monkeypatch.delenv(key)


# --------------------------------------------------------------------------- #
# Command parsing and passthrough arguments
# --------------------------------------------------------------------------- #


def test_parse_splits_passthrough_arguments():
    assert runner.parse_args(["unit", "--", "-k", "localization", "-q"]) == (
        "unit",
        [],
        ["-k", "localization", "-q"],
    )


def test_parse_without_passthrough_returns_empty_lists():
    assert runner.parse_args(["e2e"]) == ("e2e", [], [])


def test_parse_keeps_passthrough_options_intact():
    assert runner.parse_args(["e2e", "--", "--browser", "chromium", "-q"]) == (
        "e2e",
        [],
        ["--browser", "chromium", "-q"],
    )


def test_parse_rejects_unknown_layer():
    with pytest.raises(runner.UsageError) as excinfo:
        runner.parse_args(["smoke"])
    assert "smoke" in str(excinfo.value)
    assert "unit" in str(excinfo.value)


def test_parse_rejects_unknown_option_before_separator():
    with pytest.raises(runner.UsageError):
        runner.parse_args(["docker-e2e", "--compose=false"])


# --------------------------------------------------------------------------- #
# Root resolution and help
# --------------------------------------------------------------------------- #


def test_repo_root_is_the_parent_of_the_scripts_directory(tmp_path):
    script = tmp_path / "checkout" / "scripts" / "test.py"
    assert runner.repo_root(script) == script.parent.parent.resolve()


def test_help_lists_every_layer_with_scope_and_limits():
    recorder = Recorder()
    code, output = _main(["help"], run=recorder)
    assert code == 0
    assert recorder.calls == []
    for layer in ("unit", "integration", "e2e", "e2e-judge", "docker-e2e", "mutation", "coverage", "all"):
        assert layer in output
    assert "does not prove" in output.lower()
    assert "reports/" in output


def test_no_arguments_prints_help():
    code, output = _main([], run=Recorder())
    assert code == 0
    assert "python scripts/test.py" in output


# --------------------------------------------------------------------------- #
# Command construction and passthrough
# --------------------------------------------------------------------------- #


def test_unit_targets_tests_unit_with_junit_report():
    recorder = Recorder()
    code, output = _main(["unit"], run=recorder)
    assert code == 0
    command = recorder.calls[0]
    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert "tests/unit" in command
    assert "reports/junit/unit.xml" in " ".join(command)
    assert "EXIT CODE: 0" in output
    assert "RESULT: PASS" in output


def test_integration_targets_tests_integration():
    recorder = Recorder()
    _main(["integration"], run=recorder)
    assert "tests/integration" in recorder.calls[0]


def test_passthrough_arguments_are_appended_to_the_command():
    recorder = Recorder()
    _main(["unit", "--", "-k", "localization", "-q"], run=recorder)
    command = recorder.calls[0]
    assert command[-3:] == ["-k", "localization", "-q"]


def test_e2e_selects_the_controlled_provider_browser_marker():
    recorder = Recorder()
    _main(["e2e"], run=recorder)
    command = recorder.calls[0]
    assert "tests/e2e" in command
    assert "browser_e2e" in command
    assert "intent_judge" not in " ".join(command)
    assert "docker_e2e" not in " ".join(command)
    assert "reports/playwright/journeys" in " ".join(command)


def test_mutation_invokes_mutmut_as_a_module():
    recorder = Recorder(codes=[4])
    code, output = _main(["mutation"], run=recorder)
    assert recorder.calls == [[sys.executable, "-m", "mutmut", "run"]]
    assert code == 4
    assert "RESULT: FAIL" in output


def test_coverage_runs_both_offline_suites_with_reports():
    recorder = Recorder()
    _main(["coverage"], run=recorder)
    command = recorder.calls[0]
    joined = " ".join(command)
    assert "tests/unit" in command and "tests/integration" in command
    assert "--cov=gateway" in command and "--cov=backend" in command
    assert "reports/coverage.json" in joined
    assert "reports/coverage.xml" in joined


def test_reports_directories_are_created_before_running(tmp_path):
    recorder = Recorder()
    _main(["unit"], root=tmp_path, run=recorder)
    assert (tmp_path / "reports" / "junit").is_dir()


# --------------------------------------------------------------------------- #
# Exit-code propagation
# --------------------------------------------------------------------------- #


def test_first_failing_exit_code_is_returned():
    code, output = _main(["unit"], run=Recorder(codes=[3]))
    assert code == 3
    assert "RESULT: FAIL" in output


def test_all_runs_offline_layers_in_documented_order_and_stops_on_failure():
    recorder = Recorder(codes=[0, 5, 0, 0])
    code, _ = _main(["all"], run=recorder)
    assert code == 5
    assert len(recorder.calls) == 2
    assert "tests/unit" in recorder.calls[0]
    assert "tests/integration" in recorder.calls[1]


def test_all_completes_every_offline_layer_when_green():
    recorder = Recorder()
    code, _ = _main(["all"], run=recorder)
    assert code == 0
    targets = [" ".join(call) for call in recorder.calls]
    assert len(targets) == 4
    assert "tests/unit" in targets[0]
    assert "tests/integration" in targets[1]
    assert "browser_e2e" in targets[2]
    assert "--cov=gateway" in targets[3] and "--cov=backend" in targets[3]


# --------------------------------------------------------------------------- #
# Missing or invalid judge configuration
# --------------------------------------------------------------------------- #


def test_e2e_judge_fails_early_without_configuration(tmp_path):
    recorder = Recorder()
    code, output = _main(["e2e-judge"], root=tmp_path, run=recorder)
    assert code == runner.CONFIG_EXIT_CODE
    assert recorder.calls == []
    assert "tests/e2e/.env.judge" in output
    assert "RESULT: FAIL" in output


def test_e2e_judge_fails_early_on_invalid_configuration(tmp_path):
    _write_dotenv(
        tmp_path / "tests/e2e/.env.judge",
        {"E2E_JUDGE_BASE_URL": "not-a-url", "E2E_JUDGE_MODEL": "local-judge"},
    )
    recorder = Recorder()
    code, output = _main(["e2e-judge"], root=tmp_path, run=recorder)
    assert code == runner.CONFIG_EXIT_CODE
    assert recorder.calls == []
    assert "absolute http(s) URL" in output


def test_e2e_judge_runs_the_judge_suite_when_configured(tmp_path):
    _write_dotenv(tmp_path / "tests/e2e/.env.judge", _judge_env())
    recorder = Recorder()
    code, _ = _main(["e2e-judge"], root=tmp_path, run=recorder)
    assert code == 0
    joined = " ".join(recorder.calls[0])
    assert "tests/e2e/test_intent_judge.py" in joined
    assert "intent_judge" in joined
    assert "reports/junit/judge.xml" in joined


# --------------------------------------------------------------------------- #
# Docker E2E configuration and compose behaviour
# --------------------------------------------------------------------------- #


def test_docker_e2e_fails_early_without_docker_configuration(tmp_path):
    recorder = Recorder()
    code, output = _main(["docker-e2e"], root=tmp_path, run=recorder)
    assert code == runner.CONFIG_EXIT_CODE
    assert recorder.calls == []
    assert "E2E_DOCKER_BASE_URL" in output


def test_docker_e2e_fails_early_when_the_deployment_is_unreachable(tmp_path):
    _write_dotenv(tmp_path / "tests/e2e/.env.docker", _docker_env())
    _write_dotenv(tmp_path / "tests/e2e/.env.judge", _docker_env())

    def unreachable(config):
        raise RuntimeError("Docker application is unavailable at http://127.0.0.1:8000/health")

    recorder = Recorder()
    code, output = _main(["docker-e2e"], root=tmp_path, run=recorder, probe=unreachable)
    assert code == runner.CONFIG_EXIT_CODE
    assert recorder.calls == []
    assert "unavailable" in output


def test_docker_e2e_runs_only_the_real_docker_suite(tmp_path):
    _write_dotenv(tmp_path / "tests/e2e/.env.docker", _docker_env())
    _write_dotenv(tmp_path / "tests/e2e/.env.judge", _docker_env())
    recorder = Recorder()
    code, _ = _main(["docker-e2e"], root=tmp_path, run=recorder)
    assert code == 0
    command = recorder.calls[0]
    joined = " ".join(command)
    assert "tests/e2e/test_docker_e2e.py" in command
    assert "docker_e2e" in joined
    assert "browser_e2e" not in joined
    assert "tests/support/fakes" not in joined


def test_docker_e2e_does_not_start_compose_by_default(tmp_path):
    _write_dotenv(tmp_path / "tests/e2e/.env.docker", _docker_env())
    _write_dotenv(tmp_path / "tests/e2e/.env.judge", _docker_env())
    recorder = Recorder()
    _main(["docker-e2e"], root=tmp_path, run=recorder)
    assert len(recorder.calls) == 1
    assert "compose" not in " ".join(recorder.calls[0])


def test_docker_e2e_starts_compose_only_when_explicitly_requested(tmp_path):
    _write_dotenv(tmp_path / "tests/e2e/.env.docker", _docker_env())
    _write_dotenv(tmp_path / "tests/e2e/.env.judge", _docker_env())
    recorder = Recorder()
    code, _ = _main(["docker-e2e", "--compose"], root=tmp_path, run=recorder)
    assert code == 0
    assert len(recorder.calls) == 2
    assert recorder.calls[0][:2] == ["docker", "compose"]
    assert "up" in recorder.calls[0]
    assert "tests/e2e/test_docker_e2e.py" in recorder.calls[1]


def test_compose_start_failure_stops_before_the_docker_suite(tmp_path):
    _write_dotenv(tmp_path / "tests/e2e/.env.docker", _docker_env())
    _write_dotenv(tmp_path / "tests/e2e/.env.judge", _docker_env())
    recorder = Recorder(codes=[1])
    code, _ = _main(["docker-e2e", "--compose"], root=tmp_path, run=recorder)
    assert code == 1
    assert len(recorder.calls) == 1
