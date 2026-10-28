"""Mutation drafts must preserve evidence without approving their own exclusions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import build_mutation_review as builder
import check_mutation_report as gate


def rows():
    return [
        dict(
            id=f"{name}__mutmut_1",
            status=status,
            diff_sha256="ab" * 32,
            removed=[before],
            added=[after],
        )
        for name, status, before, after in [
            (
                "gateway.audio.x_validate_wav",
                "survived",
                "if peak < 128:",
                "if peak <= 128:",
            ),
            (
                "gateway.replay.x_booking_digest",
                "survived",
                "starts_at=command.starts_at",
                "starts_at=None",
            ),
            (
                "gateway.backend.x_init",
                "survived",
                'url.rstrip("/")',
                'url.rstrip("XX/XX")',
            ),
            (
                "gateway.client.x_synthesize",
                "no tests",
                'language="en"',
                'language="EN"',
            ),
        ]
    ]


def draft(tmp_path, diffs, results=None):
    results = diffs if results is None else results
    (tmp_path / "results.txt").write_text(
        "\n".join(f"{r['id']}: {r['status']}" for r in results), "utf-8"
    )
    (tmp_path / "diffs.json").write_text(json.dumps(diffs), "utf-8")
    output = tmp_path / "review.json"
    assert (
        builder.main(
            [
                "--results",
                str(tmp_path / "results.txt"),
                "--diffs",
                str(tmp_path / "diffs.json"),
                "--revision",
                "a" * 40,
                "--run-commit",
                "b" * 40,
                "--out",
                str(output),
            ]
        )
        == 0
    )
    return output


def test_drafts_preserve_all_non_killed_diffs_without_approving_or_classifying_them(
    tmp_path,
):
    data = json.loads(draft(tmp_path, rows()).read_text("utf-8"))
    assert data["approvals"] == []
    assert data["source_revision"] == "a" * 40
    assert data["mutation_run_commit"] == "b" * 40
    assert len(data["proposals"]) == 4
    for proposal, original in zip(data["proposals"], rows(), strict=True):
        assert proposal["mutant_id"] == original["id"]
        assert proposal["framework_status"] == original["status"]
        assert proposal["removed"] == original["removed"]
        assert proposal["added"] == original["added"]
        assert proposal["diff_sha256"] == original["diff_sha256"]
        assert proposal["decision"] == "pending"
        assert "verifier" not in proposal
        assert "status" not in proposal


def test_a_draft_cannot_make_the_mutation_gate_pass(tmp_path, monkeypatch):
    output = draft(tmp_path, rows())
    log = tmp_path / "run.txt"
    log.write_text("5/5 🎉 1 🫥 1 ⏰ 0 🤔 0 🙁 3 🔇 0 🧙 0", "utf-8")
    monkeypatch.setattr(gate, "SUMMARY_PATH", tmp_path / "summary.json")
    monkeypatch.setattr(gate, "gated_input_revision", lambda: "a" * 40)
    assert (
        gate.main(
            [
                "--results",
                str(tmp_path / "results.txt"),
                "--run-log",
                str(log),
                "--review",
                str(output),
            ]
        )
        == 1
    )
    summary = json.loads((tmp_path / "summary.json").read_text("utf-8"))
    assert summary["gate"] == "blocked" and summary["unresolved"] == 4
    assert summary["equivalent_survivors"] == summary["invalid_variants"] == 0


def test_all_killed_results_need_no_exclusions(tmp_path):
    data = json.loads(draft(tmp_path, []).read_text("utf-8"))
    assert data["approvals"] == data["proposals"] == []


@pytest.mark.parametrize(
    "problem", ["missing", "duplicate", "unknown", "status", "digest", "diff_lines"]
)
def test_inconsistent_evidence_is_rejected_without_writing_a_review(tmp_path, problem):
    results, diffs = rows(), rows()
    if problem == "missing":
        diffs.pop()
    elif problem == "duplicate":
        diffs.append(diffs[0])
    elif problem == "unknown":
        diffs[0]["id"] = "gateway.unknown__mutmut_99"
    elif problem == "status":
        diffs[0]["status"] = "killed"
    elif problem == "digest":
        diffs[0]["diff_sha256"] = "not-a-hash"
    else:
        diffs[0]["removed"] = "not a list of diff lines"
    with pytest.raises(SystemExit):
        draft(tmp_path, diffs, results)
    assert not (tmp_path / "review.json").exists()
