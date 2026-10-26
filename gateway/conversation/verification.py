"""Gateway-side per-session verification state.

The Backend API owns the fictional catalog, the demo code and the credential rule
(`app.backend_api`). This module keeps only what the gateway must remember per
session: the pending order number, the failure count and a 15-minute grant bound
to one session and one order. No postcode, code or grant is ever written to
session JSON or logs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

VERIFICATION_TTL_SECONDS = 15 * 60
MAX_VERIFICATION_ATTEMPTS = 5
VERIFICATION_FAILED = "verification_failed"
TOO_MANY_VERIFICATION_ATTEMPTS = "too_many_verification_attempts"


@dataclass
class _SessionState:
    pending_order: str | None = None
    verified_order: str | None = None
    expires_at: float | None = None
    failures: int = 0
    waiting_intent: str | None = None


class VerificationRegistry:
    """Per-session pending order, failure count and 15-minute verified grant."""

    def __init__(
        self,
        *,
        ttl_seconds: float = VERIFICATION_TTL_SECONDS,
        max_attempts: int = MAX_VERIFICATION_ATTEMPTS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_attempts = max_attempts
        self._clock = clock if clock is not None else time.monotonic
        self._sessions: dict[UUID, _SessionState] = {}

    def _state(self, session_id: UUID) -> _SessionState:
        state = self._sessions.get(session_id)
        if state is None:
            state = _SessionState()
            self._sessions[session_id] = state
        return state

    def pending_order(self, session_id: UUID) -> str | None:
        state = self._sessions.get(session_id)
        return None if state is None else state.pending_order

    def waiting_intent(self, session_id: UUID) -> str | None:
        """The account request (booking, installation status) still waiting for the account."""

        state = self._sessions.get(session_id)
        return None if state is None else state.waiting_intent

    def set_waiting_intent(self, session_id: UUID, intent: str | None) -> None:
        if intent is None and session_id not in self._sessions:
            return
        self._state(session_id).waiting_intent = intent

    def failures(self, session_id: UUID) -> int:
        state = self._sessions.get(session_id)
        return 0 if state is None else state.failures

    def locked_out(self, session_id: UUID) -> bool:
        """True once this session has used all of its credential attempts."""

        return self.failures(session_id) >= self.max_attempts

    def grant(self, session_id: UUID) -> str | None:
        """The verified order for an unexpired grant, clearing an expired one."""

        state = self._sessions.get(session_id)
        if state is None or state.verified_order is None or state.expires_at is None:
            return None
        if self._clock() >= state.expires_at:
            # Only the grant expires; the pending lookup order stays so the
            # customer can reverify the same order without re-entering it.
            state.verified_order = None
            state.expires_at = None
            return None
        return state.verified_order

    def set_pending_order(self, session_id: UUID, order_number: str) -> None:
        """Bind the pending order; a different order drops the grant only.

        The failure count deliberately survives an order change: otherwise a
        locked-out session could reset its five attempts by naming another
        order, and the lockout would be bypassable.
        """

        state = self._state(session_id)
        if state.pending_order == order_number:
            return
        state.pending_order = order_number
        state.verified_order = None
        state.expires_at = None

    def record_failure(self, session_id: UUID) -> None:
        """Count one failed credential attempt and drop any existing grant."""

        state = self._state(session_id)
        state.failures += 1
        state.verified_order = None
        state.expires_at = None

    def record_success(self, session_id: UUID, order_number: str) -> None:
        """Grant the order for 15 minutes; repeating success never extends it."""

        state = self._state(session_id)
        now = self._clock()
        if (
            state.verified_order == order_number
            and state.expires_at is not None
            and now < state.expires_at
        ):
            return
        state.verified_order = order_number
        state.pending_order = order_number
        state.expires_at = now + self.ttl_seconds
        state.failures = 0

    def clear_grant(self, session_id: UUID) -> None:
        """Drop the pending order and any grant, keeping the failure count."""

        state = self._sessions.get(session_id)
        if state is None:
            return
        state.pending_order = None
        state.verified_order = None
        state.expires_at = None

    def clear(self, session_id: UUID) -> None:
        self._sessions.pop(session_id, None)
