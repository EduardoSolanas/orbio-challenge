"""Pure ASGI middleware: request correlation IDs and pre-parse body size guards."""

from __future__ import annotations

import json
import logging
import time
from uuid import UUID, uuid4

from gateway.errors import INPUT_TOO_LARGE, AppError, app_error
from gateway.routes.http_support import (
    MAX_JSON_BODY_BYTES,
    MAX_MULTIPART_BYTES,
    error_payload,
)

logger = logging.getLogger(__name__)

TRANSCRIPTIONS_PATH = "/api/transcriptions"


async def _send_error(send, error: AppError, request_id: str) -> None:
    body = json.dumps(error_payload(error, request_id)).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": error.status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _header_value(scope, name: bytes) -> str | None:
    for header_name, header_value in scope["headers"]:
        if header_name.lower() == name:
            return header_value.decode("latin-1")
    return None


def _valid_request_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError):
        return None


class CorrelationIdMiddleware:
    """Resolve/echo X-Request-ID and log only sanitized request facts (IC §7)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _valid_request_id(_header_value(scope, b"x-request-id")) or str(
            uuid4()
        )
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status = 0

        async def send_with_header(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            logger.info(
                "request_id=%s method=%s path=%s status=%s duration_ms=%d",
                request_id,
                scope["method"],
                scope["path"],
                status,
                int((time.monotonic() - started) * 1000),
            )


class JsonBodyGuardMiddleware:
    """Pure ASGI preparse guard: count actual receive bytes on every POST.

    JSON routes keep the 64 KiB cap; the transcription upload keeps its separate
    10 MiB file / 10 MiB + 64 KiB multipart caps (IC §7, §12). Content-Length is
    never trusted; a single ASGI message larger than the cap is rejected before
    it is retained.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not scope["path"].startswith("/api/")
        ):
            await self.app(scope, receive, send)
            return
        limit = (
            MAX_MULTIPART_BYTES
            if scope["path"] == TRANSCRIPTIONS_PATH
            else MAX_JSON_BODY_BYTES
        )

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > limit:
                request_id = scope.get("state", {}).get("request_id") or str(uuid4())
                await _send_error(send, app_error(INPUT_TOO_LARGE), request_id)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        joined = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": joined, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
