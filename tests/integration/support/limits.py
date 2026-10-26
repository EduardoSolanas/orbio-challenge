"""Near-cap session seeding shared by the limits and intent tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from gateway.providers.prompts import GREETING
from gateway.schemas import Confirmation, Message, Session, Slots
from gateway.storage import JsonSessionRepository
from tests.integration.support.intake import FULL_SLOTS

TURN_LIMIT_MESSAGE = (
    "This conversation has reached its message limit. You can confirm the current "
    "details or start a new conversation."
)

AT_CAP = 50

PARTIAL_SLOTS = {**FULL_SLOTS, "urgency": None}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _seeded_messages(turns: int, now) -> list[Message]:
    """Greeting plus one committed user/assistant pair per seeded turn."""

    messages = [
        Message(
            id=uuid4(),
            role="assistant",
            text=GREETING,
            input_mode="system",
            created_at=now,
            citations=[],
            sentiment=None,
            frustration=None,
        )
    ]
    for index in range(turns):
        messages.append(
            Message(
                id=uuid4(),
                role="user",
                text=f"Seeded customer turn {index + 1}.",
                input_mode="text",
                created_at=now,
                citations=[],
                sentiment="neutral",
                frustration="none",
            )
        )
        messages.append(
            Message(
                id=uuid4(),
                role="assistant",
                text=f"Seeded assistant reply {index + 1}.",
                input_mode="system",
                created_at=now,
                citations=[],
                sentiment=None,
                frustration=None,
            )
        )
    return messages


async def seed_session(
    session_dir: Path,
    *,
    revision: int,
    status: str,
    slots: dict,
    summary: str | None,
    review_revision: int | None,
    collection_turn_count: int,
    review_rejected: bool = False,
    confirmation: Confirmation | None = None,
) -> str:
    """Build a schema-validated Session and persist it with the real repository.

    This is the near-cap state factory: the document is written by
    `JsonSessionRepository.save()` (atomic temp-file replace) into the isolated
    temp directory the app lifespan then serves, so the routes load it through
    the same validated path as any real session. It never fabricates a request
    body or bypasses schema validation.
    """

    now = _now()
    session = Session(
        schema_version=2,
        session_id=uuid4(),
        revision=revision,
        created_at=now,
        updated_at=now,
        status=status,
        frustration_active=False,
        slots=Slots(**slots),
        messages=_seeded_messages(revision, now),
        pending_clarifications={},
        pending_review_revision=review_revision,
        review_rejected=review_rejected,
        collection_turn_count=collection_turn_count,
        summary=summary,
        confirmation=confirmation,
        requests=[],
    )
    await JsonSessionRepository(session_dir).save(session)
    return str(session.session_id)


def raw_bytes(session_dir: Path, session_id: str) -> bytes:
    return (Path(session_dir) / f"{session_id}.json").read_bytes()
