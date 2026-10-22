#!/usr/bin/env python3
"""Export the mutmut framework state into the mutation inventory manifest (read-only).

Reads the pinned framework's own per-source-file mutation data under ``mutants/``
and the current AST of the seven required modules, then writes
``reports/mutation-inventory.json``. It never generates or executes mutants and
never modifies source.

Mutant ids are copied verbatim from the framework state. A framework hash that no
longer matches the current source is stale evidence and fails the export.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from check_mutation_inventory import (
    MODULES,
    coverage_entry,
    current_source_revision,
    discover_module,
    load_coverage,
)

NESTED_REASON = "nested function is outside mutmut's top-level mutation scope"
NO_MUTANTS_REASON = "mutmut generated no mutants for this function"


def load_framework_state(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"mutants: missing framework state {path}")
        return None
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"mutants: unreadable framework state {path} ({exc})")
        return None
    if not isinstance(data, dict):
        errors.append(f"mutants: {path} top level must be an object")
        return None
    hashes = data.get("hash_by_function_name")
    exit_codes = data.get("exit_code_by_key")
    if not isinstance(hashes, dict) or not isinstance(exit_codes, dict):
        errors.append(f"mutants: {path} lacks hash_by_function_name/exit_code_by_key")
        return None
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export the installed mutmut state into the mutation inventory manifest. "
            "Read-only: never generates or applies mutants and never modifies source."
        )
    )
    parser.add_argument("--source-root", default="app", help="directory with the seven modules")
    parser.add_argument("--mutants", default="mutants", help="mutmut generated-state directory")
    parser.add_argument(
        "--coverage",
        default="reports/coverage.json",
        help="coverage.py JSON with --cov-context=test evidence",
    )
    parser.add_argument(
        "--output",
        default="reports/mutation-inventory.json",
        help="inventory manifest to write",
    )
    parser.add_argument(
        "--source-revision",
        default=None,
        help="source revision to record (defaults to git HEAD)",
    )
    args = parser.parse_args(argv)

    revision = args.source_revision or current_source_revision()
    if not revision:
        print(
            "ERROR: exporter: no source revision (pass --source-revision or run in a git checkout)",
            file=sys.stderr,
        )
        return 1

    source_root = Path(args.source_root)
    mutants_root = Path(args.mutants)
    errors: list[str] = []
    functions: list[dict[str, Any]] = []
    coverage_files = load_coverage(Path(args.coverage), errors)

    for module in MODULES:
        path = source_root / f"{module}.py"
        if not path.is_file():
            errors.append(f"source: required module {path} is missing")
            continue
        discovered = discover_module(path, f"app.{module}", errors)
        state = load_framework_state(mutants_root / f"{path.as_posix()}.meta", errors)
        if state is None:
            continue
        hashes: dict[str, str] = state["hash_by_function_name"]
        exit_codes: dict[str, Any] = state["exit_code_by_key"]
        coverage = coverage_entry(coverage_files, path.as_posix(), errors)
        executed: set[int] = set()
        contexts: dict[str, Any] = {}
        if coverage is not None:
            executed = {
                line for line in coverage.get("executed_lines", []) if isinstance(line, int)
            }
            contexts = coverage["contexts"]

        for qualified_name, info in sorted(discovered.items()):
            test_nodeids = sorted(
                {
                    context[:-4]
                    for line in info.body_lines & executed
                    for context in contexts.get(str(line), [])
                    if isinstance(context, str) and context.endswith("|run")
                }
            )
            if not test_nodeids:
                errors.append(
                    f"coverage: {qualified_name} has no executed test context on a body line"
                )
            mutant_ids: list[str] = []
            reason: str | None = None
            if info.mangled_key is None:
                reason = NESTED_REASON
            else:
                framework_hash = hashes.get(info.mangled_key)
                if framework_hash is None:
                    reason = NO_MUTANTS_REASON
                elif framework_hash != info.source_sha256[:12]:
                    errors.append(
                        f"mutants: stale framework hash for {qualified_name} "
                        f"({framework_hash} != {info.source_sha256[:12]})"
                    )
                    continue
                else:
                    prefix = f"app.{module}.{info.mangled_key}__mutmut_"
                    mutant_ids = sorted(
                        mutant_id
                        for mutant_id in exit_codes
                        if isinstance(mutant_id, str) and mutant_id.startswith(prefix)
                    )
                    if not mutant_ids:
                        reason = NO_MUTANTS_REASON
            functions.append(
                {
                    "qualified_name": qualified_name,
                    "path": path.as_posix(),
                    "source_sha256": info.source_sha256,
                    "test_nodeids": test_nodeids,
                    "mutant_ids": mutant_ids,
                    "no_operator_reason": reason,
                }
            )

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"Mutation inventory export FAILED ({len(errors)} error(s))", file=sys.stderr)
        return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "source_revision": revision,
                "functions": sorted(functions, key=lambda item: item["qualified_name"]),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Mutation inventory exported: {len(functions)} functions -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
