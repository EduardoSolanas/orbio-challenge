"""The patterns that set unobservable mutations aside must keep matching, and stay narrow."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PATTERNS = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["tool"][
    "mutmut"
]["do_not_mutate_patterns"]
SOURCES = [
    path
    for folder in ("gateway", "backend_api", "shared")
    for path in sorted((ROOT / folder).rglob("*.py"))
]
MOST_LINES_ONE_PATTERN_MAY_SKIP = 10


def matching_lines(pattern: str) -> list[str]:
    compiled = re.compile(pattern)
    return [
        f"{path.relative_to(ROOT)}:{number}"
        for path in SOURCES
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1)
        if compiled.search(line)
    ]


@pytest.mark.parametrize("pattern", PATTERNS)
def test_each_pattern_still_matches_something_and_stays_narrow(pattern: str) -> None:
    lines = matching_lines(pattern)

    assert lines, f"{pattern} matches nothing any more; remove it"
    assert len(lines) <= MOST_LINES_ONE_PATTERN_MAY_SKIP, (
        f"{pattern} is too broad: {lines}"
    )
