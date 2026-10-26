"""scripts/build_mutation_review.py approves only mutants that provably cannot matter."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_mutation_review as builder  # noqa: E402
import check_mutation_report  # noqa: E402


def mutant(name: str, removed: str, added: str, status: str = "survived") -> dict:
    return {
        "id": f"{name}__mutmut_1",
        "status": status,
        "diff_sha256": "ab" * 32,
        "removed": [removed],
        "added": [added],
    }


@pytest.mark.parametrize(
    ("row", "rule"),
    [
        (
            mutant(
                "gateway.backend_client.xǁBackendApiClientǁ__init__",
                'headers={"Accept": "application/json"},',
                'headers={"accept": "application/json"},',
            ),
            "header name case",
        ),
        (
            mutant(
                "gateway.backend_client.xǁBackendApiClientǁverify_order",
                'self._request("POST", PATH)',
                'self._request("post", PATH)',
            ),
            "http method case",
        ),
        (
            mutant(
                "gateway.knowledge.retrieval.x_load_knowledge",
                'raw.decode("utf-8")',
                'raw.decode("UTF-8")',
            ),
            "codec name case",
        ),
        (
            mutant(
                "gateway.storage.x_migrate_session_document",
                'raise ValueError("unsupported")',
                "raise ValueError(None)",
            ),
            "exception message text",
        ),
        (
            mutant(
                "gateway.localization.x_request_reference",
                'replace("-", "")',
                'replace("XX-XX", "")',
            ),
            "request reference uses the first eight characters",
        ),
        (
            mutant(
                "gateway.conversation.engine.xǁConversationEngineǁ_plan_turn",
                "recovered_this_turn=recovered_this_turn,",
                "recovered_this_turn=None,",
            ),
            "recovered flag already implied by the review check",
        ),
        (
            mutant(
                "gateway.conversation.flows.x_collect_details",
                "return StepResult() if missing is None else StepResult(missing)",
                "return StepResult() if (missing is None) and False else StepResult(missing)",
            ),
            "empty reply is the default reply",
        ),
    ],
)
def test_a_mutant_that_cannot_change_behaviour_is_recognised(row, rule) -> None:
    assert builder.classify(row).name == rule


@pytest.mark.parametrize(
    "row",
    [
        # the same kind of change in a function no rule names
        mutant(
            "gateway.conversation.engine.xǁConversationEngineǁ_plan_turn",
            "completed = False",
            "completed = True",
        ),
        # a boundary change that does alter behaviour
        mutant(
            "gateway.config.x__backend_api_timeout",
            "if not 0 < value <= MAX:",
            "if not 0 <= value <= MAX:",
        ),
        # a header-looking line whose change is more than case
        mutant(
            "gateway.providers.client.x__build_client",
            'headers = {"Accept": "a"}',
            'headers = {"Accept": "b"}',
        ),
    ],
)
def test_anything_that_could_matter_is_left_unapproved(row) -> None:
    assert builder.classify(row) is None


def test_the_review_it_writes_passes_the_report_gate_and_covers_each_decision(
    tmp_path: Path,
) -> None:
    approved = mutant(
        "gateway.localization.x_request_reference",
        'replace("-", "")',
        'replace("-", "XXXX")',
    )
    stub = mutant(
        "gateway.providers.client.xǁAIProviderǁsynthesize",
        'language: str = "en"',
        'language: str = "EN"',
        "no tests",
    )
    open_gap = mutant(
        "gateway.conversation.engine.xǁConversationEngineǁ_plan_turn",
        "completed = False",
        "completed = True",
    )
    results = tmp_path / "results.txt"
    results.write_text(
        "\n".join(
            f"    {row['id']}: {row['status']}" for row in (approved, stub, open_gap)
        )
        + "\n",
        "utf-8",
    )
    diffs = tmp_path / "diffs.json"
    diffs.write_text(json.dumps([approved, stub, open_gap]), "utf-8")
    out = tmp_path / "review.json"

    builder.main(
        [
            "--results",
            str(results),
            "--diffs",
            str(diffs),
            "--revision",
            "abc123",
            "--out",
            str(out),
            "--run-commit",
            "def456",
        ]
    )

    review = json.loads(out.read_text("utf-8"))
    assert (
        review["source_revision"] == "abc123"
        and review["mutation_run_commit"] == "def456"
    )
    assert {a["mutant_id"]: a["status"] for a in review["approvals"]} == {
        approved["id"]: "equivalent",
        stub["id"]: "invalid",
    }
    errors: list[str] = []
    revision, by_mutant = check_mutation_report.validate_review(review, errors)
    assert errors == [] and revision == "abc123" and len(by_mutant) == 2
