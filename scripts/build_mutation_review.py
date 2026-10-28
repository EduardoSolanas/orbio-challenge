#!/usr/bin/env python3
"""Prepare pending review entries from framework results and exact mutation diffs.

This command never approves an exclusion or decides that a mutant is equivalent or
invalid. An independent verifier must review the diff and evidence before adding a
separate approval. Feed a draft to the report gate and every non-killed mutant stays
unresolved.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from check_mutation_report import parse_results


def pending_reviews(results: str, diffs: object) -> list[dict]:
    entries, errors = parse_results(results)
    if errors:
        raise ValueError("; ".join(errors))
    expected = {
        mutant_id: status for mutant_id, status in entries if status != "killed"
    }
    if not isinstance(diffs, list):
        raise ValueError("diffs must be a list")
    seen: set[str] = set()
    proposals = []
    for row in diffs:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError("each diff needs a framework mutant id")
        mutant_id = row["id"]
        if mutant_id in seen or mutant_id not in expected:
            raise ValueError(f"duplicate or unexpected diff: {mutant_id}")
        if row.get("status") != expected[mutant_id]:
            raise ValueError(
                f"diff status does not match framework results: {mutant_id}"
            )
        digest = row.get("diff_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError(f"invalid diff hash: {mutant_id}")
        for key in ("removed", "added"):
            if not isinstance(row.get(key), list) or not all(
                isinstance(line, str) for line in row[key]
            ):
                raise ValueError(f"{key} must contain diff lines: {mutant_id}")
        seen.add(mutant_id)
        proposals.append(
            {
                "mutant_id": mutant_id,
                "framework_status": expected[mutant_id],
                "diff_sha256": digest,
                "removed": row["removed"],
                "added": row["added"],
                "decision": "pending",
            }
        )
    missing = expected.keys() - seen
    if missing:
        raise ValueError(f"missing diffs: {', '.join(sorted(missing))}")
    return proposals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--diffs", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--run-commit", default="")
    args = parser.parse_args(argv)
    try:
        proposals = pending_reviews(
            args.results.read_text("utf-8"),
            json.loads(args.diffs.read_text("utf-8")),
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    document = {
        "source_revision": args.revision,
        "mutation_run_commit": args.run_commit,
        "approvals": [],
        "function_approvals": [],
        "proposals": proposals,
    }
    args.out.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", "utf-8"
    )
    print(f"prepared {len(proposals)} pending reviews; no exclusions approved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
