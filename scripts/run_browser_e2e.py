#!/usr/bin/env python3
"""Portable controlled-provider browser E2E gate.

Runs the real-browser the ten browser journeys (+ confirmation retry) journeys against the local Uvicorn
fixture with the controlled provider fake. This suite proves the browser
workflow (JavaScript, HTTP, storage, keyboard, mobile, media, retry, timeout,
stale-state); it never proves a live provider or a deployed Docker system.

Writes non-secret run metadata to reports/e2e-controlled/run-config.json and
JUnit output to reports/e2e-controlled/controlled.xml.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RUN_CONFIG = {
    "environment_type": "controlled-provider",
    "provider_mode": "controlled-fake",
    "application_url": "local uvicorn fixture (loopback, port 0)",
    "judge_model": None,
    "prompt_version": None,
    "judge_note": "no LLM judge in this suite; deterministic assertions are authoritative",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default="chromium", help="playwright browser (default: chromium)")
    args = parser.parse_args()
    evidence = ROOT / "reports" / "e2e-controlled"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "run-config.json").write_text(
        json.dumps(RUN_CONFIG, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    command = [
        sys.executable, "-m", "pytest", "tests/e2e", "-m", "browser_e2e",
        "--browser", args.browser,
        "--tracing", "retain-on-failure", "--screenshot", "only-on-failure",
        "--video", "retain-on-failure", "--output", "reports/e2e-controlled/playwright",
        "--junitxml=reports/e2e-controlled/controlled.xml",
    ]
    completed = subprocess.run(command, cwd=ROOT, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
