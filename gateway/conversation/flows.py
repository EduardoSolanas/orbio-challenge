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
from gateway.conversation.verification import VerificationRegistry
from gateway.conversation.visits import booking_offer, installation_reply
from gateway.localization import text
from gateway.schemas import Extraction, Intent, Language, SlotOption

DEFAULT_FLOWS_PATH = Path(__file__).resolve().parents[1] / "flows.json"


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
    return StepResult(text("auth.verification_prompt", ctx.language), verification_required=True)


async def show_installation(ctx: FlowContext) -> StepResult:
    reply = await installation_reply(ctx.customer_data, ctx.order_number, ctx.language)
    return StepResult(reply, finish=True)


async def offer_windows(ctx: FlowContext) -> StepResult:
    reply, slots = await booking_offer(ctx.customer_data, ctx.order_number, ctx.language)
    return StepResult(reply, slot_options=slots, finish=True)


STEPS: dict[str, Callable[[FlowContext], Awaitable[StepResult]]] = {
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
            raise FlowsInvalid(f"flow {name!r} needs a non-empty list of distinct steps")
        unknown = [step for step in steps if step not in STEPS]
        if unknown:
            raise FlowsInvalid(f"flow {name!r} uses unknown steps {unknown}")
        flows[name] = FlowDefinition(name, intent, tuple(steps))
    starting = [flow.intent for flow in flows.values()]
    if len(set(starting)) != len(starting):
        raise FlowsInvalid("two flows start on the same intent")
    return flows


def only_account_number(extraction: Extraction) -> bool:
    """A message that carries nothing but an account number, such as "NS-000123"."""

    candidates = extraction.candidates
    return (
        extraction.intent == "provide_details"
        and not extraction.has_question
        and candidates.order_number is not None
        and not (candidates.category or candidates.description or candidates.urgency)
    )


async def run_flow(definition: FlowDefinition, step: str, ctx: FlowContext) -> FlowOutcome:
    """Run from ``step`` until a step has something to say."""

    start = definition.steps.index(step) if step in definition.steps else 0
    for current in definition.steps[start:]:
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
