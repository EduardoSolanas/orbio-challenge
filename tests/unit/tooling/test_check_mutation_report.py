"""Tests for scripts/check_mutation_report.py.

The run-log and results fixtures use the exact line formats printed by the
installed pinned framework, mutmut 3.8.0:
  * ``mutmut run`` summary: ``<checked>/<total>  🎉 <killed> 🫥 <no tests>  ...``
  * ``mutmut results``: ``    <mutant id>: <status>``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support.git_repo import commit_all, init_repo

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_mutation_report  # noqa: E402

SUMMARY = "reports/mutation-summary.json"


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def run_log(
    *,
    total: int = 5,
    checked: int = 5,
    killed: int = 5,
    no_tests: int = 0,
    timeout: int = 0,
    suspicious: int = 0,
    survived: int = 0,
    skipped: int = 0,
    caught: int = 0,
) -> str:
    """Real mutmut 3.8.0 run output, including progress lines."""
    progress = (
        f"1/{total}  🎉 {min(killed, 1)} 🫥 {no_tests}  ⏰ {timeout}  🤔 {suspicious}  "
        f"🙁 {survived}  🔇 {skipped}  🧙 {caught}"
    )
    final = (
        f"{checked}/{total}  🎉 {killed} 🫥 {no_tests}  ⏰ {timeout}  🤔 {suspicious}  "
        f"🙁 {survived}  🔇 {skipped}  🧙 {caught}"
    )
    return (
        "    done in 1024ms (1 files mutated, 3 ignored, 0 unmodified)\n"
        "Running mutation testing\n"
        f"{progress}\n"
        f"{final}\n"
        "24.14 mutations/second\n"
    )


def review(approvals: list[dict] | None = None, revision: str = "rev1") -> dict:
    return {"source_revision": revision, "approvals": approvals or []}


def approval(
    mutant_id: str,
    status: str,
    *,
    decision: str = "approved",
    verifier: str = "independent-verifier",
    rationale: str = "no observable behaviour changes",
    revision: str = "rev1",
) -> dict:
    return {
        "mutant_id": mutant_id,
        "status": status,
        "rationale": rationale,
        "source_revision": revision,
        "diff_sha256": "0" * 64,
        "verifier": verifier,
        "decision": decision,
    }


def write_evidence(tmp_path: Path, *, run_text: str, results_text: str, review_data: dict | None):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["gateway/"]\n', encoding="utf-8"
    )
    (tmp_path / "mutation-run.txt").write_text(run_text, encoding="utf-8")
    (tmp_path / "mutation-results.txt").write_text(results_text, encoding="utf-8")
    if review_data is not None:
        (tmp_path / "mutation-review.json").write_text(json.dumps(review_data), encoding="utf-8")
    return tmp_path / "mutation-run.txt", tmp_path / "mutation-results.txt", tmp_path / "mutation-review.json"


def run(tmp_path: Path, paths) -> int:
    run_path, results_path, review_path = paths
    return check_mutation_report.main(
        [
            "--results",
            str(results_path),
            "--run-log",
            str(run_path),
            "--review",
            str(review_path),
        ]
    )


def test_all_killed_run_passes_and_writes_summary(tmp_path: Path) -> None:
    paths = write_evidence(tmp_path, run_text=run_log(), results_text="", review_data=review())
    assert run(tmp_path, paths) == 0
    summary = json.loads((tmp_path / SUMMARY).read_text(encoding="utf-8"))
    assert summary["generated"] == 5
    assert summary["executed"] == 5
    assert summary["killed"] == 5
    assert summary["gate"] == "passed"


def test_unapproved_survivor_fails(tmp_path: Path) -> None:
    results_text = "    app.config.xǁSettingsǁfrom_env__mutmut_6: survived\n"
    paths = write_evidence(
        tmp_path, run_text=run_log(killed=4, survived=1), results_text=results_text, review_data=review()
    )
    assert run(tmp_path, paths) != 0


def test_approved_equivalent_survivor_passes(tmp_path: Path) -> None:
    mutant = "gateway.config.xǁSettingsǁfrom_env__mutmut_6"
    results_text = f"    {mutant}: survived\n"
    paths = write_evidence(
        tmp_path,
        run_text=run_log(killed=4, survived=1),
        results_text=results_text,
        review_data=review([approval(mutant, "equivalent")]),
    )
    assert run(tmp_path, paths) == 0
    summary = json.loads((tmp_path / SUMMARY).read_text(encoding="utf-8"))
    assert summary["equivalent_survivors"] == 1
    assert summary["unresolved"] == 0


def test_survivor_approved_with_invalid_status_fails(tmp_path: Path) -> None:
    mutant = "gateway.config.xǁSettingsǁfrom_env__mutmut_6"
    results_text = f"    {mutant}: survived\n"
    paths = write_evidence(
        tmp_path,
        run_text=run_log(killed=4, survived=1),
        results_text=results_text,
        review_data=review([approval(mutant, "invalid")]),
    )
    assert run(tmp_path, paths) != 0


def test_approved_invalid_timeout_passes(tmp_path: Path) -> None:
    mutant = "gateway.config.xǁSettingsǁfrom_env__mutmut_7"
    results_text = f"    {mutant}: timeout\n"
    paths = write_evidence(
        tmp_path,
        run_text=run_log(killed=4, timeout=1),
        results_text=results_text,
        review_data=review([approval(mutant, "invalid")]),
    )
    assert run(tmp_path, paths) == 0
    summary = json.loads((tmp_path / SUMMARY).read_text(encoding="utf-8"))
    assert summary["invalid_variants"] == 1


def test_unapproved_timeout_fails(tmp_path: Path) -> None:
    mutant = "gateway.config.xǁSettingsǁfrom_env__mutmut_7"
    results_text = f"    {mutant}: timeout\n"
    paths = write_evidence(
        tmp_path, run_text=run_log(killed=4, timeout=1), results_text=results_text, review_data=review()
    )
    assert run(tmp_path, paths) != 0


def test_malformed_results_line_fails(tmp_path: Path) -> None:
    paths = write_evidence(
        tmp_path,
        run_text=run_log(),
        results_text="    this is not framework output\n",
        review_data=review(),
    )
    assert run(tmp_path, paths) != 0


def test_missing_run_summary_fails(tmp_path: Path) -> None:
    paths = write_evidence(
        tmp_path, run_text="Running mutation testing\n", results_text="", review_data=review()
    )
    assert run(tmp_path, paths) != 0


def test_zero_generated_run_fails(tmp_path: Path) -> None:
    zero = "0/0  🎉 0 🫥 0  ⏰ 0  🤔 0  🙁 0  🔇 0  🧙 0\n"
    paths = write_evidence(tmp_path, run_text=zero, results_text="", review_data=review())
    assert run(tmp_path, paths) != 0


def test_unexecuted_mutants_fail(tmp_path: Path) -> None:
    paths = write_evidence(
        tmp_path,
        run_text=run_log(total=5, checked=4, killed=4, survived=1),
        results_text="    app.config.x_covered__mutmut_1: survived\n",
        review_data=review([approval("gateway.config.x_covered__mutmut_1", "equivalent")]),
    )
    assert run(tmp_path, paths) != 0


def test_result_count_mismatch_fails(tmp_path: Path) -> None:
    paths = write_evidence(
        tmp_path,
        run_text=run_log(killed=3, survived=2),
        results_text="    app.config.x_covered__mutmut_1: survived\n",
        review_data=review([approval("gateway.config.x_covered__mutmut_1", "equivalent")]),
    )
    assert run(tmp_path, paths) != 0


def test_duplicate_result_line_fails(tmp_path: Path) -> None:
    mutant = "gateway.config.x_covered__mutmut_1"
    results_text = f"    {mutant}: survived\n    {mutant}: survived\n"
    paths = write_evidence(
        tmp_path,
        run_text=run_log(killed=3, survived=2),
        results_text=results_text,
        review_data=review([approval(mutant, "equivalent")]),
    )
    assert run(tmp_path, paths) != 0


def test_missing_review_file_fails(tmp_path: Path) -> None:
    paths = write_evidence(tmp_path, run_text=run_log(), results_text="", review_data=None)
    assert run(tmp_path, paths) != 0


def test_approval_without_verifier_fails(tmp_path: Path) -> None:
    mutant = "gateway.config.x_covered__mutmut_1"
    paths = write_evidence(
        tmp_path,
        run_text=run_log(killed=4, survived=1),
        results_text=f"    {mutant}: survived\n",
        review_data=review([approval(mutant, "equivalent", verifier="")]),
    )
    assert run(tmp_path, paths) != 0


def test_approval_without_rationale_fails(tmp_path: Path) -> None:
    mutant = "gateway.config.x_covered__mutmut_1"
    paths = write_evidence(
        tmp_path,
        run_text=run_log(killed=4, survived=1),
        results_text=f"    {mutant}: survived\n",
        review_data=review([approval(mutant, "equivalent", rationale="")]),
    )
    assert run(tmp_path, paths) != 0


def test_approval_for_unknown_mutant_fails(tmp_path: Path) -> None:
    paths = write_evidence(
        tmp_path,
        run_text=run_log(),
        results_text="",
        review_data=review([approval("gateway.config.x_covered__mutmut_99", "equivalent")]),
    )
    assert run(tmp_path, paths) != 0


def test_all_results_include_killed_lines(tmp_path: Path) -> None:
    killed = [f"gateway.config.x__fail__mutmut_{i}" for i in range(1, 5)]
    survived = "gateway.config.x_covered__mutmut_1"
    results_text = "".join(f"    {mutant}: killed\n" for mutant in killed)
    results_text += f"    {survived}: survived\n"
    paths = write_evidence(
        tmp_path,
        run_text=run_log(killed=4, survived=1),
        results_text=results_text,
        review_data=review([approval(survived, "equivalent")]),
    )
    assert run(tmp_path, paths) == 0


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        check_mutation_report.main(["--help"])
    assert excinfo.value.code == 0


# --- commit-safe revision policy ---------------------------------





def write_repo_inputs(root: Path) -> None:
    """Create one file under every gated input path."""
    (root / "gateway").mkdir(parents=True, exist_ok=True)
    (root / "gateway" / "config.py").write_text("class Settings:\n    pass\n", encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_unit.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "gate.py").write_text("GATE = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["gateway/"]\n', encoding="utf-8"
    )


def test_review_at_inputs_revision_passes_after_reports_only_commit(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_repo_inputs(tmp_path)
    inputs_revision = commit_all(tmp_path, "code at inputs revision")
    paths = write_evidence(
        tmp_path,
        run_text=run_log(),
        results_text="",
        review_data=review(revision=inputs_revision),
    )
    commit_all(tmp_path, "store evidence")
    assert run(tmp_path, paths) == 0


def test_commit_touching_app_after_review_fails(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_repo_inputs(tmp_path)
    inputs_revision = commit_all(tmp_path, "code at inputs revision")
    paths = write_evidence(
        tmp_path,
        run_text=run_log(),
        results_text="",
        review_data=review(revision=inputs_revision),
    )
    commit_all(tmp_path, "store evidence")
    (tmp_path / "gateway" / "config.py").write_text(
        "class Settings:\n    value = 2\n", encoding="utf-8"
    )
    commit_all(tmp_path, "later app change")
    assert run(tmp_path, paths) != 0


def test_commit_touching_tests_after_review_fails(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_repo_inputs(tmp_path)
    inputs_revision = commit_all(tmp_path, "code at inputs revision")
    paths = write_evidence(
        tmp_path,
        run_text=run_log(),
        results_text="",
        review_data=review(revision=inputs_revision),
    )
    commit_all(tmp_path, "store evidence")
    (tmp_path / "tests" / "test_unit.py").write_text(
        "def test_x():\n    assert 1\n", encoding="utf-8"
    )
    commit_all(tmp_path, "later test change")
    assert run(tmp_path, paths) != 0


def test_review_revision_from_an_older_commit_fails(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_repo_inputs(tmp_path)
    stale_revision = commit_all(tmp_path, "code at inputs revision")
    (tmp_path / "gateway" / "config.py").write_text(
        "class Settings:\n    value = 3\n", encoding="utf-8"
    )
    commit_all(tmp_path, "later app change")
    paths = write_evidence(
        tmp_path,
        run_text=run_log(),
        results_text="",
        review_data=review(revision=stale_revision),
    )
    assert run(tmp_path, paths) != 0
