"""Tests for scripts/check_e2e_layers.py.

Guards the controlled-provider vs real-Docker evidence separation:
controlled reports must be labelled controlled, must never claim live
Docker/provider coverage, must not use the unqualified name "full E2E",
and must never contain secrets.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_e2e_layers  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def controlled_collection() -> dict:
    return {
        "source_revision": "rev1",
        "browser": "chromium",
        "environment_type": "controlled-provider",
        "provider_mode": "controlled-fake",
        "application_url": "local uvicorn fixture (loopback, port 0)",
        "collected": [],
        "deselected": [],
        "errors": [],
    }


def controlled_results() -> dict:
    return {
        "source_revision": "rev1",
        "browser": "chromium",
        "environment_type": "controlled-provider",
        "provider_mode": "controlled-fake",
        "application_url": "local uvicorn fixture (loopback, port 0)",
        "exit_status": 0,
        "outcomes": [],
    }


def test_pass_controlled_only(tmp_path: Path) -> None:
    collection = write_json(tmp_path / "collection.json", controlled_collection())
    results = write_json(tmp_path / "results.json", controlled_results())
    assert check_e2e_layers.main([
        "--collection", str(collection),
        "--results", str(results),
        "--sources-dir", str(tmp_path / "empty-src"),
    ]) == 0


def test_controlled_report_claiming_live_docker_fails(tmp_path: Path) -> None:
    bad = controlled_collection()
    bad["environment_type"] = "real-docker"
    collection = write_json(tmp_path / "collection.json", bad)
    results = write_json(tmp_path / "results.json", controlled_results())
    assert check_e2e_layers.main([
        "--collection", str(collection),
        "--results", str(results),
        "--sources-dir", str(tmp_path / "empty-src"),
    ]) == 1


def test_missing_controlled_label_fails(tmp_path: Path) -> None:
    bad = controlled_collection()
    del bad["provider_mode"]
    collection = write_json(tmp_path / "collection.json", bad)
    results = write_json(tmp_path / "results.json", controlled_results())
    assert check_e2e_layers.main([
        "--collection", str(collection),
        "--results", str(results),
        "--sources-dir", str(tmp_path / "empty-src"),
    ]) == 1


def test_unqualified_full_e2e_in_source_fails(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "test_j01_x.py").write_text(
        '"""Browser workflow E2E (controlled provider)."""\nFULL = "full E2E run"\n',
        encoding="utf-8",
    )
    collection = write_json(tmp_path / "collection.json", controlled_collection())
    results = write_json(tmp_path / "results.json", controlled_results())
    assert check_e2e_layers.main([
        "--collection", str(collection),
        "--results", str(results),
        "--sources-dir", str(src),
    ]) == 1


def test_qualified_full_e2e_passes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "test_j01_x.py").write_text(
        '"""Browser workflow E2E (controlled provider, not the full E2E live system)."""\n'
        'LABEL = "full E2E (controlled-provider browser workflow)"\n',
        encoding="utf-8",
    )
    collection = write_json(tmp_path / "collection.json", controlled_collection())
    results = write_json(tmp_path / "results.json", controlled_results())
    assert check_e2e_layers.main([
        "--collection", str(collection),
        "--results", str(results),
        "--sources-dir", str(src),
    ]) == 0


def test_secret_in_controlled_config_fails(tmp_path: Path) -> None:
    config = write_json(
        tmp_path / "run-config.json",
        {"environment_type": "controlled-provider", "E2E_JUDGE_API_KEY": "sk-live-123"},
    )
    collection = write_json(tmp_path / "collection.json", controlled_collection())
    results = write_json(tmp_path / "results.json", controlled_results())
    assert check_e2e_layers.main([
        "--collection", str(collection),
        "--results", str(results),
        "--sources-dir", str(tmp_path / "empty-src"),
        "--controlled-config", str(config),
    ]) == 1


def test_require_docker_without_evidence_fails(tmp_path: Path) -> None:
    collection = write_json(tmp_path / "collection.json", controlled_collection())
    results = write_json(tmp_path / "results.json", controlled_results())
    assert check_e2e_layers.main([
        "--collection", str(collection),
        "--results", str(results),
        "--sources-dir", str(tmp_path / "empty-src"),
        "--require-docker",
        "--docker-config", str(tmp_path / "missing-run-config.json"),
        "--docker-xml", str(tmp_path / "missing-docker.xml"),
    ]) == 1


def test_require_docker_with_evidence_passes(tmp_path: Path) -> None:
    docker_config = write_json(
        tmp_path / "run-config.json",
        {
            "environment_type": "real-docker",
            "provider_mode": "real-provider",
            "application_origin": "http://127.0.0.1:8080",
            "judge_model": "local-model-1",
            "prompt_version": "v1",
        },
    )
    docker_xml = tmp_path / "docker.xml"
    docker_xml.write_text("<testsuite tests=\"1\"/>", encoding="utf-8")
    collection = write_json(tmp_path / "collection.json", controlled_collection())
    results = write_json(tmp_path / "results.json", controlled_results())
    assert check_e2e_layers.main([
        "--collection", str(collection),
        "--results", str(results),
        "--sources-dir", str(tmp_path / "empty-src"),
        "--require-docker",
        "--docker-config", str(docker_config),
        "--docker-xml", str(docker_xml),
    ]) == 0


def test_docker_config_claiming_controlled_fails(tmp_path: Path) -> None:
    docker_config = write_json(
        tmp_path / "run-config.json",
        {"environment_type": "controlled-provider", "provider_mode": "real-provider"},
    )
    docker_xml = tmp_path / "docker.xml"
    docker_xml.write_text("<testsuite tests=\"1\"/>", encoding="utf-8")
    collection = write_json(tmp_path / "collection.json", controlled_collection())
    results = write_json(tmp_path / "results.json", controlled_results())
    assert check_e2e_layers.main([
        "--collection", str(collection),
        "--results", str(results),
        "--sources-dir", str(tmp_path / "empty-src"),
        "--require-docker",
        "--docker-config", str(docker_config),
        "--docker-xml", str(docker_xml),
    ]) == 1
