"""E2E fixtures: real Uvicorn on a pre-bound port-0 socket, fresh temp store and fake per test.

TC §5: function-scoped server in a background thread, an already-bound loopback
socket passed to `Server.run(sockets=[...])` (no free-port race), `/health` polled
for at most 10 s, teardown sets `should_exit` and fails if the thread outlives a
10 s join. Dependency injection of the fake happens here only; nothing in the
application exposes a test mode.

Two narrow environmental seams live in the ASGI wrapper that fronts the real app
(IC §13.3 "test-only pre-handler failure middleware"): a request/response ledger
and one-shot pre-handler faults. Neither replaces a successful application route.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pytest
import uvicorn
from playwright.sync_api import Locator, Page, Response, expect

from app.config import Settings
from app.backend_api import create_backend_api_app
from app.errors import app_error
from app.main import create_app
from tests.e2e.hooks import (  # noqa: F401  (pytest registers these hook functions)
    pytest_collection_finish,
    pytest_collectreport,
    pytest_deselected,
    pytest_runtest_logreport,
    pytest_sessionfinish,
)
from tests.support.fakes import GREETING, E2EProvider

EXPECT_TIMEOUT_MS = 10_000
ACTION_TIMEOUT_MS = 15_000
STARTUP_SECONDS = 10.0
SHUTDOWN_JOIN_SECONDS = 10.0
HTTP_TIMEOUT_SECONDS = 10.0

expect.set_options(timeout=EXPECT_TIMEOUT_MS)

# Passive spy: records and then calls the original APIs. It never changes behaviour.
MEDIA_SPY_JS = """
(() => {
  const spy = window.__spy = {created: [], revoked: [], plays: [], pauses: [], contexts: []};
  const create = URL.createObjectURL.bind(URL);
  URL.createObjectURL = (obj) => {
    const url = create(obj);
    spy.created.push({url, type: obj.type, size: obj.size});
    return url;
  };
  const revoke = URL.revokeObjectURL.bind(URL);
  URL.revokeObjectURL = (url) => { spy.revoked.push(url); return revoke(url); };
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    spy.plays.push(this.currentSrc || this.src);
    return play.apply(this, arguments);
  };
  const pause = HTMLMediaElement.prototype.pause;
  HTMLMediaElement.prototype.pause = function () {
    spy.pauses.push(this.currentSrc || this.src);
    return pause.apply(this, arguments);
  };
  const AudioContextBase = window.AudioContext;
  if (AudioContextBase) {
    window.AudioContext = class extends AudioContextBase {
      constructor(...args) { super(...args); spy.contexts.push(this); }
    };
  }
})();
"""

# Media-policy failure seam (voice_success): the browser refuses microphone access.
DENY_MICROPHONE_JS = """
(() => {
  const denied = () => Promise.reject(new DOMException('Permission denied', 'NotAllowedError'));
  if (navigator.mediaDevices) { navigator.mediaDevices.getUserMedia = denied; }
})();
"""


# --------------------------------------------------------------------------- #
# Browser configuration (fake media device for the voice_success recording take)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict[str, Any]) -> dict[str, Any]:
    args = list(browser_type_launch_args.get("args") or [])
    args += [
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--mute-audio",
    ]
    return {**browser_type_launch_args, "args": args}


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    return {
        **browser_context_args,
        "locale": "en-US",
        "viewport": {"width": 1280, "height": 800},
        "service_workers": "block",
    }


# --------------------------------------------------------------------------- #
# Request ledger and one-shot pre-handler faults
# --------------------------------------------------------------------------- #


class RequestLog:
    """Every HTTP request the real server saw: body, status, response body."""

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._entries: list[dict[str, Any]] = []

    def begin(self, method: str, path: str, headers: dict[str, str]) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "seq": 0,
            "method": method,
            "path": path,
            "headers": headers,
            "request_body": b"",
            "status": None,
            "response_headers": {},
            "response_body": b"",
            "response_bytes": 0,
            "done": False,
            "faulted": False,
        }
        with self._changed:
            entry["seq"] = len(self._entries) + 1
            self._entries.append(entry)
            self._changed.notify_all()
        return entry

    def touch(self) -> None:
        with self._changed:
            self._changed.notify_all()

    def where(
        self, method: str | None = None, path: str | None = None, *, done: bool | None = None
    ) -> list[dict[str, Any]]:
        pattern = re.compile(path) if path else None
        with self._changed:
            return [
                dict(e)
                for e in self._entries
                if (method is None or e["method"] == method)
                and (pattern is None or pattern.search(e["path"]))
                and (done is None or e["done"] is done)
            ]

    def wait_until(
        self, predicate: Callable[[], bool], timeout: float = 10.0, what: str = ""
    ) -> None:
        deadline = time.monotonic() + timeout
        with self._changed:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"request ledger condition not reached: {what}")
                self._changed.wait(min(remaining, 0.25))


class Fault:
    def __init__(self, method: str, path: str, status: int, code: str) -> None:
        self.method = method
        self.path = re.compile(path)
        self.status = status
        self.code = code
        self.triggered = 0


class Faults:
    """One-shot synthetic failures returned before the application handler runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed: list[Fault] = []

    def fail_next(
        self, method: str, path: str, *, status: int = 503, code: str = "storage_unavailable"
    ) -> Fault:
        fault = Fault(method, path, status, code)
        with self._lock:
            self._armed.append(fault)
        return fault

    def take(self, method: str, path: str) -> Fault | None:
        with self._lock:
            for fault in self._armed:
                if fault.triggered == 0 and fault.method == method and fault.path.search(path):
                    fault.triggered += 1
                    return fault
        return None


