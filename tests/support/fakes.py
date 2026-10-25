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

from backend.gateway.errors import app_error
from backend.gateway.providers.client import SynthesizedAudio
from backend.gateway.schemas import Extraction, KnowledgeAnswer, Slots, ToolRequest

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


STREAMING_SIZE = 0xFFFFFFFF


def make_streaming_wav(
    *,
    rate: int = 24000,
    channels: int = 1,
    width: int = 2,
    frames: int = 1600,
    riff_size: int | None = None,
    data_size: int | None = None,
    with_list: bool = True,
    bits: int | None = None,
) -> bytes:
    """PCM WAV in Groq's streaming form: unspecified RIFF/data sizes.

    ``riff_size``/``data_size`` default to ``0xFFFFFFFF`` (unspecified); pass an
    integer to emit a finite size. A ``LIST``/``INFO`` chunk mirrors the real
    Groq Orpheus header when ``with_list`` is true. ``bits`` overrides the
    sample-width-derived bit count for unsupported-metadata cases.
    """

    payload = b"\x00" * frames * channels * width
    fmt = struct.pack(
        "<HHLLHH",
        1,
        channels,
        rate,
        rate * channels * width,
        channels * width,
        width * 8 if bits is None else bits,
    )
    chunks = b"fmt " + struct.pack("<L", len(fmt)) + fmt
    if with_list:
        info = b"INFOISFT" + struct.pack("<L", 12) + b"Lavf61.7.100"
        chunks += b"LIST" + struct.pack("<L", len(info)) + info
    chunks += b"data" + struct.pack(
        "<L", STREAMING_SIZE if data_size is None else data_size
    ) + payload
    return (
        b"RIFF"
        + struct.pack("<L", STREAMING_SIZE if riff_size is None else riff_size)
        + b"WAVE"
        + chunks
    )


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
    media_type: str = "audio/mpeg"


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
        media_type: str = "audio/mpeg",
        fail: str | None = None,
        hold: Gate | None = None,
    ) -> Step:
        return self._script(
            Step("synthesize", text, audio, fail, hold, media_type)
        )

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
            if method == "synthesize":
                result = SynthesizedAudio(
                    reply_mp3_bytes() if result is None else result, step.media_type
                )
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

    async def synthesize(self, text: str, language: str = "en") -> SynthesizedAudio:
        return await self._call(
            "synthesize", text, {"text": text, "language": language}
        )

    async def close(self) -> None:
        self.close_count += 1


# --------------------------------------------------------------------------- #
# Contract literals (IC §5 templates, §13.1 scripted inputs, RAG §4)
# --------------------------------------------------------------------------- #

STT_TEXT = "Order NS-000123 arrived broken and this is urgent"

PART = "My order NS-000123 arrived broken."
MIXED_PART = "Hello, my order NS-000123 arrived broken."
CATEGORY = "It is a damaged item."
HIGH = "High urgency."
FULL = "My order NS-000123 arrived broken. It is a damaged item and high urgency."
CHANGE = "Actually make the urgency low."
NO = "No."
YES = "Yes."
RETURN_Q = "Can I return an unused item?"
GREETING_STANDING_CHARGE_Q = "Hello, what does a standing charge cover on my energy bill?"
PAYMENT_Q = "Which payment methods are accepted?"
DELIVERY_Q = "Hello, how long does delivery take?"
CAPABILITY_Q = "What can you help me with?"
UNRELATED = "Tell me a joke."
THANKS = "Thanks."
RESUME = "Continue my issue."

ORDER_STATUS_Q = "What is the status of my energy account?"
INSTALLATION_Q = "What is the status of my smart meter installation?"
BOOK_Q = "I would like to book a technician."
ORDER_NUMBER_TEXT = "My energy account reference is NS-000123."
AUTH_ORDER_NUMBER_PROMPT = (
    "Please provide the order number you want to check, for example NS-123456."
)
AUTH_VERIFICATION_PROMPT = (
    "Enter the service postcode and demo verification code for this energy account."
)
AUTH_DEMO_CODE_NOTICE = (
    "Email delivery is simulated. No email is sent. Demo code: 246810."
)
AUTH_VERIFICATION_FAILED = (
    "We couldn't verify those details. Check the postcode and code and try again."
)
AUTH_VERIFIED = "Energy account verified for this demo."
AUTH_ORDER_RESULT = (
    "I found the Northstar Energy account NS-000123 for Casey: Electricity and gas supply. Account status: Active."
)

