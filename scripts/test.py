#!/usr/bin/env python3
"""Portable test command runner.

One command interface for every test layer on Linux, macOS and Windows. It only
uses the Python standard library: no Make, Bash, PowerShell or task-runner
dependency is required.

Usage:
    python scripts/test.py unit
    python scripts/test.py integration
    python scripts/test.py e2e
    python scripts/test.py e2e-judge
    python scripts/test.py docker-e2e
    python scripts/test.py docker-e2e --compose
    python scripts/test.py mutation
    python scripts/test.py coverage
    python scripts/test.py all
    python scripts/test.py help

Passthrough arguments after ``--`` are appended to the layer's pytest command:

    python scripts/test.py unit -- -k localization -q

The runner resolves the repository root from this file's location, prints the
exact executed command, preserves child output, and returns the first failing
exit code. Configuration errors exit with code 2 before any child starts.
Secrets are never printed: the judge API key stays in the process environment.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_EXIT_CODE = 2
HEALTH_TIMEOUT_SECONDS = 120.0


class UsageError(Exception):
    """The command line does not name a supported layer or option."""


class ConfigError(Exception):
    """A selected layer is missing or has invalid configuration."""


@dataclass(frozen=True)
class LayerInfo:
    summary: str
    proves: str
    does_not_prove: str
    prerequisites: str
    reports: str


LAYER_INFO: dict[str, LayerInfo] = {
    "unit": LayerInfo(
        summary="Fast unit tests (tests/unit).",
        proves="Pure domain rules, validation and adapters against test doubles.",
        does_not_prove="HTTP wiring, persistence, browser behaviour, real providers.",
        prerequisites="Python development requirements only (pytest).",
        reports="reports/junit/unit.xml",
    ),
    "integration": LayerInfo(
        summary="Integration tests against real application boundaries (tests/integration).",
        proves="FastAPI routes, storage, error mapping and wiring with controlled HTTP doubles.",
        does_not_prove="A live provider, a deployed Docker stack, or real browser behaviour.",
        prerequisites="Python development requirements only (pytest).",
        reports="reports/junit/integration.xml",
    ),
    "e2e": LayerInfo(
        summary="Controlled-provider browser journeys (the ten browser journeys + confirmation retry, marker browser_e2e).",
        proves="The shipped page in a real browser: JavaScript, HTTP, storage, keyboard, mobile, media, retry and stale-state behaviour, against a controlled provider fake.",
        does_not_prove="A live LLM/STT/TTS provider or a deployed Docker system; never call this 'full E2E'.",
        prerequisites="pytest-playwright and the Chromium browser (python -m playwright install chromium). No Docker, no provider credentials.",
        reports="reports/junit/e2e.xml, reports/playwright/journeys/, reports/e2e-results.json",
    ),
    "e2e-judge": LayerInfo(
        summary="Controlled-provider semantic intent judge (tests/e2e/test_intent_judge.py, marker intent_judge).",
        proves="Whether responses follow the expected intent rule according to the configured judge model.",
        does_not_prove="Deterministic state or citation correctness; a passing judge never overrides a deterministic failure.",
        prerequisites="tests/e2e/.env.judge with E2E_JUDGE_BASE_URL and E2E_JUDGE_MODEL (copy .env.judge.example); Chromium installed.",
        reports="reports/junit/judge.xml, reports/playwright/judge/, reports/e2e-judge/",
    ),
    "docker-e2e": LayerInfo(
        summary="Real Docker E2E against a deployed stack and real provider (tests/e2e/test_docker_e2e.py, test_docker_flows.py and test_docker_scenarios.py, marker docker_e2e).",
        proves="The shipped UI and HTTP routes against a real deployment and a real provider, judged semantically.",
        does_not_prove="Production configuration, multi-worker behaviour or data retention policy.",
        prerequisites="tests/e2e/.env.docker with E2E_DOCKER_BASE_URL, a complete tests/e2e/.env.judge, a reachable deployment and judge. --compose starts the local Compose stack explicitly; it is never a fallback to the controlled-provider suite.",
        reports="reports/junit/docker.xml, reports/playwright/docker/, reports/e2e-docker/judge/",
    ),
    "mutation": LayerInfo(
        summary="Mutation testing over the configured mutmut scope (pyproject.toml [tool.mutmut]).",
        proves="Whether the selected tests detect changes to the mutated source.",
        does_not_prove="Integration, browser or provider behaviour outside the configured scope.",
        prerequisites="mutmut installed (not available on Windows); a clean source tree.",
        reports=".mutmut-cache, mutants/; inspect survivors with python -m mutmut results",
    ),
    "coverage": LayerInfo(
        summary="Unit and integration suites with branch coverage (gate 95%).",
        proves="Line and branch coverage of gateway/, backend_api/ and shared/ by the offline suites.",
        does_not_prove="Browser, Docker or real-provider paths; coverage is not correctness.",
        prerequisites="pytest-cov installed.",
        reports="reports/coverage.json, reports/coverage.xml, reports/junit/coverage.xml",
    ),
    "all": LayerInfo(
        summary="Offline checks in order: unit, integration, e2e, coverage. Stops on the first failure.",
        proves="The offline quality gates that need neither Docker nor provider credentials.",
        does_not_prove="Judge, mutation or real Docker E2E layers; run those explicitly.",
        prerequisites="Python development requirements and Chromium.",
        reports="Reports of each layer it runs.",
    ),
}

ALL_ORDER = ("unit", "integration", "e2e", "coverage")


def repo_root(script_path: Path | str) -> Path:
    """Resolve the repository root from the script location."""
    return Path(script_path).resolve().parents[1]


def parse_args(argv: Sequence[str]) -> tuple[str, list[str], list[str]]:
    """Split argv into (layer, runner options, passthrough arguments)."""
    if not argv:
        return "help", [], []
    layer, *rest = list(argv)
    if layer == "help":
        if rest:
            raise UsageError("'help' takes no arguments")
        return "help", [], []
    if layer not in LAYER_INFO:
        raise UsageError(
            f"unknown layer {layer!r}; expected one of: "
            + ", ".join(name for name in LAYER_INFO if name != "help")
        )
    if "--" in rest:
        separator = rest.index("--")
        options, passthrough = rest[:separator], rest[separator + 1 :]
    else:
        options, passthrough = rest, []
    if options and not (layer == "docker-e2e" and options == ["--compose"]):
        raise UsageError(
            f"unsupported option(s) for {layer}: {' '.join(options)}; "
            "pass test arguments after --"
        )
    return layer, options, passthrough


def pytest_command(
    targets: Sequence[str],
    passthrough: Sequence[str],
    *,
    junit: str,
    extra: Sequence[str] = (),
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        *targets,
        *extra,
        f"--junitxml={junit}",
        *passthrough,
    ]


def build_commands(
    layer: str, passthrough: Sequence[str], *, compose: bool = False
) -> list[list[str]]:
    """Return the child commands for a layer, in execution order."""
    if layer == "unit":
        return [
            pytest_command(["tests/unit"], passthrough, junit="reports/junit/unit.xml")
        ]
    if layer == "integration":
        return [
            pytest_command(
                ["tests/integration"],
                passthrough,
                junit="reports/junit/integration.xml",
            )
        ]
    if layer == "e2e":
        return [
            pytest_command(
                ["tests/e2e"],
                passthrough,
                junit="reports/junit/e2e.xml",
                extra=[
                    "-m",
                    "browser_e2e",
                    "--browser",
                    "chromium",
                    "--tracing",
                    "retain-on-failure",
                    "--screenshot",
                    "only-on-failure",
                    "--video",
                    "retain-on-failure",
                    "--output",
                    "reports/playwright/journeys",
                ],
            )
        ]
    if layer == "e2e-judge":
        return [
            pytest_command(
                ["tests/e2e/test_intent_judge.py"],
                passthrough,
                junit="reports/junit/judge.xml",
                extra=[
                    "-m",
                    "intent_judge",
                    "--browser",
                    "chromium",
                    "--output",
                    "reports/playwright/judge",
                ],
            )
        ]
    if layer == "docker-e2e":
        commands: list[list[str]] = []
        if compose:
            commands.append(["docker", "compose", "up", "--build", "-d"])
        commands.append(
            pytest_command(
                [
                    "tests/e2e/test_docker_e2e.py",
                    "tests/e2e/test_docker_flows.py",
                    "tests/e2e/test_docker_scenarios.py",
                ],
                passthrough,
                junit="reports/junit/docker.xml",
                extra=[
                    "-m",
                    "docker_e2e",
                    "--browser",
                    "chromium",
                    "--tracing",
                    "retain-on-failure",
                    "--screenshot",
                    "only-on-failure",
                    "--video",
                    "retain-on-failure",
                    "--output",
                    "reports/playwright/docker",
                ],
            )
        )
        return commands
    if layer == "mutation":
        return [[sys.executable, "-m", "mutmut", "run"]]
    if layer == "coverage":
        return [
            pytest_command(
                ["tests/unit", "tests/integration"],
                passthrough,
                junit="reports/junit/coverage.xml",
                extra=[
                    "--cov=gateway",
                    "--cov=backend_api",
                    "--cov=shared",
                    "--cov-branch",
                    "--cov-context=test",
                    "--cov-report=json:reports/coverage.json",
                    "--cov-report=term-missing",
                    "--cov-report=xml:reports/coverage.xml",
                ],
            )
        ]
    if layer == "all":
        commands = []
        for name in ALL_ORDER:
            commands.extend(build_commands(name, passthrough))
        return commands
    raise UsageError(f"unknown layer {layer!r}")


def _import_module(root: Path, name: str):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return importlib.import_module(name)


def judge_config_error(root: Path) -> str | None:
    """Validate tests/e2e/.env.judge with the suite's own loader."""
    try:
        module = _import_module(root, "tests.e2e.judge_config")
        module.load_config(root / "tests/e2e" / ".env.judge")
    except (RuntimeError, ValueError, OSError, ImportError) as exc:
        return str(exc) or exc.__class__.__name__
    return None


