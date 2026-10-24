# Startup and deployment contract

Status: proposed setup for the future application. The repository currently contains specifications, an architecture page, and deployment templates. `app/` and `requirements.txt` have not been implemented; the container cannot be built successfully yet.

Read [implementation-contract.md](implementation-contract.md) for exact wiring/model/API choices and [testing-contract.md](testing-contract.md) for strict TDD, unit/integration/Playwright/mutmut gates. All listed quality gates are mandatory.

Model-service configuration is now independent by role: [provider-compatibility.md](provider-compatibility.md). Extraction, RAG answers, STT and TTS may use separate OpenAI-compatible servers. Hosted OpenAI is optional, no fixed model allowlist or universal TTS date cutoff applies, and local auth=none services need no OpenAI credential.

RAG is mandatory: [rag-contract.md](rag-contract.md). The four-record FAQ ships inside app/knowledge and is loaded/validated at startup; a missing or invalid KB prevents readiness. Lexical retrieval adds no embedding API, vector service or container. The configured RAG text model generates grounded policy answers with citations.

## Recommended runtime

Use Docker Compose with **two services**. The browser-facing `app` gateway runs one Uvicorn worker on the published loopback port 8000 and serves the static UI plus JSON/audio endpoints from the same origin. The private `order-api` service runs one Uvicorn worker on port 8001 with **no host port**, joins only the internal Compose network (`internal: true`), and exposes `POST /internal/v1/order-verifications`, `GET /internal/v1/orders/{order_number}` and `GET /health`. Only the gateway's `OrderApiClient` calls it, with the shared `ORDER_API_TOKEN`; the private API is unreachable from the browser and the gateway exposes no `/internal/*` route (those paths return 404). Both containers use the same image; the second overrides the command. The gateway calls independently configured model services over local HTTP or remote HTTPS for extraction, RAG, STT and TTS. Those are logical capabilities, not local containers. A named Docker volume stores JSON sessions at `/data/sessions`.

The gateway declares `depends_on: order-api: condition: service_healthy`, so Compose starts the private service first and reports the gateway healthy only after the boundary answers. There is still no database, Redis, reverse proxy, GPU, Node runtime, or ffmpeg required for the core demo. Standard-library `wave` validates the supported PCM WAV uploads. A verifier supervises build artifacts; it is not a running application component.

## Prerequisites

- Docker Engine with Compose v2, or Docker Desktop configured for Linux containers. On Windows, ensure Docker Desktop's supported WSL 2 or Hyper-V backend is working.
- Internet access for image/package downloads and provider API calls.
- Running compatible model servers for all required roles, with valid model/voice IDs and role-specific credentials only where authentication is enabled. Hosted services may additionally require account access/billing.
- A free local port 8000, browser, and short fictional PCM WAV recording for the voice smoke test.
- Implemented `app/main.py` exporting `app`, `order_api/main.py` exporting `app`, the fictional catalogue `order_api/data/orders.json`, and pinned `requirements.txt` before using the templates.
- A locally generated `ORDER_API_TOKEN` shared secret in `docs/deployment/.env`; both services read it and it is never sent to the browser.

Proposed Python runtime dependencies: `fastapi`, `uvicorn`, `pydantic`, `python-multipart` for uploads, and httpx for generic compatible-service transport. All pinned development dependencies in testing-contract.md are required, including pytest-asyncio, pytest-playwright, Playwright and Linux/WSL mutmut. Pin versions tested together during implementation; Docker base image should be pinned to a tested digest before reproducible delivery.

## Configuration

Use docs/deployment/.env.example and provider-compatibility.md as the exact settings contract. Each service prefix LLM_, RAG_, STT_, TTS_ has BASE_URL, MODEL, AUTH_MODE and API_KEY. Text roles have OUTPUT_MODE and TTS has VOICE. Blank RAG settings inherit the complete LLM profile; a separately configured RAG profile inherits no secrets. SESSION_DIR and PROVIDER_MAX_CONCURRENCY retain their original meanings.

The private boundary adds `ORDER_API_URL` (gateway only; Compose sets `http://order-api:8001`), `ORDER_API_TOKEN` (shared by both services) and `ORDER_API_TIMEOUT_SECONDS` (default 5). The Order API also reads `ORDER_API_CATALOG_PATH` (default `order_api/data/orders.json`) and `ORDER_API_GRANT_TTL_SECONDS` (default 300). Startup validates all of them offline: a missing/blank token, invalid URL, invalid timeout/TTL or invalid catalogue path aborts with `configuration_invalid: Check required Order API settings.`; a missing, corrupt, empty, oversize or duplicate-ID catalogue aborts the Order API with `catalog_invalid: Check the demo order catalogue.` No secret is ever exposed to the browser or written to logs.

The example host.docker.internal addresses are illustrative servers on the Docker host; they are not started by Compose. On Linux Docker Engine add `extra_hosts: ["host.docker.internal:host-gateway"]` to the app service when using those host addresses. On Docker Desktop use the documented host alias. Same-network model servers use service DNS; external servers use actual HTTPS API prefixes. Operator must supply valid served model names and supported voice. The compatibility smoke in provider-compatibility.md is required before claiming a backend works. Local app startup only validates settings, writable storage and bundled KB.

