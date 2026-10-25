"""Real loopback Backend API service for gateway-boundary integration tests.

The shipped ``create_backend_api_app`` is served by Uvicorn on an ephemeral
loopback port, so every gateway verification/lookup crosses a genuine TCP/HTTP
boundary through ``BackendApiClient`` (no transport double, no in-process fake).
The server records each request, and can be stopped and restarted on the same
port to prove outage and recovery behaviour.
"""

from __future__ import annotations

import socket
import threading
from typing import Any

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware

from backend_api.main import OrderRecord, create_backend_api_app
from tests.support.waiting import wait_until

STARTUP_SECONDS = 10.0
SHUTDOWN_JOIN_SECONDS = 10.0


class _RecordMiddleware(BaseHTTPMiddleware):
    """Record method, path and raw body without changing the response."""

    def __init__(self, app, owner: "LocalBackendApiServer") -> None:
        super().__init__(app)
        self.owner = owner

    async def dispatch(self, request, call_next):
        body = await request.body()
        self.owner.requests.append(
            {"method": request.method, "path": request.url.path, "body": body}
        )
        return await call_next(request)


class LocalBackendApiServer:
    """The real Backend API on a loopback socket, with stop/restart on one port."""

    def __init__(self, orders: dict[str, OrderRecord] | None = None) -> None:
        self._orders = orders
        self.requests: list[dict[str, Any]] = []
        self.port: int | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._thread_error: BaseException | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port or 0))
        sock.listen(128)
        self.port = sock.getsockname()[1]
        self._socket = sock
        app = create_backend_api_app(self._orders)
        app.add_middleware(_RecordMiddleware, owner=self)
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", lifespan="on", access_log=False)
        )
        self._server = server
        self._thread_error = None

        def run() -> None:
            try:
                server.run(sockets=[sock])
            except BaseException as exc:  # surfaced by the health wait
                self._thread_error = exc

        thread = threading.Thread(target=run, name=f"backend-api-{self.port}", daemon=True)
        self._thread = thread
        thread.start()
        self._wait_healthy()

    def _wait_healthy(self) -> None:
        import urllib.request

        def healthy() -> bool:
            if self._thread is None or not self._thread.is_alive():
                raise AssertionError(f"backend API thread exited: {self._thread_error!r}")
            try:
                with urllib.request.urlopen(f"{self.base_url}/health", timeout=1) as response:
                    return response.status == 200
            except OSError:
                return False

        wait_until(healthy, f"backend API /health within {STARTUP_SECONDS}s", STARTUP_SECONDS)

    def stop(self) -> None:
        server, thread = self._server, self._thread
        if server is None or thread is None:
            return
        server.should_exit = True
        thread.join(SHUTDOWN_JOIN_SECONDS)
        if thread.is_alive():
            raise AssertionError(
                f"backend API thread alive {SHUTDOWN_JOIN_SECONDS}s after should_exit"
            )
        if self._socket is not None:
            self._socket.close()
        self._server = self._thread = self._socket = None

    def restart(self) -> None:
        self.start()

    def verification_posts(self) -> list[dict[str, Any]]:
        import json

        return [
            json.loads(request["body"])
            for request in self.requests
            if request["method"] == "POST"
            and request["path"] == "/internal/v1/order-verifications"
        ]

    def order_gets(self) -> list[str]:
        return [
            request["path"].rsplit("/", 1)[-1]
            for request in self.requests
            if request["method"] == "GET" and "/internal/v1/orders/" in request["path"]
        ]

    def __enter__(self) -> "LocalBackendApiServer":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
