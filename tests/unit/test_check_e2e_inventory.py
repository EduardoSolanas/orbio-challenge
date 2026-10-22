"""Tests for scripts/check_e2e_inventory.py (ORB-15).

Fixtures mirror the evidence files emitted by the pytest collection/results hooks
described in docs/implementation-contract.md 13.4 and docs/testing-contract.md 8.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_e2e_inventory  # noqa: E402

JOURNEY_IDS = [f"J{i:02d}" for i in range(1, 11)]


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
    collected["deselected"] = [nodeid("J03")]
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
