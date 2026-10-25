#!/usr/bin/env python3
"""Container image smoke harness (testing-contract §8 image-build).

Run inside the built image with ``tests/`` mounted read-only at ``/checks``:

    docker run --rm --entrypoint python -v "$PWD/tests:/checks:ro" \\
        northstar-test /checks/support/image_smoke.py

The harness uses only the standard library plus the installed runtime packages.
It asserts the process is not root, imports the test fake from ``/checks`` (the
server-side AI boundary, never a production fake-mode switch), starts the real
application with an injected fake provider and a temporary session directory,
waits for ``/health`` and completes a real session/turn roundtrip. Any failure
exits nonzero with a fixed, sanitized message: no credential, path or body is
printed.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

CHECKS_DIR = Path(os.environ.get("IMAGE_SMOKE_CHECKS_DIR", "/checks"))
HOST = "127.0.0.1"
STARTUP_SECONDS = 60.0
SHUTDOWN_JOIN_SECONDS = 45.0
MESSAGE = "My order NS-000123 arrived broken."


class SmokeFailure(RuntimeError):
    """A fixed, sanitized smoke failure."""


def require_non_root(uid: int | None = None) -> None:
    effective_uid = os.getuid() if uid is None else uid
    if effective_uid == 0:
        raise SmokeFailure("image_smoke: container must not run as root (uid 0)")


def health_ok(status_code: int, payload: object) -> bool:
    return (
        status_code == 200
        and isinstance(payload, dict)
        and payload.get("status") == "ok"
    )


def session_ok(status_code: int, payload: object) -> bool:
    return (
        status_code == 201
        and isinstance(payload, dict)
        and isinstance(payload.get("session_id"), str)
        and bool(payload["session_id"])
    )


def turn_ok(status_code: int, payload: object) -> bool:
    if status_code != 200 or not isinstance(payload, dict):
        return False
    revision = payload.get("revision")
    reply = payload.get("reply")
    return (
        isinstance(revision, int)
        and revision >= 1
        and isinstance(reply, str)
        and bool(reply.strip())
    )


def _settings(session_dir: Path) -> Any:
    from gateway.config import Settings

    # Explicit test configuration, resolved by the real Settings validator.
    return Settings.from_env(
        {
            "AI_PROVIDER": "openai_compatible",
            "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "LLM_MODEL": "smoke-llm",
            "LLM_AUTH_MODE": "none",
            "LLM_OUTPUT_MODE": "json_schema",
            "STT_BASE_URL": "http://127.0.0.1:9/v1",
            "STT_MODEL": "smoke-stt",
            "STT_AUTH_MODE": "none",
            "TTS_BASE_URL": "http://127.0.0.1:9/v1",
            "TTS_MODEL": "smoke-tts",
            "TTS_AUTH_MODE": "none",
            "TTS_VOICE": "smoke-voice",
            "SESSION_DIR": str(session_dir),
            "PROVIDER_MAX_CONCURRENCY": "2",
            "BACKEND_API_BASE_URL": "http://backend-api.test",
        }
    )


def _wait_health(client: httpx.Client, thread: threading.Thread) -> dict[str, object]:
    deadline = time.monotonic() + STARTUP_SECONDS
    while True:
        if not thread.is_alive():
            raise SmokeFailure("image_smoke: server exited before /health")
        payload: object = None
        try:
            response = client.get("/health")
        except httpx.HTTPError:
            response = None
        if response is not None:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if health_ok(response.status_code, payload):
                return payload  # type: ignore[return-value]
        if time.monotonic() > deadline:
            raise SmokeFailure("image_smoke: /health did not answer 200 within 60s")
        time.sleep(0.05)


def main() -> int:
    try:
        require_non_root()
    except SmokeFailure as exc:
        print(exc, file=sys.stderr)
        return 1

    # Running a script does not put the working directory on sys.path, so the
    # application package (WORKDIR /app in the image) and the mounted checks
    # tree are added explicitly.
    sys.path.insert(0, os.getcwd())
    sys.path.insert(0, str(CHECKS_DIR))
    try:
        import uvicorn

        from gateway.main import create_app
        from gateway.schemas import Extraction, Slots
        from support.fakes import E2EProvider
    except Exception as exc:  # sanitized: class and missing module only
        missing = getattr(exc, "name", None)
        detail = f": {missing}" if missing else ""
        print(
            f"image_smoke: application import failed "
            f"({type(exc).__name__}{detail})",
            file=sys.stderr,
        )
        return 1

    provider = E2EProvider()
    server = None
    thread = None
    sock = None
    try:
        with tempfile.TemporaryDirectory(prefix="image-smoke-") as temporary:
            app = create_app(
                settings=_settings(Path(temporary)), provider=provider
            )
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((HOST, 0))
            sock.listen(128)
            port = sock.getsockname()[1]
            config = uvicorn.Config(
                app,
                log_level="warning",
                lifespan="on",
                access_log=False,
                timeout_graceful_shutdown=40,
            )
            server = uvicorn.Server(config)
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True
            )
            thread.start()

            with httpx.Client(
                base_url=f"http://{HOST}:{port}", timeout=10.0, trust_env=False
            ) as client:
                health_payload = _wait_health(client, thread)

                created = client.post("/api/sessions", json={})
                created_payload = created.json()
                if not session_ok(created.status_code, created_payload):
                    raise SmokeFailure(
                        f"image_smoke: session creation failed ({created.status_code})"
                    )
                session_id = created_payload["session_id"]

                provider.script_extract(
                    MESSAGE,
                    Extraction(
                        intent="provide_details",
                        has_question=False,
                        candidates=Slots(
                            order_number="NS-000123",
                            category="damaged_item",
                            description="arrived broken",
                            urgency=None,
                        ),
                        explicit_corrections=[],
                        ambiguous_fields=[],
                        sentiment="neutral",
                        frustration="none",
                        detected_language="en",
                        requested_language=None,
                    ),
                )
                turn = client.post(
                    f"/api/sessions/{session_id}/turns",
                    json={
                        "request_id": str(uuid4()),
                        "expected_revision": 0,
                        "text": MESSAGE,
                        "input_mode": "text",
                    },
                )
                turn_payload = turn.json()
                if not turn_ok(turn.status_code, turn_payload):
                    raise SmokeFailure(
                        f"image_smoke: session/turn roundtrip failed ({turn.status_code})"
                    )

            print(f"image_smoke: uid={os.getuid()} (non-root)")
            print(f"image_smoke: /health {health_payload}")
            print(
                "image_smoke: session/turn roundtrip ok "
                f"revision={turn_payload['revision']}"
            )
            return 0
    except SmokeFailure as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:  # sanitized: class name only
        print(f"image_smoke: failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        provider.release_all()
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(SHUTDOWN_JOIN_SECONDS)
        if sock is not None:
            sock.close()


if __name__ == "__main__":
    sys.exit(main())
