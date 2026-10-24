"""Tests for scripts/check_mutation_inventory.py and scripts/export_mutation_inventory.py.

Fixtures mirror the real evidence formats named by docs/testing-contract.md 10:
  * coverage.py JSON produced with ``show_contexts = true`` and ``--cov-context=test``
  * mutmut 3.8.0 per-source-file state under ``mutants/<path>.meta``
  * the inventory manifest ``{ "source_revision": ..., "functions": [...] }``
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_mutation_inventory  # noqa: E402

MODULES = (
    "conversation",
    "validation",
    "storage",
    "audio",
    "providers",
    "config",
    "retrieval",
)

SOURCES = {
    "conversation": "def start():\n    return {'status': 'ok'}\n",
    "validation": "def validate(payload):\n    return payload.get('slots', {})\n",
    "storage": "def save(session):\n    return session['id']\n",
    "audio": "def transcribe(chunk):\n    return chunk.decode()\n",
    "providers": (
        "from typing import Protocol\n"
        "\n"
        "class AIProvider(Protocol):\n"
        "    def chat(self, prompt: str) -> str: ...\n"
        "\n"
        "class OpenAICompatibleProvider:\n"
        "    def chat(self, prompt):\n"
        "        return prompt\n"
    ),
    "config": (
        "class Settings:\n"
        "    def from_env(self):\n"
        "        return Settings()\n"
    ),
    "retrieval": (
        "def search(query):\n"
        "    return []\n"
        "\n"
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner()\n"
    ),
}


SOURCES.update(
    {
        module: f"def {module}_helper():\n    return 1\n"
        for module in ("intent", "plans", "replay", "provider_wire", "speech_wav")
    }
)


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every fixture path and git lookup is resolved inside the temporary tree."""
    monkeypatch.chdir(tmp_path)


def write_sources(root: Path, sources: dict[str, str]) -> None:
    app = root / "app"
    app.mkdir(exist_ok=True)
    for module, source in sources.items():
        (app / f"{module}.py").write_text(source, encoding="utf-8")


def locate(source: str, parts: tuple[str, ...]):
    """Return (node, class_chain, func_chain) for a dotted path inside one module."""
    body = ast.parse(source).body
    class_chain: list[str] = []
    func_chain: list[str] = []
    node = None
    for part in parts:
        node = next(
            child
            for child in body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and child.name == part
        )
        if isinstance(node, ast.ClassDef):
            class_chain.append(part)
        else:
            func_chain.append(part)
        body = node.body
    return node, class_chain, func_chain


def function_hash(node: ast.AST) -> str:
    return hashlib.sha256(ast.dump(node, annotate_fields=False).encode()).hexdigest()


CLASS_SEPARATOR = "\u01c1"


def mangled_key(class_chain: list[str], func_chain: list[str], name: str) -> str | None:
    if len(func_chain) > 1:
        return None
    if class_chain:
        joined = CLASS_SEPARATOR.join(class_chain)
        return f"x{CLASS_SEPARATOR}{joined}{CLASS_SEPARATOR}{name}"
    return f"x_{name}"


def body_lines(node: ast.AST) -> set[int]:
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
    return lines


def entry(
    module: str,
    parts: tuple[str, ...],
    *,
    mutants: int = 1,
    reason: str | None = None,
) -> dict:
    source = SOURCES[module]
    node, class_chain, func_chain = locate(source, parts)
    key = mangled_key(class_chain, func_chain, parts[-1])
    mutant_ids = (
        [f"app.{module}.{key}__mutmut_{index}" for index in range(1, mutants + 1)]
        if key is not None and mutants
        else []
    )
    if key is None:
        reason = "nested function is outside mutmut's top-level mutation scope"
    return {
        "qualified_name": f"app.{module}.{'.'.join(parts)}",
        "path": f"app/{module}.py",
        "source_sha256": function_hash(node),
        "test_nodeids": [f"tests/unit/test_{module}.py::test_{parts[-1]}"],
        "mutant_ids": mutant_ids,
        "no_operator_reason": reason,
    }


def inventory(entries: list[dict], revision: str = "rev1") -> dict:
    return {"source_revision": revision, "functions": entries}


def default_inventory() -> dict:
    return inventory(
        [
            entry("conversation", ("start",)),
            entry("validation", ("validate",)),
            entry("storage", ("save",)),
            entry("audio", ("transcribe",)),
            entry("providers", ("OpenAICompatibleProvider", "chat")),
            entry("config", ("Settings", "from_env")),
            entry("retrieval", ("search",)),
            entry("retrieval", ("outer",)),
            entry("retrieval", ("outer", "inner")),
            *(
                entry(module, (f"{module}_helper",))
                for module in ("intent", "plans", "replay", "provider_wire", "speech_wav")
            ),
        ]
    )


