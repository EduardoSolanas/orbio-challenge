"""Unit tests for the frozen settings layer (IC section 2/3, PC section 2).

Settings.from_env(environ) is validated offline: no dotenv load, no network.
"""

from pathlib import Path

import pytest

from gateway.config import ServiceProfile, Settings, SpeechProfile, TextProfile
from tests.unit.gateway.config_support import (
    SESSION_DIR,
    assert_configuration_invalid,
    complete_rag_environ,
    valid_environ,
)


def test_from_env_reads_every_role_profile():
    settings = Settings.from_env(valid_environ())

    assert settings.ai_provider == "openai_compatible"
    assert settings.llm.base_url == "http://host.docker.internal:8001/v1"
    assert settings.llm.model == "extraction-model"
    assert settings.llm.auth_mode == "none"
    assert settings.llm.api_key == ""
    assert settings.llm.output_mode == "json_schema"
    assert settings.stt.base_url == "http://host.docker.internal:8002/v1"
    assert settings.stt.model == "transcription-model"
    assert settings.stt.auth_mode == "none"
    assert settings.tts.base_url == "http://host.docker.internal:8003/v1"
    assert settings.tts.model == "speech-model"
    assert settings.tts.voice == "alloy"


def test_profile_types_match_the_role_contract():
    settings = Settings.from_env(valid_environ())

    assert isinstance(settings.llm, TextProfile)
    assert isinstance(settings.rag, TextProfile)
    assert isinstance(settings.stt, ServiceProfile)
    assert isinstance(settings.tts, SpeechProfile)


def test_blank_rag_fields_inherit_the_complete_llm_profile():
    environ = valid_environ()
    environ["LLM_AUTH_MODE"] = "bearer"
    environ["LLM_API_KEY"] = "llm-secret"
    environ.update(
        {
            "RAG_BASE_URL": "",
            "RAG_MODEL": "",
            "RAG_AUTH_MODE": "",
            "RAG_API_KEY": "",
            "RAG_OUTPUT_MODE": "",
        }
    )

    settings = Settings.from_env(environ)

    assert settings.rag.base_url == settings.llm.base_url
    assert settings.rag.model == settings.llm.model
    assert settings.rag.auth_mode == "bearer"
    assert settings.rag.api_key == "llm-secret"
    assert settings.rag.output_mode == settings.llm.output_mode


def test_inherited_rag_profile_is_a_separate_immutable_object():
    settings = Settings.from_env(valid_environ())

    assert settings.rag is not settings.llm
    assert settings.rag == settings.llm


def test_absent_rag_variables_also_inherit_the_llm_profile():
    environ = valid_environ()
    for key in list(environ):
        if key.startswith("RAG_"):
            del environ[key]

    settings = Settings.from_env(environ)

    assert settings.rag == settings.llm


def test_complete_rag_profile_is_used_verbatim():
    environ = valid_environ()
    environ.update(complete_rag_environ())

    settings = Settings.from_env(environ)

    assert settings.rag.base_url == "http://rag.internal:9001/v1"
    assert settings.rag.model == "rag-model"
    assert settings.rag.auth_mode == "none"
    assert settings.rag.api_key == ""
    assert settings.rag.output_mode == "json_object"


def test_separately_configured_rag_never_inherits_the_llm_key():
    environ = valid_environ()
    environ["LLM_AUTH_MODE"] = "bearer"
    environ["LLM_API_KEY"] = "llm-secret"
    environ.update(complete_rag_environ())

    settings = Settings.from_env(environ)

    assert settings.llm.api_key == "llm-secret"
    assert settings.rag.api_key == ""
    assert settings.rag.auth_mode == "none"


def test_base_urls_keep_only_trailing_slashes_stripped():
    environ = valid_environ()
    environ["LLM_BASE_URL"] = "http://host.docker.internal:8001/v1///"
    environ["STT_BASE_URL"] = "https://stt.example.com/api/v1/"
    environ["TTS_BASE_URL"] = "http://host.docker.internal:8003"

    settings = Settings.from_env(environ)

    assert settings.llm.base_url == "http://host.docker.internal:8001/v1"
    assert settings.stt.base_url == "https://stt.example.com/api/v1"
    assert settings.tts.base_url == "http://host.docker.internal:8003"


