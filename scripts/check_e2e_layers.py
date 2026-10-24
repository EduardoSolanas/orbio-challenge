#!/usr/bin/env python3
"""Gate the controlled-provider vs real-Docker E2E evidence separation (ORB-53).

Fails closed when:
- a controlled-provider report is not labelled
  environment_type=controlled-provider / provider_mode=controlled-fake;
- controlled evidence claims live Docker/provider coverage;
- any scanned test or report uses the unqualified name "full E2E" for the
  controlled-provider suite (qualified forms such as
  "full E2E (controlled-provider ...)" are allowed);
- a scanned JSON evidence file carries a secret value;
- --require-docker is passed (the ORB-51 real-Docker gate) without real-Docker
  run-config and JUnit evidence, or with a run-config that does not declare
  environment_type=real-docker / provider_mode=real-provider.

This checker only inspects evidence. It never runs tests or modifies source.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

CONTROLLED_ENVIRONMENT = "controlled-provider"
CONTROLLED_PROVIDER = "controlled-fake"
DOCKER_ENVIRONMENT = "real-docker"
DOCKER_PROVIDER = "real-provider"

# "full E2E" (any separator/case) is forbidden unless the same line carries an
# explicit layer qualification.
FULL_E2E = re.compile(r"full[\s_\-]*e2e", re.IGNORECASE)
QUALIFIED = re.compile(r"controlled-provider|controlled provider|real-docker|real docker", re.IGNORECASE)

# Live-coverage claims that must never appear in controlled-provider evidence.
LIVE_CLAIM = re.compile(
    r"real-docker|real docker|live.?docker|live.?provider|docker_e2e|live system",
    re.IGNORECASE,
)

SECRET_KEYS = {"api_key", "apikey", "secret", "authorization", "e2e_judge_api_key"}
SECRET_ALLOWLIST = {"", "no-key-required", "none", "null"}


def load_json(path: Path, label: str, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"{label}: missing file {path}")
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{label}: unreadable JSON ({exc})")
    return None


def check_controlled(payload: Any, label: str, errors: list[str]) -> None:
    if not isinstance(payload, dict):
        errors.append(f"{label}: top level must be an object")
        return
    if payload.get("environment_type") != CONTROLLED_ENVIRONMENT:
        errors.append(
            f"{label}: environment_type must be {CONTROLLED_ENVIRONMENT!r}, "
            f"got {payload.get('environment_type')!r}"
        )
    provider = payload.get("provider_mode")
    if not isinstance(provider, str) or not provider.startswith("controlled"):
        errors.append(
            f"{label}: provider_mode must be a controlled-provider value, got {provider!r}"
        )
    text = json.dumps(payload, sort_keys=True)
    claim = LIVE_CLAIM.search(text)
    if claim:
        errors.append(
            f"{label}: controlled-provider evidence claims live coverage ({claim.group(0)!r})"
        )


def check_full_e2e_naming(path: Path, errors: list[str]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"sources: unreadable {path} ({exc})")
        return
    for lineno, line in enumerate(text.splitlines(), start=1):
        if FULL_E2E.search(line) and not QUALIFIED.search(line):
            errors.append(
                f"sources: unqualified 'full E2E' in {path.name}:{lineno}"
            )


def check_no_secrets(payload: Any, label: str, errors: list[str]) -> None:
    stack: list[Any] = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and key.lower() in SECRET_KEYS:
                    if isinstance(item, str) and item.strip().lower() not in SECRET_ALLOWLIST:
                        errors.append(f"{label}: secret value under key {key!r}")
                    elif item not in (None, "") and not (
                        isinstance(item, str) and item.strip().lower() in SECRET_ALLOWLIST
                    ):
                        errors.append(f"{label}: secret value under key {key!r}")
                else:
                    stack.append(item)
        elif isinstance(value, list):
            stack.extend(value)


def check_report_file(path: Path, errors: list[str]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"report: unreadable {path} ({exc})")
        return
    for lineno, line in enumerate(text.splitlines(), start=1):
        if FULL_E2E.search(line) and not QUALIFIED.search(line):
            errors.append(
                f"report: unqualified 'full E2E' in {path.name}:{lineno}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gate controlled-provider vs real-Docker E2E evidence."
    )
    parser.add_argument("--collection", required=True, help="pytest collection JSON")
    parser.add_argument("--results", required=True, help="pytest results JSON")
    parser.add_argument("--sources-dir", required=True, help="directory of e2e test sources to scan")
    parser.add_argument("--controlled-config", default=None, help="controlled run-config JSON")
    parser.add_argument("--require-docker", action="store_true", help="also require real-Docker evidence")
    parser.add_argument("--docker-config", default=None, help="real-Docker run-config JSON")
    parser.add_argument("--docker-xml", default=None, help="real-Docker JUnit XML")
    args = parser.parse_args(argv)

    errors: list[str] = []

    collection = load_json(Path(args.collection), "collection", errors)
    results = load_json(Path(args.results), "results", errors)
    if collection is not None:
        check_controlled(collection, "collection", errors)
    if results is not None:
        check_controlled(results, "results", errors)

    sources = Path(args.sources_dir)
    if sources.is_dir():
        for path in sorted(sources.glob("*.py")) + sorted(sources.glob("*.json")):
            check_full_e2e_naming(path, errors)
    for name in ("collection", "results"):
        check_report_file(Path(args.collection if name == "collection" else args.results), errors)

    if args.controlled_config:
        config = load_json(Path(args.controlled_config), "controlled-config", errors)
        if config is not None:
            if not isinstance(config, dict) or config.get("environment_type") != CONTROLLED_ENVIRONMENT:
                errors.append("controlled-config: environment_type must be 'controlled-provider'")
            check_no_secrets(config, "controlled-config", errors)

    if args.require_docker:
        if not args.docker_config or not args.docker_xml:
            errors.append("docker: --require-docker needs --docker-config and --docker-xml")
        else:
            docker_config = load_json(Path(args.docker_config), "docker-config", errors)
            if docker_config is not None:
                if not isinstance(docker_config, dict):
                    errors.append("docker-config: top level must be an object")
                else:
                    if docker_config.get("environment_type") != DOCKER_ENVIRONMENT:
                        errors.append("docker-config: environment_type must be 'real-docker'")
                    if docker_config.get("provider_mode") != DOCKER_PROVIDER:
                        errors.append("docker-config: provider_mode must be 'real-provider'")
                    check_no_secrets(docker_config, "docker-config", errors)
            xml_path = Path(args.docker_xml)
            if not xml_path.is_file():
                errors.append(f"docker-xml: missing file {xml_path}")
    elif args.docker_config:
        docker_config = load_json(Path(args.docker_config), "docker-config", errors)
        if isinstance(docker_config, dict):
            check_no_secrets(docker_config, "docker-config", errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"E2E layers gate FAILED ({len(errors)} error(s))", file=sys.stderr)
        return 1
    print("E2E layers gate passed: controlled-provider and real-Docker evidence separated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
