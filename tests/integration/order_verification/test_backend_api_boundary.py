"""Integration tests for order verification over the real Backend API.

The gateway app, lifespan, routes, engine and JSON repository run against an
isolated temp store; only the external AI provider is scripted. The shipped
Backend API is served by real Uvicorn on a loopback port, so every verification
and lookup crosses a genuine TCP/HTTP boundary through `BackendApiClient`."""

from __future__ import annotations

import asyncio
import socket
import threading
from uuid import UUID

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from backend_api.main import (
    DEMO_VERIFICATION_CODE,
    FIXTURE_ORDER_NUMBER,
    FIXTURE_POSTCODE,
)
from gateway.backend_client import BackendApiClient
from gateway.errors import BACKEND_API_TIMEOUT, INVALID_BACKEND_API_RESPONSE
from gateway.localization import text
from gateway.main import create_app
from tests.integration.order_verification.support import (
    ORDER_NUMBER_TEXT,
    ORDER_RESULT,
    ORDER_STATUS_Q,
    create_session,
    order_lookup,
    send_turn,
    verify,
)
from tests.integration.support.api import running_client
from tests.support.waiting import wait_until


async def test_backend_api_outage_during_verification_is_safe_and_recovers(
    client, provider, verification, backend_api, session_dir
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    assert (await send_turn(client, session_id, ORDER_STATUS_Q, 0)).status_code == 200
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    assert (
        await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
    ).status_code == 200
    before = (session_dir / f"{session_id}.json").read_bytes()

    backend_api.stop()
    down = await verify(client, session_id)

    assert down.status_code == 503
    assert down.json()["error"]["code"] == "backend_api_unavailable"
    assert down.json()["error"]["message"] == text(
        "errors.backend_api_unavailable", "en"
    )
    assert down.json()["error"]["retryable"] is True
    assert (session_dir / f"{session_id}.json").read_bytes() == before
    assert verification.grant(UUID(session_id)) is None
    assert verification.failures(UUID(session_id)) == 0
    assert FIXTURE_POSTCODE not in down.text
    assert DEMO_VERIFICATION_CODE not in down.text

    backend_api.restart()
    recovered = await verify(client, session_id)

    assert recovered.status_code == 200
    assert recovered.json() == {"verified": True, "error": None}
    assert verification.grant(UUID(session_id)) == FIXTURE_ORDER_NUMBER
    assert backend_api.verification_posts()[-1] == {
        "order_number": FIXTURE_ORDER_NUMBER,
        "delivery_postcode": FIXTURE_POSTCODE,
        "verification_code": DEMO_VERIFICATION_CODE,
    }


async def test_backend_api_outage_during_lookup_preserves_the_grant_and_recovers(
    client, provider, verification, backend_api, session_dir
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    assert (await send_turn(client, session_id, ORDER_STATUS_Q, 0)).status_code == 200
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    assert (
        await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
    ).status_code == 200
    assert (await verify(client, session_id)).json()["verified"] is True
    before = (session_dir / f"{session_id}.json").read_bytes()

    backend_api.stop()
    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, tool=True)
    )
    down = await send_turn(client, session_id, ORDER_STATUS_Q, 2)

    assert down.status_code == 503
    assert down.json()["error"]["code"] == "backend_api_unavailable"
    assert down.json()["error"]["message"] == text(
        "errors.backend_api_unavailable", "en"
    )
    assert (session_dir / f"{session_id}.json").read_bytes() == before
    assert verification.grant(UUID(session_id)) == FIXTURE_ORDER_NUMBER
    assert "Casey" not in down.text

    backend_api.restart()
    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, tool=True)
    )
    recovered = await send_turn(client, session_id, ORDER_STATUS_Q, 2)

    assert recovered.status_code == 200
    assert recovered.json()["reply"] == ORDER_RESULT
    assert recovered.json()["order_verification_required"] is False
    assert backend_api.order_gets() == [FIXTURE_ORDER_NUMBER]