def test_llm_output_mode_defaults_to_json_schema():
    environ = valid_environ()
    del environ["LLM_OUTPUT_MODE"]

    assert Settings.from_env(environ).llm.output_mode == "json_schema"


def test_reasoning_effort_is_optional_and_validated():
    environ = valid_environ()
    environ["LLM_REASONING_EFFORT"] = "none"

    assert Settings.from_env(environ).llm.reasoning_effort == "none"

    environ["LLM_REASONING_EFFORT"] = "invalid"
    with pytest.raises(RuntimeError):
        Settings.from_env(environ)


def test_provider_max_concurrency_defaults_to_four():
    environ = valid_environ()
    del environ["PROVIDER_MAX_CONCURRENCY"]

    assert Settings.from_env(environ).provider_max_concurrency == 4


def test_provider_max_concurrency_is_parsed_as_an_integer():
    environ = valid_environ()
    environ["PROVIDER_MAX_CONCURRENCY"] = "8"

    assert Settings.from_env(environ).provider_max_concurrency == 8


def test_session_dir_is_a_path():
    settings = Settings.from_env(valid_environ())

    assert isinstance(settings.session_dir, Path)
    assert settings.session_dir == Path(SESSION_DIR)


def test_settings_and_profiles_are_frozen():
    settings = Settings.from_env(valid_environ())

    with pytest.raises(Exception):
        settings.llm = settings.stt  # type: ignore[misc]
    with pytest.raises(Exception):
        settings.llm.model = "other"  # type: ignore[misc]
    with pytest.raises(Exception):
        settings.session_dir = Path("/tmp")  # type: ignore[misc]


def test_from_env_without_argument_reads_the_process_environment(monkeypatch):
    for key, value in valid_environ().items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("RAG_BASE_URL", raising=False)
    monkeypatch.delenv("RAG_MODEL", raising=False)
    monkeypatch.delenv("RAG_AUTH_MODE", raising=False)
    monkeypatch.delenv("RAG_API_KEY", raising=False)
    monkeypatch.delenv("RAG_OUTPUT_MODE", raising=False)

    settings = Settings.from_env()

    assert settings.llm.model == "extraction-model"
    assert settings.rag == settings.llm


def test_from_env_does_not_mutate_the_supplied_mapping():
    environ = valid_environ()
    snapshot = dict(environ)

    Settings.from_env(environ)

    assert environ == snapshot


# --- Validation slice: exact startup RuntimeError and offline settings matrix ---


REQUIRED_VARIABLES = [
    "AI_PROVIDER",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_AUTH_MODE",
    "STT_BASE_URL",
    "STT_MODEL",
    "STT_AUTH_MODE",
    "TTS_BASE_URL",
    "TTS_MODEL",
    "TTS_AUTH_MODE",
    "TTS_VOICE",
    "SESSION_DIR",
]


@pytest.mark.parametrize("variable", REQUIRED_VARIABLES)
def test_missing_required_variable_is_a_configuration_failure(variable):
    environ = valid_environ()
    del environ[variable]

    assert_configuration_invalid(environ)


@pytest.mark.parametrize("variable", REQUIRED_VARIABLES)
def test_blank_required_variable_is_a_configuration_failure(variable):
    environ = valid_environ()
    environ[variable] = "   "

    assert_configuration_invalid(environ)


@pytest.mark.parametrize("role", ["LLM", "RAG", "STT", "TTS"])
def test_bearer_auth_without_a_key_is_a_configuration_failure(role):
    environ = valid_environ()
    if role == "RAG":
        environ.update(complete_rag_environ())
    environ[f"{role}_AUTH_MODE"] = "bearer"
    environ[f"{role}_API_KEY"] = ""

    assert_configuration_invalid(environ)


@pytest.mark.parametrize("role", ["LLM", "RAG", "STT", "TTS"])
def test_none_auth_with_a_key_is_a_configuration_failure(role):
    environ = valid_environ()
    if role == "RAG":
        environ.update(complete_rag_environ())
    environ[f"{role}_AUTH_MODE"] = "none"
    environ[f"{role}_API_KEY"] = "unexpected-key"

    assert_configuration_invalid(environ)


