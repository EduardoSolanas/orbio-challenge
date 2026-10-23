"""Durable per-session JSON storage (IC §3, §8).

One JSON document per session named by the canonical lowercase UUID. Loads are
schema-validated; corrupt or future-version files are never repaired. Writes are
atomic replacements so a failed save leaves the previous bytes intact.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncContextManager
from uuid import UUID

from pydantic import ValidationError

from app.errors import app_error
from app.schemas import Session

SESSION_FILE_SUFFIX = ".json"
LOCK_ACQUISITION_TIMEOUT_SECONDS = 2.0


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
            return Session.model_validate_json(raw)
        except (ValidationError, ValueError):
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
        handle = None
        try:
            handle = tempfile.NamedTemporaryFile(dir=self.directory, delete=False)
            handle.write(b"")
            handle.flush()
            name = handle.name
            handle.close()
            handle = None
            os.unlink(name)
        except OSError:
            raise app_error("storage_unavailable") from None
        finally:
            if handle is not None:
                handle.close()
