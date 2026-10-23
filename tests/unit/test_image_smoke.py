"""Tests for the pure helpers of tests/support/image_smoke.py (TC §8, ORB-16).

The container harness itself is exercised by the real ``docker run`` smoke; here
the failure predicates that make it exit nonzero are pinned: root uid, a failing
/health probe, a failed session creation and a failed session/turn roundtrip.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))

import image_smoke  # noqa: E402


def test_root_uid_is_rejected() -> None:
    with pytest.raises(image_smoke.SmokeFailure):
        image_smoke.require_non_root(0)


def test_non_root_uid_is_accepted() -> None:
    assert image_smoke.require_non_root(10001) is None


def test_health_predicate_requires_status_ok_payload() -> None:
    assert image_smoke.health_ok(200, {"status": "ok"}) is True
    assert image_smoke.health_ok(500, {"status": "ok"}) is False
    assert image_smoke.health_ok(200, {"status": "degraded"}) is False
    assert image_smoke.health_ok(200, {"detail": "ok"}) is False
    assert image_smoke.health_ok(200, "ok") is False


def test_session_predicate_requires_201_and_session_id() -> None:
    assert image_smoke.session_ok(201, {"session_id": "abc", "revision": 0}) is True
    assert image_smoke.session_ok(200, {"session_id": "abc", "revision": 0}) is False
    assert image_smoke.session_ok(201, {"revision": 0}) is False
    assert image_smoke.session_ok(201, {"session_id": 5}) is False


def test_turn_predicate_requires_revision_and_nonblank_reply() -> None:
    good = {"session_id": "abc", "revision": 1, "reply": "What is your order number?"}
    assert image_smoke.turn_ok(200, good) is True
    assert image_smoke.turn_ok(500, good) is False
    assert image_smoke.turn_ok(200, {**good, "revision": 0}) is False
    assert image_smoke.turn_ok(200, {**good, "reply": "   "}) is False
    assert image_smoke.turn_ok(200, {**good, "reply": None}) is False
