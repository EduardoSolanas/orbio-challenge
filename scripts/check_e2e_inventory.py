#!/usr/bin/env python3
"""Gate the Playwright E2E evidence against the browser journey manifest.

Reads the journey manifest, the collection report and the results report. Fails closed on missing, duplicated, skipped, xfailed, xpassed,
deselected, unexecuted, failed, rerun, checkpoint-less or stale-revision evidence.

This checker only inspects evidence. It never runs tests, creates files or
modifies source.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from evidence_revision import gated_input_revision

JOURNEY_IDS = (
    "complete_intake",
    "rejected_review_recovery",
    "resume_after_restart",
    "failed_request_retry",
    "two_tab_conflict",
    "voice_success",
    "voice_failure",
    "policy_questions",
    "keyboard_mobile",
    "stale_voice_results",
)
FORBIDDEN_OUTCOMES = {
    "skipped",
    "xfailed",
    "xpassed",
    "failed",
    "error",
    "rerun",
    "deselected",
    "not run",
}
FORBIDDEN_MARKERS = ("skip", "skipif", "xfail")


def load_json(path: Path, label: str, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"{label}: missing file {path}")
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{label}: unreadable JSON ({exc})")
    return None


def require_string(value: Any, field: str, label: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label}: {field} must be a non-empty string")
        return ""
    return value


def require_string_list(
    value: Any, field: str, label: str, errors: list[str]
) -> list[str]:
    if not isinstance(value, list) or not value:
        errors.append(f"{label}: {field} must be a non-empty list")
        return []
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{label}: {field} contains a non-string or blank entry")
            continue
        result.append(item)
    return result


def nodeid_of(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        value = entry.get("nodeid")
        return value if isinstance(value, str) else None
    return None


def check_manifest(data: Any, errors: list[str]) -> tuple[str, dict[str, dict]]:
    label = "manifest"
    if not isinstance(data, dict):
        errors.append(f"{label}: top level must be an object")
        return "", {}
    revision = require_string(
        data.get("source_revision"), "source_revision", label, errors
    )
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append(f"{label}: cases must be a non-empty list")
        return revision, {}
    by_id: dict[str, dict] = {}
    by_nodeid: dict[str, str] = {}
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"{label}: case {index} must be an object")
            continue
        case_id = require_string(case.get("id"), f"cases[{index}].id", label, errors)
        if case_id and case_id not in JOURNEY_IDS:
            errors.append(f"{label}: unknown journey id {case_id}")
        if case_id in by_id:
            errors.append(f"{label}: duplicate journey id {case_id}")
        node = require_string(
            case.get("nodeid"), f"cases[{index}].nodeid", label, errors
        )
        if node and node in by_nodeid:
            errors.append(
                f"{label}: duplicate nodeid {node} for {case_id} and {by_nodeid[node]}"
            )
        if node:
            by_nodeid[node] = case_id
        require_string_list(
            case.get("requirement_ids"),
            f"cases[{index}].requirement_ids",
            label,
            errors,
        )
        require_string_list(
            case.get("checkpoints"), f"cases[{index}].checkpoints", label, errors
        )
        if case_id:
            by_id[case_id] = case
    missing = sorted(set(JOURNEY_IDS) - set(by_id))
    if missing:
        errors.append(f"{label}: missing journeys {missing}")
    return revision, by_id


def check_collection(data: Any, manifest_nodeids: set[str], errors: list[str]) -> str:
    label = "collection"
    if not isinstance(data, dict):
        errors.append(f"{label}: top level must be an object")
        return ""
    revision = require_string(
        data.get("source_revision"), "source_revision", label, errors
    )
    collected = data.get("collected")
    if not isinstance(collected, list) or not collected:
        errors.append(f"{label}: collected must be a non-empty list")
        collected = []
    deselected = data.get("deselected") or []
    if not isinstance(deselected, list):
        errors.append(f"{label}: deselected must be a list")
        deselected = []
    if deselected:
        errors.append(f"{label}: deselected journeys are forbidden: {deselected}")
    errors_list = data.get("errors") or []
    if errors_list:
        errors.append(f"{label}: collection errors are forbidden: {errors_list}")
    seen: list[str] = []
    for entry in collected:
        node = nodeid_of(entry)
        if node is None:
            errors.append(f"{label}: collected entry without a nodeid: {entry!r}")
            continue
        if node in seen:
            errors.append(f"{label}: duplicate collected nodeid {node}")
        seen.append(node)
        if isinstance(entry, dict):
            outcome = entry.get("outcome", "collected")
            if outcome != "collected":
                errors.append(f"{label}: {node} collected with outcome {outcome!r}")
            markers = entry.get("markers") or []
            for marker in markers:
                name = marker if isinstance(marker, str) else str(marker)
                if any(forbidden in name for forbidden in FORBIDDEN_MARKERS):
                    errors.append(f"{label}: {node} carries forbidden marker {name}")
    collected_set = set(seen)
    missing = sorted(manifest_nodeids - collected_set)
    extra = sorted(collected_set - manifest_nodeids)
    if missing:
        errors.append(f"{label}: manifest journeys were not collected: {missing}")
    if extra:
        errors.append(f"{label}: collected nodeids absent from manifest: {extra}")
    return revision


def check_results(data: Any, manifest: dict[str, dict], errors: list[str]) -> str:
    label = "results"
    if not isinstance(data, dict):
        errors.append(f"{label}: top level must be an object")
        return ""
    revision = require_string(
        data.get("source_revision"), "source_revision", label, errors
    )
    outcomes = data.get("outcomes")
    if not isinstance(outcomes, list) or not outcomes:
        errors.append(f"{label}: outcomes must be a non-empty list")
        return revision
    seen: list[str] = []
    manifest_by_nodeid = {
        case["nodeid"]: case for case in manifest.values() if case.get("nodeid")
    }
    for index, entry in enumerate(outcomes):
        if not isinstance(entry, dict):
            errors.append(f"{label}: outcome {index} must be an object")
            continue
        node = require_string(
            entry.get("nodeid"), f"outcomes[{index}].nodeid", label, errors
        )
        if node in seen:
            errors.append(f"{label}: duplicate outcome for {node}")
        seen.append(node)
        outcome = entry.get("outcome")
        if outcome != "passed":
            errors.append(f"{label}: {node} outcome is {outcome!r}, not 'passed'")
        phases = entry.get("phases")
        if not isinstance(phases, dict):
            errors.append(f"{label}: {node} has no phase evidence")
        else:
            for phase in ("setup", "call", "teardown"):
                if phases.get(phase) != "passed":
                    errors.append(
                        f"{label}: {node} phase {phase} is {phases.get(phase)!r}, not 'passed'"
                    )
        reruns = entry.get("reruns", 0)
        if reruns != 0:
            errors.append(f"{label}: {node} was rerun {reruns} time(s)")
        if node not in manifest_by_nodeid:
            errors.append(f"{label}: outcome for unknown nodeid {node}")
            continue
        case = manifest_by_nodeid[node]
        expected_checkpoints = set(case.get("checkpoints") or [])
        reported = require_string_list(
            entry.get("checkpoints"), f"outcomes[{index}].checkpoints", label, errors
        )
        missing = sorted(expected_checkpoints - set(reported))
        if missing:
            errors.append(f"{label}: {node} lacks checkpoint evidence {missing}")
    missing = sorted(set(manifest_by_nodeid) - set(seen))
    if missing:
        errors.append(f"{label}: journeys without an execution outcome: {missing}")
    return revision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gate Playwright E2E evidence against the browser journey manifest."
    )
    parser.add_argument("--manifest", required=True, help="tests/e2e/cases.json")
    parser.add_argument("--collection", required=True, help="pytest collection JSON")
    parser.add_argument("--results", required=True, help="pytest results JSON")
    args = parser.parse_args(argv)

    errors: list[str] = []
    manifest_path = Path(args.manifest)
    collection_path = Path(args.collection)
    results_path = Path(args.results)

    manifest_data = load_json(manifest_path, "manifest", errors)
    collection_data = load_json(collection_path, "collection", errors)
    results_data = load_json(results_path, "results", errors)

    manifest_revision, manifest = ("", {})
    collection_revision = ""
    results_revision = ""
    if manifest_data is not None:
        manifest_revision, manifest = check_manifest(manifest_data, errors)
    manifest_nodeids = {
        case["nodeid"] for case in manifest.values() if case.get("nodeid")
    }
    if collection_data is not None:
        collection_revision = check_collection(
            collection_data, manifest_nodeids, errors
        )
    if results_data is not None:
        results_revision = check_results(results_data, manifest, errors)

    revisions = {
        "manifest": manifest_revision,
        "collection": collection_revision,
        "results": results_revision,
    }
    populated = {name: value for name, value in revisions.items() if value}
    if len(set(populated.values())) > 1:
        errors.append(f"stale source revision across evidence: {revisions}")
    expected_revision = gated_input_revision()
    if (
        expected_revision
        and populated
        and set(populated.values()) != {expected_revision}
    ):
        errors.append(
            f"stale source revision: evidence {sorted(set(populated.values()))} "
            f"!= gated inputs revision {expected_revision}"
        )

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"E2E inventory gate FAILED ({len(errors)} error(s))", file=sys.stderr)
        return 1
    print(f"E2E inventory gate passed: {len(manifest)} journeys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
