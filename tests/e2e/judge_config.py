"""Dedicated OpenAI-compatible configuration and client for intent E2E judging."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import REASONING_EFFORTS

PROMPT_VERSION = "intent-judge-v2"


@dataclass(frozen=True)
class JudgeConfig:
    base_url: str
    model: str
    api_key: str | None
    timeout_seconds: float
    max_tokens: int
    temperature: float
    prompt_version: str
    # Optional: ask a reasoning judge to think less (same values as LLM_REASONING_EFFORT).
    reasoning_effort: str | None = None


def parse_reasoning_effort(value: str | None) -> str | None:
    if not value:
        return None
    if value not in REASONING_EFFORTS:
        raise ValueError(f"E2E_JUDGE_REASONING_EFFORT must be one of {', '.join(REASONING_EFFORTS)}")
    return value


def judge_payload(config: JudgeConfig, messages: list[dict[str, str]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    if config.reasoning_effort is not None:
        payload["reasoning_effort"] = config.reasoning_effort
    return payload


def _dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def load_config(path: Path | None = None) -> JudgeConfig:
    values = _dotenv(path or Path(__file__).with_name(".env.judge"))
    values.update({key: value for key, value in os.environ.items() if key.startswith("E2E_JUDGE_")})
    required = ("E2E_JUDGE_BASE_URL", "E2E_JUDGE_MODEL")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise RuntimeError(
            "E2E intent judge is not configured; set "
            + ", ".join(missing)
            + " in tests/e2e/.env.judge (see .env.judge.example)."
        )
    base_url = values["E2E_JUDGE_BASE_URL"].rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("E2E_JUDGE_BASE_URL must be an absolute http(s) URL")
    timeout = float(values.get("E2E_JUDGE_TIMEOUT_SECONDS", "30"))
    max_tokens = int(values.get("E2E_JUDGE_MAX_TOKENS", "4096"))
    temperature = float(values.get("E2E_JUDGE_TEMPERATURE", "0"))
    prompt_version = values.get("E2E_JUDGE_PROMPT_VERSION", PROMPT_VERSION)
    if timeout <= 0 or timeout > 120:
        raise ValueError("E2E_JUDGE_TIMEOUT_SECONDS must be > 0 and <= 120")
    if max_tokens < 1 or max_tokens > 4096:
        raise ValueError("E2E_JUDGE_MAX_TOKENS must be between 1 and 4096")
    if not 0 <= temperature <= 2:
        raise ValueError("E2E_JUDGE_TEMPERATURE must be between 0 and 2")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", prompt_version):
        raise ValueError("E2E_JUDGE_PROMPT_VERSION must be 1-80 safe characters")
    return JudgeConfig(
        base_url=base_url,
        model=values["E2E_JUDGE_MODEL"],
        api_key=values.get("E2E_JUDGE_API_KEY") or None,
        timeout_seconds=timeout,
        max_tokens=max_tokens,
        temperature=temperature,
        prompt_version=prompt_version,
        reasoning_effort=parse_reasoning_effort(values.get("E2E_JUDGE_REASONING_EFFORT")),
    )


def validate_verdict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"pass", "intent", "reason", "violations"}:
        raise ValueError("judge output must have exactly pass, intent, reason, violations")
    if not isinstance(value["pass"], bool) or not isinstance(value["intent"], str):
        raise ValueError("judge pass must be boolean and intent must be a string")
    if not isinstance(value["reason"], str) or not isinstance(value["violations"], list):
        raise ValueError("judge reason must be a string and violations must be a list")
    if not all(isinstance(item, str) for item in value["violations"]):
        raise ValueError("judge violations must contain strings only")
    return value


def judge(config: JudgeConfig, conversation: str, rule: str) -> dict[str, Any]:
    system = (
        "You are a strict intent-routing evaluator. Return only JSON with exactly these keys: "
        'pass (boolean), intent (string), reason (string), violations (array of strings). '
        "Judge visible response quality only; do not infer hidden state or require the assistant to narrate it. "
        "Deterministic assertions check state preservation separately."
    )
    user = json.dumps({"conversation": conversation, "expected_rule": rule}, ensure_ascii=False)
    payload = judge_payload(
        config, [{"role": "system", "content": system}, {"role": "user", "content": user}]
    )
    headers = {"Content-Type": "application/json"}
    if config.api_key and config.api_key != "no-key-required":
        headers["Authorization"] = f"Bearer {config.api_key}"
    request = urllib.request.Request(
        f"{config.base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise AssertionError(f"intent judge request failed: {exc}") from exc
    try:
        choice = raw["choices"][0]
        content = choice["message"]["content"]
        if not content and choice.get("finish_reason") == "length":
            raise ValueError(
                "the judge ran out of tokens before answering (a reasoning model spends them "
                "thinking); raise E2E_JUDGE_MAX_TOKENS"
            )
        verdict = validate_verdict(json.loads(content))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
        raise AssertionError(f"intent judge returned invalid structured JSON: {exc}") from exc
    return {"model": config.model, "prompt_version": config.prompt_version, "output": content, "verdict": verdict}
