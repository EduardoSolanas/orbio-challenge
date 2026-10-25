# Startup and deployment contract

The application and deployment files are present. See [README.md](../README.md#setup-and-exact-commands) for operational commands and recorded verification limits. All commands below run from the repository root; paths are relative to that root.

Read [implementation-contract.md](implementation-contract.md) for exact wiring/model/API choices and [testing-contract.md](testing-contract.md) for strict TDD, unit/integration/Playwright/mutmut gates. All listed quality gates are mandatory.

Model-service configuration is now independent by role: [provider-compatibility.md](provider-compatibility.md). Extraction, RAG answers, STT and TTS may use separate OpenAI-compatible servers. Hosted OpenAI is optional, no fixed model allowlist or universal TTS date cutoff applies, and local auth=none services need no OpenAI credential.

RAG is mandatory: [rag-contract.md](rag-contract.md). The four-record FAQ ships inside gateway/knowledge and is loaded/validated at startup; a missing or invalid KB prevents readiness. Lexical retrieval adds no embedding API, vector service or container. The configured RAG text model generates grounded policy answers with citations.

## Recommended runtime

Use Docker Compose with **three services**, each built from its own Dockerfile. `web` is an nginx container and the only service with a host port: it serves the static browser UI and forwards `/api/` and `/health` to the gateway, so the browser sees one origin. `gateway` is the chat gateway: one Uvicorn worker, FastAPI serving the JSON/audio endpoints on the internal network. `backend-api` is a private FastAPI service that owns the fictional demo account fixture, the demo verification code and the credential rule; it is reachable only on the internal Compose network, publishes no host port, and the gateway calls it through `gateway.backend_client.BackendApiClient` over HTTP. FastAPI in the gateway calls independently configured model services over local HTTP or remote HTTPS for extraction, RAG, STT and TTS. Those are logical capabilities, not local containers. A named Docker volume stores JSON sessions at `/data/sessions`.

The gateway starts only after `backend-api` reports healthy, and `web` only after the gateway does (`depends_on: condition: service_healthy`). The Backend API `/health` endpoint is private: it confirms the service is up, is not exposed to the host or browser, and makes no model or catalog write. Gateway `/health` checks writable session storage and the loaded knowledge base; verification and lookup failures are reported at request time as fixed safe errors (`backend_api_unavailable`, `backend_api_timeout`, `invalid_backend_api_response`), never as transport detail.

No database, Redis, GPU, Node runtime, or ffmpeg is required for the core demo. Standard-library `wave` validates the supported PCM WAV uploads. A verifier supervises build artifacts; it is not a running application component.

## Prerequisites

- Docker with Compose v2 running and able to build the application image.
- Internet access for image/package downloads and provider API calls.
- Running compatible model servers for all required roles, with valid model/voice IDs and role-specific credentials only where authentication is enabled. Hosted services may additionally require account access/billing.
- A free local port 8000, browser, and short fictional PCM WAV recording for the voice smoke test.
- Application source and pinned dependencies from this checkout; build context (the repository root) must include `gateway/`, `backend_api/`, `shared/`, `web/` and `requirements.txt`.

Python runtime dependencies (pins are in requirements.txt): `fastapi`, `uvicorn`, `pydantic`, `python-multipart` for uploads, and httpx for generic compatible-service transport. All pinned development dependencies in testing-contract.md are required, including pytest-asyncio, pytest-playwright, Playwright and Linux/WSL mutmut. Pin versions tested together during implementation; Docker base image should be pinned to a tested digest before reproducible delivery.

## Configuration

Use .env.example and provider-compatibility.md as the exact settings contract. Each service prefix LLM_, RAG_, STT_, TTS_ has BASE_URL, MODEL, AUTH_MODE and API_KEY. Text roles have OUTPUT_MODE and TTS has VOICE, AUDIO_FORMAT and MAX_INPUT_CHARS. Groq Orpheus requires AUDIO_FORMAT=wav and MAX_INPUT_CHARS=200; its implementation contract includes bounded WAV assembly. Blank RAG settings inherit the complete LLM profile; a separately configured RAG profile inherits no secrets. SESSION_DIR and PROVIDER_MAX_CONCURRENCY retain their original meanings.

The example host.docker.internal addresses are illustrative servers on the Docker host; they are not started by Compose. The root Compose file supplies `host.docker.internal:host-gateway`; host model servers must accept connections from Docker. Same-network model servers use service DNS; external servers use actual HTTPS API prefixes. Operator must supply valid served model names and supported voice. The compatibility smoke in provider-compatibility.md is required before claiming a backend works. Local app startup only validates settings, writable storage and bundled KB.

## Compose startup from repository root

Duplicate `.env.example` as `.env` using your editor or file manager, then
edit the local file with real model IDs, voice, endpoints and any required keys.
Do not overwrite an existing local configuration. `.env` is excluded from
Git and image build context; `env_file` injects values at container creation.

```text
docker compose config --quiet
docker compose up --build -d --wait --wait-timeout 120
docker compose ps
docker compose logs --tail 100 gateway
docker compose logs --tail 100 backend-api
docker compose exec -T backend-api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=2).close()"
```

Open http://127.0.0.1:8000 and http://127.0.0.1:8000/health in your browser. The Backend API has no host port; `docker compose ps` must show it running and healthy without a published port. `GET /internal/v1/orders/{order_number}` and `POST /internal/v1/order-verifications` exist only on the internal network. The root Compose file builds from the repository root. Compose paths are relative to that file, so `.env` is also in the root. After editing configuration, repeat `docker compose up --build -d --wait --wait-timeout 120` to recreate both services.

```text
docker compose restart gateway
docker compose down
```

Ordinary `down` retains the named session volume. Do not add `--volumes` unless
you intend to delete stored sessions. Run exactly one worker; JSON locks and
idempotency are process-local. Volume retention is not backup.

## Startup acceptance gate

1. Fresh build succeeds with pinned dependencies; container runs as non-root with exactly one worker.
2. Compose reports both `gateway` and `backend-api` healthy; gateway `/health` succeeds with writable session storage and the private Backend API `/health` answers on the internal network. No provider call is made by either health check, and the Backend API publishes no host port.
3. Open page, create a session, complete a real text interview and correction, then confirm.
4. Restart the container and resume an incomplete session; confirm saved JSON survives.
5. Upload a valid PCM WAV, inspect/edit the transcript, submit, and play an assistant reply.
6. Invalid profile/auth settings or unwritable storage produces an actionable sanitized error. Provider outage preserves committed state; TTS failure leaves text usable.
7. Mocked tests pass without live credentials and without touching saved demo data.
8. Browser requests to `/internal/v1/...` on the public origin return 404; the private Backend API is reachable only from the gateway container, and stopping it yields a fixed safe gateway error until it is restarted.

For observed image, application and provider gate results, consult README.md and reports/. Do not infer successful execution from this contract.

## References

Compose configuration, mounts, `env_file`, ports, and health checks follow the [Docker service reference](https://docs.docker.com/reference/compose-file/services/). See also [Docker Compose quickstart](https://docs.docker.com/compose/gettingstarted/) and [setting container environment variables](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/).


## Mandatory test-first delivery

Follow testing-contract.md for exact pinned frameworks, suite boundaries, Playwright browser installation, mutation commands/report gate, and CI. Unit/integration and browser tests precede their respective implementation behavior. Mutation generation and execution are owned by mutmut, never hand-authored source modifications. Windows mutation runs use WSL/Linux. The initial five-hour estimate does not waive any of these requirements.