@pytest.mark.parametrize("role", ["LLM", "RAG", "STT", "TTS"])
def test_unknown_auth_mode_is_a_configuration_failure(role):
    environ = valid_environ()
    if role == "RAG":
        environ.update(complete_rag_environ())
    environ[f"{role}_AUTH_MODE"] = "basic"

    assert_configuration_invalid(environ)


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "/v1",
        "http:///v1",
        "ftp://host:8001/v1",
        "host:8001/v1",
        "http://user:secret@host:8001/v1",
        "http://user@host:8001/v1",
        "http://host:8001/v1?query=1",
        "http://host:8001/v1#fragment",
        "http://host:8001/v1/../v1",
        "http://host:8001/%2e%2e/v1",
        " http://host:8001/v1",
        "http://host:8001/v1 ",
    ],
    ids=[
        "empty",
        "relative",
        "missing-host",
        "ftp",
        "no-scheme",
        "credentials",
        "username-only",
        "query",
        "fragment",
        "traversal",
        "encoded-traversal",
        "leading-space",
        "trailing-space",
    ],
)
def test_invalid_base_url_is_a_configuration_failure(base_url):
    environ = valid_environ()
    environ["LLM_BASE_URL"] = base_url

    assert_configuration_invalid(environ)


@pytest.mark.parametrize("role", ["LLM", "RAG", "STT", "TTS"])
def test_every_role_validates_its_base_url(role):
    environ = valid_environ()
    if role == "RAG":
        environ.update(complete_rag_environ())
    environ[f"{role}_BASE_URL"] = "http://user:secret@host:8001/v1"

    assert_configuration_invalid(environ)


@pytest.mark.parametrize("role", ["LLM", "RAG", "STT", "TTS"])
def test_model_longer_than_200_characters_is_a_configuration_failure(role):
    environ = valid_environ()
    if role == "RAG":
        environ.update(complete_rag_environ())
    environ[f"{role}_MODEL"] = "m" * 201

    assert_configuration_invalid(environ)


@pytest.mark.parametrize("role", ["LLM", "RAG", "STT", "TTS"])
def test_model_of_exactly_200_characters_is_accepted(role):
    environ = valid_environ()
    if role == "RAG":
        environ.update(complete_rag_environ())
    environ[f"{role}_MODEL"] = "m" * 200

    settings = Settings.from_env(environ)

    assert getattr(settings, role.lower()).model == "m" * 200


@pytest.mark.parametrize("role", ["LLM", "RAG", "STT", "TTS"])
def test_api_key_longer_than_4096_characters_is_a_configuration_failure(role):
    environ = valid_environ()
    if role == "RAG":
        environ.update(complete_rag_environ())
    environ[f"{role}_AUTH_MODE"] = "bearer"
    environ[f"{role}_API_KEY"] = "k" * 4097

    assert_configuration_invalid(environ)


@pytest.mark.parametrize("role", ["LLM", "RAG", "STT", "TTS"])
def test_api_key_of_exactly_4096_characters_is_accepted(role):
    environ = valid_environ()
    if role == "RAG":
        environ.update(complete_rag_environ())
    environ[f"{role}_AUTH_MODE"] = "bearer"
    environ[f"{role}_API_KEY"] = "k" * 4096

    settings = Settings.from_env(environ)

    assert getattr(settings, role.lower()).api_key == "k" * 4096


@pytest.mark.parametrize("output_mode", ["yaml", "JSON_SCHEMA", "json", " json_schema"])
def test_invalid_llm_output_mode_is_a_configuration_failure(output_mode):
    environ = valid_environ()
    environ["LLM_OUTPUT_MODE"] = output_mode

    assert_configuration_invalid(environ)


@pytest.mark.parametrize("output_mode", ["json_schema", "json_object", "prompt_json"])
def test_valid_llm_output_mode_is_accepted(output_mode):
    environ = valid_environ()
    environ["LLM_OUTPUT_MODE"] = output_mode

    assert Settings.from_env(environ).llm.output_mode == output_mode


@pytest.mark.parametrize("output_mode", ["", "yaml", "prompt-json"])
def test_configured_rag_requires_its_own_valid_output_mode(output_mode):
    environ = valid_environ()
    environ.update(complete_rag_environ())
    environ["RAG_OUTPUT_MODE"] = output_mode

    assert_configuration_invalid(environ)


