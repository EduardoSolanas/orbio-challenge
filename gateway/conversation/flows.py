"""Flows: ordered steps kept in ``gateway/flows.json``, run by one generic runner.

The model only starts a flow (its intent names one) and supplies what a step asks for,
such as the account number. The code decides which step comes next, so a conversation
cannot wander out of a flow because the model labelled one message differently. A step
id in the file is looked up in ``STEPS`` below; an unknown id stops the gateway at
startup rather than failing mid-conversation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, get_args
from uuid import UUID

from gateway.backend_client import BackendApiClient
from gateway.conversation.plans import first_issue, first_missing
from gateway.conversation.verification import VerificationRegistry
from gateway.conversation.visits import booking_offer, installation_reply
from gateway.localization import build_review_reply, text
from gateway.schemas import (
    Clarification,
    Extraction,
    FieldName,
    FlowState,
    Intent,
    Language,
    SlotOption,
    Slots,
)

DEFAULT_FLOWS_PATH = Path(__file__).resolve().parents[1] / "flows.json"

# The support request is the default conversation: nothing starts it, so the engine finds
# it by name. Its step is not stored as a flow; the session's `status` is the step.
SUPPORT_REQUEST = "support_request"
INTAKE_STATUS = {
    "collect_details": "collecting",
    "review_details": "awaiting_confirmation",
}


class FlowsInvalid(ValueError):
    """The flows file is missing, malformed or names something the code cannot run."""


@dataclass(frozen=True)
class FlowDefinition:
    name: str
    intent: str
    steps: tuple[str, ...]


@dataclass(frozen=True)
class FlowContext:
    """What a step may look at. It never holds the postcode or the code."""

    session_id: UUID
    order_number: str | None
    order_invalid: bool
    language: Language
    verification: VerificationRegistry
    customer_data: BackendApiClient | None
    # Only the support-request steps look at these.
    slots: Slots | None = None
    pending: dict[FieldName, Clarification] | None = None
    review_rejected: bool = False


@dataclass(frozen=True)
class StepResult:
    """No reply: the step is satisfied and the next one runs. A reply stops the flow here."""

    reply: str | None = None
    verification_required: bool = False
    slot_options: tuple[SlotOption, ...] = ()
    finish: bool = False


@dataclass(frozen=True)
class FlowOutcome:
    reply: str | None
    step: str | None  # where the flow waits; None once it has finished
    verification_required: bool = False
    slot_options: tuple[SlotOption, ...] = ()


async def ask_account(ctx: FlowContext) -> StepResult:
    if ctx.order_invalid:
        return StepResult(text("intake.invalid_order", ctx.language))
    if ctx.order_number is None:
        return StepResult(text("auth.order_number_prompt", ctx.language))
    return StepResult()


async def verify_account(ctx: FlowContext) -> StepResult:
    if ctx.verification.grant(ctx.session_id) == ctx.order_number:
        return StepResult()
    ctx.verification.set_pending_order(ctx.session_id, ctx.order_number)
    return StepResult(
        text("auth.verification_prompt", ctx.language), verification_required=True
    )


async def show_installation(ctx: FlowContext) -> StepResult:
    reply = await installation_reply(ctx.customer_data, ctx.order_number, ctx.language)
    return StepResult(reply, finish=True)


async def offer_windows(ctx: FlowContext) -> StepResult:
    reply, slots = await booking_offer(
        ctx.customer_data, ctx.order_number, ctx.language
    )
    return StepResult(reply, slot_options=slots, finish=True)


async def collect_details(ctx: FlowContext) -> StepResult:
    """Ask about the first thing still wrong or missing; nothing to ask moves on to the review."""

    if ctx.pending:
        return StepResult(first_issue(ctx.pending).question)
    if ctx.review_rejected:
        return StepResult(text("reject_prompt", ctx.language))
    missing = first_missing(ctx.slots, ctx.language)
    return StepResult() if missing is None else StepResult(missing)


async def review_details(ctx: FlowContext) -> StepResult:
    return StepResult(build_review_reply(ctx.slots, ctx.language))


# Which kind of context each step needs; a flow may not mix them.
STEP_KINDS = {
    "ask_account": "account",
    "verify_account": "account",
    "show_installation": "account",
    "offer_windows": "account",
    "collect_details": "intake",
    "review_details": "intake",
}

# A step that needs what an earlier step settled: that step must come before it in the flow.
NEEDS = {
    "verify_account": "ask_account",
    "show_installation": "verify_account",
    "offer_windows": "verify_account",
}

# A flow has to end on a step that says something, or a customer could be left without a reply.
ENDING_STEPS = {"show_installation", "offer_windows", "review_details"}

STEPS: dict[str, Callable[[FlowContext], Awaitable[StepResult]]] = {
    "collect_details": collect_details,
    "review_details": review_details,
    "ask_account": ask_account,
    "verify_account": verify_account,
    "show_installation": show_installation,
    "offer_windows": offer_windows,
}


def load_flows(path: Path = DEFAULT_FLOWS_PATH) -> dict[str, FlowDefinition]:
    """Read and check the flows file; the key is the flow name."""

    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as error:
        raise FlowsInvalid(f"cannot read flows from {path}: {error}") from error
    if not isinstance(raw, dict) or not raw:
        raise FlowsInvalid("the flows file must be a non-empty object of flows")
    intents = get_args(Intent)
    flows: dict[str, FlowDefinition] = {}
    for name, body in raw.items():
        if not isinstance(body, dict) or set(body) != {"intent", "steps"}:
            raise FlowsInvalid(f"flow {name!r} needs exactly an intent and steps")
        intent, steps = body["intent"], body["steps"]
        if intent not in intents:
            raise FlowsInvalid(f"flow {name!r} starts on an unknown intent {intent!r}")
        if not isinstance(steps, list) or not steps or len(set(steps)) != len(steps):
            raise FlowsInvalid(
                f"flow {name!r} needs a non-empty list of distinct steps"
            )
        unknown = [step for step in steps if step not in STEPS]
        if unknown:
            raise FlowsInvalid(f"flow {name!r} uses unknown steps {unknown}")
        if steps[-1] not in ENDING_STEPS:
            raise FlowsInvalid(f"flow {name!r} must end with a step that replies")
        for index, step in enumerate(steps):
            if step in NEEDS and NEEDS[step] not in steps[:index]:
                raise FlowsInvalid(
                    f"flow {name!r}: {step} needs {NEEDS[step]} before it"
                )
        if len({STEP_KINDS[step] for step in steps}) != 1:
            raise FlowsInvalid(
                f"flow {name!r} mixes account steps and support-request steps"
            )
        flows[name] = FlowDefinition(name, intent, tuple(steps))
    if SUPPORT_REQUEST not in flows:
        raise FlowsInvalid(f"the flows file must define {SUPPORT_REQUEST!r}")
    starting = [flow.intent for flow in flows.values()]
    if len(set(starting)) != len(starting):
        raise FlowsInvalid("two flows start on the same intent")
    return flows


def only_account_number(extraction: Extraction) -> bool:
    """A message that carries nothing but an account number: "NS-000123", or "sorry, I meant NS-000123"."""

    candidates = extraction.candidates
    return (
        extraction.intent in ("provide_details", "correction")
        and not extraction.has_question
        and candidates.order_number is not None
        and not (candidates.category or candidates.description or candidates.urgency)
    )


async def run_flow(definition: FlowDefinition, ctx: FlowContext) -> FlowOutcome:
    """Run the steps in order until one has something to say.

    It always starts at the first step: every step is a check that passes quietly when it is
    already satisfied, so a message that changes the account is re-checked from the top.
    """

    for current in definition.steps:
        result = await STEPS[current](ctx)
        if result.reply is None:
            continue
        return FlowOutcome(
            result.reply,
            None if result.finish else current,
            result.verification_required,
            result.slot_options,
        )
    return FlowOutcome(None, None)


def flow_state_after(
    definition: FlowDefinition, outcome: FlowOutcome
) -> FlowState | None:
    """What the session file keeps: where the flow waits, or nothing once it has finished."""

    return (
        None
        if outcome.step is None
        else FlowState(name=definition.name, step=outcome.step)
    )