class ObservedApp:
    """ASGI wrapper in front of the real app: ledger + one-shot pre-handler faults."""

    def __init__(self, app: Any, log: RequestLog, faults: Faults) -> None:
        self.app = app
        self.log = log
        self.faults = faults

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        entry = self.log.begin(scope["method"], scope["path"], headers)
        fault = self.faults.take(scope["method"], scope["path"])
        try:
            if fault is not None:
                await self._send_fault(fault, entry, send)
                return

            async def observed_receive() -> dict[str, Any]:
                message = await receive()
                if message["type"] == "http.request":
                    entry["request_body"] += message.get("body", b"")
                return message

            async def observed_send(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    entry["status"] = message["status"]
                    entry["response_headers"] = {
                        k.decode("latin-1").lower(): v.decode("latin-1")
                        for k, v in message.get("headers", [])
                    }
                    self.log.touch()
                elif message["type"] == "http.response.body":
                    body = message.get("body", b"")
                    entry["response_bytes"] += len(body)
                    if "json" in entry["response_headers"].get("content-type", ""):
                        entry["response_body"] += body
                await send(message)

            await self.app(scope, observed_receive, observed_send)
        finally:
            entry["done"] = True
            self.log.touch()

    async def _send_fault(self, fault: Fault, entry: dict[str, Any], send: Any) -> None:
        error = app_error(fault.code)
        payload = json.dumps(
            {
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                },
                "request_id": str(uuid4()),
            }
        ).encode()
        entry["faulted"] = True
        entry["status"] = fault.status
        entry["response_headers"] = {"content-type": "application/json"}
        entry["response_body"] = payload
        await send(
            {
                "type": "http.response.start",
                "status": fault.status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


# --------------------------------------------------------------------------- #
# Real Uvicorn server
# --------------------------------------------------------------------------- #


def build_settings(session_dir: Path, backend_api_base_url: str) -> Settings:
    """Complete isolated auth=none environment; no developer configuration is read."""

    return Settings.from_env(
        {
            "AI_PROVIDER": "openai_compatible",
            "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "LLM_MODEL": "e2e-llm",
            "LLM_AUTH_MODE": "none",
            "LLM_OUTPUT_MODE": "json_schema",
            "STT_BASE_URL": "http://127.0.0.1:9/v1",
            "STT_MODEL": "e2e-stt",
            "STT_AUTH_MODE": "none",
            "TTS_BASE_URL": "http://127.0.0.1:9/v1",
            "TTS_MODEL": "e2e-tts",
            "TTS_AUTH_MODE": "none",
            "TTS_VOICE": "e2e-voice",
            "SESSION_DIR": str(session_dir),
            "PROVIDER_MAX_CONCURRENCY": "4",
            "BACKEND_API_BASE_URL": backend_api_base_url,
        }
    )


class AppServer:
    """A real create_app() served by Uvicorn on a loopback socket bound to port 0."""

    def __init__(
        self, session_dir: Path, provider: E2EProvider, knowledge_path: Path | None = None
    ) -> None:
        self.session_dir = session_dir
        self.provider = provider
        self.knowledge_path = knowledge_path
        self.settings: Settings | None = None
        self._order_server: uvicorn.Server | None = None
        self._order_thread: threading.Thread | None = None
        self._order_socket: socket.socket | None = None
        self._order_port: int | None = None
        self.log = RequestLog()
        self.faults = Faults()
        self.port: int | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._thread_error: BaseException | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self.start_backend_api()
        self.settings = build_settings(
            self.session_dir, f"http://127.0.0.1:{self._order_port}"
        )
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port or 0))
        sock.listen(128)
        self.port = sock.getsockname()[1]
        self._socket = sock
        assert self.settings is not None
        app = create_app(
            settings=self.settings, provider=self.provider, knowledge_path=self.knowledge_path
        )
        config = uvicorn.Config(
            ObservedApp(app, self.log, self.faults),
            log_level="warning",
            lifespan="on",
            access_log=False,
            timeout_graceful_shutdown=3,
        )
        server = uvicorn.Server(config)
        self._server = server
        self._thread_error = None

        def run() -> None:
            try:
                server.run(sockets=[sock])
            except BaseException as exc:  # surfaced by the health wait
                self._thread_error = exc

        thread = threading.Thread(target=run, name=f"e2e-uvicorn-{self.port}", daemon=True)
        self._thread = thread
        thread.start()
        self._wait_healthy()

    def _wait_healthy(self) -> None:
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            if not self.running:
                raise AssertionError(f"server thread exited during startup: {self._thread_error!r}")
            try:
                status, _, _ = self.request("GET", "/health", timeout=1.0)
            except OSError:
                status = None
            if status == 200:
                return
            time.sleep(0.05)
        raise AssertionError(f"/health did not answer 200 within {STARTUP_SECONDS}s")

    def start_backend_api(self) -> None:
        """Start the real private Backend API on a loopback socket, reusing its port."""

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self._order_port or 0))
        sock.listen(128)
        self._order_port = sock.getsockname()[1]
        self._order_socket = sock
        order_server = uvicorn.Server(
            uvicorn.Config(
                create_backend_api_app(),
                log_level="warning",
                lifespan="on",
                access_log=False,
                timeout_graceful_shutdown=3,
            )
        )
        self._order_server = order_server
        self._order_thread = threading.Thread(
            target=lambda: order_server.run(sockets=[sock]),
            name=f"e2e-backend-api-{self._order_port}",
            daemon=True,
        )
        self._order_thread.start()
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self._order_port, timeout=1)
                try:
                    conn.request("GET", "/health")
                    if conn.getresponse().status == 200:
                        return
                finally:
                    conn.close()
            except OSError:
                time.sleep(0.05)
        raise AssertionError("Backend API /health did not answer 200 during E2E startup")

    def stop_backend_api(self) -> None:
        """Stop the real Backend API while keeping its port for a same-origin restart."""

        order_server, order_thread = self._order_server, self._order_thread
        if order_server is not None and order_thread is not None:
            order_server.should_exit = True
            order_thread.join(SHUTDOWN_JOIN_SECONDS)
            if order_thread.is_alive():
                raise AssertionError(
                    f"Backend API thread still alive {SHUTDOWN_JOIN_SECONDS}s after should_exit"
                )
        if self._order_socket is not None:
            self._order_socket.close()
        self._order_server = self._order_thread = self._order_socket = None

    def stop(self) -> None:
        server, thread = self._server, self._thread
        if server is None or thread is None:
            return
        self.provider.release_all()
        server.should_exit = True
        thread.join(SHUTDOWN_JOIN_SECONDS)
        if thread.is_alive():
            raise AssertionError(
                f"uvicorn thread still alive {SHUTDOWN_JOIN_SECONDS}s after should_exit"
            )
        if self._socket is not None:
            self._socket.close()
        self._server = self._thread = self._socket = None
        self.stop_backend_api()

    def restart(self) -> None:
        """Stop and recreate the app on the same origin against the same SESSION_DIR."""

        self.stop()
        self.start()

    # ---- observation helpers ------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            request_headers = dict(headers or {})
            if body is not None:
                request_headers.setdefault("Content-Type", "application/json")
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, data
        finally:
            connection.close()

    def session_path(self, session_id: str) -> Path:
        return self.session_dir / f"{session_id}.json"

    def session_ids(self) -> list[str]:
        return sorted(p.stem for p in self.session_dir.glob("*.json"))

    def session_bytes(self, session_id: str) -> bytes:
        return self.session_path(session_id).read_bytes()

    def read_session(self, session_id: str) -> dict[str, Any]:
        return json.loads(self.session_bytes(session_id).decode("utf-8"))


