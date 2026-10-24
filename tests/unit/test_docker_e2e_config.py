from __future__ import annotations

from tests.e2e.docker_config import load_config


def test_docker_e2e_config_reads_judge_values_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("E2E_DOCKER_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("E2E_JUDGE_BASE_URL", "http://judge.local/v1")
    monkeypatch.setenv("E2E_JUDGE_MODEL", "local-model")
    monkeypatch.setenv("E2E_JUDGE_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("E2E_JUDGE_MAX_TOKENS", "1024")
    monkeypatch.setenv("E2E_JUDGE_TEMPERATURE", "0")
    monkeypatch.setenv("E2E_JUDGE_PROMPT_VERSION", "test-v1")
    monkeypatch.setenv("E2E_JUDGE_API_KEY", "no-key-required")

    config = load_config(tmp_path)

    assert config.app_url == "http://localhost:8000"
    assert config.judge.base_url == "http://judge.local/v1"
    assert config.judge.model == "local-model"
    assert config.judge.max_tokens == 1024
