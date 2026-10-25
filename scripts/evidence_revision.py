#!/usr/bin/env python3
"""Commit-safe evidence revision policy shared by the evidence gates.

Evidence files are stamped with the revision of the last commit that touched a
*gated input* -- the code, tests, tooling and contracts the evidence describes.
Reports are outputs: committing regenerated evidence must never move the
revision the evidence is stamped with, otherwise no evidence could ever pass in
the committed checkout. Evidence that predates a later gated-input commit is
stale and fails the gate.

Outside a git checkout every helper returns ``None`` and the gates keep their
non-git behaviour.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

GATED_INPUTS = (
    "gateway",
    "backend",
    "tests",
    "scripts",
    "pyproject.toml",
    "docs/implementation-contract.md",
    "docs/testing-contract.md",
)
# The E2E manifest is evidence about the journeys, not a gated input: it lives
# under tests/ but its own revision stamp must not move the expected revision.
EVIDENCE_PATHSPEC_EXCLUDES = ("tests/e2e/cases.json",)


def current_source_revision(root: Path = Path(".")) -> str | None:
    """Resolve git HEAD without subprocess. Returns None outside a git checkout."""
    git_dir = root / ".git"
    if git_dir.is_file():
        try:
            pointer = git_dir.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not pointer.startswith("gitdir:"):
            return None
        git_dir = (root / pointer.split(":", 1)[1].strip()).resolve()
    if not git_dir.is_dir():
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if head.startswith("ref: "):
        ref = head[len("ref: ") :].strip()
        try:
            return (git_dir / ref).read_text(encoding="utf-8").strip()
        except OSError:
            packed = git_dir / "packed-refs"
            try:
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.startswith("#"):
                        continue
                    sha, _, name = line.partition(" ")
                    if name.strip() == ref:
                        return sha.strip()
            except OSError:
                return None
            return None
    return head or None


def gated_input_revision(root: Path = Path(".")) -> str | None:
    """Last commit touching a gated input; None outside a git checkout."""
    if current_source_revision(root) is None:
        return None
    pathspecs = [
        *GATED_INPUTS,
        *(f":(exclude){path}" for path in EVIDENCE_PATHSPEC_EXCLUDES),
    ]
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%H", "--", *pathspecs],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None
