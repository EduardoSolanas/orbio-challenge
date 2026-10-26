from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tests.e2e.judge_config import JudgeConfig, judge, judge_payload, load_config


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "pass": True,
                                    "intent": "social",
                                    "reason": "Brief greeting.",
                                    "violations": [],
                                }
                            )
                        }
                    }
                ]
            }
        ).encode()


def test_judge_uses_prompted_json_without_unsupported_structured_output():
    config = JudgeConfig(
        base_url="http://judge.local/v1",
        model="local-model",
        api_key=None,
        timeout_seconds=5,
        max_tokens=128,
        temperature=0,
        prompt_version="test-v1",
    )
    with patch(
        "tests.e2e.judge_config.urllib.request.urlopen", return_value=_Response()
    ) as opened:
        result = judge(config, "ASSISTANT Hello", "brief greeting")

    payload = json.loads(opened.call_args.args[0].data)
    assert "response_format" not in payload
    assert result["verdict"] == {
        "pass": True,
        "intent": "social",
        "reason": "Brief greeting.",
        "violations": [],
    }


def test_judge_default_output_budget_handles_reasoning_models(tmp_path):
    config_file = tmp_path / ".env.judge"
    config_file.write_text(
        "E2E_JUDGE_BASE_URL=http://judge.local/v1\nE2E_JUDGE_MODEL=local-model\n",
        encoding="utf-8",
    )

    assert load_config(config_file).max_tokens == 4096


class _Truncated(_Response):
    def read(self) -> bytes:
        return json.dumps(
            {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
        ).encode()


def test_a_judge_that_runs_out_of_tokens_says_so_instead_of_reporting_bad_json():
    config = JudgeConfig(
        base_url="http://judge.local/v1",
        model="local-model",
        api_key=None,
        timeout_seconds=5,
        max_tokens=128,
        temperature=0,
        prompt_version="test-v1",
    )
    with patch(
        "tests.e2e.judge_config.urllib.request.urlopen", return_value=_Truncated()
    ):
        with pytest.raises(
            AssertionError, match="ran out of tokens.*E2E_JUDGE_MAX_TOKENS"
        ):
            judge(config, "ASSISTANT Hello", "brief greeting")


def _config(**overrides):
    base = dict(
        base_url="http://judge.local/v1",
        model="local-model",
        api_key=None,
        timeout_seconds=5,
        max_tokens=128,
        temperature=0,
        prompt_version="test-v1",
    )
    return JudgeConfig(**{**base, **overrides})


def test_reasoning_effort_is_sent_only_when_configured():
    messages = [{"role": "user", "content": "x"}]

    assert "reasoning_effort" not in judge_payload(_config(), messages)
    assert (
        judge_payload(_config(reasoning_effort="low"), messages)["reasoning_effort"]
        == "low"
    )


def test_the_reasoning_effort_setting_is_read_and_validated(tmp_path):
    config_file = tmp_path / ".env.judge"
    base = "E2E_JUDGE_BASE_URL=http://judge.local/v1\nE2E_JUDGE_MODEL=local-model\n"
    config_file.write_text(base + "E2E_JUDGE_REASONING_EFFORT=low\n", encoding="utf-8")
    assert load_config(config_file).reasoning_effort == "low"

    config_file.write_text(base + "E2E_JUDGE_REASONING_EFFORT=\n", encoding="utf-8")
    assert load_config(config_file).reasoning_effort is None

    config_file.write_text(base + "E2E_JUDGE_REASONING_EFFORT=loud\n", encoding="utf-8")
    with pytest.raises(ValueError, match="E2E_JUDGE_REASONING_EFFORT"):
        load_config(config_file)
