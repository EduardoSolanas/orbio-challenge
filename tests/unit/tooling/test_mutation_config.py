"""Runtime statements must not disappear from mutation testing through regex rules."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_runtime_statements_are_not_excluded_by_regex() -> None:
    root = Path(__file__).resolve().parents[3]
    config = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))

    assert config["tool"]["mutmut"].get("do_not_mutate_patterns", []) == []
