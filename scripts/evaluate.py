#!/usr/bin/env python3
"""Operator-invoked live evaluation of intent detection and FAQ retrieval.

Runs the labelled utterances in ``tests/eval/utterances.json`` against the
services in the process environment (a ``.env`` file in the repository root is
read for any variable not already set). It is never part of the automatic test
run: it calls the real LLM and, when ``EMBED_*`` is configured, the embedding
model.

* ``intents``: one extraction per utterance; reports accuracy per intent and every miss.
* ``retrieval``: the best two records per question, from embeddings (when
  configured) and from keyword matching; reports hit rate, correct abstentions
  and every miss.

It writes ``reports/evaluation.json`` and exits 0 whatever the score: it measures,
it does not gate.

Usage:
    python scripts/evaluate.py [intents|frustration|retrieval|all] [--cases PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.config import Settings  # noqa: E402
from gateway.conversation.safety import SafetyIndex, load_examples  # noqa: E402
from gateway.knowledge.retrieval import load_knowledge  # noqa: E402
from gateway.knowledge.semantic import SemanticIndex  # noqa: E402
from gateway.main import (  # noqa: E402
    DEFAULT_KNOWLEDGE_PATH,
    DEFAULT_SAFETY_EXAMPLES_PATH,
    _build_provider,
)
from gateway.schemas import ExtractionContext, Slots  # noqa: E402

DEFAULT_CASES = REPO_ROOT / "tests" / "eval" / "utterances.json"
DEFAULT_REPORT = REPO_ROOT / "reports" / "evaluation.json"


def load_dotenv() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
                value = value[
                    1:-1
                ]  # the quotes Compose strips, e.g. LLM_HEADERS='{...}'
            os.environ.setdefault(key, value)
    # Compose supplies these two; a local run does not need them to be real.
    os.environ.setdefault(
        "SESSION_DIR", str(REPO_ROOT / "reports" / "evaluation-sessions")
    )
    os.environ.setdefault("BACKEND_API_BASE_URL", "http://127.0.0.1:8001")


def context_for(text: str) -> ExtractionContext:
    return ExtractionContext(
        slots=Slots(order_number=None, category=None, description=None, urgency=None),
        pending_clarifications={},
        recent_messages=[],
        current_message=text,
        status="collecting",
        intake_started=False,
        frustration_active=False,
    )


async def evaluate_intents(provider, cases: list[dict]) -> dict:
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    misses = []
    for case in cases:
        try:
            got = (await provider.extract(context_for(case["text"]))).intent
        except Exception as error:  # noqa: BLE001 - one bad call must not stop the run
            got = f"error:{getattr(error, 'code', type(error).__name__)}"
        totals[case["intent"]][1] += 1
        if got == case["intent"]:
            totals[case["intent"]][0] += 1
        else:
            misses.append(
                {"text": case["text"], "expected": case["intent"], "got": got}
            )
    correct = sum(right for right, _ in totals.values())
    count = sum(total for _, total in totals.values())
    print(f"\nIntent detection: {correct}/{count} ({100 * correct / count:.0f}%)")
    for intent, (right, total) in sorted(totals.items()):
        print(f"  {intent:20} {right}/{total}")
    for miss in misses:
        print(
            f"  miss: {miss['text']!r} expected {miss['expected']}, got {miss['got']}"
        )
    return {"correct": correct, "total": count, "misses": misses}


async def evaluate_safety(provider, cases: list[dict], ordinary: list[dict]) -> dict:
    """Recall and false alarms of the gas-safety triage: embedding check, model flag, both.

    ``ordinary`` are everyday messages (the intent set) that must never raise the alarm.
    """

    index = SafetyIndex(load_examples(DEFAULT_SAFETY_EXAMPLES_PATH), provider.embed)
    rows = []
    for case in [*cases, *ordinary]:
        try:
            embedding = await index.concerning(case["text"])
        except Exception:  # noqa: BLE001 - one bad call must not stop the run
            embedding = False
        try:
            model = (await provider.extract(context_for(case["text"]))).safety_concern
        except Exception:  # noqa: BLE001
            model = False
        rows.append((case, embedding, model))
    layers = {
        "embedding": lambda embedding, model: embedding,
        "model flag": lambda embedding, model: model,
        "either": lambda embedding, model: embedding or model,
    }
    labelled = [(c, e, m) for c, e, m in rows[: len(cases)]]
    everyday = [(c, e, m) for c, e, m in rows[len(cases) :]]
    positives = sum(1 for c, _, _ in labelled if c["concern"])
    lookalikes = len(labelled) - positives
    report = {}
    print("\nSafety triage (recall matters most):")
    for name, fires in layers.items():
        caught = sum(1 for c, e, m in labelled if c["concern"] and fires(e, m))
        alarms = sum(1 for c, e, m in labelled if not c["concern"] and fires(e, m))
        everyday_alarms = sum(1 for _, e, m in everyday if fires(e, m))
        print(
            f"  {name:11} caught {caught}/{positives}, false alarms {alarms}/{lookalikes} "
            f"look-alikes, {everyday_alarms}/{len(everyday)} everyday"
        )
        report[name] = {
            "caught": caught,
            "positives": positives,
            "false_alarms": alarms,
            "look_alikes": lookalikes,
            "everyday_false_alarms": everyday_alarms,
            "everyday": len(everyday),
        }
    combined = layers["either"]
    misses = [
        {
            "text": c["text"],
            "expected": c.get("concern", False),
            "embedding": e,
            "model": m,
        }
        for c, e, m in rows
        if combined(e, m) != c.get("concern", False)
    ]
    for miss in misses:
        print(
            f"  miss: {miss['text']!r} expected {miss['expected']}, "
            f"embedding={miss['embedding']} model={miss['model']}"
        )
    report["misses"] = misses
    return report


async def evaluate_category(provider, cases: list[dict]) -> dict:
    """Does the stored support area match what the customer described?"""

    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    misses = []
    for case in cases:
        try:
            got = (
                await provider.extract(context_for(case["text"]))
            ).candidates.category
        except Exception as error:  # noqa: BLE001 - one bad call must not stop the run
            got = f"error:{getattr(error, 'code', type(error).__name__)}"
        totals[case["category"]][1] += 1
        if got == case["category"]:
            totals[case["category"]][0] += 1
        else:
            misses.append(
                {"text": case["text"], "expected": case["category"], "got": got}
            )
    correct = sum(right for right, _ in totals.values())
    count = sum(total for _, total in totals.values())
    print(f"\nSupport area: {correct}/{count} ({100 * correct / count:.0f}%)")
    for category, (right, total) in sorted(totals.items()):
        print(f"  {category:14} {right}/{total}")
    for miss in misses:
        print(
            f"  miss: {miss['text']!r} expected {miss['expected']}, got {miss['got']}"
        )
    return {"correct": correct, "total": count, "misses": misses}


async def evaluate_frustration(provider, cases: list[dict]) -> dict:
    """A clear 'present' label hands the customer to a simulated agent; 'uncertain' never does."""

    outcomes: dict[str, list[str]] = defaultdict(list)
    misses = []
    for case in cases:
        try:
            got = (await provider.extract(context_for(case["text"]))).frustration
        except Exception as error:  # noqa: BLE001
            got = f"error:{getattr(error, 'code', type(error).__name__)}"
        outcomes[case["frustration"]].append(got)
        if (got == "present") != (case["frustration"] == "present"):
            misses.append(
                {"text": case["text"], "expected": case["frustration"], "got": got}
            )
    handed_off = sum(1 for got in outcomes["present"] if got == "present")
    calm_ok = sum(1 for got in outcomes["none"] if got != "present")
    print(
        f"\nFrustration hand-off: angry messages handed off {handed_off}/{len(outcomes['present'])}, "
        f"calm or anxious messages kept in the chat {calm_ok}/{len(outcomes['none'])}"
    )
    for label, gots in sorted(outcomes.items()):
        print(
            f"  expected {label:8} labels returned: {dict(sorted((g, gots.count(g)) for g in set(gots)))}"
        )
    for miss in misses:
        print(
            f"  miss: {miss['text']!r} expected {miss['expected']}, got {miss['got']}"
        )
    return {"handed_off": handed_off, "kept": calm_ok, "misses": misses}


def score(name: str, results: list[tuple[dict, list[str]]]) -> dict:
    found = [(c, ids) for c, ids in results if c["record"] is not None]
    abstain = [(c, ids) for c, ids in results if c["record"] is None]
    hits = sum(1 for c, ids in found if c["record"] in ids)
    top1 = sum(1 for c, ids in found if ids and ids[0] == c["record"])
    abstained = sum(1 for _, ids in abstain if not ids)
    print(
        f"  {name:10} right record in top 2: {hits}/{len(found)}  first: {top1}/{len(found)}  "
        f"abstained correctly: {abstained}/{len(abstain)}"
    )
    misses = [
        {"text": c["text"], "expected": c["record"], "got": ids}
        for c, ids in results
        if (c["record"] is None and ids)
        or (c["record"] is not None and c["record"] not in ids)
    ]
    for miss in misses:
        print(
            f"    miss: {miss['text']!r} expected {miss['expected']}, got {miss['got']}"
        )
    return {"hits": hits, "top1": top1, "abstained": abstained, "misses": misses}


async def evaluate_retrieval(
    settings: Settings, provider, cases: list[dict], label: str
) -> dict:
    knowledge = load_knowledge(DEFAULT_KNOWLEDGE_PATH)
    print(f"\nRetrieval, {label} ({len(knowledge.records)} records):")
    report = {
        "keyword": score(
            "keyword",
            [(c, [r.id for r in knowledge.retrieve(c["text"])]) for c in cases],
        )
    }
    if settings.embed is None:
        print("  embedding  skipped: EMBED_* is not configured")
        return report
    index = SemanticIndex(
        knowledge.records,
        provider.embed,
        min_similarity=settings.embed_min_similarity,
        alternates=knowledge.spanish_texts(),
    )
    results = []
    for case in cases:
        try:
            ids = [r.id for r in await index.retrieve(case["text"])]
        except Exception as error:  # noqa: BLE001
            ids = [f"error:{getattr(error, 'code', type(error).__name__)}"]
        results.append((case, ids))
    report["embedding"] = score("embedding", results)
    report["embedding"]["min_similarity"] = settings.embed_min_similarity
    return report


async def main(mode: str, cases_path: Path) -> None:
    load_dotenv()
    settings = Settings.from_env()
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    provider = _build_provider(settings)
    report: dict = {}
    try:
        if mode in ("intents", "all"):
            report["intents"] = await evaluate_intents(provider, cases["intents"])
        if mode in ("category", "all"):
            report["category"] = await evaluate_category(provider, cases["category"])
        if mode in ("safety", "all"):
            report["safety"] = await evaluate_safety(
                provider, cases["safety"], cases["intents"]
            )
        if mode in ("frustration", "all"):
            report["frustration"] = await evaluate_frustration(
                provider, cases["frustration"]
            )
        if mode in ("retrieval", "all"):
            # The floor was tuned on "retrieval"; "retrieval_holdout" was written
            # before any score was seen and is the fair comparison.
            for key, label in (
                ("retrieval", "tuning set"),
                ("retrieval_holdout", "held-out set"),
            ):
                report[key] = await evaluate_retrieval(
                    settings, provider, cases[key], label
                )
    finally:
        await provider.close()
    DEFAULT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_REPORT.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8"
    )
    print(f"\nWrote {DEFAULT_REPORT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("intents", "category", "frustration", "safety", "retrieval", "all"),
        default="all",
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    arguments = parser.parse_args()
    asyncio.run(main(arguments.mode, arguments.cases))