class ServerFactory:
    def __init__(self, base: Path, provider: E2EProvider) -> None:
        self._base = base
        self._provider = provider
        self.servers: list[AppServer] = []

    def start(self, knowledge_path: Path | None = None) -> AppServer:
        server = AppServer(self._base / "sessions", self._provider, knowledge_path)
        self.servers.append(server)
        server.start()
        return server

    def shutdown(self) -> None:
        problems: list[str] = []
        self._provider.release_all()
        for server in self.servers:
            try:
                server.stop()
            except AssertionError as exc:
                problems.append(str(exc))
        if self._provider.unexpected:
            problems.append(f"unexpected provider calls: {self._provider.unexpected}")
        if problems:
            raise AssertionError("; ".join(problems))


@pytest.fixture
def provider() -> E2EProvider:
    return E2EProvider()


@pytest.fixture
def server_factory(tmp_path: Path, provider: E2EProvider):
    factory = ServerFactory(tmp_path, provider)
    yield factory
    factory.shutdown()


@pytest.fixture
def server(server_factory: ServerFactory) -> AppServer:
    return server_factory.start()


# --------------------------------------------------------------------------- #
# Browser-side driver: role/name selectors from IC §9, ledgers from the server
# --------------------------------------------------------------------------- #

TURNS = r"/api/sessions/[^/]+/turns$"
CONFIRMS = r"/api/sessions/[^/]+/confirm$"
SPEECH = r"/api/sessions/[^/]+/speech$"
ORDER_VERIFICATIONS = r"/api/sessions/[^/]+/order-verification$"
SESSION_ITEM = r"/api/sessions/[^/]+$"
TRANSCRIPTIONS = r"/api/transcriptions$"


