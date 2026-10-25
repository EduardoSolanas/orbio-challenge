#!/usr/bin/env python3
"""Gate the mutation reachability inventory against source, coverage and review evidence.

AST-discovers every concrete function in the seven required modules, then verifies
that the inventory manifest covers each one exactly once, with an executed body-line
coverage context proving a real test mapping, framework-generated mutant ids that
match the current source hash, or a verifier-approved "no applicable operator" reason.

This is an evidence checker. It never generates or executes mutants and never
modifies source.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evidence_revision import gated_input_revision

# Dotted module paths from the repository root; each is also the prefix of its mutant ids.
MODULES = (
    "backend.gateway.conversation.engine",
    "backend.gateway.conversation.intent",
    "backend.gateway.conversation.plans",
    "backend.gateway.conversation.replay",
    "backend.gateway.conversation.validation",
    "backend.gateway.storage",
    "backend.gateway.providers.audio",
    "backend.gateway.providers.client",
    "backend.gateway.providers.wire",
    "backend.gateway.providers.speech_wav",
    "backend.gateway.config",
    "backend.gateway.knowledge.retrieval",
)
CLASS_SEPARATOR = "\u01c1"
ENTRY_FIELDS = {
    "qualified_name",
    "path",
    "source_sha256",
    "test_nodeids",
    "mutant_ids",
    "no_operator_reason",
}
INVENTORY_FIELDS = {"source_revision", "functions"}
APPROVAL_FIELDS = {
    "qualified_name",
    "source_sha256",
    "rationale",
    "source_revision",
    "verifier",
    "decision",
}


@dataclass(frozen=True)
class FunctionInfo:
    qualified_name: str
    path: str
    mangled_key: str | None
    body_lines: frozenset[int]
    source_sha256: str


def load_json(path: Path, label: str, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"{label}: missing file {path}")
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{label}: unreadable JSON ({exc})")
    return None


def function_hash(node: ast.AST) -> str:
    """Full sha256 of the function AST, the mutmut 3.8 per-function hash basis."""
    return hashlib.sha256(ast.dump(node, annotate_fields=False).encode()).hexdigest()


def mangled_key(name: str, class_chain: tuple[str, ...], nested: bool) -> str | None:
    if nested:
        return None
    if class_chain:
        joined = CLASS_SEPARATOR.join(class_chain)
        return f"x{CLASS_SEPARATOR}{joined}{CLASS_SEPARATOR}{name}"
    return f"x_{name}"


def is_stub(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True for bodies consisting only of pass/ellipsis (docstring excluded)."""
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    if not body:
        return True
    for statement in body:
        if isinstance(statement, ast.Pass):
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and statement.value.value is Ellipsis
        ):
            continue
        return False
    return True


