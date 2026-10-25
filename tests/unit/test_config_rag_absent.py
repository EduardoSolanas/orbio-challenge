"""A configured RAG profile must name every required field explicitly (PC section 2)."""

import pytest

from app.config import Settings
from tests.unit.config_support import (
    assert_configuration_invalid,
    complete_rag_environ,
    valid_environ,
)


@pytest.mark.parametrize(
    "field", ["RAG_BASE_URL", "RAG_MODEL", "RAG_AUTH_MODE", "RAG_OUTPUT_MODE"]
)
def test_rag_profile_with_one_required_variable_absent_is_a_configuration_failure(field):
    environ = valid_environ()
    environ.update(complete_rag_environ())
    del environ[field]

    assert_configuration_invalid(environ)


def test_rag_profile_without_an_api_key_variable_uses_the_empty_key_in_none_mode():
    environ = valid_environ()
    environ.update(complete_rag_environ())
    del environ["RAG_API_KEY"]

    settings = Settings.from_env(environ)

    assert settings.rag.api_key == ""
    assert settings.rag.model == "rag-model"
