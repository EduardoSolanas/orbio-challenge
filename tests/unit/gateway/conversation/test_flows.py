"""Flows: the steps live in gateway/flows.json, one generic runner follows them."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gateway.conversation.flows import (
    DEFAULT_FLOWS_PATH,
    INTAKE_STATUS,
    STEPS,
    SUPPORT_REQUEST,
    FlowContext,
    FlowsInvalid,
    load_flows,
    only_account_number,
    run_flow,
)
from gateway.conversation.verification import VerificationRegistry
from gateway.localization import build_review_reply, missing_question, render, text
from gateway.schemas import Clarification
from tests.support.unit_fakes import make_extraction, make_slots

ORDER = "NS-000123"


class Customer:
    """Only the two reads a flow makes; the Backend API itself is tested elsewhere."""

    async def installation(self, order_number):
        return None

    async def availability(self):
        return SimpleNamespace(slots=[])


def context(*, order=ORDER, invalid=False, verification=None, session_id=None) -> FlowContext:
    return FlowContext(
        session_id=session_id or uuid4(),
        order_number=order,
        order_invalid=invalid,
        language="en",
        verification=verification or VerificationRegistry(),
        customer_data=Customer(),
    )


# --- the file -------------------------------------------------------------------


def test_the_shipped_flows_load_and_only_use_known_steps() -> None:
    flows = load_flows()

    assert {"installation_status", "book_technician"} <= set(flows)
    assert all(step in STEPS for flow in flows.values() for step in flow.steps)
    assert flows["book_technician"].steps[:2] == ("ask_account", "verify_account")
    assert flows[SUPPORT_REQUEST].steps == ("collect_details", "review_details")


@pytest.mark.parametrize(
    "content",
    [
        None,
        "{not json",
        [],
        {},
        {"f": {"intent": "book_technician"}},
        {"f": {"intent": "nonsense", "steps": ["ask_account"]}},
        {"f": {"intent": "book_technician", "steps": []}},
        {"f": {"intent": "book_technician", "steps": ["ask_account", "ask_account"]}},
        {"f": {"intent": "book_technician", "steps": ["no_such_step"]}},
        {"f": {"intent": "book_technician", "steps": ["ask_account"], "extra": 1}},
        {"f": {"intent": "book_technician", "steps": ["ask_account", "review_details"]}},
        {"f": {"intent": "book_technician", "steps": ["ask_account"]}},
        {
            "a": {"intent": "book_technician", "steps": ["ask_account"]},
            "b": {"intent": "book_technician", "steps": ["ask_account"]},
            SUPPORT_REQUEST: {"intent": "start_intake", "steps": ["collect_details"]},
        },
    ],
)
def test_a_missing_or_unrunnable_flows_file_is_rejected(tmp_path: Path, content) -> None:
    path = tmp_path / "flows.json"
    if content is not None:
        path.write_text(content if isinstance(content, str) else json.dumps(content), "utf-8")

    with pytest.raises(FlowsInvalid):
        load_flows(path)


def test_the_default_path_is_the_shipped_file() -> None:
    assert DEFAULT_FLOWS_PATH.name == "flows.json" and DEFAULT_FLOWS_PATH.exists()


# --- the runner -----------------------------------------------------------------


async def test_a_flow_without_an_account_asks_for_it() -> None:
    flow = load_flows()["book_technician"]

    outcome = await run_flow(flow, "ask_account", context(order=None))

    assert outcome.reply == text("auth.order_number_prompt", "en")
    assert outcome.step == "ask_account"


async def test_a_malformed_account_is_corrected_not_accepted() -> None:
    flow = load_flows()["book_technician"]

    outcome = await run_flow(flow, "ask_account", context(order=None, invalid=True))

    assert outcome.reply == text("intake.invalid_order", "en")
    assert outcome.step == "ask_account"


async def test_an_unverified_account_stops_at_verification() -> None:
    flow = load_flows()["book_technician"]
    verification = VerificationRegistry()
    ctx = context(verification=verification)

    outcome = await run_flow(flow, "ask_account", ctx)

    assert outcome.reply == text("auth.verification_prompt", "en")
    assert outcome.verification_required is True
    assert outcome.step == "verify_account"
    assert verification.pending_order(ctx.session_id) == ORDER


async def test_a_verified_account_runs_on_to_the_last_step_and_finishes() -> None:
    verification = VerificationRegistry()
    ctx = context(verification=verification)
    verification.record_success(ctx.session_id, ORDER)

    booking = await run_flow(load_flows()["book_technician"], "verify_account", ctx)
    status = await run_flow(load_flows()["installation_status"], "verify_account", ctx)

    assert booking.reply == text("booking.no_slots", "en") and booking.step is None
    assert status.reply == render("installation.awaiting_visit", "en", {"order_number": ORDER})
    assert status.step is None


async def test_an_unknown_stored_step_starts_the_flow_again() -> None:
    outcome = await run_flow(load_flows()["book_technician"], "removed_step", context(order=None))

    assert outcome.step == "ask_account"


# --- what feeds a flow ----------------------------------------------------------


def test_only_a_bare_account_number_feeds_a_running_flow() -> None:
    bare = make_extraction(candidates={"order_number": ORDER})

    assert only_account_number(bare)
    assert not only_account_number(make_extraction(candidates={"order_number": None}))
    assert not only_account_number(
        make_extraction(candidates={"order_number": ORDER, "description": "broken"})
    )
    assert not only_account_number(make_extraction(has_question=True, candidates={"order_number": ORDER}))
    assert not only_account_number(make_extraction(intent="start_intake", candidates={"order_number": ORDER}))


# --- the support request ----------------------------------------------------------

FULL = make_slots(ORDER, "damaged_item", "It arrived broken", "high")


def intake(*, slots, pending=None, rejected=False) -> FlowContext:
    from dataclasses import replace

    return replace(context(order=None), slots=slots, pending=pending or {}, review_rejected=rejected)


async def run_intake(ctx):
    flow = load_flows()[SUPPORT_REQUEST]
    return await run_flow(flow, flow.steps[0], ctx)


async def test_a_missing_detail_is_asked_for_in_the_fixed_order() -> None:
    outcome = await run_intake(intake(slots=make_slots(ORDER, None, None, None)))

    assert outcome.reply == missing_question("category", "en")
    assert INTAKE_STATUS[outcome.step] == "collecting"


async def test_a_pending_problem_is_asked_about_before_anything_missing() -> None:
    question = Clarification(code="invalid", candidate="x", question="That does not look right.")
    outcome = await run_intake(intake(slots=make_slots(None, None, None, None), pending={"order_number": question}))

    assert outcome.reply == "That does not look right."
    assert INTAKE_STATUS[outcome.step] == "collecting"


async def test_a_rejected_review_goes_back_to_collecting_even_when_complete() -> None:
    outcome = await run_intake(intake(slots=FULL, rejected=True))

    assert outcome.reply == text("reject_prompt", "en")
    assert INTAKE_STATUS[outcome.step] == "collecting"


async def test_complete_details_go_to_the_review_and_wait_for_confirmation() -> None:
    outcome = await run_intake(intake(slots=FULL))

    assert outcome.reply == build_review_reply(FULL, "en")
    assert INTAKE_STATUS[outcome.step] == "awaiting_confirmation"