def build_coverage(root: Path, sources: dict[str, str]) -> Path:
    """Emit a coverage.py JSON report with per-line test contexts, as pytest-cov does."""
    files = {}
    for module, source in sources.items():
        executed: set[int] = set()
        contexts: dict[str, list[str]] = {}
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            executed.add(node.lineno)
            nodeid = f"tests/unit/test_{module}.py::test_{node.name}|run"
            for line in body_lines(node):
                executed.add(line)
                contexts[str(line)] = [nodeid]
        files[f"app/{module}.py"] = {
            "executed_lines": sorted(executed),
            "missing_lines": [],
            "excluded_lines": [],
            "contexts": contexts,
            "summary": {"covered_lines": len(executed), "num_statements": len(executed)},
        }
    path = root / "coverage.json"
    path.write_text(json.dumps({"meta": {"format": 3}, "files": files}), encoding="utf-8")
    return path


def review(
    inventory_data: dict,
    *,
    revision: str = "rev1",
    approvals: list[dict] | None = None,
    function_approvals: list[dict] | None = None,
) -> dict:
    if function_approvals is None:
        function_approvals = [
            {
                "qualified_name": item["qualified_name"],
                "source_sha256": item["source_sha256"],
                "rationale": "mutmut generates no mutants for this function",
                "source_revision": revision,
                "verifier": "independent-verifier",
                "decision": "approved",
            }
            for item in inventory_data["functions"]
            if not item["mutant_ids"]
        ]
    return {
        "source_revision": revision,
        "approvals": approvals or [],
        "function_approvals": function_approvals,
    }


def write_evidence(
    root: Path,
    *,
    sources: dict[str, str] | None = None,
    inventory_data: dict | None = None,
    review_data: dict | None = None,
    coverage_data: Path | None = None,
):
    sources = SOURCES if sources is None else sources
    write_sources(root, sources)
    coverage_path = coverage_data or build_coverage(root, sources)
    inventory_data = default_inventory() if inventory_data is None else inventory_data
    inventory_path = root / "mutation-inventory.json"
    inventory_path.write_text(json.dumps(inventory_data), encoding="utf-8")
    review_path = root / "mutation-review.json"
    if review_data is not None:
        review_path.write_text(json.dumps(review_data), encoding="utf-8")
    return coverage_path, inventory_path, review_path


def run_checker(paths) -> int:
    coverage_path, inventory_path, review_path = paths
    return check_mutation_inventory.main(
        [
            "--coverage",
            str(coverage_path),
            "--inventory",
            str(inventory_path),
            "--review",
            str(review_path),
        ]
    )


def test_complete_inventory_passes(tmp_path: Path) -> None:
    paths = write_evidence(tmp_path, review_data=review(default_inventory()))
    assert run_checker(paths) == 0


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        check_mutation_inventory.main(["--help"])
    assert excinfo.value.code == 0


def test_missing_module_fails(tmp_path: Path) -> None:
    sources = dict(SOURCES)
    sources.pop("audio")
    paths = write_evidence(tmp_path, sources=sources, review_data=review(default_inventory()))
    assert run_checker(paths) != 0


def test_missing_inventory_entry_fails(tmp_path: Path) -> None:
    data = default_inventory()
    data["functions"].pop()
    paths = write_evidence(tmp_path, inventory_data=data, review_data=review(data))
    assert run_checker(paths) != 0


def test_unknown_inventory_entry_fails(tmp_path: Path) -> None:
    data = default_inventory()
    data["functions"].append(entry("audio", ("transcribe",)))
    data["functions"][-1]["qualified_name"] = "app.audio.ghost"
    paths = write_evidence(tmp_path, inventory_data=data, review_data=review(data))
    assert run_checker(paths) != 0


def test_duplicate_inventory_entry_fails(tmp_path: Path) -> None:
    data = default_inventory()
    data["functions"].append(dict(data["functions"][0]))
    paths = write_evidence(tmp_path, inventory_data=data, review_data=review(data))
    assert run_checker(paths) != 0


def test_unknown_field_fails(tmp_path: Path) -> None:
    data = default_inventory()
    data["functions"][0]["confidence"] = "high"
    paths = write_evidence(tmp_path, inventory_data=data, review_data=review(data))
    assert run_checker(paths) != 0