BAD_ORDER = "My order NS-12345 arrived broken."
FIX_ORDER = "The order number is NS-000123."
CONFLICT_ORDER = "Actually the order is NS-000999."
KEEP_OLD = "Keep the old one, NS-000123."
USE_NEW = "Use NS-000999 instead."

GREETING = "Hi, welcome to Northstar Energy. How can I help you today?"
ASK_CATEGORY = (
    "Which area do you need help with: a bill, a supply issue, a meter reading, moving home, or something else?"
)
ASK_URGENCY = "How urgent is this: low, normal, or high?"
INVALID_ORDER = "Use NS- followed by exactly six digits for your energy account reference, for example NS-123456."
WHAT_TO_CHANGE = "What would you like to change?"
REVIEW_FOOTER = "Please confirm these details or tell me what to change."
CONFIRM_PROMPT_AT_REVIEW = "Please confirm the reviewed details or tell me what to change."
ABSTENTION = (
    "I do not have that information in Northstar Energy's help guides. "
    "I can still help with your energy question or service issue."
)
RECEIPT_TEMPLATE = (
    "Saved locally as {session_id}. No request has been sent to a real support team."
)
RETURN_ANSWER_TEXT = (
    "You can request a return within 30 calendar days of delivery for an unused item in "
    "its original packaging. Refund eligibility requires support review."
)
STANDING_CHARGE_ANSWER_TEXT = (
    "A standing charge is a fixed daily amount that helps cover the cost of supplying "
    "electricity or gas to your property. It is charged for each day your account is "
    "connected, even when you use little or no energy. This explanation is illustrative "
    "and is not a tariff or a quote."
)


def conflict_question(old: str, candidate: str, label: str) -> str:
    if label == "order number":
        label = "energy account reference"
    return f"You previously gave {old}. Should I keep that or use {candidate} for {label}?"


def summary_text(order: str, category: str, description: str, urgency: str) -> str:
    category_label = {
        "delivery": "supply issue",
        "damaged_item": "equipment issue",
        "wrong_item": "meter reading issue",
        "return_refund": "billing issue",
        "other": "other energy issue",
    }[category]
    return (
        f"Account reference: {order}\nEnergy support area: {category_label}\n"
        f"Issue: {description}\nUrgency: {urgency}"
    )


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
    detected_language: str = "en",
    requested_language: str | None = None,
    sentiment: str = "neutral",
    frustration: str = "none",
    tool_request: ToolRequest | None = None,
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
        detected_language=detected_language,
        requested_language=requested_language,
        sentiment=sentiment,
        frustration=frustration,
        tool_request=tool_request,
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


def x_social() -> Extraction:
    return extraction(intent="social")


def x_gratitude() -> Extraction:
    return extraction(intent="gratitude", sentiment="positive")


def x_capability(has_question: bool = True) -> Extraction:
    return extraction(intent="capability", has_question=has_question)


def x_start_intake() -> Extraction:
    return extraction(intent="start_intake")


def x_unrelated() -> Extraction:
    return extraction(intent="unrelated")


def x_order_lookup(
    order_number: str | None = None, *, tool: bool = False, language: str = "en"
) -> Extraction:
    return extraction(
        intent="order_lookup",
        has_question=True,
        order_number=order_number,
        detected_language=language,
        tool_request=(
            ToolRequest(name="lookup_verified_order", arguments={}) if tool else None
        ),
    )


def return_answer() -> KnowledgeAnswer:
    return KnowledgeAnswer(
        answerable=True, answer=RETURN_ANSWER_TEXT, cited_ids=["faq-returns"]
    )


def standing_charge_answer() -> KnowledgeAnswer:
    return KnowledgeAnswer(
        answerable=True,
        answer=STANDING_CHARGE_ANSWER_TEXT,
        cited_ids=["faq-standing-charge"],
    )
