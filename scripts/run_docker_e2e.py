#!/usr/bin/env python3
"""Portable real-Docker E2E gate.

Usage: python scripts/run_docker_e2e.py
The application and judge must be isolated deployments configured in ignored files.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.e2e.docker_config import load_config, probe  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-deterministic", action="store_true", help="run fake-provider the ten browser journeys first")
    args = parser.parse_args()
    config = load_config(ROOT)
    probe(config)
    evidence = ROOT / "reports/e2e-docker"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "run-config.json").write_text(
        json.dumps(
            {
                "environment_type": "real-docker",
                "provider_mode": "real-provider",
                "application_origin": config.app_url,
                "judge_model": config.judge.model,
                "prompt_version": config.judge.prompt_version,
                "deterministic_note": "deterministic assertions precede the judge and stay authoritative; "
                "a passing judge never overrides a deterministic failure",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    if args.with_deterministic:
        commands.append([sys.executable, "-m", "pytest", "tests/e2e", "-q", "-m", "browser_e2e", "--browser", "chromium", "--junitxml=reports/e2e-docker/deterministic.xml"])
    commands.append([sys.executable, "-m", "pytest", "tests/e2e/test_docker_e2e.py", "--browser", "chromium", "--tracing", "retain-on-failure", "--screenshot", "only-on-failure", "--video", "retain-on-failure", "--output", "reports/e2e-docker/playwright", "--junitxml=reports/e2e-docker/docker.xml"])
    for command in commands:
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
