"""E2E test doubles and spec literals (TC §3, IC §13.1).

`E2EProvider` is the only replaced dependency in the browser journeys: the
server-side AI boundary. Every call must be scripted with its exact input; an
unexpected or mismatched call is recorded and raises, so the application never
silently receives a different provider operation. The fake never normalizes
fields, chooses replies, retrieves records or mutates sessions: it only returns
the output the test scripted for that exact message/audio/text.

The "contract literals" section holds the scripted inputs/outputs and exact
templates quoted from the contracts. They are fixture data for assertions, not
rules that production code depends on.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Iterable

from app.errors import app_error
from app.schemas import Extraction, KnowledgeAnswer, Slots

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
REPLY_MP3_PATH = FIXTURES_DIR / "reply.mp3"
GATE_TIMEOUT_SECONDS = 25.0

# --------------------------------------------------------------------------- #
# Media fixtures
# --------------------------------------------------------------------------- #


def reply_mp3_bytes() -> bytes:
    """The committed finite MP3 returned by the fake TTS (real MP3, not WAV)."""

    return REPLY_MP3_PATH.read_bytes()


def make_wav(
    *,
    rate: int = 16000,
    channels: int = 1,
    width: int = 2,
    seconds: float = 1.0,
    peak: int = 1000,
    silent: bool = False,
) -> bytes:
    """Deterministic PCM WAV: a square wave of exactly +/-``peak`` (zeros if silent)."""

    frames = int(round(rate * seconds))
    pack = {1: "B", 2: "<h", 4: "<i"}[width]
    offset = 128 if width == 1 else 0
    samples = bytearray()
    for index in range(frames):
        value = 0 if silent else (peak if (index // 20) % 2 == 0 else -peak)
        sample = struct.pack(pack, value + offset)
        samples += sample * channels
    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(bytes(samples))
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Barrier for held provider calls
# --------------------------------------------------------------------------- #


class Gate:
    """Bounded event barrier: `entered` when the provider call starts, then it
    waits for `release()`. Used to hold one scripted call open."""

    def __init__(self, label: str = "") -> None:
        self.label = label
        self._entered = threading.Event()
        self._released = threading.Event()

    def wait_entered(self, timeout: float = 10.0) -> None:
        if not self._entered.wait(timeout):
            raise AssertionError(f"gate {self.label!r}: provider call not reached in {timeout}s")

    def release(self) -> None:
        self._released.set()

    @property
    def entered(self) -> bool:
        return self._entered.is_set()

    async def _hold(self) -> None:
        self._entered.set()
        deadline = time.monotonic() + GATE_TIMEOUT_SECONDS
        while not self._released.is_set():
            if time.monotonic() > deadline:
                raise AssertionError(f"gate {self.label!r} was never released")
            await asyncio.sleep(0.01)


# --------------------------------------------------------------------------- #
# Scripted provider
# --------------------------------------------------------------------------- #


@dataclass
class Step:
    method: str
    expect: str | None
    result: Any = None
    fail: str | None = None
    hold: Gate | None = None


class E2EProvider:
    """AIProvider (extract, answer_knowledge, transcribe, synthesize, close)."""

    def __init__(self) -> None:
        self._steps: deque[Step] = deque()
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self.ledger: list[dict[str, Any]] = []
        self.unexpected: list[str] = []
        self.close_count = 0
        self._gates: list[Gate] = []

    # ---- scripting --------------------------------------------------------

    def _script(self, step: Step) -> Step:
        with self._lock:
            self._steps.append(step)
            if step.hold is not None:
                self._gates.append(step.hold)
        return step

    def script_extract(
        self,
        message: str,
        extraction: Extraction | None = None,
        *,
        fail: str | None = None,
        hold: Gate | None = None,
    ) -> Step:
        return self._script(Step("extract", message, extraction, fail, hold))

    def script_answer(
        self,
        question: str,
        answer: KnowledgeAnswer | None = None,
        *,
        fail: str | None = None,
        hold: Gate | None = None,
    ) -> Step:
        return self._script(Step("answer_knowledge", question, answer, fail, hold))

    def script_transcribe(
        self,
        audio_sha256: str | None = None,
        text: str | None = None,
        *,
        fail: str | None = None,
        hold: Gate | None = None,
    ) -> Step:
        return self._script(
            Step("transcribe", audio_sha256, STT_TEXT if text is None else text, fail, hold)
        )

    def script_synthesize(
        self,
        text: str,
        audio: bytes | None = None,
        *,
        fail: str | None = None,
        hold: Gate | None = None,
    ) -> Step:
        return self._script(Step("synthesize", text, audio, fail, hold))

    def discard_script(self) -> None:
        with self._lock:
            self._steps.clear()

    # ---- assertions -------------------------------------------------------

    def assert_consumed(self) -> None:
        with self._lock:
            left = [f"{s.method}({s.expect!r})" for s in self._steps]
            unexpected = list(self.unexpected)
        if unexpected:
            raise AssertionError(f"unexpected provider calls: {unexpected}")
        if left:
            raise AssertionError(f"scripted provider steps never consumed: {left}")

    def calls(self, method: str, outcome: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(c)
                for c in self.ledger
                if c["method"] == method and (outcome is None or c["outcome"] == outcome)
            ]

    def methods(self) -> list[str]:
        with self._lock:
            return [c["method"] for c in self.ledger]

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        with self._changed:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("provider ledger condition not reached in time")
                self._changed.wait(remaining)

    def wait_finished(self, method: str, count: int, timeout: float = 10.0) -> None:
        """Block until `count` calls of `method` have an outcome (ok or failed)."""

        def done() -> bool:
            return (
                sum(1 for c in self.ledger if c["method"] == method and c["outcome"] != "started")
                >= count
            )

        self.wait_for(done, timeout)

    def release_all(self) -> None:
        for gate in list(self._gates):
            gate.release()

    # ---- AIProvider -------------------------------------------------------

    async def _call(
        self, method: str, key: str, record: dict[str, Any]
    ) -> Any:
        with self._changed:
            step = self._steps.popleft() if self._steps else None
            if step is None or step.method != method or (
                step.expect is not None and step.expect != key
            ):
                scripted = "nothing" if step is None else f"{step.method}({step.expect!r})"
                self.unexpected.append(f"{method}({key!r}) but scripted {scripted}")
                self.ledger.append({"method": method, "key": key, "outcome": "unexpected", **record})
                self._changed.notify_all()
                raise AssertionError(f"unexpected provider call {method}({key!r}); scripted {scripted}")
            entry = {"method": method, "key": key, "outcome": "started", **record}
            self.ledger.append(entry)
            self._changed.notify_all()
        try:
            if step.hold is not None:
                await step.hold._hold()
            if step.fail is not None:
                raise app_error(step.fail)
            result = step.result
            if method == "synthesize" and result is None:
                result = reply_mp3_bytes()
        except BaseException as exc:
            outcome = f"failed:{getattr(exc, 'code', type(exc).__name__)}"
            with self._changed:
                entry["outcome"] = outcome
                self._changed.notify_all()
            raise
        with self._changed:
            entry["outcome"] = "ok"
            self._changed.notify_all()
        return result

    async def extract(self, context: Any) -> Extraction:
        return await self._call(
            "extract", context.current_message, {"context": context.model_dump(mode="json")}
        )

    async def answer_knowledge(self, context: Any) -> KnowledgeAnswer:
        return await self._call(
            "answer_knowledge", context.question, {"context": context.model_dump(mode="json")}
        )

    async def transcribe(self, audio: bytes) -> str:
        return await self._call(
            "transcribe",
            hashlib.sha256(audio).hexdigest(),
            {"audio": audio, "audio_bytes": len(audio)},
        )

    async def synthesize(self, text: str) -> bytes:
        return await self._call("synthesize", text, {"text": text})

    async def close(self) -> None:
        self.close_count += 1


# --------------------------------------------------------------------------- #
# Contract literals (IC §5 templates, §13.1 scripted inputs, RAG §4)
# --------------------------------------------------------------------------- #

STT_TEXT = "Order NS-000123 arrived broken and this is urgent"

PART = "My order NS-000123 arrived broken."
CATEGORY = "It is a damaged item."
HIGH = "High urgency."
FULL = "My order NS-000123 arrived broken. It is a damaged item and high urgency."
CHANGE = "Actually make the urgency low."
NO = "No."
YES = "Yes."
RETURN_Q = "Can I return an unused item?"
PAYMENT_Q = "Which payment methods are accepted?"

BAD_ORDER = "My order NS-12345 arrived broken."
FIX_ORDER = "The order number is NS-000123."
CONFLICT_ORDER = "Actually the order is NS-000999."
KEEP_OLD = "Keep the old one, NS-000123."
USE_NEW = "Use NS-000999 instead."

GREETING = (
    "I can help record an issue for Northstar Shop. What is your order number and what "
    "happened? Please do not share payment details."
)
ASK_CATEGORY = (
    "Is this about delivery, a damaged item, a wrong item, a return/refund, or something else?"
)
ASK_URGENCY = "How urgent is this: low, normal, or high?"
INVALID_ORDER = "Please use NS- followed by exactly six digits, for example NS-123456."
WHAT_TO_CHANGE = "What would you like to change?"
REVIEW_FOOTER = "Please confirm these details or tell me what to change."
CONFIRM_PROMPT_AT_REVIEW = "Please confirm the reviewed details or tell me what to change."
ABSTENTION = (
    "I do not have that information in the Northstar Shop knowledge base. "
    "I can still record your issue for review."
)
RECEIPT_TEMPLATE = (
    "Saved locally as {session_id}. No request has been sent to a real support team."
)
RETURN_ANSWER_TEXT = (
    "You can request a return within 30 calendar days of delivery for an unused item in "
    "its original packaging. Refund eligibility requires support review."
)


def conflict_question(old: str, candidate: str, label: str) -> str:
    return f"You previously gave {old}. Should I keep that or use {candidate} for {label}?"


def summary_text(order: str, category: str, description: str, urgency: str) -> str:
    return f"Order: {order}\nCategory: {category}\nProblem: {description}\nUrgency: {urgency}"


def review_reply(summary: str) -> str:
    return f"{summary}\n{REVIEW_FOOTER}"


def with_followup(answer: str, followup: str) -> str:
    return f"{answer}\n\n{followup}"


def extraction(
    *,
    intent: str = "provide_details",
    has_question: bool = False,
    order_number: str | None = None,
    category: str | None = None,
    description: str | None = None,
    urgency: str | None = None,
    explicit_corrections: Iterable[str] = (),
    ambiguous_fields: Iterable[str] = (),
) -> Extraction:
    return Extraction(
        intent=intent,
        has_question=has_question,
        candidates=Slots(
            order_number=order_number,
            category=category,
            description=description,
            urgency=urgency,
        ),
        explicit_corrections=list(explicit_corrections),
        ambiguous_fields=list(ambiguous_fields),
    )


def x_part(order: str = "NS-000123") -> Extraction:
    return extraction(order_number=order, description="My order arrived broken")


def x_category() -> Extraction:
    return extraction(category="damaged_item")


def x_high() -> Extraction:
    return extraction(urgency="high")


def x_full(order: str = "NS-000123") -> Extraction:
    return extraction(
        order_number=order,
        category="damaged_item",
        description="My order arrived broken",
        urgency="high",
    )


def x_change() -> Extraction:
    return extraction(
        intent="correction", urgency="low", explicit_corrections=["urgency"]
    )


def x_no() -> Extraction:
    return extraction(intent="reject")


def x_yes() -> Extraction:
    return extraction(intent="confirm")


def x_question() -> Extraction:
    return extraction(intent="question", has_question=True)


def x_unrelated() -> Extraction:
    return extraction(intent="unrelated")


def return_answer() -> KnowledgeAnswer:
    return KnowledgeAnswer(
        answerable=True, answer=RETURN_ANSWER_TEXT, cited_ids=["faq-returns"]
    )
