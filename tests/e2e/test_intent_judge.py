"""semantic intent-routing checks layered over deterministic browser assertions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import response_of, wait_until
from tests.e2e.judge_config import (
    JudgeConfig,
    judge,
    load_config,
    validate_verdict,
    write_artifact,
)
from tests.support.fakes import (
    ABSTENTION,
    ASK_CATEGORY,
    AUTH_ORDER_RESULT,
    AUTH_VERIFICATION_FAILED,
    AUTH_VERIFICATION_PROMPT,
    BOOK_Q,
    CAPABILITY_Q,
    CATEGORY,
    CHANGE,
    FULL,
    GREETING_STANDING_CHARGE_Q,
    INSTALLATION_Q,
    MIXED_PART,
    NO,
    ORDER_STATUS_Q,
    PART,
    PAYMENT_Q,
    RESUME,
    THANKS,
    UNRELATED,
    WHAT_TO_CHANGE,
    YES,
    receipt_for,
    review_reply,
    standing_charge_answer,
    summary_text,
    x_capability,
    x_category,
    x_change,
    x_full,
    x_gratitude,
    x_no,
    x_order_lookup,
    x_part,
    x_question,
    x_social,
    x_start_intake,
    x_unrelated,
    x_yes,
)
from tests.support.fakes import (
    extraction as make_extraction,
)

CAPABILITY_REPLY = "I can answer questions about Northstar Energy bills, tariffs, meter readings, moving home, and supply issues. I can also help you report a service problem and, once you verify the demo account, check an installation or book a technician. What do you need help with?"
JUDGE_RULES = {
    "facts": "real facts outrank the social preface: start intake, accept the explicit issue facts, and ask only the next missing issue field. A relevant category/area question is valid; do not require the assistant to repeat the supplied facts.",
    "policy": "question, supported: answer the policy question with grounded information and no intake follow-up.",
    "unsupported": "question, unsupported: safe abstention with no invented facts or citations.",
    "resume": (
        "After a saved issue, an FAQ detour and page refresh, the final assistant "
        "response resumes the same issue by asking only for the next missing field "
        "(the issue category/area). It must not ask again for the known account "
        "reference or description, repeat the FAQ answer, greet as a new session, "
        "or claim to have lost the conversation. Judge the final response only."
    ),
    "faq_interrupt": (
        "The final assistant response answers the latest in-scope standing-charge FAQ, "
        "using only the supplied citation. Do not ask for intake fields, request account "
        "verification, repeat an earlier workflow prompt, or claim to have changed workflow "
        "state. Judge the final answer; earlier conversation may contain these prompts."
    ),
    "faq_unsupported_interrupt": (
        "The final assistant response safely abstains from the unsupported payment-method "
        "FAQ, makes no unsupported policy claim, and gives no citation. Ignore earlier "
        "workflow prompts; judge the final response only."
    ),
    "unrelated": (
        "unrelated: politely decline by restating what the assistant can help with (energy "
        "questions, a verified demo account, reporting a service issue). It must not tell a "
        "joke or answer the off-topic request, start intake, or retrieve a policy answer."
    ),
    "correction_confirm": (
        "The customer corrects one detail of a review, then confirms. After the correction the "
        "assistant shows the updated review with only that detail changed and asks to confirm. "
        "After the confirmation it says the details were saved locally and that no request was "
        "sent to a real support team. It must not claim a real ticket, agent or follow-up."
    ),
    "reject_correct": (
        "After the customer says no to a review, the assistant asks only what to change. After "
        "the customer states the change, it shows the updated review with only that detail "
        "changed. It must not confirm, save, hand off or ask for unrelated details."
    ),
    "order_status_requires_verification": (
        "Right after the first assistant response there is a note of what the customer did in the "
        "separate verification form, which is outside the chat because credentials never enter chat. "
        "The customer asks for their account status before verifying. The first assistant "
        "response must ask for the service postcode and verification code and reveal no "
        "account data. After verification and a repeated request, the assistant reports the "
        "account reference, first name, service and status. It must not reveal account data "
        "before verification or which verification detail failed."
    ),
    "installation_status_requires_verification": (
        "Right after the first assistant response there is a note of what the customer did in the "
        "separate verification form, which is outside the chat because credentials never enter chat. "
        "The customer asks for their smart meter installation status before verifying. The "
        "first assistant response must ask for the service postcode and verification code and "
        "reveal no installation details. After verification the assistant, without the customer "
        "repeating the request, reports that the installation is approved and waiting for a "
        "technician visit and says how to book one. It must not reveal details before verification or "
        "invent a date."
    ),
    "booking_requires_verification": (
        "Right after the first assistant response there is a note of what the customer did in the "
        "separate verification form, which is outside the chat because credentials never enter chat. "
        "The customer asks to book a technician before verifying the account. The first "
        "assistant response must ask for the service postcode and verification code and "
        "must not offer or confirm any time. After the account is verified the assistant answers "
        "the original request on its own, without the customer repeating it: it says technician "
        "times are available to choose from, and once one is chosen it confirms the window the customer clicked, as shown on the "
        "button including its end time, with a booking reference. It must not reveal which "
        "verification detail failed, offer or confirm a booking before verification, or "
        "invent a time that was not chosen."
    ),
    "frustration_handoff": (
        "The final assistant response must empathize and clearly indicate the "
        "human-agent handoff is simulated, with no live agent connected and no real "
        "support request sent. It must not continue answering, collecting issue fields, "
        "requesting verification, or claim a real transfer occurred."
    ),
}

FRUSTRATION_HANDOFF_FLOWS = [
    "during_issue_intake",
    "during_supported_faq",
    "during_pending_verification",
    "during_review",
    "while_off_topic",
    "in_spanish",
]


def _with_form_events(transcript: str, events: str) -> str:
    """The verification form is outside the chat, so the judge is told what happened there."""

    note = f" [Outside the chat, in the separate verification form: {events}]"
    return transcript.replace(AUTH_VERIFICATION_PROMPT, AUTH_VERIFICATION_PROMPT + note, 1)


FORM_EVENTS = (
    "first the customer submitted a wrong code and the form rejected it with a generic "
    "message that does not say which detail was wrong; then the customer submitted the "
    "correct code and the account was verified"
)


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
        artifact.update(judge(config, conversation, JUDGE_RULES[key]))
    except Exception as exc:
        artifact.update({"verdict": {"pass": False, "intent": "judge_failure", "reason": str(exc), "violations": [str(exc)]}})
        write_artifact(Path("reports/e2e-judge"), request.node.nodeid, artifact)
        raise
    write_artifact(Path("reports/e2e-judge"), request.node.nodeid, artifact)
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
def test_intent_judge_greeting_only(ui, provider):
    provider.script_extract("Hello", x_social())
    ui.open()
    assert ui.send("Hello").status == 200
    expect(ui.log).to_contain_text("Hello! How can I help with Northstar Energy?")
    assert len(provider.calls("answer_knowledge")) == 0
    assert "What is your energy account reference?" not in ui.transcript_text()
    provider.assert_consumed()


@pytest.mark.intent_judge
def test_intent_judge_thanks_preserves_intake(ui, provider):
    provider.script_extract("My order NS-000123 arrived broken.", x_part())
    provider.script_extract(THANKS, x_gratitude())
    ui.open()
    sid = ui.current_session_id()
    assert ui.send("My order NS-000123 arrived broken.").status == 200
    before = ui.disk(sid)
    assert ui.send(THANKS).status == 200
    expect(ui.log).to_contain_text(
        "You're welcome! How else can I help with your energy service?"
    )
    expect(ui.log).not_to_contain_text("Hello! How can I help with Northstar Energy?")
    assert ui.disk(sid)["slots"] == before["slots"]
    assert len(provider.calls("answer_knowledge")) == 0
    provider.assert_consumed()


@pytest.mark.intent_judge
def test_intent_judge_social_preface_facts(ui, provider, judge_config, request):
    provider.script_extract(MIXED_PART, x_part())
    ui.open()
    assert ui.send(MIXED_PART).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    assert ui.disk()["slots"]["order_number"] == "NS-000123"
    provider.assert_consumed()
    _record_and_judge(judge_config, request, ui.transcript_text(), "facts")


@pytest.mark.intent_judge
def test_intent_judge_social_preface_policy(ui, provider, judge_config, request):
    provider.script_extract(GREETING_STANDING_CHARGE_Q, x_question())
    provider.script_answer(GREETING_STANDING_CHARGE_Q, standing_charge_answer())
    ui.open()
    assert ui.send(GREETING_STANDING_CHARGE_Q).status == 200
    expect(ui.log).to_contain_text("fixed daily amount")
    assert ui.last_assistant()["citations"][0]["source_id"] == "faq-standing-charge"
    assert len(provider.calls("answer_knowledge")) == 1
    provider.assert_consumed()
    _record_and_judge(judge_config, request, ui.transcript_text(), "policy")


@pytest.mark.intent_judge
def test_intent_judge_capability_request(ui, provider):
    provider.script_extract(CAPABILITY_Q, x_capability())
    ui.open()
    assert ui.send(CAPABILITY_Q).status == 200
    expect(ui.log).to_contain_text(CAPABILITY_REPLY)
    assert set(ui.disk()["slots"].values()) == {None}
    assert "Which area do you need help with" not in ui.transcript_text()
    assert len(provider.calls("answer_knowledge")) == 0
    provider.assert_consumed()


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
    provider.script_extract(PART, x_part())
    provider.script_extract(GREETING_STANDING_CHARGE_Q, x_question())
    provider.script_answer(GREETING_STANDING_CHARGE_Q, standing_charge_answer())
    provider.script_extract(RESUME, x_start_intake())
    ui.open()
    sid = ui.current_session_id()
    assert ui.send(PART).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    state_after_issue = ui.disk(sid)
    assert state_after_issue["slots"] == {
        "order_number": "NS-000123",
        "category": None,
        "description": "My order arrived broken",
        "urgency": None,
    }

    assert ui.send(GREETING_STANDING_CHARGE_Q).status == 200
    expect(ui.log).to_contain_text("fixed daily amount")
    assert ui.last_assistant(sid)["citations"][0]["source_id"] == "faq-standing-charge"
    before_resume = ui.disk(sid)
    prior_transcript = ui.transcript_text()

    gets_before = len(ui.session_gets())
    ui.page.reload()
    expect(ui.log).to_contain_text("fixed daily amount")
    wait_until(
        lambda: len(ui.session_gets()) > gets_before,
        "session GET after explicit-resume judge refresh",
    )
    assert ui.stored_session_id() == sid
    assert ui.transcript_text() == prior_transcript
    assert ui.disk(sid)["slots"] == before_resume["slots"]
    assert ui.disk(sid)["revision"] == before_resume["revision"]

    assert ui.send(RESUME).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    expect(ui.log).to_contain_text("fixed daily amount")
    assert ui.last_assistant(sid)["text"] == ASK_CATEGORY
    after_resume = ui.disk(sid)
    assert after_resume["slots"] == before_resume["slots"]
    assert after_resume["revision"] == before_resume["revision"] + 1
    resume_context = provider.calls("extract")[-1]["context"]
    assert resume_context["current_message"] == RESUME
    assert resume_context["slots"] == before_resume["slots"]
    assert any(
        message["role"] == "user" and message["text"] == GREETING_STANDING_CHARGE_Q
        for message in resume_context["recent_messages"]
    )
    assert any(
        message["role"] == "assistant" and "fixed daily amount" in message["text"]
        for message in resume_context["recent_messages"]
    )
    assert len(provider.calls("answer_knowledge")) == 1
    provider.assert_consumed()
    _record_and_judge(judge_config, request, ui.transcript_text(), "resume")


FAQ_INTERRUPTIONS = [
    "partial_issue_intake",
    "after_category_before_urgency",
    "review_pending",
    "review_rejected",
    "verification_pending",
    "verification_after_wrong_code",
    "verified_before_order_lookup",
    "after_order_result_before_intake_resumes",
    "partial_intake_after_reload",
    "unsupported_faq_during_verification",
]


@pytest.mark.intent_judge
@pytest.mark.parametrize("flow", FAQ_INTERRUPTIONS, ids=FAQ_INTERRUPTIONS)
def test_intent_judge_faq_interruptions(flow, ui, provider, judge_config, request):
    """Judge FAQ routing in each active workflow state; Python asserts state retention."""
    ui.open()
    sid = ui.current_session_id()

    if flow in {"partial_issue_intake", "after_category_before_urgency", "partial_intake_after_reload"}:
        provider.script_extract(PART, x_part())
        assert ui.send(PART).status == 200
        if flow == "after_category_before_urgency":
            provider.script_extract(CATEGORY, x_category())
            assert ui.send(CATEGORY).status == 200
        if flow == "partial_intake_after_reload":
            ui.page.reload()
            expect(ui.log).to_contain_text(PART)
    elif flow in {"review_pending", "review_rejected"}:
        provider.script_extract(FULL, x_full())
        assert ui.send(FULL).status == 200
        if flow == "review_rejected":
            provider.script_extract(NO, x_no())
            assert ui.send(NO).status == 200
    elif flow in {
        "verification_pending", "verification_after_wrong_code",
        "verified_before_order_lookup", "after_order_result_before_intake_resumes",
        "unsupported_faq_during_verification",
    }:
        provider.script_extract(PART, x_part())
        assert ui.send(PART).status == 200
        provider.script_extract(ORDER_STATUS_Q, x_order_lookup())
        assert ui.send(ORDER_STATUS_Q).status == 200
        expect(ui.verification).to_be_visible()
        if flow == "verification_after_wrong_code":
            assert ui.verify_order("SW1A 1AA", "123456").status == 200
            expect(ui.verification_error).to_contain_text(AUTH_VERIFICATION_FAILED)
        elif flow in {"verified_before_order_lookup", "after_order_result_before_intake_resumes"}:
            assert ui.verify_order("SW1A 1AA", "246810").status == 200
            expect(ui.verification).to_be_hidden()
            if flow == "after_order_result_before_intake_resumes":
                provider.script_extract(
                    ORDER_STATUS_Q, x_order_lookup(order_number="NS-000123", tool=True)
                )
                assert ui.send(ORDER_STATUS_Q).status == 200

    before = ui.disk(sid)
    tracked = {
        key: before.get(key)
        for key in ("slots", "status", "pending_review_revision", "summary", "review_rejected")
    }
    faq = PAYMENT_Q if flow == "unsupported_faq_during_verification" else GREETING_STANDING_CHARGE_Q
    provider.script_extract(faq, x_question())
    if faq == GREETING_STANDING_CHARGE_Q:
        provider.script_answer(faq, standing_charge_answer())
    assert ui.send(faq).status == 200

    response = ui.last_assistant(sid)
    after = ui.disk(sid)
    assert {key: after.get(key) for key in tracked} == tracked
    if faq == GREETING_STANDING_CHARGE_Q:
        assert response["text"] == standing_charge_answer().answer
        assert [citation["source_id"] for citation in response["citations"]] == ["faq-standing-charge"]
        rule = "faq_interrupt"
        judge_conversation = ui.transcript_text() + "\nGrounding citations: " + json.dumps(
            response["citations"], ensure_ascii=False
        )
        expect(ui.log).to_contain_text("fixed daily amount")
    else:
        assert response["text"] == ABSTENTION and response["citations"] == []
        rule = "faq_unsupported_interrupt"
        judge_conversation = ui.transcript_text()
        expect(ui.log).to_contain_text(ABSTENTION)
    if flow.startswith("verification_") or flow == "unsupported_faq_during_verification":
        expect(ui.verification).to_be_visible()
        assert response_of(ui.turn_posts()[-1])["order_verification_required"] is True
    else:
        assert response_of(ui.turn_posts()[-1])["order_verification_required"] is False
    if flow == "review_pending":
        expect(ui.review).to_be_visible()
    if flow == "unsupported_faq_during_verification":
        assert len(provider.calls("answer_knowledge")) == 0
    provider.assert_consumed()
    _record_and_judge(judge_config, request, judge_conversation, rule)


@pytest.mark.intent_judge
@pytest.mark.parametrize(
    "flow",
    FRUSTRATION_HANDOFF_FLOWS,
    ids=[
        "JH01-during-issue-intake",
        "JH02-during-supported-faq",
        "JH03-during-pending-verification",
        "JH04-during-review",
        "JH05-while-off-topic",
        "JH06-spanish",
    ],
)
def test_intent_judge_frustration_hands_off(flow, ui, provider, judge_config, request):
    """Frustration terminates the bot flow with a clear demo-only handoff."""
    ui.open()
    sid = ui.current_session_id()
    if flow in {"during_issue_intake", "during_pending_verification"}:
        provider.script_extract(PART, x_part())
        assert ui.send(PART).status == 200
        if flow == "during_pending_verification":
            provider.script_extract(ORDER_STATUS_Q, x_order_lookup())
            assert ui.send(ORDER_STATUS_Q).status == 200
            expect(ui.verification).to_be_visible()
    elif flow == "during_review":
        provider.script_extract(FULL, x_full())
        assert ui.send(FULL).status == 200
        expect(ui.review).to_be_visible()

    if flow == "during_supported_faq":
        message = GREETING_STANDING_CHARGE_Q
        classification = make_extraction(
            intent="question", has_question=True, sentiment="negative", frustration="present"
        )
    elif flow == "in_spanish":
        message = "Estoy frustrado y necesito que me atienda una persona."
        classification = make_extraction(
            intent="unrelated", sentiment="negative", frustration="present",
            detected_language="es",
        )
    else:
        message = "I am really frustrated. I want a human agent."
        classification = make_extraction(
            intent="unrelated", sentiment="negative", frustration="present"
        )
    before_slots = ui.disk(sid)["slots"]
    provider.script_extract(message, classification)
    assert ui.send(message).status == 200

    response = ui.last_assistant(sid)
    view = ui.disk(sid)
    assert response["text"] == (
        "Entiendo que esto ha sido frustrante. Esta demostración simula la transferencia "
        "a un agente humano. No hay ningún agente en directo conectado ni se ha enviado "
        "una solicitud real de soporte."
        if flow == "in_spanish"
        else "I understand this has been frustrating. This demo simulates a transfer to "
        "a human support agent. No live agent is connected, and no real support request "
        "has been sent."
    )
    assert response["citations"] == []
    assert view["status"] == "handed_off"
    assert view["frustration_active"] is True
    assert view["slots"] == before_slots
    # Ids, not accessible names: the Spanish page labels these controls in Spanish.
    expect(ui.page.locator("#message")).to_have_js_property("readOnly", True)
    expect(ui.page.locator("#send")).to_be_disabled()
    expect(ui.page.locator("#order-verification")).to_be_hidden()
    expect(ui.page.locator("#review")).to_be_hidden()
    expect(ui.page.locator("#new-intake")).to_be_enabled()
    assert response_of(ui.turn_posts()[-1])["order_verification_required"] is False
    assert len(provider.calls("answer_knowledge")) == 0
    provider.assert_consumed()
    expect(ui.log).to_contain_text(response["text"])
    _record_and_judge(judge_config, request, ui.transcript_text(), "frustration_handoff")


@pytest.mark.intent_judge
def test_intent_judge_booking_requires_verification_first(ui, provider, judge_config, request):
    slots = ui.page.locator("#slot-picker button")
    provider.script_extract(BOOK_Q, make_extraction(intent="book_technician", order_number="NS-000123"))
    ui.open()

    # Unverified: the form is asked for and no time is offered.
    assert ui.send(BOOK_Q).status == 200
    expect(ui.log).to_contain_text(AUTH_VERIFICATION_PROMPT)
    expect(ui.page.locator("#order-verification")).to_be_visible()
    expect(slots).to_have_count(0)

    # A wrong code keeps the form open, says nothing about which detail failed, offers no time.
    wrong = ui.verify_order("SW1A 1AA", "123456")
    assert json.loads(wrong.text()) == {"verified": False, "error": "verification_failed"}
    expect(ui.page.locator("#verification-error")).to_have_text(AUTH_VERIFICATION_FAILED)
    expect(ui.page.locator("#order-verification")).to_be_visible()
    expect(slots).to_have_count(0)

    # Verified: the request made before verifying is answered at once with six windows;
    # choosing one books exactly that one.
    assert json.loads(ui.verify_order("SW1A 1AA", "246810").text())["verified"] is True
    expect(ui.page.locator("#order-verification")).to_be_hidden()
    expect(slots).to_have_count(6)
    chosen = slots.first.get_attribute("data-starts-at")
    labels = [label.strip() for label in slots.all_inner_texts()]
    clicked = labels[0]
    with ui.page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/bookings")
    ) as pending:
        slots.first.click()
    assert pending.value.status == 200
    expect(ui.log).to_contain_text(f"Booked: a technician will visit on {chosen[:10]}")
    expect(slots).to_have_count(0)
    provider.assert_consumed()
    _record_and_judge(
        judge_config, request,
        _with_form_events(
            ui.transcript_text(),
            f"{FORM_EVENTS}; right after that, without the request being repeated, the page showed these time buttons: "
            f"{labels}; the customer clicked '{clicked}', the first one",
        ),
        "booking_requires_verification",
    )


@pytest.mark.intent_judge
def test_intent_judge_unrelated_is_redirected(ui, provider, judge_config, request):
    provider.script_extract(UNRELATED, x_unrelated())
    ui.open()
    assert ui.send(UNRELATED).status == 200
    expect(ui.log).to_contain_text("I cannot change a real account or contact an energy network")
    assert len(provider.calls("answer_knowledge")) == 0
    expect(ui.page.locator("#review")).to_be_hidden()
    provider.assert_consumed()
    _record_and_judge(judge_config, request, ui.transcript_text(), "unrelated")


@pytest.mark.intent_judge
def test_intent_judge_correction_then_confirm(ui, provider, judge_config, request):
    provider.script_extract(FULL, x_full())
    provider.script_extract(CHANGE, x_change())
    provider.script_extract(YES, x_yes())
    ui.open()
    sid = ui.current_session_id()
    assert ui.send(FULL).status == 200
    first = summary_text("NS-000123", "damaged_item", "My order arrived broken", "high")
    expect(ui.log).to_contain_text(review_reply(first))
    assert ui.send(CHANGE).status == 200
    low = summary_text("NS-000123", "damaged_item", "My order arrived broken", "low")
    expect(ui.log).to_contain_text(review_reply(low))
    assert ui.disk(sid)["slots"]["urgency"] == "low"
    assert ui.send(YES).status == 200
    expect(ui.log).to_contain_text(receipt_for(sid))
    assert ui.disk(sid)["status"] == "completed"
    provider.assert_consumed()
    _record_and_judge(judge_config, request, ui.transcript_text(), "correction_confirm")


@pytest.mark.intent_judge
def test_intent_judge_reject_then_correct(ui, provider, judge_config, request):
    change = "Make it normal urgency."
    provider.script_extract(FULL, x_full())
    provider.script_extract(NO, x_no())
    provider.script_extract(
        change, make_extraction(intent="correction", urgency="normal", explicit_corrections=["urgency"])
    )
    ui.open()
    sid = ui.current_session_id()
    assert ui.send(FULL).status == 200
    assert ui.send(NO).status == 200
    expect(ui.log).to_contain_text(WHAT_TO_CHANGE)
    assert ui.send(change).status == 200
    normal = summary_text("NS-000123", "damaged_item", "My order arrived broken", "normal")
    expect(ui.log).to_contain_text(review_reply(normal))
    assert ui.disk(sid)["status"] == "awaiting_confirmation"
    provider.assert_consumed()
    _record_and_judge(judge_config, request, ui.transcript_text(), "reject_correct")


def _verify_after_asking(ui, first_question):
    """Ask while unverified, fail the form once, then verify."""

    ui.open()
    assert ui.send(first_question).status == 200
    expect(ui.log).to_contain_text(AUTH_VERIFICATION_PROMPT)
    expect(ui.page.locator("#order-verification")).to_be_visible()
    assert json.loads(ui.verify_order("SW1A 1AA", "123456").text())["verified"] is False
    expect(ui.page.locator("#verification-error")).to_have_text(AUTH_VERIFICATION_FAILED)
    assert json.loads(ui.verify_order("SW1A 1AA", "246810").text())["verified"] is True
    expect(ui.page.locator("#order-verification")).to_be_hidden()


@pytest.mark.intent_judge
def test_intent_judge_order_status_requires_verification(ui, provider, judge_config, request):
    provider.script_extract(ORDER_STATUS_Q, x_order_lookup(order_number="NS-000123"))
    provider.script_extract(ORDER_STATUS_Q, x_order_lookup(order_number="NS-000123", tool=True))
    _verify_after_asking(ui, ORDER_STATUS_Q)
    assert ui.send(ORDER_STATUS_Q).status == 200
    expect(ui.log).to_contain_text(AUTH_ORDER_RESULT)
    provider.assert_consumed()
    _record_and_judge(
        judge_config, request, _with_form_events(ui.transcript_text(), FORM_EVENTS),
        "order_status_requires_verification",
    )


@pytest.mark.intent_judge
def test_intent_judge_installation_status_requires_verification(ui, provider, judge_config, request):
    provider.script_extract(INSTALLATION_Q, make_extraction(intent="installation_status", order_number="NS-000123"))
    _verify_after_asking(ui, INSTALLATION_Q)
    # Verifying answers the request made before it; nothing is asked a second time.
    expect(ui.log).to_contain_text("waiting for a technician visit")
    provider.assert_consumed()
    _record_and_judge(
        judge_config, request, _with_form_events(ui.transcript_text(), FORM_EVENTS),
        "installation_status_requires_verification",
    )
