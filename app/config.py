"""Frozen settings populated from the process environment (IC section 2, PC section 2).

Validation is explicit and offline: no dotenv load, no network probe. Any
invalid or missing setting raises the exact startup RuntimeError from
implementation-contract section 11, never leaking the offending value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, NoReturn
from urllib.parse import unquote, urlsplit

from app.errors import CONFIGURATION_INVALID_MESSAGE

DEFAULT_LLM_OUTPUT_MODE = "json_schema"
DEFAULT_PROVIDER_MAX_CONCURRENCY = 4
DEFAULT_TTS_AUDIO_FORMAT = "mp3"
DEFAULT_TTS_MAX_INPUT_CHARS = 4000
MIN_TTS_MAX_INPUT_CHARS = 1
MAX_TTS_MAX_INPUT_CHARS = 4000
AUDIO_FORMATS = ("mp3", "wav")
OPENAI_COMPATIBLE = "openai_compatible"
AUTH_MODES = ("none", "bearer")
OUTPUT_MODES = ("json_schema", "json_object", "prompt_json")
MAX_MODEL_LENGTH = 200
MAX_API_KEY_LENGTH = 4096
RAG_FIELDS = ("BASE_URL", "MODEL", "AUTH_MODE", "API_KEY", "OUTPUT_MODE")
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


def _auth(auth_mode: str, api_key: str) -> None:
    if auth_mode not in AUTH_MODES:
        _fail()
    if auth_mode == "bearer" and not api_key:
        _fail()
    if auth_mode == "none" and api_key:
        _fail()


def _output_mode(value: str) -> str:
    if value not in OUTPUT_MODES:
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
    _auth(values["AUTH_MODE"], values["API_KEY"])
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


@dataclass(frozen=True)
class TextProfile(ServiceProfile):
    output_mode: str


@dataclass(frozen=True)
class SpeechProfile(ServiceProfile):
    voice: str
    audio_format: str = DEFAULT_TTS_AUDIO_FORMAT
    max_input_chars: int = DEFAULT_TTS_MAX_INPUT_CHARS


@dataclass(frozen=True)
class Settings:
    ai_provider: str
    llm: TextProfile
    rag: TextProfile
    stt: ServiceProfile
    tts: SpeechProfile
    session_dir: Path
    provider_max_concurrency: int
    tts_es: SpeechProfile | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ

        if env.get("AI_PROVIDER", "") != OPENAI_COMPATIBLE:
            _fail()

        raw_llm_output_mode = env.get("LLM_OUTPUT_MODE", "")
        llm = TextProfile(
            base_url=_base_url(env.get("LLM_BASE_URL", "")),
            model=_model(env.get("LLM_MODEL", "")),
            auth_mode=env.get("LLM_AUTH_MODE", ""),
            api_key=_api_key(env.get("LLM_API_KEY", "")),
            output_mode=(
                DEFAULT_LLM_OUTPUT_MODE
                if raw_llm_output_mode == ""
                else _output_mode(raw_llm_output_mode)
            ),
        )
        _auth(llm.auth_mode, llm.api_key)

        if any(env.get(f"RAG_{field}", "") != "" for field in RAG_FIELDS):
            rag = TextProfile(
                base_url=_base_url(env.get("RAG_BASE_URL", "")),
                model=_model(env.get("RAG_MODEL", "")),
                auth_mode=env.get("RAG_AUTH_MODE", ""),
                api_key=_api_key(env.get("RAG_API_KEY", "")),
                output_mode=_output_mode(env.get("RAG_OUTPUT_MODE", "")),
            )
            _auth(rag.auth_mode, rag.api_key)
        else:
            rag = TextProfile(
                base_url=llm.base_url,
                model=llm.model,
                auth_mode=llm.auth_mode,
                api_key=llm.api_key,
                output_mode=llm.output_mode,
            )

        stt = ServiceProfile(
            base_url=_base_url(env.get("STT_BASE_URL", "")),
            model=_model(env.get("STT_MODEL", "")),
            auth_mode=env.get("STT_AUTH_MODE", ""),
            api_key=_api_key(env.get("STT_API_KEY", "")),
        )
        _auth(stt.auth_mode, stt.api_key)

        audio_format = _audio_format(env.get("TTS_AUDIO_FORMAT", ""))
        max_input_chars = _tts_max_input_chars(env.get("TTS_MAX_INPUT_CHARS", ""))
        if audio_format == "mp3" and max_input_chars != MAX_TTS_MAX_INPUT_CHARS:
            _fail()

        tts = SpeechProfile(
            base_url=_base_url(env.get("TTS_BASE_URL", "")),
            model=_model(env.get("TTS_MODEL", "")),
            auth_mode=env.get("TTS_AUTH_MODE", ""),
            api_key=_api_key(env.get("TTS_API_KEY", "")),
            voice=_required(env.get("TTS_VOICE", "")),
            audio_format=audio_format,
            max_input_chars=max_input_chars,
        )
        _auth(tts.auth_mode, tts.api_key)

        return cls(
            ai_provider=OPENAI_COMPATIBLE,
            llm=llm,
            rag=rag,
            stt=stt,
            tts=tts,
            session_dir=Path(_required(env.get("SESSION_DIR", ""))),
            provider_max_concurrency=_concurrency(
                env.get("PROVIDER_MAX_CONCURRENCY", "")
            ),
            tts_es=_tts_es(env),
        )