@pytest.mark.parametrize(
    "field",
    ["RAG_BASE_URL", "RAG_MODEL", "RAG_AUTH_MODE", "RAG_API_KEY", "RAG_OUTPUT_MODE"],
)
def test_partial_rag_configuration_is_a_configuration_failure(field):
    environ = valid_environ()
    environ[field] = (
        "http://rag.internal:9001/v1" if field == "RAG_BASE_URL" else "value"
    )

    assert_configuration_invalid(environ)


@pytest.mark.parametrize("variable", ["TTS_VOICE"])
def test_missing_tts_voice_is_a_configuration_failure(variable):
    environ = valid_environ()
    environ[variable] = ""

    assert_configuration_invalid(environ)


@pytest.mark.parametrize(
    "value", ["0", "-1", "abc", "1.5", "4.0", "1e2", "many"], ids=str
)
def test_bad_provider_max_concurrency_is_a_configuration_failure(value):
    environ = valid_environ()
    environ["PROVIDER_MAX_CONCURRENCY"] = value

    assert_configuration_invalid(environ)


def test_provider_max_concurrency_of_one_is_accepted():
    environ = valid_environ()
    environ["PROVIDER_MAX_CONCURRENCY"] = "1"

    assert Settings.from_env(environ).provider_max_concurrency == 1


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_provider_max_concurrency_falls_back_to_the_default(value):
    environ = valid_environ()
    environ["PROVIDER_MAX_CONCURRENCY"] = value

    assert Settings.from_env(environ).provider_max_concurrency == 4


@pytest.mark.parametrize("session_dir", ["", "   "])
def test_missing_session_dir_is_a_configuration_failure(session_dir):
    environ = valid_environ()
    environ["SESSION_DIR"] = session_dir

    assert_configuration_invalid(environ)


def test_relative_session_dir_is_accepted():
    environ = valid_environ()
    environ["SESSION_DIR"] = "./data/sessions"

    assert Settings.from_env(environ).session_dir == Path("./data/sessions")


@pytest.mark.parametrize("provider", ["openai", "", "OPENAI_COMPATIBLE"])
def test_unsupported_ai_provider_is_a_configuration_failure(provider):
    environ = valid_environ()
    environ["AI_PROVIDER"] = provider

    assert_configuration_invalid(environ)


def test_bearer_profiles_are_accepted_for_every_role():
    environ = valid_environ()
    environ.update(complete_rag_environ())
    for role in ("LLM", "RAG", "STT", "TTS"):
        environ[f"{role}_AUTH_MODE"] = "bearer"
        environ[f"{role}_API_KEY"] = f"{role.lower()}-secret"

    settings = Settings.from_env(environ)

    assert settings.llm.api_key == "llm-secret"
    assert settings.rag.api_key == "rag-secret"
    assert settings.stt.api_key == "stt-secret"
    assert settings.tts.api_key == "tts-secret"


def test_failure_message_never_includes_the_offending_value():
    environ = valid_environ()
    environ["LLM_BASE_URL"] = "http://user:topsecret@host:8001/v1"

    with pytest.raises(RuntimeError) as excinfo:
        Settings.from_env(environ)

    assert "topsecret" not in str(excinfo.value)


# --- Mutation-hardening slice: non-equivalent surviving mutants ---


def test_trailing_slash_stripping_does_not_strip_other_characters():
    environ = valid_environ()
    environ["LLM_BASE_URL"] = "http://host.docker.internal:8001/v1X/"

    assert Settings.from_env(environ).llm.base_url == (
        "http://host.docker.internal:8001/v1X"
    )


@pytest.mark.parametrize("role", ["LLM", "RAG", "STT", "TTS"])
def test_absent_api_key_is_accepted_for_none_auth(role):
    environ = valid_environ()
    if role == "RAG":
        environ.update(complete_rag_environ())
    environ[f"{role}_AUTH_MODE"] = "none"
    del environ[f"{role}_API_KEY"]

    settings = Settings.from_env(environ)

    assert getattr(settings, role.lower()).api_key == ""


@pytest.mark.parametrize("role", ["LLM", "RAG", "STT", "TTS"])
def test_absent_api_key_is_a_failure_for_bearer_auth(role):
    environ = valid_environ()
    if role == "RAG":
        environ.update(complete_rag_environ())
    environ[f"{role}_AUTH_MODE"] = "bearer"
    del environ[f"{role}_API_KEY"]

    assert_configuration_invalid(environ)