def test_stale_source_hash_fails(tmp_path: Path) -> None:
    data = default_inventory()
    data["functions"][0]["source_sha256"] = "0" * 64
    paths = write_evidence(tmp_path, inventory_data=data, review_data=review(data))
    assert run_checker(paths) != 0


def test_wholly_uncovered_function_fails(tmp_path: Path) -> None:
    coverage = json.loads(build_coverage(tmp_path, SOURCES).read_text(encoding="utf-8"))
    file_entry = coverage["files"]["app/validation.py"]
    node, _, _ = locate(SOURCES["validation"], ("validate",))
    body = sorted(body_lines(node))
    file_entry["executed_lines"] = sorted(set(file_entry["executed_lines"]) - set(body))
    file_entry["missing_lines"] = body
    for line in body:
        file_entry["contexts"].pop(str(line), None)
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    paths = write_evidence(
        tmp_path, review_data=review(default_inventory()), coverage_data=coverage_path
    )
    assert run_checker(paths) != 0


def test_declaration_line_coverage_only_fails(tmp_path: Path) -> None:
    coverage = json.loads(build_coverage(tmp_path, SOURCES).read_text(encoding="utf-8"))
    file_entry = coverage["files"]["app/storage.py"]
    node, _, _ = locate(SOURCES["storage"], ("save",))
    body = sorted(body_lines(node))
    file_entry["executed_lines"] = [node.lineno]
    file_entry["missing_lines"] = body
    for line in body:
        file_entry["contexts"].pop(str(line), None)
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    paths = write_evidence(
        tmp_path, review_data=review(default_inventory()), coverage_data=coverage_path
    )
    assert run_checker(paths) != 0


def test_unmapped_test_nodeid_fails(tmp_path: Path) -> None:
    data = default_inventory()
    data["functions"][0]["test_nodeids"] = ["tests/unit/test_ghost.py::test_nothing"]
    paths = write_evidence(tmp_path, inventory_data=data, review_data=review(data))
    assert run_checker(paths) != 0


def test_missing_test_nodeids_fails(tmp_path: Path) -> None:
    data = default_inventory()
    data["functions"][0]["test_nodeids"] = []
    paths = write_evidence(tmp_path, inventory_data=data, review_data=review(data))
    assert run_checker(paths) != 0


def test_mutant_id_not_matching_function_fails(tmp_path: Path) -> None:
    data = default_inventory()
    data["functions"][0]["mutant_ids"] = ["app.audio.x_transcribe__mutmut_1"]
    paths = write_evidence(tmp_path, inventory_data=data, review_data=review(data))
    assert run_checker(paths) != 0


def test_no_variants_without_reason_fails(tmp_path: Path) -> None:
    data = default_inventory()
    nested = next(
        item
        for item in data["functions"]
        if item["qualified_name"] == "app.retrieval.outer.inner"
    )
    nested["no_operator_reason"] = None
    paths = write_evidence(tmp_path, inventory_data=data, review_data=review(data))
    assert run_checker(paths) != 0


def test_no_variants_without_approval_fails(tmp_path: Path) -> None:
    data = default_inventory()
    paths = write_evidence(
        tmp_path, inventory_data=data, review_data=review(data, function_approvals=[])
    )
    assert run_checker(paths) != 0


def test_no_variants_with_stale_approval_hash_fails(tmp_path: Path) -> None:
    data = default_inventory()
    review_data = review(data)
    review_data["function_approvals"][0]["source_sha256"] = "0" * 64
    paths = write_evidence(tmp_path, inventory_data=data, review_data=review_data)
    assert run_checker(paths) != 0


def test_review_revision_mismatch_fails(tmp_path: Path) -> None:
    data = default_inventory()
    paths = write_evidence(
        tmp_path, inventory_data=data, review_data=review(data, revision="rev0")
    )
    assert run_checker(paths) != 0


def test_missing_review_file_fails(tmp_path: Path) -> None:
    paths = write_evidence(tmp_path, review_data=None)
    assert run_checker(paths) != 0