def load_docker_config(root: Path):
    """Load the real Docker E2E configuration with the suite's own loader."""
    module = _import_module(root, "tests.e2e.docker_config")
    return module.load_config(root)


def probe_docker(config) -> None:
    """Fail before browser launch when the deployment or judge is unavailable."""
    module = importlib.import_module("tests.e2e.docker_config")
    module.probe(config)


def run_command(command: Sequence[str], root: Path) -> int:
    """Run one child process with inherited output and return its exit code."""
    return subprocess.run(list(command), cwd=str(root), check=False).returncode


def wait_for_health(url: str, timeout_seconds: float = HEALTH_TIMEOUT_SECONDS) -> None:
    """Poll the deployment health endpoint after an explicit Compose start."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if response.status == 200 and payload == {"status": "ok"}:
                return
        except (OSError, urllib.error.URLError, TimeoutError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise ConfigError(
                f"Docker deployment did not become healthy at {url} within "
                f"{timeout_seconds:.0f}s after 'docker compose up'"
            )
        time.sleep(2)


def _emit(stream, text: str = "") -> None:
    print(text, file=stream)


def print_help(stream) -> None:
    _emit(
        stream,
        "Portable test command runner — python scripts/test.py <layer> [-- args]",
    )
    _emit(stream, "")
    _emit(stream, "Layers:")
    for name, info in LAYER_INFO.items():
        _emit(stream, f"  {name:<12} {info.summary}")
        _emit(stream, f"  {'':<12} proves: {info.proves}")
        _emit(stream, f"  {'':<12} does not prove: {info.does_not_prove}")
        _emit(stream, f"  {'':<12} prerequisites: {info.prerequisites}")
        _emit(stream, f"  {'':<12} reports: {info.reports}")
        _emit(stream, "")
    _emit(stream, "Runner options:")
    _emit(
        stream,
        "  docker-e2e --compose   start the local Compose stack before the real Docker suite.",
    )
    _emit(
        stream,
        "                         Without it the runner only verifies the configured deployment.",
    )
    _emit(
        stream,
        "  -- <args>              append args to the layer's pytest command, e.g. unit -- -k localization -q",
    )
    _emit(stream, "")
    _emit(
        stream,
        "Exit codes: the first failing child's exit code; 2 for usage or configuration errors.",
    )
    _emit(
        stream,
        "Secrets are never printed; the judge API key stays in the process environment.",
    )


def _fail_config(stream, message: str) -> int:
    _emit(stream, "")
    _emit(stream, f"CONFIGURATION ERROR: {message}")
    _emit(stream, "RESULT: FAIL (nothing was executed)")
    return CONFIG_EXIT_CODE


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | str | None = None,
    run: Callable[[Sequence[str], Path], int] | None = None,
    probe: Callable[[object], None] | None = None,
    which: Callable[[str], str | None] | None = None,
    wait_health: Callable[[str, float], None] | None = None,
    stream=None,
) -> int:
    """Execute one layer and return the first failing exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    root = Path(root) if root is not None else REPO_ROOT
    run = run or run_command
    probe = probe or probe_docker
    which = which or shutil.which
    wait_health = wait_health or wait_for_health
    stream = stream or sys.stdout

    try:
        layer, options, passthrough = parse_args(arguments)
    except UsageError as exc:
        _emit(stream, f"USAGE ERROR: {exc}")
        _emit(stream, "Run 'python scripts/test.py help' for the layer list.")
        return CONFIG_EXIT_CODE

    if layer == "help":
        print_help(stream)
        return 0

    for directory in ("reports/junit", "reports/playwright", "reports/e2e"):
        (root / directory).mkdir(parents=True, exist_ok=True)

    compose = "--compose" in options
    docker_config = None
    if layer == "e2e-judge":
        error = judge_config_error(root)
        if error:
            return _fail_config(stream, f"e2e-judge — {error}")
    elif layer == "docker-e2e":
        try:
            docker_config = load_docker_config(root)
            probe(docker_config)
        except (ConfigError, RuntimeError, ValueError, OSError, ImportError) as exc:
            return _fail_config(stream, f"docker-e2e — {exc}")
        if compose and not which("docker"):
            return _fail_config(
                stream,
                "docker-e2e --compose — 'docker' was not found on PATH; "
                "start the deployment yourself and omit --compose",
            )

    commands = build_commands(layer, passthrough, compose=compose)

    _emit(stream, "=" * 78)
    _emit(stream, f"LAYER: {layer} — {LAYER_INFO[layer].summary}")
    _emit(stream, "=" * 78)

    for index, command in enumerate(commands, start=1):
        _emit(stream, "")
        _emit(stream, f"[{index}/{len(commands)}] COMMAND: {' '.join(command)}")
        _emit(stream, "-" * 78)
        try:
            if (
                compose
                and layer == "docker-e2e"
                and command[:2] == ["docker", "compose"]
            ):
                code = run(command, root)
                _emit(stream, "-" * 78)
                _emit(stream, f"EXIT CODE: {code}")
                if code:
                    _emit(
                        stream,
                        f"RESULT: FAIL (docker-e2e stopped at step {index}/{len(commands)})",
                    )
                    return code
                wait_health(f"{docker_config.app_url}/health", HEALTH_TIMEOUT_SECONDS)
                _emit(stream, f"HEALTH: {docker_config.app_url}/health is ready")
                continue
            code = run(command, root)
        except (ConfigError, RuntimeError, OSError) as exc:
            _emit(stream, f"ERROR: {exc}")
            _emit(
                stream,
                f"RESULT: FAIL ({layer} stopped at step {index}/{len(commands)})",
            )
            return CONFIG_EXIT_CODE
        _emit(stream, "-" * 78)
        _emit(stream, f"EXIT CODE: {code}")
        if code:
            _emit(stream, f"RESULT: FAIL (stopped at step {index}/{len(commands)})")
            return code

    _emit(stream, "")
    _emit(stream, f"RESULT: PASS — {layer} ({len(commands)} command(s), exit 0)")
    _emit(stream, f"REPORTS: {LAYER_INFO[layer].reports}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
