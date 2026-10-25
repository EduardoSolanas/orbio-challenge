"""Tests for scripts/check_e2e_inventory.py.

Fixtures mirror the evidence files emitted by the pytest collection/results hooks
described in docs/implementation-contract.md 13.4 and docs/testing-contract.md 8.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_e2e_inventory  # noqa: E402

JOURNEY_IDS = [
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
]


def nodeid(journey_id: str) -> str:
    return f"tests/e2e/test_journeys.py::test_{journey_id.lower()}"


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every check outside the repository git checkout."""
    monkeypatch.chdir(tmp_path)


def spec_text(ids: list[str] | None = None) -> str:
    ids = JOURNEY_IDS if ids is None else ids
    rows = "\n".join(f"| {jid} | journey {jid} | checkpoint {jid} |" for jid in ids)
    return (
        "## 13. Integration and end-to-end test specifications\n\n"
        "### 13.3 Ten Playwright end-to-end journeys\n\n"
        "| ID | Complete user workflow | Required end-to-end checkpoints |\n"
        "|---|---|---|\n"
        f"{rows}\n"
    )


def manifest(ids: list[str] | None = None) -> dict:
    ids = JOURNEY_IDS if ids is None else ids
    return {
        "source_revision": "rev1",
        "cases": [
            {
                "id": jid,
                "nodeid": nodeid(jid),
                "requirement_ids": [f"R-{jid}"],
                "checkpoints": [f"checkpoint-{jid}"],
            }
            for jid in ids
        ],
    }


def collection(ids: list[str] | None = None) -> dict:
    ids = JOURNEY_IDS if ids is None else ids
    return {
        "source_revision": "rev1",
        "collected": [nodeid(jid) for jid in ids],
        "deselected": [],
        "errors": [],
    }


def results(ids: list[str] | None = None) -> dict:
    ids = JOURNEY_IDS if ids is None else ids
    return {
        "source_revision": "rev1",
        "outcomes": [
            {
                "nodeid": nodeid(jid),
                "outcome": "passed",
                "phases": {"setup": "passed", "call": "passed", "teardown": "passed"},
                "reruns": 0,
                "checkpoints": [f"checkpoint-{jid}"],
            }
            for jid in ids
        ],
    }


def write_evidence(tmp_path: Path, *, spec=None, cases=None, collected=None, outcomes=None):
    spec_path = tmp_path / "spec.md"
    spec_path.write_text(spec_text() if spec is None else spec, encoding="utf-8")
    manifest_path = tmp_path / "cases.json"
    manifest_path.write_text(json.dumps(manifest() if cases is None else cases), encoding="utf-8")
    collection_path = tmp_path / "e2e-collection.json"
    collection_path.write_text(
        json.dumps(collection() if collected is None else collected), encoding="utf-8"
    )
    results_path = tmp_path / "e2e-results.json"
    results_path.write_text(json.dumps(results() if outcomes is None else outcomes), encoding="utf-8")
    return spec_path, manifest_path, collection_path, results_path


def run(tmp_path: Path, paths) -> int:
    spec_path, manifest_path, collection_path, results_path = paths
    return check_e2e_inventory.main(
        [
            "--spec",
            str(spec_path),
            "--manifest",
            str(manifest_path),
            "--collection",
            str(collection_path),
            "--results",
            str(results_path),
        ]
    )


def test_valid_evidence_passes(tmp_path: Path) -> None:
    assert run(tmp_path, write_evidence(tmp_path)) == 0


def test_spec_must_contain_exactly_j01_to_j10(tmp_path: Path) -> None:
    paths = write_evidence(tmp_path, spec=spec_text(JOURNEY_IDS[:-1]))
    assert run(tmp_path, paths) != 0


def test_manifest_missing_journey_fails(tmp_path: Path) -> None:
    paths = write_evidence(tmp_path, cases=manifest(JOURNEY_IDS[:-1]))
    assert run(tmp_path, paths) != 0


def test_manifest_duplicate_journey_fails(tmp_path: Path) -> None:
    cases = manifest()
    cases["cases"].append(dict(cases["cases"][0]))
    paths = write_evidence(tmp_path, cases=cases)
    assert run(tmp_path, paths) != 0


def test_empty_collection_fails(tmp_path: Path) -> None:
    paths = write_evidence(tmp_path, collected=collection([]))
    assert run(tmp_path, paths) != 0


def test_deselected_journey_fails(tmp_path: Path) -> None:
    collected = collection()
    collected["deselected"] = [nodeid("resume_after_restart")]
    paths = write_evidence(tmp_path, collected=collected)
    assert run(tmp_path, paths) != 0


@pytest.mark.parametrize("outcome", ["skipped", "xfailed", "xpassed", "failed", "error"])
def test_non_passing_outcome_fails(tmp_path: Path, outcome: str) -> None:
    outcomes = results()
    outcomes["outcomes"][4]["outcome"] = outcome
    paths = write_evidence(tmp_path, outcomes=outcomes)
    assert run(tmp_path, paths) != 0