def test_malformed_inventory_fails(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    for module, source in SOURCES.items():
        (tmp_path / "app" / f"{module}.py").write_text(source, encoding="utf-8")
    coverage_path = build_coverage(tmp_path, SOURCES)
    inventory_path = tmp_path / "mutation-inventory.json"
    inventory_path.write_text("{not json", encoding="utf-8")
    review_path = tmp_path / "mutation-review.json"
    review_path.write_text(json.dumps(review(default_inventory())), encoding="utf-8")
    assert run_checker((coverage_path, inventory_path, review_path)) != 0


def test_coverage_without_contexts_fails(tmp_path: Path) -> None:
    coverage = json.loads(build_coverage(tmp_path, SOURCES).read_text(encoding="utf-8"))
    for file_entry in coverage["files"].values():
        file_entry.pop("contexts")
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    paths = write_evidence(
        tmp_path, review_data=review(default_inventory()), coverage_data=coverage_path
    )
    assert run_checker(paths) != 0


# --- exporter slice -------------------------------------------------------

import export_mutation_inventory  # noqa: E402


def write_mutants(
    root: Path, sources: dict[str, str], counts: dict[str, int] | None = None
) -> None:
    """Write mutmut 3.8.0 per-source-file state exactly as the framework does."""
    counts = counts or {}
    target = root / "mutants" / "app"
    target.mkdir(parents=True, exist_ok=True)
    for module, source in sources.items():
        hashes: dict[str, str] = {}
        exit_codes: dict[str, int] = {}
        for node in ast.parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = f"x_{node.name}"
                qualified = f"app.{module}.{node.name}"
                hashes[key] = function_hash(node)[:12]
                for index in range(1, counts.get(qualified, 1) + 1):
                    exit_codes[f"app.{module}.{key}__mutmut_{index}"] = 1
            elif isinstance(node, ast.ClassDef):
                if any(
                    isinstance(base, ast.Name) and base.id == "Protocol"
                    for base in node.bases
                ):
                    continue
                for child in node.body:
                    if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    key = f"x{CLASS_SEPARATOR}{node.name}{CLASS_SEPARATOR}{child.name}"
                    qualified = f"app.{module}.{node.name}.{child.name}"
                    hashes[key] = function_hash(child)[:12]
                    for index in range(1, counts.get(qualified, 1) + 1):
                        exit_codes[f"app.{module}.{key}__mutmut_{index}"] = 1
        (target / f"{module}.py.meta").write_text(
            json.dumps(
                {
                    "exit_code_by_key": exit_codes,
                    "hash_by_function_name": hashes,
                    "type_check_error_by_key": {},
                    "durations_by_key": {},
                    "estimated_durations_by_key": {},
                }
            ),
            encoding="utf-8",
        )


def export(root: Path, revision: str = "rev1") -> Path:
    coverage_path = build_coverage(root, SOURCES)
    output = root / "reports" / "mutation-inventory.json"
    code = export_mutation_inventory.main(
        [
            "--source-root",
            "app",
            "--mutants",
            "mutants",
            "--coverage",
            str(coverage_path),
            "--output",
            str(output),
            "--source-revision",
            revision,
        ]
    )
    assert code == 0
    return output


def test_exporter_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        export_mutation_inventory.main(["--help"])
    assert excinfo.value.code == 0


def test_exporter_roundtrip_passes_the_gate(tmp_path: Path) -> None:
    write_sources(tmp_path, SOURCES)
    write_mutants(tmp_path, SOURCES, {"app.conversation.start": 2})
    inventory_path = export(tmp_path)
    data = json.loads(inventory_path.read_text(encoding="utf-8"))
    names = {item["qualified_name"] for item in data["functions"]}
    assert names == {
        "app.conversation.start",
        "app.validation.validate",
        "app.storage.save",
        "app.audio.transcribe",
        "app.providers.OpenAICompatibleProvider.chat",
        "app.config.Settings.from_env",
        "app.retrieval.search",
        "app.retrieval.outer",
        "app.retrieval.outer.inner",
        *(
            f"app.{module}.{module}_helper"
            for module in ("intent", "plans", "replay", "provider_wire", "speech_wav")
        ),
    }
    assert data["source_revision"] == "rev1"
    start = next(
        item for item in data["functions"] if item["qualified_name"] == "app.conversation.start"
    )
    assert start["mutant_ids"] == [
        "app.conversation.x_start__mutmut_1",
        "app.conversation.x_start__mutmut_2",
    ]
    nested = next(
        item
        for item in data["functions"]
        if item["qualified_name"] == "app.retrieval.outer.inner"
    )
    assert nested["mutant_ids"] == []
    assert nested["no_operator_reason"]

    coverage_path = build_coverage(tmp_path, SOURCES)
    review_path = tmp_path / "mutation-review.json"
    review_path.write_text(json.dumps(review(data)), encoding="utf-8")
    assert run_checker((coverage_path, inventory_path, review_path)) == 0


def test_exporter_rejects_stale_framework_state(tmp_path: Path) -> None:
    write_sources(tmp_path, SOURCES)
    write_mutants(tmp_path, SOURCES)
    meta_path = tmp_path / "mutants" / "app" / "config.py.meta"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["hash_by_function_name"]["x\u01c1Settings\u01c1from_env"] = "0" * 12
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    coverage_path = build_coverage(tmp_path, SOURCES)
    output = tmp_path / "reports" / "mutation-inventory.json"
    code = export_mutation_inventory.main(
        [
            "--source-root",
            "app",
            "--mutants",
            "mutants",
            "--coverage",
            str(coverage_path),
            "--output",
            str(output),
            "--source-revision",
            "rev1",
        ]
    )
    assert code != 0
    assert not output.exists()


def test_exporter_rejects_missing_framework_state(tmp_path: Path) -> None:
    write_sources(tmp_path, SOURCES)
    write_mutants(tmp_path, SOURCES)
    (tmp_path / "mutants" / "app" / "audio.py.meta").unlink()
    coverage_path = build_coverage(tmp_path, SOURCES)
    output = tmp_path / "reports" / "mutation-inventory.json"
    code = export_mutation_inventory.main(
        [
            "--source-root",
            "app",
            "--mutants",
            "mutants",
            "--coverage",
            str(coverage_path),
            "--output",
            str(output),
            "--source-revision",
            "rev1",
        ]
    )
    assert code != 0


def test_exporter_rejects_missing_source_module(tmp_path: Path) -> None:
    write_sources(tmp_path, SOURCES)
    write_mutants(tmp_path, SOURCES)
    (tmp_path / "app" / "audio.py").unlink()
    coverage_path = build_coverage(tmp_path, SOURCES)
    output = tmp_path / "reports" / "mutation-inventory.json"
    code = export_mutation_inventory.main(
        [
            "--source-root",
            "app",
            "--mutants",
            "mutants",
            "--coverage",
            str(coverage_path),
            "--output",
            str(output),
            "--source-revision",
            "rev1",
        ]
    )
    assert code != 0


# --- commit-safe revision policy ---------------------------------


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def init_repo(root: Path) -> None:
    git(root, "init", "--quiet")
    git(root, "config", "user.email", "evidence@example.test")
    git(root, "config", "user.name", "Evidence Test")
    git(root, "config", "commit.gpgsign", "false")


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "--message", message)
    return git(root, "rev-parse", "HEAD")


