"""Strict configuration and semantic judge for the real Docker E2E gate."""
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

from tests.e2e.judge_config import (
    JudgeConfig,
    ask_judge,
    parse_reasoning_effort,
    read_dotenv,
)


@dataclass(frozen=True)
class DockerE2EConfig:
    app_url: str
    judge: JudgeConfig


def _url(value: str, name: str) -> str:
    parsed = urlparse(value.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"{name} must be an application origin without a path or query")
    if parsed.username or parsed.password:
        raise ValueError(f"{name} must not contain credentials")
    return f"{parsed.scheme}://{parsed.netloc}"


def _endpoint(value: str, name: str) -> str:
    parsed = urlparse(value.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"{name} must not contain query, fragment, or credentials")
    return value.rstrip("/")


def load_config(root: Path | None = None) -> DockerE2EConfig:
    root = root or Path(__file__).resolve().parents[2]
    docker_values = read_dotenv(root / "tests/e2e/.env.docker")
    app_url = docker_values.get("E2E_DOCKER_BASE_URL") or os.environ.get("E2E_DOCKER_BASE_URL")
    if not app_url:
        raise RuntimeError(
            "Docker E2E is not configured: set E2E_DOCKER_BASE_URL in "
            "tests/e2e/.env.docker (see .env.docker.example)."
        )

    judge_values = read_dotenv(root / "tests/e2e/.env.judge")
    judge_values.update(
        {key: value for key, value in os.environ.items() if key.startswith("E2E_JUDGE_")}
    )
    required = (
        "E2E_JUDGE_BASE_URL",
        "E2E_JUDGE_MODEL",
        "E2E_JUDGE_TIMEOUT_SECONDS",
        "E2E_JUDGE_MAX_TOKENS",
        "E2E_JUDGE_TEMPERATURE",
        "E2E_JUDGE_PROMPT_VERSION",
    )
    missing = [key for key in required if not judge_values.get(key)]
    if missing:
        raise RuntimeError(
            "Docker E2E judge is not configured: missing "
            + ", ".join(missing)
            + " in tests/e2e/.env.judge (see .env.judge.example)."
        )
    model = judge_values["E2E_JUDGE_MODEL"]
    if model == "replace-with-exact-local-model-id":
        raise ValueError("E2E_JUDGE_MODEL must be the exact deployed model ID")
    try:
        timeout = float(judge_values["E2E_JUDGE_TIMEOUT_SECONDS"])
        max_tokens = int(judge_values["E2E_JUDGE_MAX_TOKENS"])
        temperature = float(judge_values["E2E_JUDGE_TEMPERATURE"])
    except ValueError as exc:
        raise ValueError("judge timeout, max tokens, and temperature must be numeric") from exc
    if not 0 < timeout <= 120:
        raise ValueError("E2E_JUDGE_TIMEOUT_SECONDS must be > 0 and <= 120")
    if not 1 <= max_tokens <= 4096:
        raise ValueError("E2E_JUDGE_MAX_TOKENS must be between 1 and 4096")
    if not 0 <= temperature <= 2:
        raise ValueError("E2E_JUDGE_TEMPERATURE must be between 0 and 2")
    prompt_version = judge_values["E2E_JUDGE_PROMPT_VERSION"]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", prompt_version):
        raise ValueError("E2E_JUDGE_PROMPT_VERSION must use 1-80 safe characters")
    judge = JudgeConfig(
        base_url=_endpoint(judge_values["E2E_JUDGE_BASE_URL"], "E2E_JUDGE_BASE_URL"),
        model=model,
        api_key=os.environ.get("E2E_JUDGE_API_KEY") or None,
        timeout_seconds=timeout,
        max_tokens=max_tokens,
        temperature=temperature,
        prompt_version=prompt_version,
        reasoning_effort=parse_reasoning_effort(judge_values.get("E2E_JUDGE_REASONING_EFFORT")),
    )
    return DockerE2EConfig(app_url=_url(app_url, "E2E_DOCKER_BASE_URL"), judge=judge)


def probe(config: DockerE2EConfig) -> None:
    """Fail before browser launch when either real origin is unavailable."""
    for url, label in ((f"{config.app_url}/health", "Docker application"),
                       (f"{config.judge.base_url}/models", "LLM judge")):
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=config.judge.timeout_seconds) as response:
                if response.status != 200:
                    raise RuntimeError(f"{label} returned HTTP {response.status}")
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{label} is unavailable at {url}: {exc}") from exc
        if label == "Docker application" and payload != {"status": "ok"}:
            raise RuntimeError(f"Docker application health response is not {{'status': 'ok'}}: {payload}")
        if label == "LLM judge":
            models = payload.get("data") if isinstance(payload, dict) else None
            ids = {item.get("id") for item in models or [] if isinstance(item, dict)}
            if config.judge.model not in ids:
                raise RuntimeError(f"LLM judge model {config.judge.model!r} was not advertised")


def semantic_judge(config: JudgeConfig, user_turn: str, response: str, context: str) -> dict[str, Any]:
    """Grade one reply against a written rule (the real-provider journeys)."""

    system = (
        "You are a strict semantic evaluator for a customer-support RAG UI. "
        "Return only JSON with exactly pass, intent, reason, violations. "
        "Judge relevance and grounding only; deterministic state and citation checks are external."
    )
    content, verdict = ask_judge(config, system, {"user_turn": user_turn, "response": response, "context": context})
    return {"model": config.model, "prompt_version": config.prompt_version, "raw_output": content, "verdict": verdict}
