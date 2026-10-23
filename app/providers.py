"""OpenAI-compatible provider adapter for four independent service roles.

One real ``httpx.AsyncClient`` per resolved role (LLM, RAG, STT, TTS) with
redirects disabled, ``trust_env=False`` and ``Accept-Encoding: identity``.
Every call is bounded, status-first mapped to the fixed IC section 11 error
table, and the adapter owns all retry/repair/jitter attempts inside the
operation deadline (PC sections 1-3, IC sections 4, 7, 11, 12).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import DEFAULT_PROVIDER_MAX_CONCURRENCY, ServiceProfile, TextProfile
from app.errors import AppError, app_error
from app.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    KNOWLEDGE_PROMPT,
    serialize_context,
)
from app.schemas import (
    Extraction,
    ExtractionContext,
    KnowledgeAnswer,
    KnowledgeContext,
)

Role = str
ROLES: tuple[Role, ...] = ("llm", "rag", "stt", "tts")

CHAT_COMPLETIONS_SUFFIX = "/chat/completions"
TRANSCRIPTIONS_SUFFIX = "/audio/transcriptions"
SPEECH_SUFFIX = "/audio/speech"

EXTRACTION_MAX_TOKENS = 1200
KNOWLEDGE_MAX_TOKENS = 700
EXTRACTION_SCHEMA_NAME = "extraction"
KNOWLEDGE_SCHEMA_NAME = "knowledge_answer"

SUCCESS_JSON_LIMIT = 1024 * 1024
TTS_LIMIT = 5 * 1024 * 1024

JITTER_MIN = 0.25
JITTER_MAX = 0.5

REPAIR_LINE = (
    "The previous response did not satisfy the schema. Return a complete schema object."
)
SCHEMA_INSTRUCTION = "Return one JSON object matching this schema: "

TEXT_ATTEMPT_TIMEOUT = 10.0
TEXT_TOTAL_DEADLINE = 25.0
STT_ATTEMPT_TIMEOUT = 15.0
STT_TOTAL_DEADLINE = 35.0
TTS_ATTEMPT_TIMEOUT = 20.0
TTS_TOTAL_DEADLINE = 20.0

MAX_TRANSCRIPT_CHARS = 4000
MAX_KNOWLEDGE_ANSWER_CHARS = 1200
MAX_CITED_IDS = 2


@dataclass(frozen=True)
class Budget:
    """Per-operation attempt, per-attempt and total-deadline budget."""

    attempts: int
    attempt_timeout: float
    total_deadline: float


TEXT_BUDGET = Budget(2, TEXT_ATTEMPT_TIMEOUT, TEXT_TOTAL_DEADLINE)
STT_BUDGET = Budget(2, STT_ATTEMPT_TIMEOUT, STT_TOTAL_DEADLINE)
TTS_BUDGET = Budget(1, TTS_ATTEMPT_TIMEOUT, TTS_TOTAL_DEADLINE)


class AIProvider(Protocol):
    async def extract(self, context: ExtractionContext) -> Extraction: ...

    async def answer_knowledge(self, context: KnowledgeContext) -> KnowledgeAnswer: ...

    async def transcribe(self, audio: bytes) -> str: ...

    async def synthesize(self, text: str) -> bytes: ...

    async def close(self) -> None: ...


class _InvalidContent(Exception):
    """Internal marker: successful HTTP with unusable content."""


Parsed = TypeVar("Parsed")


def _retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _status_error(status: int) -> AppError:
    """Map a non-2xx status first: retryable service vs. fixed configuration."""

    if _retryable_status(status):
        return app_error("provider_unavailable")
    return app_error("provider_configuration")


def _join(base_url: str, suffix: str) -> str:
    return base_url.rstrip("/") + suffix


def _schema_instruction(schema: type[BaseModel]) -> str:
    return SCHEMA_INSTRUCTION + json.dumps(schema.model_json_schema(), ensure_ascii=False)


def _text_payload(
    profile: TextProfile,
    system_prompt: str,
    user_content: str,
    max_tokens: int,
    schema: type[BaseModel],
    schema_name: str,
    *,
    repair: bool = False,
) -> dict[str, object]:
    system = system_prompt
    mode = profile.output_mode
    if mode in ("json_object", "prompt_json"):
        system = f"{system}\n\n{_schema_instruction(schema)}"
    if repair:
        system = f"{system}\n{REPAIR_LINE}"
    payload: dict[str, object] = {
        "model": profile.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if mode == "json_schema":
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema.model_json_schema(),
            },
        }
    elif mode == "json_object":
        payload["response_format"] = {"type": "json_object"}
    return payload


def _message_content(envelope: object) -> str:
    """Parse the first choice only, requiring an explicit clean ``stop``."""

    if not isinstance(envelope, dict):
        raise _InvalidContent
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _InvalidContent
    choice = choices[0]
    if not isinstance(choice, dict):
        raise _InvalidContent
    if choice.get("finish_reason") != "stop":
        raise _InvalidContent
    message = choice.get("message")
    if not isinstance(message, dict):
        raise _InvalidContent
    if message.get("refusal"):
        raise _InvalidContent
    if message.get("tool_calls"):
        raise _InvalidContent
    content = message.get("content")
    if not isinstance(content, str):
        raise _InvalidContent
    return content


def _loads(raw: str) -> object:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        raise _InvalidContent from None


def _parse_extraction(body: str) -> Extraction:
    payload = _loads(_message_content(_loads(body)).strip())
    if not isinstance(payload, dict):
        raise _InvalidContent
    try:
        return Extraction.model_validate(payload)
    except ValidationError:
        raise _InvalidContent from None


def _parse_knowledge_answer(body: str, context: KnowledgeContext) -> KnowledgeAnswer:
    payload = _loads(_message_content(_loads(body)).strip())
    if not isinstance(payload, dict):
        raise _InvalidContent
    try:
        answer = KnowledgeAnswer.model_validate(payload)
    except ValidationError:
        raise _InvalidContent from None
    _validate_answer(answer, context.sources)
    return answer


def _validate_answer(answer: KnowledgeAnswer, sources: list) -> None:
    """RAG section 4 citation rules, checked inside the bounded operation."""

    if not answer.answerable:
        if answer.answer != "" or answer.cited_ids:
            raise _InvalidContent
        return
    text = answer.answer.strip()
    if not 1 <= len(text) <= MAX_KNOWLEDGE_ANSWER_CHARS:
        raise _InvalidContent
    cited_ids = answer.cited_ids
    if not 1 <= len(cited_ids) <= MAX_CITED_IDS or len(set(cited_ids)) != len(cited_ids):
        raise _InvalidContent
    supplied = {record.id for record in sources}
    if any(cited_id not in supplied for cited_id in cited_ids):
        raise _InvalidContent


def _parse_transcription(body: str) -> str:
    payload = _loads(body)
    if not isinstance(payload, dict):
        raise _InvalidContent
    text = payload.get("text")
    if not isinstance(text, str):
        raise app_error("empty_transcription")
    trimmed = text.strip()
    if not trimmed:
        raise app_error("empty_transcription")
    if len(trimmed) > MAX_TRANSCRIPT_CHARS:
        raise app_error("transcript_too_long")
    return trimmed


def _media_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";")[0].strip().lower()


def _reject_encoded(response: httpx.Response) -> None:
    """Successful responses must not be compressed (IC section 12)."""

    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding and encoding != "identity":
        raise _InvalidContent


async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    """Count raw streamed bytes before retaining them; abort above ``limit``."""

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_raw():
        total += len(chunk)
        if total > limit:
            raise _InvalidContent
        chunks.append(chunk)
    return b"".join(chunks)


class OpenAICompatibleProvider:
    """Four independent role clients behind one bounded AIProvider surface."""

    def __init__(
        self,
        profiles: Mapping[Role, ServiceProfile],
        *,
        transports: Mapping[Role, httpx.AsyncBaseTransport] | None = None,
        capacity: int = DEFAULT_PROVIDER_MAX_CONCURRENCY,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._profiles = {role: profiles[role] for role in ROLES}
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._clock = clock if clock is not None else time.monotonic
        self._capacity = capacity
        self._in_use = 0
        self._closed = False
        self._clients: dict[Role, httpx.AsyncClient] = {
            "llm": self._build_client("llm", TEXT_BUDGET, transports),
            "rag": self._build_client("rag", TEXT_BUDGET, transports),
            "stt": self._build_client("stt", STT_BUDGET, transports),
            "tts": self._build_client("tts", TTS_BUDGET, transports),
        }

    def _build_client(
        self,
        role: Role,
        budget: Budget,
        transports: Mapping[Role, httpx.AsyncBaseTransport] | None,
    ) -> httpx.AsyncClient:
        profile = self._profiles[role]
        headers = {"Accept-Encoding": "identity"}
        if profile.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {profile.api_key}"
        return httpx.AsyncClient(
            transport=None if transports is None else transports[role],
            follow_redirects=False,
            trust_env=False,
            headers=headers,
            timeout=httpx.Timeout(budget.attempt_timeout),
        )

    async def extract(self, context: ExtractionContext) -> Extraction:
        self._acquire()
        try:
            profile = self._profiles["llm"]
            user_content = serialize_context(context)

            def build(repair: bool) -> dict[str, object]:
                return _text_payload(
                    profile,
                    EXTRACTION_SYSTEM_PROMPT,
                    user_content,
                    EXTRACTION_MAX_TOKENS,
                    Extraction,
                    EXTRACTION_SCHEMA_NAME,
                    repair=repair,
                )

            return await self._text_operation(
                "llm", profile, build, _parse_extraction, TEXT_BUDGET
            )
        finally:
            self._release()

    async def answer_knowledge(self, context: KnowledgeContext) -> KnowledgeAnswer:
        self._acquire()
        try:
            profile = self._profiles["rag"]
            user_content = serialize_context(context)

            def build(repair: bool) -> dict[str, object]:
                return _text_payload(
                    profile,
                    KNOWLEDGE_PROMPT,
                    user_content,
                    KNOWLEDGE_MAX_TOKENS,
                    KnowledgeAnswer,
                    KNOWLEDGE_SCHEMA_NAME,
                    repair=repair,
                )

            return await self._text_operation(
                "rag",
                profile,
                build,
                lambda body: _parse_knowledge_answer(body, context),
                TEXT_BUDGET,
            )
        finally:
            self._release()

    async def transcribe(self, audio: bytes) -> str:
        self._acquire()
        try:
            profile = self._profiles["stt"]
            files = {"file": ("upload.wav", audio, "audio/wav")}
            data = {"model": profile.model, "response_format": "json"}
            return await self._audio_operation(
                "stt", profile, files, data, _parse_transcription, STT_BUDGET
            )
        finally:
            self._release()

    async def synthesize(self, text: str) -> bytes:
        self._acquire()
        try:
            profile = self._profiles["tts"]
            payload = {
                "model": profile.model,
                "voice": profile.voice,
                "input": text,
                "response_format": "mp3",
            }
            deadline = self._clock() + TTS_BUDGET.total_deadline
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise app_error("provider_timeout")
            url = _join(profile.base_url, SPEECH_SUFFIX)
            return await self._speech_request(url, payload, remaining)
        finally:
            self._release()

    async def _speech_request(
        self, url: str, payload: dict[str, object], remaining: float
    ) -> bytes:
        try:
            async with asyncio.timeout(min(TTS_BUDGET.attempt_timeout, remaining)):
                async with self._clients["tts"].stream(
                    "POST", url, json=payload
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise _status_error(response.status_code)
                    try:
                        _reject_encoded(response)
                        if _media_type(response) not in ("audio/mpeg", "audio/mp3"):
                            raise _InvalidContent
                        audio = await _read_bounded(response, TTS_LIMIT)
                    except _InvalidContent:
                        raise app_error("invalid_provider_response") from None
                    if not audio:
                        raise app_error("invalid_provider_response")
                    return audio
        except TimeoutError:
            raise app_error("provider_timeout") from None
        except httpx.TimeoutException:
            raise app_error("provider_timeout") from None
        except httpx.TransportError:
            raise app_error("provider_unavailable") from None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for client in self._clients.values():
            await client.aclose()

    async def _text_operation(
        self,
        role: Role,
        profile: TextProfile,
        build: Callable[[bool], dict[str, object]],
        parse: Callable[[str], Parsed],
        budget: Budget,
    ) -> Parsed:
        url = _join(profile.base_url, CHAT_COMPLETIONS_SUFFIX)
        deadline = self._clock() + budget.total_deadline
        client = self._clients[role]

        async def attempt(repair: bool) -> Parsed:
            async with client.stream("POST", url, json=build(repair)) as response:
                if not 200 <= response.status_code < 300:
                    raise _status_error(response.status_code)
                _reject_encoded(response)
                body = (await _read_bounded(response, SUCCESS_JSON_LIMIT)).decode("utf-8")
            return parse(body)

        return await self._retry(budget, deadline, attempt, repair_on_invalid=True)

    async def _audio_operation(
        self,
        role: Role,
        profile: ServiceProfile,
        files: dict[str, tuple[str, bytes, str]],
        data: dict[str, str],
        parse: Callable[[str], Parsed],
        budget: Budget,
    ) -> Parsed:
        url = _join(profile.base_url, TRANSCRIPTIONS_SUFFIX)
        deadline = self._clock() + budget.total_deadline
        client = self._clients[role]

        async def attempt(repair: bool) -> Parsed:
            async with client.stream("POST", url, files=files, data=data) as response:
                if not 200 <= response.status_code < 300:
                    raise _status_error(response.status_code)
                _reject_encoded(response)
                body = (await _read_bounded(response, SUCCESS_JSON_LIMIT)).decode("utf-8")
            return parse(body)

        return await self._retry(budget, deadline, attempt, repair_on_invalid=False)

    async def _retry(
        self,
        budget: Budget,
        deadline: float,
        attempt: Callable[[bool], Awaitable[Parsed]],
        *,
        repair_on_invalid: bool,
    ) -> Parsed:
        """At most ``budget.attempts`` attempts: retry, or one repair, never both."""

        repair = False
        for attempt_number in range(1, budget.attempts + 1):
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise app_error("provider_timeout")
            try:
                async with asyncio.timeout(min(budget.attempt_timeout, remaining)):
                    return await attempt(repair)
            except _InvalidContent:
                if attempt_number == budget.attempts:
                    raise app_error("invalid_provider_response") from None
                repair = repair_on_invalid
            except UnicodeDecodeError:
                if attempt_number == budget.attempts:
                    raise app_error("invalid_provider_response") from None
            except TimeoutError:
                if attempt_number == budget.attempts:
                    raise app_error("provider_timeout") from None
                await self._pause(deadline)
            except httpx.TimeoutException:
                if attempt_number == budget.attempts:
                    raise app_error("provider_timeout") from None
                await self._pause(deadline)
            except httpx.TransportError:
                if attempt_number == budget.attempts:
                    raise app_error("provider_unavailable") from None
                await self._pause(deadline)
            except AppError as error:
                if error.code != "provider_unavailable" or attempt_number == budget.attempts:
                    raise
                await self._pause(deadline)
        raise app_error("provider_timeout")

    def _acquire(self) -> None:
        """Reject immediately with 429 provider_busy when capacity is exhausted."""

        if self._in_use >= self._capacity:
            raise app_error("provider_busy")
        self._in_use += 1

    def _release(self) -> None:
        self._in_use -= 1

    async def _pause(self, deadline: float) -> None:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise app_error("provider_timeout")
        await self._sleep(min(random.uniform(JITTER_MIN, JITTER_MAX), remaining))