def is_protocol(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == "Protocol":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "Protocol":
            return True
    return False


def body_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[int]:
    """Executable statement lines of the function body, excluding nested definitions."""
    lines: set[int] = set()

    def walk(statements: list[ast.stmt]) -> None:
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            lines.add(statement.lineno)
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(statement, field, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                    walk(inner)
            for handler in getattr(statement, "handlers", []):
                lines.add(handler.lineno)
                walk(handler.body)

    walk(node.body)
    return frozenset(lines)


def discover_module(path: Path, module_dotted: str, errors: list[str]) -> dict[str, FunctionInfo]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"source: unreadable {path} ({exc})")
        return {}
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        errors.append(f"source: {path} does not parse ({exc})")
        return {}

    found: dict[str, FunctionInfo] = {}

    def visit(
        body: list[ast.stmt], class_chain: tuple[str, ...], func_chain: tuple[str, ...]
    ) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified_name = ".".join([module_dotted, *class_chain, *func_chain, node.name])
                if not is_stub(node):
                    found[qualified_name] = FunctionInfo(
                        qualified_name=qualified_name,
                        path=path.as_posix(),
                        mangled_key=mangled_key(
                            node.name, class_chain, nested=bool(func_chain)
                        ),
                        body_lines=body_lines(node),
                        source_sha256=function_hash(node),
                    )
                visit(node.body, class_chain, (*func_chain, node.name))
            elif isinstance(node, ast.ClassDef):
                if is_protocol(node):
                    continue
                visit(node.body, (*class_chain, node.name), func_chain)

    visit(tree.body, (), ())
    return found


def module_file(source_root: Path, module: str) -> Path:
    """The source file of a dotted module path, under ``source_root``."""

    return source_root.joinpath(*module.split(".")).with_suffix(".py")


def module_of(qualified_name: str) -> str:
    """The module a qualified function name belongs to."""

    return next(module for module in MODULES if qualified_name.startswith(f"{module}."))


def discover_all(source_root: Path, errors: list[str]) -> dict[str, FunctionInfo]:
    discovered: dict[str, FunctionInfo] = {}
    for module in MODULES:
        path = module_file(source_root, module)
        if not path.is_file():
            errors.append(f"source: required module {path} is missing")
            continue
        for name, info in discover_module(path, module, errors).items():
            if name in discovered:
                errors.append(f"source: duplicate qualified name {name}")
            discovered[name] = info
    return discovered


def load_coverage(path: Path, errors: list[str]) -> dict[str, Any]:
    data = load_json(path, "coverage", errors)
    if data is None:
        return {}
    if not isinstance(data, dict):
        errors.append("coverage: top level must be an object")
        return {}
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        errors.append("coverage: files must be a non-empty object")
        return {}
    return files


def coverage_entry(files: dict[str, Any], rel_path: str, errors: list[str]) -> dict[str, Any] | None:
    entry = files.get(rel_path)
    if entry is None:
        for key, value in files.items():
            if Path(key).as_posix().endswith(rel_path):
                entry = value
                break
    if not isinstance(entry, dict):
        errors.append(f"coverage: no measured file entry for {rel_path}")
        return None
    executed = entry.get("executed_lines")
    missing = entry.get("missing_lines")
    if not isinstance(executed, list) or not isinstance(missing, list):
        errors.append(f"coverage: {rel_path} lacks executed_lines/missing_lines")
        return None
    contexts = entry.get("contexts")
    if not isinstance(contexts, dict):
        errors.append(
            f"coverage: {rel_path} has no test contexts "
            "(the quality command must use --cov-context=test)"
        )
        return None
    return entry


def context_matches(contexts: dict[str, Any], line: int, nodeid: str) -> bool:
    for context in contexts.get(str(line), []):
        if not isinstance(context, str):
            continue
        if context.endswith("|run") and context[:-4] == nodeid:
            return True
    return False


def validate_review(
    data: Any, inventory_revision: str, errors: list[str]
) -> dict[str, dict]:
    label = "review"
    if not isinstance(data, dict):
        errors.append(f"{label}: top level must be an object")
        return {}
    revision = data.get("source_revision")
    if not isinstance(revision, str) or not revision.strip():
        errors.append(f"{label}: source_revision must be a non-empty string")
        revision = ""
    elif revision != inventory_revision:
        errors.append(
            f"{label}: source_revision {revision!r} does not match inventory "
            f"{inventory_revision!r}"
        )
    approvals = data.get("approvals")
    if not isinstance(approvals, list):
        errors.append(f"{label}: approvals must be a list")
    function_approvals = data.get("function_approvals")
    if not isinstance(function_approvals, list):
        errors.append(f"{label}: function_approvals must be a list")
        function_approvals = []
    by_name: dict[str, dict] = {}
    for index, approval in enumerate(function_approvals):
        if not isinstance(approval, dict):
            errors.append(f"{label}: function_approvals[{index}] must be an object")
            continue
        missing = sorted(APPROVAL_FIELDS - set(approval))
        unknown = sorted(set(approval) - APPROVAL_FIELDS)
        if missing:
            errors.append(f"{label}: function_approvals[{index}] missing fields {missing}")
            continue
        if unknown:
            errors.append(f"{label}: function_approvals[{index}] unknown fields {unknown}")
            continue
        name = approval["qualified_name"]
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{label}: function_approvals[{index}].qualified_name is invalid")
            continue
        if name in by_name:
            errors.append(f"{label}: duplicate function approval for {name}")
        if approval["decision"] != "approved":
            errors.append(f"{label}: {name} decision is {approval['decision']!r}")
        for field in ("rationale", "verifier", "source_sha256", "source_revision"):
            if not isinstance(approval[field], str) or not approval[field].strip():
                errors.append(f"{label}: {name} {field} must be a non-empty string")
        if approval["source_revision"] != inventory_revision:
            errors.append(
                f"{label}: {name} approval revision {approval['source_revision']!r} "
                f"does not match inventory {inventory_revision!r}"
            )
        by_name[name] = approval
    return by_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Gate the mutation inventory: every AST-discovered concrete function needs "
            "executed body-line coverage with a test context, framework mutant ids for the "
            "current source hash, or an approved no-operator reason."
        )
    )
    parser.add_argument("--coverage", required=True, help="reports/coverage.json")
    parser.add_argument("--inventory", required=True, help="reports/mutation-inventory.json")
    parser.add_argument("--review", required=True, help="reports/mutation-review.json")
    args = parser.parse_args(argv)

    errors: list[str] = []
    coverage_path = Path(args.coverage)
    inventory_path = Path(args.inventory)
    review_path = Path(args.review)

    discovered = discover_all(Path("."), errors)
    files = load_coverage(coverage_path, errors)
    inventory_data = load_json(inventory_path, "inventory", errors)

    revision = ""
    entries: list[dict] = []
    if inventory_data is not None:
        if not isinstance(inventory_data, dict):
            errors.append("inventory: top level must be an object")
        else:
            missing = sorted(INVENTORY_FIELDS - set(inventory_data))
            unknown = sorted(set(inventory_data) - INVENTORY_FIELDS)
            if missing:
                errors.append(f"inventory: missing fields {missing}")
            if unknown:
                errors.append(f"inventory: unknown fields {unknown}")
            revision_value = inventory_data.get("source_revision")
            if isinstance(revision_value, str) and revision_value.strip():
                revision = revision_value
            else:
                errors.append("inventory: source_revision must be a non-empty string")
            functions = inventory_data.get("functions")
            if isinstance(functions, list) and functions:
                entries = functions
            else:
                errors.append("inventory: functions must be a non-empty list")

    review_data = load_json(review_path, "review", errors)
    approvals = (
        validate_review(review_data, revision, errors) if review_data is not None else {}
    )

    expected_revision = gated_input_revision()
    if revision and expected_revision and revision != expected_revision:
        errors.append(
            f"inventory: stale source revision {revision!r} != gated inputs "
            f"revision {expected_revision!r}"
        )

    seen: set[str] = set()
    seen_mutants: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"inventory: functions[{index}] must be an object")
            continue
        missing = sorted(ENTRY_FIELDS - set(entry))
        unknown = sorted(set(entry) - ENTRY_FIELDS)
        if missing:
            errors.append(f"inventory: functions[{index}] missing fields {missing}")
            continue
        if unknown:
            errors.append(f"inventory: functions[{index}] unknown fields {unknown}")
            continue
        qualified_name = entry["qualified_name"]
        if not isinstance(qualified_name, str) or not qualified_name.strip():
            errors.append(f"inventory: functions[{index}].qualified_name is invalid")
            continue
        if qualified_name in seen:
            errors.append(f"inventory: duplicate entry for {qualified_name}")
            continue
        seen.add(qualified_name)
        info = discovered.get(qualified_name)
        if info is None:
            errors.append(f"inventory: unknown function {qualified_name} is not in the source")
            continue

        path_value = entry["path"]
        expected_path = info.path
        if not isinstance(path_value, str) or Path(path_value).as_posix() != expected_path:
            errors.append(
                f"inventory: {qualified_name} path {path_value!r} != {expected_path!r}"
            )

        source_sha256 = entry["source_sha256"]
        if not isinstance(source_sha256, str) or len(source_sha256) != 64:
            errors.append(f"inventory: {qualified_name} source_sha256 must be 64 hex chars")
        elif source_sha256 != info.source_sha256:
            errors.append(
                f"inventory: stale source_sha256 for {qualified_name} "
                f"({source_sha256[:12]} != {info.source_sha256[:12]})"
            )

        test_nodeids = entry["test_nodeids"]
        if not isinstance(test_nodeids, list) or not test_nodeids:
            errors.append(f"inventory: {qualified_name} test_nodeids must be a non-empty list")
            test_nodeids = []
        entry_coverage = coverage_entry(files, expected_path, errors) if files else None
        executed: set[int] = set()
        contexts: dict[str, Any] = {}
        if entry_coverage is not None:
            executed = {
                line
                for line in entry_coverage.get("executed_lines", [])
                if isinstance(line, int)
            }
            contexts = entry_coverage["contexts"]
        executed_body = info.body_lines & executed
        if not executed_body:
            errors.append(
                f"inventory: {qualified_name} has no executed body-line coverage "
                f"(body lines {sorted(info.body_lines)})"
            )
        for nodeid in test_nodeids:
            if not isinstance(nodeid, str) or not nodeid.strip():
                errors.append(f"inventory: {qualified_name} has a blank test nodeid")
                continue
            if not any(context_matches(contexts, line, nodeid) for line in executed_body):
                errors.append(
                    f"inventory: {qualified_name} test {nodeid} has no executed "
                    "coverage context on a body line"
                )

        mutant_ids = entry["mutant_ids"]
        reason = entry["no_operator_reason"]
        if not isinstance(mutant_ids, list):
            errors.append(f"inventory: {qualified_name} mutant_ids must be a list")
            mutant_ids = []
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            errors.append(
                f"inventory: {qualified_name} no_operator_reason must be a string or null"
            )
        if mutant_ids:
            if info.mangled_key is None:
                errors.append(
                    f"inventory: {qualified_name} has mutant ids but is outside "
                    "mutmut's top-level scope"
                )
            if reason is not None:
                errors.append(
                    f"inventory: {qualified_name} has mutant ids and a no_operator_reason"
                )
            prefix = f"{module_of(qualified_name)}.{info.mangled_key}__mutmut_"
            for mutant_id in mutant_ids:
                if not isinstance(mutant_id, str) or not mutant_id.startswith(prefix):
                    errors.append(
                        f"inventory: {qualified_name} mutant id {mutant_id!r} does not "
                        f"match the framework naming for {info.mangled_key!r}"
                    )
                    continue
                suffix = mutant_id[len(prefix) :]
                if not suffix.isdigit():
                    errors.append(
                        f"inventory: {qualified_name} mutant id {mutant_id!r} has a "
                        "non-numeric framework suffix"
                    )
                if mutant_id in seen_mutants:
                    errors.append(f"inventory: duplicate mutant id {mutant_id}")
                seen_mutants.add(mutant_id)
        else:
            if not reason:
                errors.append(
                    f"inventory: {qualified_name} has no mutants and no no_operator_reason"
                )
            else:
                approval = approvals.get(qualified_name)
                if approval is None:
                    errors.append(
                        f"inventory: {qualified_name} has no variants and lacks an "
                        "independent function approval"
                    )
                elif approval.get("source_sha256") != info.source_sha256:
                    errors.append(
                        f"inventory: {qualified_name} function approval hash "
                        f"{approval.get('source_sha256')!r} does not match the current source"
                    )

    missing_functions = sorted(set(discovered) - seen)
    if missing_functions:
        errors.append(f"inventory: missing functions {missing_functions}")
    unknown_approvals = sorted(set(approvals) - set(discovered))
    if unknown_approvals:
        errors.append(f"review: approvals for unknown functions {unknown_approvals}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"Mutation inventory gate FAILED ({len(errors)} error(s))", file=sys.stderr)
        return 1
    print(
        f"Mutation inventory gate passed: {len(discovered)} concrete functions across "
        f"{len(MODULES)} modules"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
