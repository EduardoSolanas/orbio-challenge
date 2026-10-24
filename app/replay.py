"""Request idempotency: canonical input digests and replay lookup (IC §7)."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from app.schemas import ConfirmRequest, ReplayRecord, Session, TurnRequest

TURN_ENDPOINT = "turn"
CONFIRM_ENDPOINT = "confirm"


def _digest(endpoint: str, session_id: UUID, body: dict) -> str:
    canonical = json.dumps(
        {"endpoint": endpoint, "session_id": str(session_id), **body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def turn_digest(session_id: UUID, command: TurnRequest) -> str:
    return _digest(
        TURN_ENDPOINT,
        session_id,
        {
            "request_id": str(command.request_id),
            "expected_revision": command.expected_revision,
            "text": command.text,
            "input_mode": command.input_mode,
        },
    )


def confirm_digest(session_id: UUID, command: ConfirmRequest) -> str:
    return _digest(
        CONFIRM_ENDPOINT,
        session_id,
        {
            "request_id": str(command.request_id),
            "expected_revision": command.expected_revision,
            "review_revision": command.review_revision,
        },
    )


def find_replay(session: Session, request_id: UUID) -> ReplayRecord | None:
    for record in session.requests:
        if record.request_id == request_id:
            return record
    return None