def wait_until(condition: Callable[[], bool], what: str, timeout: float = 10.0) -> None:
    """Bounded condition wait for non-DOM state (disk, ledgers)."""

    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def body_of(entry: dict[str, Any]) -> dict[str, Any]:
    return json.loads(entry["request_body"].decode("utf-8"))


def response_of(entry: dict[str, Any]) -> dict[str, Any]:
    return json.loads(entry["response_body"].decode("utf-8"))


class UI:
    """The shipped page as the customer sees it, plus the real server ledgers."""

    def __init__(self, server: AppServer, page: Page, node: Any) -> None:
        self.server = server
        self.provider = server.provider
        self.page = page
        self._node = node
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        page.context.add_init_script(MEDIA_SPY_JS)

    # ---- locators (IC §9) ---------------------------------------------------

    @property
    def message(self) -> Locator:
        return self.page.get_by_role("textbox", name="Message")

    @property
    def log(self) -> Locator:
        return self.page.get_by_role("log")

    @property
    def review(self) -> Locator:
        return self.page.get_by_role("region", name="Review details")

    @property
    def verification(self) -> Locator:
        return self.page.get_by_role("region", name="Energy account verification")

    @property
    def verification_notice(self) -> Locator:
        return self.page.locator("#verification-notice")

    @property
    def verification_error(self) -> Locator:
        return self.page.locator("#verification-error")

    @property
    def verification_status(self) -> Locator:
        return self.page.locator("#verification-status")

    @property
    def alerts(self) -> Locator:
        return self.page.get_by_role("alert")

    @property
    def audio(self) -> Locator:
        return self.page.get_by_test_id("reply-audio")

    @property
    def file_input(self) -> Locator:
        return self.page.locator("#wav-recording")

    def button(self, name: str, *, page: Page | None = None) -> Locator:
        return (page or self.page).get_by_role("button", name=name, exact=True)

    def enabled_button(self, name: str) -> Locator:
        """Matches only when the button is present, visible and enabled."""

        return self.page.get_by_role("button", name=name, exact=True, disabled=False)

    def play_button(self, message_id: str) -> Locator:
        return self.page.locator(f'button[data-message-id="{message_id}"]')

    # ---- journey bookkeeping ------------------------------------------------

    def cp(self, name: str) -> None:
        """Record a checkpoint reached; the hook publishes it in e2e-results.json."""

        self._node.user_properties.append(("checkpoint", name))

    def new_page(self) -> Page:
        page = self.page.context.new_page()
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        return page

    # ---- navigation ---------------------------------------------------------

    def open(self, page: Page | None = None) -> Response | None:
        target = page or self.page
        response = target.goto(self.server.base_url + "/")
        expect(target.get_by_role("dialog", name="Northstar Energy support chat")).to_be_visible()
        return response

    def stored_session_id(self, page: Page | None = None) -> str | None:
        return (page or self.page).evaluate("localStorage.getItem('northstar.session_id')")

    def current_session_id(self) -> str:
        wait_until(
            lambda: self.stored_session_id() is not None, "northstar.session_id in localStorage"
        )
        value = self.stored_session_id()
        assert value is not None
        return value

    # ---- actions ------------------------------------------------------------

    def send(self, text: str, *, page: Page | None = None) -> Response:
        target = page or self.page
        target.get_by_role("textbox", name="Message").fill(text)
        with target.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/turns")
        ) as pending:
            self.button("Send", page=target).click()
        return pending.value

    def send_localized(self, text: str, *, page: Page | None = None) -> Response:
        """Send in a Spanish session: stable element IDs, localized labels."""

        target = page or self.page
        target.fill("#message", text)
        with target.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/turns")
        ) as pending:
            target.click("#send")
        return pending.value

    def confirm(self) -> Response:
        with self.page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/confirm")
        ) as pending:
            self.button("Confirm details").click()
        return pending.value

    def verify_order(self, postcode: str, code: str) -> Response:
        """Submit the dedicated mock-verification form; credentials never enter chat."""

        self.page.get_by_label("Service postcode").fill(postcode)
        self.page.get_by_label("Verification code").fill(code)
        with self.page.expect_response(
            lambda r: r.request.method == "POST"
            and r.url.endswith("/order-verification")
        ) as pending:
            self.button("Verify account").click()
        return pending.value

    def new_intake(self) -> Response:
        with self.page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/sessions")
        ) as pending:
            self.button("New conversation").click()
        return pending.value

    def upload(self, wav: Path) -> None:
        self.file_input.set_input_files(str(wav))

    def tab_to(self, locator: Locator, limit: int = 80) -> None:
        """Reach `locator` using only the Tab key (bounded)."""

        for _ in range(limit):
            if locator.evaluate("el => el === document.activeElement"):
                return
            self.page.keyboard.press("Tab")
        raise AssertionError(f"Tab never reached the control within {limit} presses")

    @staticmethod
    def focus_is_visible(locator: Locator) -> bool:
        return bool(
            locator.evaluate(
                """el => {
                  const s = getComputedStyle(el);
                  const outline = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0;
                  const shadow = !!s.boxShadow && s.boxShadow !== 'none';
                  return outline || shadow;
                }"""
            )
        )

    # ---- expectations -------------------------------------------------------

    def expect_alert(self, text: str, page: Page | None = None) -> None:
        target = (page or self.page).get_by_role("alert").filter(has_text=text)
        expect(target.first).to_be_visible()

    def expect_no_alert(self) -> None:
        expect(self.alerts.filter(has_text=re.compile(r"\S"))).to_have_count(0)

    def expect_greeting(self) -> None:
        expect(self.log).to_contain_text(GREETING)

    def transcript_text(self) -> str:
        return " ".join(self.log.inner_text().split())

    # ---- real HTTP ledger ---------------------------------------------------

    def requests(self, method: str, path: str) -> list[dict[str, Any]]:
        return self.server.log.where(method, path)

    def turn_posts(self) -> list[dict[str, Any]]:
        return self.requests("POST", TURNS)

    def confirm_posts(self) -> list[dict[str, Any]]:
        return self.requests("POST", CONFIRMS)

    def verification_posts(self) -> list[dict[str, Any]]:
        return self.requests("POST", ORDER_VERIFICATIONS)

    def speech_posts(self) -> list[dict[str, Any]]:
        return self.requests("POST", SPEECH)

    def transcription_posts(self) -> list[dict[str, Any]]:
        return self.requests("POST", TRANSCRIPTIONS)

    def create_posts(self) -> list[dict[str, Any]]:
        return self.requests("POST", r"/api/sessions$")

    def session_gets(self) -> list[dict[str, Any]]:
        return self.requests("GET", SESSION_ITEM)

    # ---- disk ---------------------------------------------------------------

    def disk(self, session_id: str | None = None) -> dict[str, Any]:
        return self.server.read_session(session_id or self.current_session_id())

    def assistant_messages(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return [m for m in self.disk(session_id)["messages"] if m["role"] == "assistant"]

    def last_assistant(self, session_id: str | None = None) -> dict[str, Any]:
        return self.assistant_messages(session_id)[-1]

    # ---- passive media spy --------------------------------------------------

    def spy(self, page: Page | None = None) -> dict[str, Any]:
        return (page or self.page).evaluate(
            """() => ({
              created: window.__spy.created,
              revoked: window.__spy.revoked,
              plays: window.__spy.plays,
              pauses: window.__spy.pauses,
              contexts: window.__spy.contexts.length
            })"""
        )


@pytest.fixture
def make_ui(page: Page, request: pytest.FixtureRequest) -> Callable[[AppServer], UI]:
    return lambda server: UI(server, page, request.node)


@pytest.fixture
def ui(server: AppServer, make_ui: Callable[[AppServer], UI]) -> UI:
    # `server` is requested before `page`, so the browser context closes (and its
    # keep-alive sockets drop) before Uvicorn is asked to stop.
    return make_ui(server)