## Compose startup: PowerShell from repository root

Run these after the application exists:

```powershell
docker --version
docker compose version
Copy-Item docs/deployment/.env.example docs/deployment/.env
# Edit docs/deployment/.env locally with provider/model settings.
docker compose -f docs/deployment/compose.yaml up --build -d --wait --wait-timeout 120
docker compose -f docs/deployment/compose.yaml ps
Invoke-RestMethod http://localhost:8000/health
```

Open `http://localhost:8000`. Copy the example only on first setup so an existing `.env` is not overwritten. `env_file` passes variables into the container; Compose's automatic interpolation `.env` behavior alone does not do this. The build context is repository root; relative paths in the Compose file resolve from its directory. Only host loopback port 8000 is published; the Order API has no host port at all, so `http://localhost:8001` must not answer. Uvicorn must listen on `0.0.0.0` inside each container. The gateway `/health` returns 200 only after its bounded probe of the Order API `/health` succeeds; it makes no model call.

```powershell
# Status and sanitized logs
docker compose -f docs/deployment/compose.yaml ps
docker compose -f docs/deployment/compose.yaml logs --tail 100 app
# Stop and remove the container; keep saved sessions
docker compose -f docs/deployment/compose.yaml down
# Apply code or configuration changes by rebuilding/recreating
docker compose -f docs/deployment/compose.yaml up --build -d --wait --wait-timeout 120

```

Run unit/integration tests with `python -m pytest tests/unit tests/integration -q` in the local/CI development environment with its mandatory dependencies installed, injected test profiles and temporary directories. For E2E, use the separate browser-enabled environment and Playwright installation/commands in testing-contract.md. Do not run an entire mounted /checks tree inside the runtime Compose service, inherit live .env credentials or mount the live session volume for testing. Do not scale `app` or `order-api` or set multiple workers: JSON locks and idempotency are process-local, and the grant registry is process memory.

The named volume survives ordinary `down`. Destructive data removal must be a separate, explicit user action; do not include it in normal stop or reset instructions. Volume retention is not backup. Docker logs must omit transcript and credential content.

## Local development alternative: PowerShell

Python 3.11+ installed locally; Docker is optional on this path:

```powershell
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
$env:AI_PROVIDER = 'openai_compatible'
# Set the complete LLM/RAG/STT/TTS profiles in this shell.
$env:SESSION_DIR = './data/sessions'
$env:ORDER_API_URL = 'http://127.0.0.1:8001'
$env:ORDER_API_TOKEN = '<locally generated shared secret>'
$env:ORDER_API_CATALOG_PATH = './order_api/data/orders.json'
# First shell: the private Order API.
& ./.venv/Scripts/python.exe -m uvicorn order_api.main:app --host 127.0.0.1 --port 8001 --reload
# Second shell: the gateway (after the Order API answers /health).
& ./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
# In a third shell, from repository root:
& ./.venv/Scripts/python.exe -m pytest tests/unit tests/integration -q
```

Local Uvicorn does not automatically load `docs/deployment/.env`; this path uses process environment variables. `--reload` is development-only. Container and local data directories are separate; switching paths does not automatically migrate sessions. The gateway must start after the Order API so its `/health` probe succeeds; restarting only the gateway is safe while the private service keeps running.

## Startup acceptance gate

1. Fresh build succeeds with pinned dependencies; both containers run as non-root with exactly one worker each.
2. Compose reports **both** services healthy; the gateway `/health` succeeds only with writable session storage and a successful bounded probe of the Order API `/health`; no provider call is made by either health check.
3. The private API is unreachable from the browser and host: `http://127.0.0.1:8001` does not answer and the gateway returns 404 for `/internal/v1/order-verifications` and `/internal/v1/orders/NS-000123`.
4. A format-valid catalogue order is accepted only after a verification call plus a grant-bound lookup; a well-formed unknown order produces the `not_found` clarification and no review; stopping the Order API yields a retryable `order_api_unavailable` error with no committed turn, and recovery lets the same request commit once.
5. Open page, create a session, complete a real text interview and correction, then confirm.
6. Restart the gateway container and resume an incomplete session; confirm saved JSON survives.
7. Upload a valid PCM WAV, inspect/edit the transcript, submit, and play an assistant reply.
8. Invalid profile/auth or Order API settings, an invalid catalogue, or unwritable storage produces an actionable sanitized error. Provider or Order API outage preserves committed state; TTS failure leaves text usable.
9. Mocked tests pass without live credentials and without touching saved demo data.

Provider calls, image build, and application execution have not been verified in this specification-only task.

## References

Compose configuration, mounts, `env_file`, ports, and health checks follow the [Docker service reference](https://docs.docker.com/reference/compose-file/services/). See also [Docker Compose quickstart](https://docs.docker.com/compose/gettingstarted/) and [setting container environment variables](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/).


## Mandatory test-first delivery

Follow testing-contract.md for exact pinned frameworks, suite boundaries, Playwright browser installation, mutation commands/report gate, and CI. Unit/integration and browser tests precede their respective implementation behavior. Mutation generation and execution are owned by mutmut, never hand-authored source modifications. Windows mutation runs use WSL/Linux. The initial five-hour estimate does not waive any of these requirements.
