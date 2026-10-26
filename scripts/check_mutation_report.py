#!/usr/bin/env python3
"""Gate the mutation evidence produced by the pinned mutmut framework.

Parses the captured ``mutmut run`` summary and ``mutmut results`` report, then
fails closed on any non-killed mutant that lacks a verifier-approved review
decision. Writes ``reports/mutation-summary.json`` with the derived counts.

This is a gate over framework output. It never generates, applies or executes a
mutant and never modifies source.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from importlib import metadata
from pathlib import Path
from typing import Any

from evidence_revision import gated_input_revision

STATUSES = {
    "killed",
    "survived",
    "no tests",
    "timeout",
    "suspicious",
    "skipped",
    "caught by type check",
    "check was interrupted by user",
    "not checked",
    "segfault",
}
NON_KILLED = STATUSES - {"killed"}
EQUIVALENT_STATUSES = {"survived"}
INVALID_STATUSES = NON_KILLED - EQUIVALENT_STATUSES
APPROVAL_KINDS = {"equivalent", "invalid"}
REQUIRED_APPROVAL_FIELDS = (
    "mutant_id",
    "status",
    "rationale",
    "source_revision",
    "diff_sha256",
    "verifier",
    "decision",
)
RUN_SUMMARY = re.compile(
    r"(\d+)/(\d+)\s+🎉\s*(\d+)\s+🫥\s*(\d+)\s+⏰\s*(\d+)\s+🤔\s*(\d+)"
    r"\s+🙁\s*(\d+)\s+🔇\s*(\d+)\s+🧙\s*(\d+)"
)
RESULT_LINE = re.compile(r"^\s*(\S+):\s*(.+?)\s*$")
SUMMARY_PATH = Path("reports/mutation-summary.json")


def parse_run_log(text: str) -> dict[str, int] | None:
    matches = RUN_SUMMARY.findall(text.replace("\r", "\n"))
    if not matches:
        return None
    checked, total, killed, no_tests, timeout, suspicious, survived, skipped, caught = (
        int(value) for value in matches[-1]
    )
    return {
        "generated": total,
        "executed": checked,
        "killed": killed,
        "no_tests": no_tests,
        "timeout": timeout,
        "suspicious": suspicious,
        "survived": survived,
        "skipped": skipped,
        "caught_by_type_check": caught,
    }


def parse_results(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    entries: list[tuple[str, str]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        match = RESULT_LINE.match(raw)
        if not match:
            errors.append(f"results:{line_number}: unrecognized line {raw!r}")
            continue
        mutant_id, status = match.groups()
        if status not in STATUSES:
            errors.append(f"results:{line_number}: unknown status {status!r}")
            continue
        if "__mutmut_" not in mutant_id:
            errors.append(f"results:{line_number}: malformed mutant id {mutant_id!r}")
            continue
        if mutant_id in seen:
            errors.append(f"results:{line_number}: duplicate mutant id {mutant_id}")
            continue
        seen.add(mutant_id)
        entries.append((mutant_id, status))
    return entries, errors


def load_json(path: Path, label: str, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"{label}: missing file {path}")
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{label}: unreadable JSON ({exc})")
    return None


def validate_review(data: Any, errors: list[str]) -> tuple[str, dict[str, dict]]:
    label = "review"
    if not isinstance(data, dict):
        errors.append(f"{label}: top level must be an object")
        return "", {}
    revision = data.get("source_revision")
    if not isinstance(revision, str) or not revision.strip():
        errors.append(f"{label}: source_revision must be a non-empty string")
        revision = ""
    approvals = data.get("approvals")
    if not isinstance(approvals, list):
        errors.append(f"{label}: approvals must be a list")
        return revision, {}
    by_mutant: dict[str, dict] = {}
    for index, approval in enumerate(approvals):
        if not isinstance(approval, dict):
            errors.append(f"{label}: approvals[{index}] must be an object")
            continue
        missing = [field for field in REQUIRED_APPROVAL_FIELDS if field not in approval]
        if missing:
            errors.append(f"{label}: approvals[{index}] missing fields {missing}")
            continue
        mutant_id = approval["mutant_id"]
        if not isinstance(mutant_id, str) or not mutant_id:
            errors.append(
                f"{label}: approvals[{index}].mutant_id must be a non-empty string"
            )
            continue
        if mutant_id in by_mutant:
            errors.append(f"{label}: duplicate approval for {mutant_id}")
        kind = approval["status"]
        if kind not in APPROVAL_KINDS:
            errors.append(
                f"{label}: {mutant_id} approval status {kind!r} is not allowed"
            )
        if approval["decision"] != "approved":
            errors.append(f"{label}: {mutant_id} decision is {approval['decision']!r}")
        for field in ("rationale", "verifier", "diff_sha256"):
            if not isinstance(approval[field], str) or not approval[field].strip():
                errors.append(
                    f"{label}: {mutant_id} {field} must be a non-empty string"
                )
        if approval["source_revision"] != revision:
            errors.append(
                f"{label}: {mutant_id} approval revision {approval['source_revision']!r} "
                f"does not match review revision {revision!r}"
            )
        by_mutant[mutant_id] = approval
    return revision, by_mutant


def mutation_scope(errors: list[str]) -> list[str]:
    try:
        import tomllib

        data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        paths = data.get("tool", {}).get("mutmut", {}).get("source_paths")
        if isinstance(paths, list) and paths:
            return [str(path) for path in paths]
    except (OSError, tomllib.TOMLDecodeError):
        pass
    errors.append("scope: pyproject.toml has no [tool.mutmut] source_paths")
    return []


def framework_version() -> str:
    try:
        return metadata.version("mutmut")
    except metadata.PackageNotFoundError:
        return "unknown"


def write_summary(summary: dict[str, Any]) -> None:
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gate mutmut results: every non-killed mutant needs review approval."
    )
    parser.add_argument(
        "--results", required=True, help="captured mutmut results output"
    )
    parser.add_argument("--run-log", required=True, help="captured mutmut run output")
    parser.add_argument("--review", required=True, help="reports/mutation-review.json")
    args = parser.parse_args(argv)

    errors: list[str] = []
    results_path = Path(args.results)
    run_log_path = Path(args.run_log)
    review_path = Path(args.review)

    run_text = ""
    try:
        run_text = run_log_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"run-log: unreadable {run_log_path} ({exc})")

    results_text = ""
    try:
        results_text = results_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"results: unreadable {results_path} ({exc})")

    run_counts = parse_run_log(run_text)
    if run_counts is None:
        errors.append("run-log: no mutmut run summary line")
        run_counts = {
            "generated": 0,
            "executed": 0,
            "killed": 0,
            "no_tests": 0,
            "timeout": 0,
            "suspicious": 0,
            "survived": 0,
            "skipped": 0,
            "caught_by_type_check": 0,
        }
    else:
        if run_counts["generated"] <= 0:
            errors.append("run-log: zero generated mutants")
        if run_counts["executed"] <= 0:
            errors.append("run-log: zero executed mutants")
        if run_counts["executed"] != run_counts["generated"]:
            errors.append(
                f"run-log: {run_counts['generated'] - run_counts['executed']} mutant(s) "
                "were not checked"
            )
        if run_counts["killed"] <= 0:
            errors.append("run-log: zero killed mutants")

    entries, parse_errors = parse_results(results_text)
    errors.extend(parse_errors)

    review_data = load_json(review_path, "review", errors)
    review_revision, approvals = ("", {})
    if review_data is not None:
        review_revision, approvals = validate_review(review_data, errors)

    counts = Counter(status for _, status in entries)
    expected = {
        "survived": run_counts["survived"],
        "no tests": run_counts["no_tests"],
        "timeout": run_counts["timeout"],
        "suspicious": run_counts["suspicious"],
        "skipped": run_counts["skipped"],
        "caught by type check": run_counts["caught_by_type_check"],
    }
    for status, count in expected.items():
        if counts.get(status, 0) != count:
            errors.append(
                f"results: {counts.get(status, 0)} {status!r} lines do not match "
                f"the run summary count {count}"
            )
    if counts.get("killed", 0) and counts["killed"] != run_counts["killed"]:
        errors.append(
            f"results: {counts['killed']} killed lines do not match the run summary "
            f"count {run_counts['killed']}"
        )
    not_checked = run_counts["generated"] - run_counts["executed"]
    if counts.get("not checked", 0) != not_checked:
        errors.append(
            f"results: {counts.get('not checked', 0)} 'not checked' lines do not match "
            f"the run summary count {not_checked}"
        )

    unresolved = 0
    equivalent_survivors = 0
    invalid_variants = 0
    reviewed_exclusions: list[dict[str, Any]] = []
    for mutant_id, status in entries:
        if status == "killed":
            continue
        approval = approvals.get(mutant_id)
        expected_kind = "equivalent" if status in EQUIVALENT_STATUSES else "invalid"
        if approval is None:
            unresolved += 1
            errors.append(f"unapproved {status} mutant {mutant_id}")
            continue
        if approval.get("status") != expected_kind:
            unresolved += 1
            errors.append(
                f"{mutant_id} is {status} but approved as {approval.get('status')!r}; "
                f"expected {expected_kind!r}"
            )
            continue
        if expected_kind == "equivalent":
            equivalent_survivors += 1
        else:
            invalid_variants += 1
        reviewed_exclusions.append(
            {
                "mutant_id": mutant_id,
                "status": status,
                "approval": approval.get("status"),
                "verifier": approval.get("verifier"),
                "rationale": approval.get("rationale"),
                "diff_sha256": approval.get("diff_sha256"),
            }
        )

    for mutant_id in approvals:
        if mutant_id not in {entry[0] for entry in entries}:
            errors.append(
                f"review: approval for mutant {mutant_id} not present in results"
            )

    expected_revision = gated_input_revision()
    if review_revision and expected_revision and review_revision != expected_revision:
        errors.append(
            f"review: stale source revision {review_revision!r} != gated inputs "
            f"revision {expected_revision!r}"
        )
    if not review_revision:
        errors.append("review: source_revision is required for a mutation gate")

    scope = mutation_scope(errors)
    summary = {
        "framework": "mutmut",
        "framework_version": framework_version(),
        "scope": scope,
        "run_log": str(run_log_path),
        "results": str(results_path),
        "review": str(review_path),
        "source_revision": review_revision or expected_revision,
        "generated": run_counts["generated"],
        "executed": run_counts["executed"],
        "killed": run_counts["killed"],
        "survived": run_counts["survived"],
        "no_tests": run_counts["no_tests"],
        "timeout": run_counts["timeout"],
        "suspicious": run_counts["suspicious"],
        "skipped": run_counts["skipped"],
        "caught_by_type_check": run_counts["caught_by_type_check"],
        "not_checked": not_checked,
        "equivalent_survivors": equivalent_survivors,
        "invalid_variants": invalid_variants,
        "unresolved": unresolved,
        "reviewed_exclusions": reviewed_exclusions,
        "gate": "blocked" if errors else "passed",
        "errors": errors,
    }
    write_summary(summary)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"Mutation report gate FAILED ({len(errors)} error(s))", file=sys.stderr)
        return 1
    print(
        f"Mutation report gate passed: {run_counts['generated']} generated, "
        f"{run_counts['killed']} killed, {equivalent_survivors} approved equivalent, "
        f"{invalid_variants} approved invalid"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