def write_repo_inputs(root: Path) -> None:
    """Create one file under every gated input path."""
    write_sources(root, SOURCES)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_unit.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "gate.py").write_text("GATE = 1\n", encoding="utf-8")


def test_inventory_at_inputs_revision_passes_after_reports_only_commit(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_repo_inputs(tmp_path)
    inputs_revision = commit_all(tmp_path, "code at inputs revision")
    data = default_inventory()
    data["source_revision"] = inputs_revision
    paths = write_evidence(
        tmp_path, inventory_data=data, review_data=review(data, revision=inputs_revision)
    )
    commit_all(tmp_path, "store evidence")
    assert run_checker(paths) == 0


def test_commit_touching_app_after_inventory_fails(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_repo_inputs(tmp_path)
    inputs_revision = commit_all(tmp_path, "code at inputs revision")
    data = default_inventory()
    data["source_revision"] = inputs_revision
    paths = write_evidence(
        tmp_path, inventory_data=data, review_data=review(data, revision=inputs_revision)
    )
    commit_all(tmp_path, "store evidence")
    (tmp_path / "app" / "audio.py").write_text(
        "def transcribe(chunk):\n    return chunk.decode('utf-8')\n", encoding="utf-8"
    )
    commit_all(tmp_path, "later app change")
    assert run_checker(paths) != 0


def test_commit_touching_tests_after_inventory_fails(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_repo_inputs(tmp_path)
    inputs_revision = commit_all(tmp_path, "code at inputs revision")
    data = default_inventory()
    data["source_revision"] = inputs_revision
    paths = write_evidence(
        tmp_path, inventory_data=data, review_data=review(data, revision=inputs_revision)
    )
    commit_all(tmp_path, "store evidence")
    (tmp_path / "tests" / "test_unit.py").write_text(
        "def test_x():\n    assert 1\n", encoding="utf-8"
    )
    commit_all(tmp_path, "later test change")
    assert run_checker(paths) != 0


def test_disagreeing_inventory_and_review_revisions_fail(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_repo_inputs(tmp_path)
    inputs_revision = commit_all(tmp_path, "code at inputs revision")
    data = default_inventory()
    data["source_revision"] = inputs_revision
    paths = write_evidence(
        tmp_path, inventory_data=data, review_data=review(data, revision="0" * 40)
    )
    commit_all(tmp_path, "store evidence")
    assert run_checker(paths) != 0