async def test_gateway_origin_does_not_expose_the_private_backend_api(client):
    for method, path in (
        ("POST", "/internal/v1/order-verifications"),
        ("GET", f"/internal/v1/orders/{FIXTURE_ORDER_NUMBER}"),
        ("GET", "/internal/v1/orders"),
    ):
        response = await client.request(method, path)

        assert response.status_code == 404
        assert "Casey" not in response.text
        assert "Electricity and gas supply" not in response.text


class _FaultyBackendApiServer:
    """Real loopback Backend API that stalls or returns unusable bodies (OA-I07)."""

    SLOW_SECONDS = 2.0
    STARTUP_SECONDS = 10.0
    SHUTDOWN_JOIN_SECONDS = 10.0

    def __init__(self) -> None:
        self.mode = "malformed"
        self.port: int | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        self.port = sock.getsockname()[1]
        self._socket = sock
        server = uvicorn.Server(
            uvicorn.Config(
                self._build_app(), log_level="warning", lifespan="on", access_log=False
            )
        )
        self._server = server
        thread = threading.Thread(
            target=lambda: server.run(sockets=[sock]),
            name=f"faulty-backend-api-{self.port}",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        wait_until(
            lambda: server.started,
            "the faulty Backend API to become ready",
            self.STARTUP_SECONDS,
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(self.SHUTDOWN_JOIN_SECONDS)
            assert not self._thread.is_alive(), (
                "faulty Backend API thread survived shutdown"
            )
        if self._socket is not None:
            self._socket.close()

    def _build_app(self) -> FastAPI:
        owner = self
        app = FastAPI()

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.post("/internal/v1/order-verifications")
        async def verify() -> JSONResponse:
            if owner.mode == "slow":
                await asyncio.sleep(owner.SLOW_SECONDS)
                return JSONResponse({"verified": True, "error": None})
            if owner.mode == "server_error":
                return JSONResponse({"error": "internal detail"}, status_code=500)
            return JSONResponse({"verified": "yes", "error": None})

        return app


@pytest.fixture
def faulty_backend_api():
    server = _FaultyBackendApiServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.mark.parametrize(
    "mode,status,code",
    [
        pytest.param("slow", 504, BACKEND_API_TIMEOUT, id="OA-I07-timeout"),
        pytest.param(
            "malformed", 502, INVALID_BACKEND_API_RESPONSE, id="OA-I07-malformed"
        ),
        pytest.param(
            "server_error", 502, INVALID_BACKEND_API_RESPONSE, id="OA-I07-5xx"
        ),
    ],
)
async def test_unusable_backend_api_replies_map_to_fixed_safe_errors(
    mode,
    status,
    code,
    faulty_backend_api,
    settings,
    provider,
    verification,
    session_dir,
):
    """OA-I07: slow/malformed/5xx Backend API replies never reach the browser raw."""

    faulty_backend_api.mode = mode
    app = create_app(
        settings=settings,
        provider=provider,
        verification_registry=verification,
        backend_api_client=BackendApiClient(
            faulty_backend_api.base_url, timeout_seconds=0.2
        ),
    )
    async with running_client(app) as client:
        session_id = await create_session(client)
        provider.script_extraction(order_lookup())
        assert (
            await send_turn(client, session_id, ORDER_STATUS_Q, 0)
        ).status_code == 200
        provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
        assert (
            await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
        ).status_code == 200
        before = (session_dir / f"{session_id}.json").read_bytes()

        response = await verify(client, session_id)

        assert response.status_code == status
        error = response.json()["error"]
        assert error["code"] == code
        assert error["message"] == text(f"errors.{code}", "en")
        assert error["retryable"] is True
        assert (session_dir / f"{session_id}.json").read_bytes() == before
        assert verification.grant(UUID(session_id)) is None
        assert verification.failures(UUID(session_id)) == 0
        for leaked in (
            faulty_backend_api.base_url,
            "127.0.0.1",
            str(faulty_backend_api.port),
            FIXTURE_POSTCODE,
            DEMO_VERIFICATION_CODE,
            "internal detail",
        ):
            assert leaked not in response.text
