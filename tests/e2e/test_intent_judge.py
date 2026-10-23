"""ORB-36 semantic intent-routing checks layered over deterministic browser assertions."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.judge_config import JudgeConfig, judge, load_config, validate_verdict
from tests.support.fakes import (
    ABSTENTION,
    ASK_CATEGORY,
    CAPABILITY_Q,
    GREETING_RETURN_Q,
    MIXED_PART,
    PAYMENT_Q,
    RESUME,
    THANKS,
    x_capability,
    x_part,
    x_question,
    x_social,
    x_start_intake,
    return_answer,
)

CAPABILITY_REPLY = "I can answer Northstar Shop policy questions or help record an order issue."
JUDGE_RULES = {
    "greeting": "social: brief fixed acknowledgment, no field question or review reminder, no retrieval.",
    "thanks": "social: brief acknowledgment, preserve business state, no retrieval.",
    "facts": "real facts outrank social preface: provide_details starts or continues intake.",
    "policy": "question, supported: answer the policy question with grounded information and no intake follow-up.",
    "capability": "capability: fixed capability response, no retrieval and no intake start.",
    "unsupported": "question, unsupported: safe abstention with no invented facts or citations.",
    "resume": "start_intake: resume and show the first pending clarification or missing field.",
}


@pytest.fixture(scope="session")
def judge_config() -> JudgeConfig:
    return load_config()


def _record_and_judge(config: JudgeConfig, request: pytest.FixtureRequest, conversation: str, key: str) -> None:
    artifact = {
        "model": config.model,
        "prompt_version": config.prompt_version,
        "conversation": conversation,
        "expected_rule": JUDGE_RULES[key],
    }
    try:
        result = judge(config, conversation, JUDGE_RULES[key])
        artifact.update(result)
    except Exception as exc:
        artifact.update({"verdict": {"pass": False, "intent": "judge_failure", "reason": str(exc), "violations": [str(exc)]}})
        path = Path("reports/e2e-judge") / (re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid) + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        raise
    path = Path("reports/e2e-judge") / (re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid) + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert artifact["verdict"]["pass"], f"LLM intent judge failed: {json.dumps(artifact, ensure_ascii=False)}"


def test_intent_judge_rejects_non_strict_output() -> None:
    with pytest.raises(ValueError, match="exactly pass, intent, reason, violations"):
        validate_verdict({"pass": True, "intent": "social", "reason": "ok", "violations": [], "extra": 1})


def test_intent_judge_accepts_passing_and_failing_structured_fixtures() -> None:
    passing = validate_verdict({"pass": True, "intent": "social", "reason": "brief", "violations": []})
    failing = validate_verdict({"pass": False, "intent": "question", "reason": "invented fact", "violations": ["hallucination"]})
    assert passing["pass"] is True
    assert failing["pass"] is False


@pytest.mark.intent_judge
def test_intent_judge_greeting_only(ui, provider, judge_config, request):
    provider.script_extract("Hello", x_social())
    ui.open()
    assert ui.send("Hello").status == 200
    expect(ui.log).to_contain_text("Hello! How can I help with Northstar Shop?")
    assert len(provider.calls("answer_knowledge")) == 0
    assert "What is your order number?" not in ui.transcript_text()
    provider.assert_consumed()
    _record_and_judge(judge_config, request, ui.transcript_text(), "greeting")


@pytest.mark.intent_judge
def test_intent_judge_thanks_preserves_intake(ui, provider, judge_config, request):
    provider.script_extract("My order NS-000123 arrived broken.", x_part())
    provider.script_extract(THANKS, x_social())
    ui.open()
    sid = ui.current_session_id()
    assert ui.send("My order NS-000123 arrived broken.").status == 200
    before = ui.disk(sid)
    assert ui.send(THANKS).status == 200
    assert ui.disk(sid)["slots"] == before["slots"]
    assert len(provider.calls("answer_knowledge")) == 0
    provider.assert_consumed()
    _record_and_judge(judge_config, request, ui.transcript_text(), "thanks")


@pytest.mark.intent_judge
def test_intent_judge_social_preface_facts(ui, provider, judge_config, request):
    provider.script_extract(MIXED_PART, x_part())
    ui.open()
    assert ui.send(MIXED_PART).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    assert ui.disk()["intake_started"] is True
    provider.assert_consumed()
    _record_and_judge(judge_config, request, ui.transcript_text(), "facts")


@pytest.mark.intent_judge
def test_intent_judge_social_preface_policy(ui, provider, judge_config, request):
    provider.script_extract(GREETING_RETURN_Q, x_question())
    provider.script_answer(GREETING_RETURN_Q, return_answer())
    ui.open()
    assert ui.send(GREETING_RETURN_Q).status == 200
    expect(ui.log).to_contain_text("30 calendar days")
    assert len(provider.calls("answer_knowledge")) == 1
    provider.assert_consumed()
    _record_and_judge(judge_config, request, ui.transcript_text(), "policy")


@pytest.mark.intent_judge
def test_intent_judge_capability_request(ui, provider, judge_config, request):
    provider.script_extract(CAPABILITY_Q, x_capability())
    ui.open()
    assert ui.send(CAPABILITY_Q).status == 200
    expect(ui.log).to_contain_text(CAPABILITY_REPLY)
    assert ui.disk()["intake_started"] is False
    assert len(provider.calls("answer_knowledge")) == 0
    provider.assert_consumed()
    _record_and_judge(judge_config, request, ui.transcript_text(), "capability")


@pytest.mark.intent_judge
def test_intent_judge_unsupported_policy(ui, provider, judge_config, request):
    provider.script_extract(PAYMENT_Q, x_question())
    ui.open()
    assert ui.send(PAYMENT_Q).status == 200
    expect(ui.log).to_contain_text(ABSTENTION)
    assert ui.last_assistant()["citations"] == []
    assert len(provider.calls("answer_knowledge")) == 0
    provider.assert_consumed()
    _record_and_judge(judge_config, request, ui.transcript_text(), "unsupported")


@pytest.mark.intent_judge
def test_intent_judge_explicit_resume(ui, provider, judge_config, request):
    provider.script_extract(RESUME, x_start_intake())
    ui.open()
    assert ui.send(RESUME).status == 200
    expect(ui.log).to_contain_text("What is your order number?")
    assert ui.disk()["intake_started"] is True
    assert len(provider.calls("answer_knowledge")) == 0
    provider.assert_consumed()
    _record_and_judge(judge_config, request, ui.transcript_text(), "resume")