def test_unexecuted_journey_fails(tmp_path: Path) -> None:
    outcomes = results()
    outcomes["outcomes"].pop()
    paths = write_evidence(tmp_path, outcomes=outcomes)
    assert run(tmp_path, paths) != 0


def test_missing_result_phase_fails(tmp_path: Path) -> None:
    outcomes = results()
    outcomes["outcomes"][0]["phases"]["call"] = "failed"
    paths = write_evidence(tmp_path, outcomes=outcomes)
    assert run(tmp_path, paths) != 0


def test_rerun_fails(tmp_path: Path) -> None:
    outcomes = results()
    outcomes["outcomes"][0]["reruns"] = 1
    paths = write_evidence(tmp_path, outcomes=outcomes)
    assert run(tmp_path, paths) != 0


def test_missing_checkpoint_evidence_fails(tmp_path: Path) -> None:
    outcomes = results()
    outcomes["outcomes"][0]["checkpoints"] = []
    paths = write_evidence(tmp_path, outcomes=outcomes)
    assert run(tmp_path, paths) != 0


def test_stale_source_revision_fails(tmp_path: Path) -> None:
    collected = collection()
    collected["source_revision"] = "rev0"
    paths = write_evidence(tmp_path, collected=collected)
    assert run(tmp_path, paths) != 0


def test_missing_evidence_file_fails(tmp_path: Path) -> None:
    paths = list(write_evidence(tmp_path))
    paths[3].unlink()
    assert run(tmp_path, paths) != 0


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        check_e2e_inventory.main(["--help"])
    assert excinfo.value.code == 0


# --- commit-safe revision policy ---------------------------------
# The policy is about commit ancestry, so the only faithful fixture is a real
# temporary git repository with real commits.


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
    (root / "gateway").mkdir(parents=True, exist_ok=True)
    (root / "gateway" / "conversation.py").write_text("def start():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "e2e").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "e2e" / "test_journeys.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8"
    )
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "gate.py").write_text("GATE = 1\n", encoding="utf-8")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "implementation-contract.md").write_text(spec_text(), encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")


def write_repo_evidence(root: Path, revision: str):
    """Lay the evidence out exactly where the real gate reads it."""
    spec_path = root / "docs" / "implementation-contract.md"
    manifest_path = root / "tests" / "e2e" / "cases.json"
    cases = manifest()
    cases["source_revision"] = revision
    manifest_path.write_text(json.dumps(cases), encoding="utf-8")
    collection_path = root / "reports" / "e2e-collection.json"
    collection_path.parent.mkdir(exist_ok=True)
    collected = collection()
    collected["source_revision"] = revision
    collection_path.write_text(json.dumps(collected), encoding="utf-8")
    results_path = root / "reports" / "e2e-results.json"
    outcomes = results()
    outcomes["source_revision"] = revision
    results_path.write_text(json.dumps(outcomes), encoding="utf-8")
    return spec_path, manifest_path, collection_path, results_path


def test_evidence_at_inputs_revision_passes_after_reports_only_commit(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_repo_inputs(tmp_path)
    inputs_revision = commit_all(tmp_path, "code at inputs revision")
    paths = write_repo_evidence(tmp_path, inputs_revision)
    commit_all(tmp_path, "store evidence (reports/ and manifest only)")
    assert run(tmp_path, paths) == 0


def test_commit_touching_app_after_evidence_fails(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_repo_inputs(tmp_path)
    inputs_revision = commit_all(tmp_path, "code at inputs revision")
    paths = write_repo_evidence(tmp_path, inputs_revision)
    commit_all(tmp_path, "store evidence")
    (tmp_path / "gateway" / "conversation.py").write_text(
        "def start():\n    return 2\n", encoding="utf-8"
    )
    commit_all(tmp_path, "later app change")
    assert run(tmp_path, paths) != 0


def test_commit_touching_tests_after_evidence_fails(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_repo_inputs(tmp_path)
    inputs_revision = commit_all(tmp_path, "code at inputs revision")
    paths = write_repo_evidence(tmp_path, inputs_revision)
    commit_all(tmp_path, "store evidence")
    (tmp_path / "tests" / "e2e" / "test_journeys.py").write_text(
        "def test_placeholder():\n    assert 1\n", encoding="utf-8"
    )
    commit_all(tmp_path, "later test change")
    assert run(tmp_path, paths) != 0


def test_disagreeing_evidence_revisions_fail_in_git_checkout(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_repo_inputs(tmp_path)
    inputs_revision = commit_all(tmp_path, "code at inputs revision")
    paths = write_repo_evidence(tmp_path, inputs_revision)
    outcomes = json.loads(paths[3].read_text(encoding="utf-8"))
    outcomes["source_revision"] = "0" * 40
    paths[3].write_text(json.dumps(outcomes), encoding="utf-8")
    commit_all(tmp_path, "store evidence")
    assert run(tmp_path, paths) != 0
