"""Frozen settings populated from the process environment (IC section 2, PC section 2).

Validation is explicit and offline: no dotenv load, no network probe. Any
invalid or missing setting raises the exact startup RuntimeError from
the configuration rules, never leaking the offending value.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn
from urllib.parse import unquote, urlsplit

from gateway.errors import CONFIGURATION_INVALID_MESSAGE

DEFAULT_LLM_OUTPUT_MODE = "json_schema"
DEFAULT_PROVIDER_MAX_CONCURRENCY = 4
DEFAULT_TTS_AUDIO_FORMAT = "mp3"
DEFAULT_TTS_MAX_INPUT_CHARS = 4000
DEFAULT_BACKEND_API_TIMEOUT_SECONDS = 5.0
MAX_BACKEND_API_TIMEOUT_SECONDS = 60.0
MIN_TTS_MAX_INPUT_CHARS = 1
MAX_TTS_MAX_INPUT_CHARS = 4000
AUDIO_FORMATS = ("mp3", "wav")
OPENAI_COMPATIBLE = "openai_compatible"
AUTH_MODES = ("none", "bearer")
# Speech only: the xi-api-key header and ElevenLabs' own routes (text-to-speech with mp3 replies,
# speech-to-text).
ELEVENLABS = "elevenlabs"
OUTPUT_MODES = ("json_schema", "json_object", "prompt_json")
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
MAX_MODEL_LENGTH = 200
MAX_API_KEY_LENGTH = 4096
MAX_EXTRA_HEADERS = 8
MAX_HEADER_VALUE_LENGTH = 256
_HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}")
_HEADER_VALUE = re.compile(r"[ -~]+")
# Set by the client itself; a configured value must not replace them.
RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "xi-api-key",
        "accept-encoding",
        "content-type",
        "host",
        "content-length",
    }
)
RAG_FIELDS = ("BASE_URL", "MODEL", "AUTH_MODE", "API_KEY", "OUTPUT_MODE")
EMBED_FIELDS = ("BASE_URL", "MODEL", "AUTH_MODE", "API_KEY")
DEFAULT_EMBED_MIN_SIMILARITY = 0.25
TTS_ES_FIELDS = (
    "BASE_URL",
    "MODEL",
    "AUTH_MODE",
    "API_KEY",
    "VOICE",
    "AUDIO_FORMAT",
    "MAX_INPUT_CHARS",
)


def _fail() -> NoReturn:
    raise RuntimeError(CONFIGURATION_INVALID_MESSAGE)


def _required(value: str) -> str:
    if not value or value != value.strip():
        _fail()
    return value


def _base_url(value: str) -> str:
    """Validate an absolute http/https API-prefix URL and strip trailing slashes."""

    _required(value)
    stripped = value.rstrip("/")
    try:
        parts = urlsplit(stripped)
        hostname = parts.hostname
    except ValueError:
        _fail()
    if parts.scheme not in ("http", "https"):
        _fail()
    if not hostname:
        _fail()
    if parts.username is not None or parts.password is not None:
        _fail()
    if parts.query or parts.fragment:
        _fail()
    if any(unquote(segment) == ".." for segment in parts.path.split("/")):
        _fail()
    return stripped


def _model(value: str) -> str:
    _required(value)
    if len(value) > MAX_MODEL_LENGTH:
        _fail()
    return value


def _api_key(value: str) -> str:
    if len(value) > MAX_API_KEY_LENGTH:
        _fail()
    return value


def _headers(raw: str) -> tuple[tuple[str, str], ...]:
    """Extra static request headers from a JSON object, for providers that need one (a session id)."""

    if raw == "":
        return ()
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        _fail()
    if not isinstance(value, dict) or not 1 <= len(value) <= MAX_EXTRA_HEADERS:
        _fail()
    for name, content in value.items():
        if (
            _HEADER_NAME.fullmatch(name) is None
            or name.lower() in RESERVED_HEADERS
            or not isinstance(content, str)
            or len(content) > MAX_HEADER_VALUE_LENGTH
            or _HEADER_VALUE.fullmatch(content) is None
        ):
            _fail()
    return tuple(value.items())


def _auth(
    auth_mode: str,
    api_key: str,
    audio_format: str | None = None,
    *,
    stt: bool = False,
) -> None:
    """Only speech roles may use ElevenLabs: ``audio_format`` is given for a voice, ``stt`` for transcription."""

    speech = audio_format is not None or stt
    if auth_mode not in AUTH_MODES and not (speech and auth_mode == ELEVENLABS):
        _fail()
    if auth_mode == ELEVENLABS and audio_format not in (None, "mp3"):
        _fail()
    if auth_mode in ("bearer", ELEVENLABS) and not api_key:
        _fail()
    if auth_mode == "none" and api_key:
        _fail()


def _output_mode(value: str) -> str:
    if value not in OUTPUT_MODES:
        _fail()
    return value


def _reasoning_effort(value: str) -> str | None:
    if value == "":
        return None
    if value not in REASONING_EFFORTS:
        _fail()
    return value


def _concurrency(raw: str) -> int:
    text = raw.strip()
    if not text:
        return DEFAULT_PROVIDER_MAX_CONCURRENCY
    if not text.isdigit():
        _fail()
    value = int(text)
    if value < 1:
        _fail()
    return value


def _audio_format(value: str) -> str:
    """Exactly ``mp3`` or ``wav``; empty keeps the MP3 default (PC §3)."""

    if value == "":
        return DEFAULT_TTS_AUDIO_FORMAT
    if value not in AUDIO_FORMATS:
        _fail()
    return value


def _tts_max_input_chars(raw: str) -> int:
    if raw == "":
        return DEFAULT_TTS_MAX_INPUT_CHARS
    if not raw.isdigit():
        _fail()
    value = int(raw)
    if not MIN_TTS_MAX_INPUT_CHARS <= value <= MAX_TTS_MAX_INPUT_CHARS:
        _fail()
    return value


def _backend_api_timeout(raw: str) -> float:
    """Positive seconds up to the bounded maximum; empty keeps the 5 s default."""

    text = raw.strip()
    if text == "":
        return DEFAULT_BACKEND_API_TIMEOUT_SECONDS
    try:
        value = float(text)
    except ValueError:
        _fail()
    if not 0 < value <= MAX_BACKEND_API_TIMEOUT_SECONDS:
        _fail()
    return value


def _embed(env: Mapping[str, str]) -> ServiceProfile | None:
    """Optional embedding service: all blank (keyword retrieval) or one complete profile."""

    values = {field: env.get(f"EMBED_{field}", "") for field in EMBED_FIELDS}
    if all(value == "" for value in values.values()):
        return None
    if any(values[field] == "" for field in ("BASE_URL", "MODEL", "AUTH_MODE")):
        _fail()
    _auth(values["AUTH_MODE"], values["API_KEY"])
    return ServiceProfile(
        base_url=_base_url(values["BASE_URL"]),
        model=_model(values["MODEL"]),
        auth_mode=values["AUTH_MODE"],
        api_key=_api_key(values["API_KEY"]),
    )


def _min_similarity(raw: str) -> float:
    """Cosine floor in (0, 1); empty keeps the default."""

    text = raw.strip()
    if text == "":
        return DEFAULT_EMBED_MIN_SIMILARITY
    try:
        value = float(text)
    except ValueError:
        _fail()
    if not 0 < value < 1:
        _fail()
    return value


def _tts_es(env: Mapping[str, str]) -> SpeechProfile | None:
    """Optional Spanish speech profile: all blank, or one complete explicit profile."""

    values = {field: env.get(f"TTS_ES_{field}", "") for field in TTS_ES_FIELDS}
    if all(value == "" for value in values.values()):
        return None
    if any(values[field] == "" for field in TTS_ES_FIELDS if field != "API_KEY"):
        _fail()
    raw_format = values["AUDIO_FORMAT"]
    if raw_format not in AUDIO_FORMATS:
        _fail()
    raw_max = values["MAX_INPUT_CHARS"]
    if not raw_max.isdigit():
        _fail()
    max_input_chars = int(raw_max)
    if not MIN_TTS_MAX_INPUT_CHARS <= max_input_chars <= MAX_TTS_MAX_INPUT_CHARS:
        _fail()
    if raw_format == "mp3" and max_input_chars != MAX_TTS_MAX_INPUT_CHARS:
        _fail()
    _auth(values["AUTH_MODE"], values["API_KEY"], raw_format)
    return SpeechProfile(
        base_url=_base_url(values["BASE_URL"]),
        model=_model(values["MODEL"]),
        auth_mode=values["AUTH_MODE"],
        api_key=_api_key(values["API_KEY"]),
        voice=_required(values["VOICE"]),
        audio_format=raw_format,
        max_input_chars=max_input_chars,
    )


@dataclass(frozen=True)
class ServiceProfile:
    base_url: str
    model: str
    auth_mode: str
    api_key: str
    headers: tuple[tuple[str, str], ...] = field(default=(), kw_only=True)


@dataclass(frozen=True)
class TextProfile(ServiceProfile):
    output_mode: str
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class SpeechProfile(ServiceProfile):
    voice: str
    audio_format: str = DEFAULT_TTS_AUDIO_FORMAT
    max_input_chars: int = DEFAULT_TTS_MAX_INPUT_CHARS


def _role(
    get: Callable[[str], str],
    prefix: str,
    profile_cls: type[ServiceProfile],
    **extra: Any,
) -> Any:
    """Read one model role (``LLM``, ``RAG``, ``STT``, ``TTS``) from its ``<prefix>_*`` settings."""

    profile = profile_cls(
        base_url=_base_url(get(f"{prefix}_BASE_URL")),
        model=_model(get(f"{prefix}_MODEL")),
        auth_mode=get(f"{prefix}_AUTH_MODE"),
        api_key=_api_key(get(f"{prefix}_API_KEY")),
        headers=_headers(get(f"{prefix}_HEADERS")),
        **extra,
    )
    _auth(
        profile.auth_mode,
        profile.api_key,
        getattr(profile, "audio_format", None),
        stt=prefix == "STT",
    )
    return profile


@dataclass(frozen=True)
class Settings:
    ai_provider: str
    llm: TextProfile
    rag: TextProfile
    stt: ServiceProfile
    tts: SpeechProfile
    session_dir: Path
    provider_max_concurrency: int
    backend_api_base_url: str
    backend_api_timeout_seconds: float
    tts_es: SpeechProfile | None = None
    embed: ServiceProfile | None = None
    embed_min_similarity: float = DEFAULT_EMBED_MIN_SIMILARITY

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ

        def get(key: str) -> str:
            return env.get(key, "")

        if get("AI_PROVIDER") != OPENAI_COMPATIBLE:
            _fail()

        raw_llm_output_mode = get("LLM_OUTPUT_MODE")
        llm = _role(
            get,
            "LLM",
            TextProfile,
            output_mode=(
                DEFAULT_LLM_OUTPUT_MODE
                if raw_llm_output_mode == ""
                else _output_mode(raw_llm_output_mode)
            ),
            reasoning_effort=_reasoning_effort(get("LLM_REASONING_EFFORT")),
        )

        if any(get(f"RAG_{field}") != "" for field in RAG_FIELDS):
            rag = _role(
                get,
                "RAG",
                TextProfile,
                output_mode=_output_mode(get("RAG_OUTPUT_MODE")),
                reasoning_effort=_reasoning_effort(get("RAG_REASONING_EFFORT")),
            )
        else:
            rag = TextProfile(
                base_url=llm.base_url,
                model=llm.model,
                auth_mode=llm.auth_mode,
                api_key=llm.api_key,
                headers=llm.headers,
                output_mode=llm.output_mode,
                reasoning_effort=(
                    _reasoning_effort(get("RAG_REASONING_EFFORT"))
                    or llm.reasoning_effort
                ),
            )

        stt = _role(get, "STT", ServiceProfile)

        audio_format = _audio_format(get("TTS_AUDIO_FORMAT"))
        max_input_chars = _tts_max_input_chars(get("TTS_MAX_INPUT_CHARS"))
        if audio_format == "mp3" and max_input_chars != MAX_TTS_MAX_INPUT_CHARS:
            _fail()

        tts = _role(
            get,
            "TTS",
            SpeechProfile,
            voice=_required(get("TTS_VOICE")),
            audio_format=audio_format,
            max_input_chars=max_input_chars,
        )

        return cls(
            ai_provider=OPENAI_COMPATIBLE,
            llm=llm,
            rag=rag,
            stt=stt,
            tts=tts,
            session_dir=Path(_required(get("SESSION_DIR"))),
            provider_max_concurrency=_concurrency(get("PROVIDER_MAX_CONCURRENCY")),
            backend_api_base_url=_base_url(get("BACKEND_API_BASE_URL")),
            backend_api_timeout_seconds=_backend_api_timeout(
                get("BACKEND_API_TIMEOUT_SECONDS")
            ),
            tts_es=_tts_es(env),
            embed=_embed(env),
            embed_min_similarity=_min_similarity(get("EMBED_MIN_SIMILARITY")),
        )
