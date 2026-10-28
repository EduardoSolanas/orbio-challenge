"""Durable per-session JSON storage (IC §3, §8).

One JSON document per session named by the canonical lowercase UUID. Loads are
schema-validated; corrupt or future-version files are never repaired. Writes are
atomic replacements so a failed save leaves the previous bytes intact.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncContextManager
from uuid import UUID

from pydantic import ValidationError

from gateway.errors import app_error
from gateway.schemas import (
    LEGACY_SESSION_SCHEMA_VERSION,
    SESSION_SCHEMA_VERSION,
    Session,
)

SESSION_FILE_SUFFIX = ".json"
LOCK_ACQUISITION_TIMEOUT_SECONDS = 2.0


def migrate_session_document(document: dict) -> dict:
    """Return a version-2 copy of a stored session document.

    Version-1 documents gain the additive version-2 fields; version-2 documents
    written before the emotion extension gain only the newly introduced fields.
    Existing IDs, revisions, slots, transcripts, citations, confirmation data and
    replay records are preserved verbatim, and historical messages are never
    classified: legacy labels default to ``null`` and ``frustration_active`` to
    ``false``. The input document is never mutated.
    """

    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("unsupported session schema_version")
    if version == SESSION_SCHEMA_VERSION:
        return _fill_emotion_metadata(copy.deepcopy(document))
    if version == LEGACY_SESSION_SCHEMA_VERSION:
        return _upgrade_legacy_document(copy.deepcopy(document))
    raise ValueError("unsupported session schema_version")


def _upgrade_legacy_document(document: dict) -> dict:
    document["schema_version"] = SESSION_SCHEMA_VERSION
    document.setdefault("reply_language", "en")
    document.setdefault("frustration_active", False)
    messages = document.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                message.setdefault("language", "en")
                message.setdefault("sentiment", None)
                message.setdefault("frustration", None)
    requests = document.get("requests")
    if isinstance(requests, list):
        for record in requests:
            response = record.get("response") if isinstance(record, dict) else None
            if isinstance(response, dict):
                response.setdefault("reply_language", "en")
                response.setdefault("frustration_active", False)
    return document


def _fill_emotion_metadata(document: dict) -> dict:
    document.setdefault("frustration_active", False)
    messages = document.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                message.setdefault("sentiment", None)
                message.setdefault("frustration", None)
    requests = document.get("requests")
    if isinstance(requests, list):
        for record in requests:
            response = record.get("response") if isinstance(record, dict) else None
            if isinstance(response, dict):
                response.setdefault("frustration_active", False)
    return document


class JsonSessionRepository:
    """Filesystem repository for session documents in ``directory``."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._locks: weakref.WeakValueDictionary[UUID, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def _path(self, session_id: UUID) -> Path:
        return self.directory / f"{session_id}{SESSION_FILE_SUFFIX}"

    def locked(self, session_id: UUID) -> AsyncContextManager[None]:
        return self._lock_context(session_id)

    @asynccontextmanager
    async def _lock_context(self, session_id: UUID):
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        try:
            await asyncio.wait_for(
                lock.acquire(), timeout=LOCK_ACQUISITION_TIMEOUT_SECONDS
            )
        except TimeoutError:
            raise app_error("session_busy") from None
        try:
            yield None
        finally:
            lock.release()

    async def load(self, session_id: UUID) -> Session:
        path = self._path(session_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise app_error("session_not_found") from None
        except OSError:
            raise app_error("storage_unavailable") from None
        try:
            document = json.loads(raw)
            if not isinstance(document, dict):
                raise ValueError("session document must be a JSON object")
            return Session.model_validate(migrate_session_document(document))
        except (ValidationError, ValueError, TypeError, AttributeError, KeyError):
            raise app_error("storage_corrupt") from None

    async def save(self, session: Session) -> None:
        payload = json.dumps(
            session.model_dump(mode="json"), ensure_ascii=False, indent=2
        ).encode("utf-8")
        handle = None
        staged_name: str | None = None
        try:
            handle = tempfile.NamedTemporaryFile(dir=self.directory, delete=False)
            staged_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.replace(staged_name, self._path(session.session_id))
            staged_name = None
        except OSError:
            raise app_error("storage_unavailable") from None
        finally:
            if handle is not None:
                handle.close()
            if staged_name is not None:
                try:
                    os.unlink(staged_name)
                except OSError:
                    pass

    async def check_writable(self) -> None:
        try:
            with tempfile.TemporaryFile(dir=self.directory) as handle:
                # A zero-byte write succeeds even when storage rejects all data writes.
                handle.write(b"\0")
                handle.flush()
        except OSError:
            raise app_error("storage_unavailable") from None
