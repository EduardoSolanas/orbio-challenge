"""A real temporary git repository, for the policies that are about commit ancestry."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def init_repo(root: Path) -> None:
    git(root, "init", "--quiet")
    git(root, "config", "user.email", "evidence@example.test")
    git(root, "config", "user.name", "Evidence Test")
    git(root, "config", "commit.gpgsign", "false")


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "--message", message)
    return git(root, "rev-parse", "HEAD")
