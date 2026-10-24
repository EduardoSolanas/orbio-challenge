from __future__ import annotations

import json
from unittest.mock import patch

from tests.e2e.judge_config import JudgeConfig, judge, load_config


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
    with patch("tests.e2e.judge_config.urllib.request.urlopen", return_value=_Response()) as opened:
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
        "E2E_JUDGE_BASE_URL=http://judge.local/v1\n"
        "E2E_JUDGE_MODEL=local-model\n",
        encoding="utf-8",
    )

    assert load_config(config_file).max_tokens == 1024
