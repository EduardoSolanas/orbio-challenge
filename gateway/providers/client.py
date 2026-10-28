"""OpenAI-compatible provider adapter for four independent service roles.

One real ``httpx.AsyncClient`` per resolved role (LLM, RAG, STT, TTS) with
redirects disabled, ``trust_env=False`` and ``Accept-Encoding: identity``.
Every call is bounded, status-first mapped to the fixed IC section 11 error
table, and the adapter owns all retry/repair/jitter attempts inside the
operation deadline (PC sections 1-3, IC sections 4, 7, 11, 12).

Request payloads and response parsers live in ``provider_wire``; speech chunk
and WAV helpers live in ``speech_wav``.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Mapping, Protocol, TypeVar
from urllib.parse import quote

import httpx

from gateway.config import (
    DEFAULT_PROVIDER_MAX_CONCURRENCY,
    ELEVENLABS,
    ServiceProfile,
    SpeechProfile,
    TextProfile,
)
from gateway.errors import AppError, app_error
from gateway.providers.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    KNOWLEDGE_PROMPT,
    serialize_context,
)
from gateway.providers.speech_wav import (
    TTS_LIMIT,
    PcmChunk,
    assemble_wav,
    decode_pcm_wav,
    split_tts_text,
)
from gateway.providers.wire import (
    REPAIR_LINE,
    SCHEMA_INSTRUCTION,
    InvalidContent,
    parse_embeddings,
    parse_extraction,
    parse_knowledge_answer,
    parse_transcription,
    text_payload,
)
from gateway.schemas import (
    Extraction,
    ExtractionContext,
    KnowledgeAnswer,
    KnowledgeContext,
)

__all__ = [
    "REPAIR_LINE",
    "SCHEMA_INSTRUCTION",
    "SUCCESS_JSON_LIMIT",
    "AIProvider",
    "Budget",
    "OpenAICompatibleProvider",
    "SynthesizedAudio",
    "assemble_wav",
    "decode_pcm_wav",
    "split_tts_text",
]

Role = str
ROLES: tuple[Role, ...] = ("llm", "rag", "stt", "tts")
SPANISH_SPEECH_ROLE: Role = "tts_es"
EMBED_ROLE: Role = "embed"
SPEECH_ROLE_BY_LANGUAGE: dict[str, Role] = {"en": "tts", "es": SPANISH_SPEECH_ROLE}

CHAT_COMPLETIONS_SUFFIX = "/chat/completions"
TRANSCRIPTIONS_SUFFIX = "/audio/transcriptions"
SPEECH_SUFFIX = "/audio/speech"
ELEVENLABS_QUERY = "?output_format=mp3_44100_128"
ELEVENLABS_TRANSCRIPTION_SUFFIX = "/speech-to-text"
EMBEDDINGS_SUFFIX = "/embeddings"

EXTRACTION_MAX_TOKENS = 1200
KNOWLEDGE_MAX_TOKENS = 700
EXTRACTION_SCHEMA_NAME = "extraction"
KNOWLEDGE_SCHEMA_NAME = "knowledge_answer"

SUCCESS_JSON_LIMIT = 1024 * 1024
TTS_MAX_CHUNKS = 20
MP3_MEDIA_TYPES = ("audio/mpeg", "audio/mp3")
WAV_MEDIA_TYPES = ("audio/wav", "audio/x-wav", "audio/wave")

JITTER_MIN = 0.25
JITTER_MAX = 0.5

TEXT_ATTEMPT_TIMEOUT = 10.0
TEXT_TOTAL_DEADLINE = 25.0
STT_ATTEMPT_TIMEOUT = 15.0
STT_TOTAL_DEADLINE = 35.0
TTS_ATTEMPT_TIMEOUT = 20.0
TTS_TOTAL_DEADLINE = 20.0


@dataclass(frozen=True)
class Budget:
    """Per-operation attempt, per-attempt and total-deadline budget."""

    attempts: int
    attempt_timeout: float
    total_deadline: float


@dataclass(frozen=True)
class SynthesizedAudio:
    """Validated speech bytes with the media type the browser must play."""

    data: bytes
    media_type: Literal["audio/mpeg", "audio/wav"]


TEXT_BUDGET = Budget(2, TEXT_ATTEMPT_TIMEOUT, TEXT_TOTAL_DEADLINE)
STT_BUDGET = Budget(2, STT_ATTEMPT_TIMEOUT, STT_TOTAL_DEADLINE)
TTS_BUDGET = Budget(1, TTS_ATTEMPT_TIMEOUT, TTS_TOTAL_DEADLINE)

ROLE_BUDGETS: dict[Role, Budget] = {
    "llm": TEXT_BUDGET,
    "rag": TEXT_BUDGET,
    "stt": STT_BUDGET,
    "tts": TTS_BUDGET,
    SPANISH_SPEECH_ROLE: TTS_BUDGET,
    EMBED_ROLE: TEXT_BUDGET,
}


class AIProvider(Protocol):
    async def extract(self, context: ExtractionContext) -> Extraction: ...

    async def answer_knowledge(self, context: KnowledgeContext) -> KnowledgeAnswer: ...

    async def transcribe(self, audio: bytes) -> str: ...

    async def synthesize(self, text: str, language: str = "en") -> SynthesizedAudio: ...

    async def close(self) -> None: ...


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


def _speech_url(profile: SpeechProfile) -> str:
    if profile.auth_mode == ELEVENLABS:
        voice = quote(profile.voice, safe="")
        return _join(profile.base_url, f"/text-to-speech/{voice}{ELEVENLABS_QUERY}")
    return _join(profile.base_url, SPEECH_SUFFIX)


def _media_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";")[0].strip().lower()


def _reject_encoded(response: httpx.Response) -> None:
    """Successful responses must not be compressed (IC section 12)."""

    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding and encoding != "identity":
        raise InvalidContent


async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    """Count raw streamed bytes before retaining them; abort above ``limit``."""

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_raw():
        total += len(chunk)
        if total > limit:
            raise InvalidContent
        chunks.append(chunk)
    return b"".join(chunks)


async def _success_json_body(response: httpx.Response) -> str:
    """Status-first, uncompressed, size-bounded JSON body of a 2xx response."""

    if not 200 <= response.status_code < 300:
        raise _status_error(response.status_code)
    _reject_encoded(response)
    return (await _read_bounded(response, SUCCESS_JSON_LIMIT)).decode("utf-8")


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
        if SPANISH_SPEECH_ROLE in profiles:
            self._profiles[SPANISH_SPEECH_ROLE] = profiles[SPANISH_SPEECH_ROLE]
        if EMBED_ROLE in profiles:
            self._profiles[EMBED_ROLE] = profiles[EMBED_ROLE]
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._clock = clock if clock is not None else time.monotonic
        self._capacity = capacity
        self._in_use = 0
        self._closed = False
        self._clients: dict[Role, httpx.AsyncClient] = {
            role: self._build_client(role, ROLE_BUDGETS[role], transports)
            for role in self._profiles
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
        elif profile.auth_mode == ELEVENLABS:
            headers["xi-api-key"] = profile.api_key
        headers.update(profile.headers)
        return httpx.AsyncClient(
            transport=None if transports is None else transports.get(role),
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
                return text_payload(
                    profile,
                    EXTRACTION_SYSTEM_PROMPT,
                    user_content,
                    EXTRACTION_MAX_TOKENS,
                    Extraction,
                    EXTRACTION_SCHEMA_NAME,
                    repair=repair,
                )

            return await self._text_operation(
                "llm", profile, build, parse_extraction, TEXT_BUDGET
            )
        finally:
            self._release()

    async def answer_knowledge(self, context: KnowledgeContext) -> KnowledgeAnswer:
        self._acquire()
        try:
            profile = self._profiles["rag"]
            user_content = serialize_context(context)

            def build(repair: bool) -> dict[str, object]:
                return text_payload(
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
                lambda body: parse_knowledge_answer(body, context),
                TEXT_BUDGET,
            )
        finally:
            self._release()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, in order; needs the optional ``embed`` profile."""

        self._acquire()
        try:
            profile = self._profiles.get(EMBED_ROLE)
            if profile is None:
                raise app_error("provider_configuration")
            url = _join(profile.base_url, EMBEDDINGS_SUFFIX)
            payload = {"model": profile.model, "input": texts}
            deadline = self._clock() + TEXT_BUDGET.total_deadline
            client = self._clients[EMBED_ROLE]

            async def attempt(repair: bool) -> list[list[float]]:
                async with client.stream("POST", url, json=payload) as response:
                    body = await _success_json_body(response)
                return parse_embeddings(body, len(texts))

            return await self._retry(
                TEXT_BUDGET, deadline, attempt, repair_on_invalid=False
            )
        finally:
            self._release()

    async def transcribe(self, audio: bytes) -> str:
        self._acquire()
        try:
            profile = self._profiles["stt"]
            files = {"file": ("upload.wav", audio, "audio/wav")}
            data = (
                # Scribe tags sounds like "(laughter)" into the text unless told not to.
                {"model_id": profile.model, "tag_audio_events": "false"}
                if profile.auth_mode == ELEVENLABS
                else {"model": profile.model, "response_format": "json"}
            )
            return await self._audio_operation(
                "stt", profile, files, data, parse_transcription, STT_BUDGET
            )
        finally:
            self._release()

    async def synthesize(self, text: str, language: str = "en") -> SynthesizedAudio:
        self._acquire()
        try:
            role = SPEECH_ROLE_BY_LANGUAGE.get(language)
            if role is None or role not in self._profiles:
                raise app_error("provider_configuration")
            profile = self._profiles[role]
            chunks = split_tts_text(text, profile.max_input_chars)
            if not chunks:
                raise app_error("invalid_provider_response")
            if len(chunks) > TTS_MAX_CHUNKS or (
                profile.audio_format == "mp3" and len(chunks) > 1
            ):
                raise app_error("provider_configuration")
            deadline = self._clock() + TTS_BUDGET.total_deadline
            client = self._clients[role]
            url = _speech_url(profile)
            if profile.audio_format == "mp3":
                audio = await self._speech_request(
                    client, url, profile, chunks[0], deadline, TTS_LIMIT
                )
                return SynthesizedAudio(audio, "audio/mpeg")
            return await self._synthesize_wav(client, url, profile, chunks, deadline)
        finally:
            self._release()

    async def _synthesize_wav(
        self,
        client: httpx.AsyncClient,
        url: str,
        profile: SpeechProfile,
        chunks: list[str],
        deadline: float,
    ) -> SynthesizedAudio:
        """One request per text chunk, joined under a single WAV header."""

        raw_total = 0
        parts: list[PcmChunk] = []
        for chunk in chunks:
            raw = await self._speech_request(
                client, url, profile, chunk, deadline, TTS_LIMIT - raw_total
            )
            raw_total += len(raw)
            try:
                parts.append(decode_pcm_wav(raw))
            except ValueError:
                raise app_error("invalid_provider_response") from None
        try:
            return SynthesizedAudio(assemble_wav(parts), "audio/wav")
        except ValueError:
            raise app_error("invalid_provider_response") from None

    async def _speech_request(
        self,
        client: httpx.AsyncClient,
        url: str,
        profile: SpeechProfile,
        text: str,
        deadline: float,
        limit: int,
    ) -> bytes:
        payload = (
            {"text": text, "model_id": profile.model}
            if profile.auth_mode == ELEVENLABS
            else {
                "model": profile.model,
                "voice": profile.voice,
                "input": text,
                "response_format": profile.audio_format,
            }
        )
        allowed = WAV_MEDIA_TYPES if profile.audio_format == "wav" else MP3_MEDIA_TYPES
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise app_error("provider_timeout")
        try:
            async with asyncio.timeout(min(TTS_BUDGET.attempt_timeout, remaining)):
                async with client.stream("POST", url, json=payload) as response:
                    if not 200 <= response.status_code < 300:
                        raise _status_error(response.status_code)
                    try:
                        _reject_encoded(response)
                        if _media_type(response) not in allowed:
                            raise InvalidContent
                        audio = await _read_bounded(response, limit)
                    except InvalidContent:
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
                body = await _success_json_body(response)
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
        suffix = (
            ELEVENLABS_TRANSCRIPTION_SUFFIX
            if profile.auth_mode == ELEVENLABS
            else TRANSCRIPTIONS_SUFFIX
        )
        url = _join(profile.base_url, suffix)
        deadline = self._clock() + budget.total_deadline
        client = self._clients[role]

        async def attempt(repair: bool) -> Parsed:
            async with client.stream("POST", url, files=files, data=data) as response:
                body = await _success_json_body(response)
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
            last = attempt_number == budget.attempts
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise app_error("provider_timeout")
            try:
                async with asyncio.timeout(min(budget.attempt_timeout, remaining)):
                    return await attempt(repair)
            except InvalidContent:
                if last:
                    raise app_error("invalid_provider_response") from None
                repair = repair_on_invalid
            except UnicodeDecodeError:
                if last:
                    raise app_error("invalid_provider_response") from None
            except (TimeoutError, httpx.TimeoutException):
                if last:
                    raise app_error("provider_timeout") from None
                await self._pause(deadline)
            except httpx.TransportError:
                if last:
                    raise app_error("provider_unavailable") from None
                await self._pause(deadline)
            except AppError as error:
                if error.code != "provider_unavailable" or last:
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
