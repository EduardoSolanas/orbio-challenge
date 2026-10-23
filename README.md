# Northstar Shop support intake

A small Python/FastAPI application that interviews a customer about an order
problem, validates the answers, asks for confirmation, and saves a structured
support intake as JSON. It is a **demo**: it records an intake locally and never
contacts a real support team, changes an order, or issues a refund.

The behaviour is defined by the contracts in `docs/`: `implementation-contract.md`
(IC), `testing-contract.md` (TC), `rag-contract.md` (RAG),
`provider-compatibility.md` (PC), `startup.md` (SU) and `build-spec.md` (BS).

## Fictional scenario

Northstar Shop is a fictional online retailer. A customer writes: "Hi, my order
NS-000123 arrived broken. It is a damaged item and this is urgent." The agent
extracts and validates the order number, category, description and urgency, shows
a structured review, answers a policy question ("Can I return an unused item?")
from the bundled fictional knowledge base with citations, keeps the review open,
and saves the confirmed intake locally. The customer, order and policies are
invented test data; the knowledge base is authored for this demo and is **not
real Northstar Shop policy**.

## Chosen models and provider profiles

Model, voice and endpoint choices are **operator-supplied**; no model IDs or
credentials are committed to this repository. The application speaks the
OpenAI-compatible Chat Completions and audio contracts in `provider-compatibility.md`
to four independently configurable roles:

| Role | Purpose | Required settings |
|---|---|---|
| `LLM_` | Structured extraction and intent | `BASE_URL`, `MODEL`, `AUTH_MODE`, `API_KEY`, `OUTPUT_MODE` |
| `RAG_` | Grounded policy answers | same as LLM; if **all** RAG values are blank the whole LLM profile is inherited atomically |
| `STT_` | WAV transcription | `BASE_URL`, `MODEL`, `AUTH_MODE`, `API_KEY` |
| `TTS_` | Reply speech | `BASE_URL`, `MODEL`, `AUTH_MODE`, `API_KEY`, `VOICE`, `AUDIO_FORMAT`, `MAX_INPUT_CHARS` |

Rationale: generic HTTP against Chat Completions/audio endpoints keeps the app
independent of a single hosted vendor, allows local `auth=none` servers and lets
each role use a different model. `OUTPUT_MODE` is `json_schema` (default),
`json_object` or `prompt_json`. The operator must record the exact served model
and voice IDs they used, and must run the compatibility smoke in
`provider-compatibility.md` before claiming a backend works. **No specific model
or live provider has been verified for this delivery** — all deterministic gates
use httpx transport doubles and the E2E provider fake, not a real service.

## Setup and exact commands

All commands run from the **repository root**, containing `compose.yaml` and
`README.md`. You only need Docker with Compose v2 running; Compose builds and
runs Python and the application dependencies inside the container.

### 1. Edit the local configuration

Create `.env` in the root by duplicating `.env.example` with your editor or
file manager. Keep your actual settings in `.env`; commit only `.env.example`.
Edit it before starting the app. Both Git and the Docker build context ignore
`.env`; Compose passes its values into the running container using
`env_file`, so credentials are not baked into image layers.

| Setting | Default / what to edit |
|---|---|
| `AI_PROVIDER` | `openai_compatible` |
| `LLM_BASE_URL` | `http://host.docker.internal:8001/v1` |
| `STT_BASE_URL` | `http://host.docker.internal:8002/v1` |
| `TTS_BASE_URL` | `http://host.docker.internal:8003/v1` |
| `LLM_MODEL`, `STT_MODEL`, `TTS_MODEL`, `TTS_VOICE` | Replace example values with actual served IDs |
| `TTS_AUDIO_FORMAT` | `mp3` (default) or `wav`; Groq Orpheus needs `wav` |
| `TTS_MAX_INPUT_CHARS` | Per-request provider limit, default `4000`; Groq Orpheus needs `200` (WAV replies are chunked, up to 20 chunks) |
| Each role's `AUTH_MODE` / `API_KEY` | Defaults to `none` / empty; use `bearer` and its key if required |
| `LLM_OUTPUT_MODE` | `json_schema`; change to `json_object` or `prompt_json` if required by your backend |
| All `RAG_*` values | Blank inherits the complete LLM profile; set a complete profile for a separate answer server |
| `PROVIDER_MAX_CONCURRENCY` | `4` |

Model servers are provisioned separately: Compose does not download weights or
start LLM/STT/TTS servers. The default URLs reach servers on the Docker host;
Compose configures the host gateway alias. Those servers must listen on an
interface reachable from Docker. For externally hosted services, replace URLs
with their actual HTTP(S) API prefixes. `127.0.0.1` inside the app container refers
to the app container, not your model server.

Networking is also configured in `.env`: `APP_PUBLISH_HOST` and
`APP_PUBLISH_PORT` control the host binding; `APP_BIND_HOST` and `APP_PORT`
control Uvicorn inside the container; `APP_HEALTH_URL` controls the internal
readiness request. Its default references `${APP_PORT}` so a port change stays
in sync. `MODEL_HOST_ALIAS` and `MODEL_HOST_GATEWAY` configure the provider-host
mapping. Example browser URLs below use the defaults; adjust them if you change
the published port. Keep the published host on loopback for this local demo.

Compose automatically reads the root `.env` for variable substitution and uses
the same file to populate the container environment; no extra flags are required.

### 2. Build and start

```text
docker compose config --quiet
docker compose up --build -d --wait --wait-timeout 120
```

Open **http://127.0.0.1:8000**. The readiness endpoint is
http://127.0.0.1:8000/health. Readiness checks the app and storage, not live model
compatibility. Invalid configuration fails startup with
`configuration_invalid: Check required provider and model settings.`

### 3. Inspect, apply configuration changes, or stop

```text
docker compose ps
docker compose logs --tail 100 app
```

After editing `.env`, recreate the app to apply the new values:

```text
docker compose up --build -d --wait --wait-timeout 120
```

For an ordinary restart or stop:

```text
docker compose restart app
docker compose down
```

Compose runs one worker. The default published address is loopback port 8000;
change `APP_PUBLISH_HOST` and `APP_PUBLISH_PORT` in `.env` to configure it. Session JSON
lives at `/data/sessions` in the named `sessions` volume. Ordinary `down` retains
sessions; adding `--volumes` deletes them. The repository's host `./data` folder
is separate from this volume.

The recorded Compose startup/restart and live-provider acceptance gates remain
**not verified**; setup instructions are not evidence of a successful run.
Developer test commands and historical gate results are below; the full strict
TDD, Playwright browser setup and framework mutation procedure are specified in
[the testing contract](docs/testing-contract.md).

## Architecture and data flow

- `app/main.py` — FastAPI app factory, lifespan assembly (settings, JSON
  repository, knowledge base, provider, engine), raw-ASGI body/multipart limits,
  request IDs, error translation and the HTTP routes.
- `app/schemas.py` — strict Pydantic wire and stored-session models (extra keys
  forbidden; schema version 1).
- `app/conversation.py` — the intake state machine: extraction, merge policy,
  questions, review, confirmation, idempotency replay, turn limit, RAG routing and
  atomic persistence under a per-session lock.
- `app/validation.py` — deterministic normalization and field rules.
- `app/prompts.py` — literal greeting, deterministic questions/review/summary,
  extraction and grounded-answer prompts.
- `app/providers.py` — `OpenAICompatibleProvider` for the four roles with bounded
  bodies, retries, repair attempt, deadlines and a shared semaphore.
- `app/retrieval.py` — knowledge-base loading/validation, lexical retrieval,
  citation validation.
- `app/storage.py` — one JSON document per session, atomic temp-file replace and
  per-session locks.
- `app/audio.py` — PCM WAV validation before any paid request.
- `app/static/` — the server-served chat/voice page.

Data flow for a text turn: `POST /api/sessions/{id}/turns` → validate UUID/body and
revision → acquire the session lock → load and schema-validate the JSON document →
bounded LLM extraction → merge candidates with deterministic validators → select
the next missing/invalid question or the review → if it is a pure policy question,
retrieve locally and call the RAG model once, validating citations → atomically
replace the session file (including the replay record) → return the turn response.
`POST /api/sessions/{id}/confirm` writes `confirmation`, `status=completed` and the
local reference receipt in the same atomic way. Distinct sessions proceed
independently; one process, one worker.

## State and validation rules

Statuses: `collecting` → `awaiting_confirmation` → `completed`. A completed session
cannot be edited or confirmed again; a stale or missing review token is rejected.

- `order_number`: `NS-` plus exactly six digits; trims, uppercases and accepts a
  bare six-digit number by prefixing `NS-`; never invents digits.
- `category`: one of `delivery`, `damaged_item`, `wrong_item`, `return_refund`,
  `other` after alias normalization.
- `description`: 10–2000 characters after trimming; must not be only a category
  label.
- `urgency`: one of `low`, `normal`, `high` after alias normalization.
- Turn text: 1–4000 characters after trimming; at most 50 collection turns.
- Requests carry `request_id` and `expected_revision`; replaying the same request
  returns the identical stored response, while the same ID with different input is
  rejected. A revision mismatch is `409 revision_conflict`.
- Business-field problems are conversational `200` replies (clarification), not
  HTTP errors. HTTP errors use the contract envelope
  `{ "error": { "code", "message", "retryable" }, "request_id" }`.
- The deterministic summary is exactly `Order: …`, `Category: …`, `Problem: …`,
  `Urgency: …` built from accepted slots.

## Example session JSON

`examples/session.example.json` is a sanitized, hand-authored example that
validates against the stored `Session` schema (`schema_version` 2). It contains a
greeting, one intake turn, one policy question with a `faq-returns` citation
snapshot, the explicit confirmation, the completed summary, the local receipt and
the three replay records. It was authored from the contract templates and models,
not captured from a live provider run; the policy answer is illustrative generated
text. Stored customer messages carry the extraction `sentiment`/`frustration`
labels; assistant and system messages carry `null`, and `frustration_active` is
`false`. Key excerpts:

```json
{
  "status": "completed",
  "slots": {
    "order_number": "NS-000123",
    "category": "damaged_item",
    "description": "The item in order NS-000123 arrived with a cracked casing and will not switch on.",
    "urgency": "high"
  },
  "summary": "Order: NS-000123\nCategory: damaged_item\nProblem: The item in order NS-000123 arrived with a cracked casing and will not switch on.\nUrgency: high",
  "confirmation": { "confirmed_at": "2026-10-22T12:02:30Z", "review_revision": 1 },
  "messages": [
    {
      "role": "assistant",
      "text": "An unused item in its original packaging can be requested for return within 30 calendar days of delivery. Refund eligibility requires a support review.",
      "citations": [
        {
          "source_id": "faq-returns",
          "title": "Returns and refunds",
          "excerpt": "An unused item in its original packaging can be requested for return within 30 calendar days of delivery. Refund eligibility requires a support review; this demo cannot approve or issue a refund.",
          "kb_version": "2026-10-22"
        }
      ]
    }
  ]
}
```

No credentials, tokens, real personal data or live endpoints appear in the file;
the only order number is the fictional `NS-000123`.

## Voice workflow

1. Record in the page (raw samples encoded client-side to 16-bit PCM WAV) or upload
   a PCM WAV file: mono/stereo, 16-bit, 8000–48000 Hz, at most 10 MB and 60 seconds.
2. `POST /api/transcriptions` validates the header, channels, rate, width, duration
   and available frame bytes **before** any paid STT request. Silent, truncated,
   compressed/unsupported and overlength audio is rejected with a clear error; the
   temporary audio is deleted in a `finally` block and never stored.
3. The transcription appears as an **editable draft**; nothing is committed until
   the customer presses send. The committed transcript goes through the same turn
   validation as typed text.
4. `POST /api/sessions/{id}/speech` with an existing assistant `message_id` returns
   the configured audio (`audio/mpeg` for MP3, `audio/wav` for PCM WAV) with
   `Cache-Control: no-store`; audio is never persisted. Long replies are split at
   `TTS_MAX_INPUT_CHARS` for WAV profiles and the decoded chunks are assembled
   under one WAV header. TTS is opt-in per reply and a failure leaves the text
   reply and saved state intact.

## RAG corpus and citation display

Retrieval is deterministic lexical (NFKC → casefold → `[a-z0-9]+`, fixed stop
words, keyword qualification, score `3 × keyword overlap + title/body overlap`,
top two records, ties by ascending id). There is no embedding model, vector store
or extra service, and no semantic-retrieval claim. The bundled fictional corpus
`app/knowledge/northstar-faq.json` (version `2026-10-22`) has exactly four records:

| ID | Title | Fixture content |
|---|---|---|
| `faq-delivery` | Delivery times | standard delivery 2–5 business days; no per-order status |
| `faq-damaged` | Damaged items | keep packaging, describe damage, support review required |
| `faq-returns` | Returns and refunds | unused item, original packaging, 30 calendar days, review required |
| `faq-urgency` | Urgency levels | low/normal/high meanings; no response-time guarantee |

The model receives only the retrieved records and must answer with 1–2 exact
supplied IDs. The application validates membership and builds the citation
snapshots itself (title, full retrieved body as excerpt, KB version); model-supplied
titles, excerpts and URLs are never trusted. Unsupported questions use the fixed
abstention `I do not have that information in the Northstar Shop knowledge base. I
can still record your issue for review.` The page renders a plain-text answer with
an accessible `Sources` region per assistant message (title + ID summary, excerpt
and KB version in the details). A policy question sent together with facts or a
correction gets the intake response plus `Please ask your policy question in a
separate message.` — one policy question per message.

## Failure behaviour

- Invalid or missing settings fail startup with the exact sanitized
  `configuration_invalid` error; an unwritable `SESSION_DIR` fails with
  `storage_unavailable`. `/health` checks process, storage and the loaded KB without
  any provider call.
- Provider timeout → `504 provider_timeout`; unavailable/configuration → `503`;
  invalid provider payload → `502 invalid_provider_response`; capacity exhaustion →
  `429 provider_busy`; per-session lock deadline → `429 session_busy`. Each
  logical operation allows at most two attempts (one retry with jitter, one schema
  repair); extraction plus grounded generation share one 25-second outer deadline.
- Failed writes never return success: the previous JSON file remains parseable and
  unchanged. A corrupt or unsupported-schema file returns a controlled error and is
  never overwritten with an empty session.
- STT/TTS failures leave chat usable; a failed RAG answer rolls back the whole turn;
  an invalid citation result is not displayed.
- Logs contain request ID, status, timing and sanitized error codes only — no
  transcript text, order numbers or credentials.

## Quality gates: commands and actual status

Run from the repository root with the project `.venv` (Python 3.11.15,
pytest 9.1.1, mutmut 3.8.0). Statuses below are the **actually observed** results
recorded in `reports/gates/gate1.md` (revision `7f9f258`), `reports/gates/gate2.md`
(revision `1a91e86`) and the independent re-run in `reports/final-verification.md`
(HEAD `eed3ac2`, logs in `reports/logs/ORB-20/`). They are not projections.

```bash
.venv/bin/python -m pip check
.venv/bin/python -m pytest tests/unit tests/integration -q
.venv/bin/python -m pytest tests/unit tests/integration --cov=app --cov-branch --cov-context=test --cov-report=json:reports/coverage.json --cov-report=term-missing --cov-report=xml:reports/coverage.xml --junitxml=reports/unit-integration.xml
.venv/bin/python -m pytest tests/e2e --browser chromium --tracing retain-on-failure --screenshot only-on-failure --video retain-on-failure --output reports/playwright --junitxml=reports/e2e.xml
.venv/bin/python scripts/check_e2e_inventory.py --spec docs/implementation-contract.md --manifest tests/e2e/cases.json --collection reports/e2e-collection.json --results reports/e2e-results.json
.venv/bin/python -m pytest tests/unit/test_example_session.py -q
```

| Gate | Actual status |
|---|---|
| `pip check` | **PASS** — exit 0, "No broken requirements found." |
| Unit + integration + coverage | **PASS** — gate 1: 1167 passed, aggregate coverage 97.15% (gate 95%). Re-run at HEAD `eed3ac2`: 1249 passed, 0 failed, 0 skipped (coverage not re-measured since gate 1); 185/185 IT ids collected and passed exactly once. |
| Playwright journeys J01–J10 | **PASS** — 10 passed, 0 failed, 0 skipped, 0 reruns (Chromium). |
| E2E inventory checker | **PASS when stamped and run at the revision under test** (gate 1 at `7f9f258`; re-run at `eed3ac2` in a throwaway worktree with `tests/e2e/cases.json` re-stamped: 10/10 passed, checker exit 0). **FAILS in the committed checkout** (exit 1, `stale source revision`) because the manifest and `reports/e2e-*.json` pin an older revision than HEAD; any later commit re-stales it. Re-stamp `tests/e2e/cases.json` and re-run the Playwright command at the final delivery revision. |
| Example-session/README test | **PASS** — `.venv/bin/python -m pytest tests/unit/test_example_session.py -q`, 28 passed. At this documentation revision the full suite was 1195 passed; it has since grown to 1249 (see above). |
| Mutation gate | **Mutation gate: incomplete at HEAD as a literal command.** The full `mutmut` 3.8.0 run is complete and approved (`reports/gates/gate2.md`, revision `1a91e86`): 2360 generated, 2250 killed, 110 approved equivalent, 0 invalid, 0 unresolved; both checkers exit 0 when run at that pinned revision (re-verified in a worktree: `reports/logs/ORB-20/05b-…` and `06b-…`). In the committed checkout at HEAD both checkers exit 1 with `stale source revision` because they pin to git HEAD and later commits moved it. Residual: `_lock_context` is skipped by mutmut itself (no mutation evidence for the lock path). |
| Container image build/smoke | **PASS (image only)** — `docker build -f docs/deployment/Dockerfile -t northstar-test .` exit 0; `docker run --rm --entrypoint python -v "$PWD/tests:/checks:ro" northstar-test /checks/support/image_smoke.py` exit 0 (uid 10001 non-root, `/health` ok, session/turn roundtrip). Two separate containers sharing one bind-mounted directory created then resumed a session (HTTP 200, revision 0). **Not verified:** `docker compose up` healthy state, restart/resume through Compose, and the live text/voice acceptance steps (startup.md steps 2–5). |
| Live provider smoke | **Live provider smoke: not verified** — no real LLM/STT/TTS credentials or compatible servers were used. Command: configure all four role profiles, then `.venv/bin/python scripts/check_backends.py` (writes `reports/backend-compatibility.json`). It needs a real spoken WAV at `tests/fixtures/audio/spoken.wav` (or `--wav PATH`); none is committed, so the STT check fails with `fixture_missing` until the operator supplies one. There is no `tests/live` suite in this repository. |

**Actual time spent:** approximately 4 hours 50 minutes of agent wall-clock from
the first implementation commit (`2026-10-22T22:24Z`) to the Gate 1 run
(`2026-10-23T03:08Z`). This interval is within the PDF's 4–6 hour core estimate and
does not include later verification/documentation. The additional strict-TDD,
Playwright, mutation, RAG and per-role-provider gates expand the core scope.
The time is build/tool wall-clock, not a productivity benchmark.

## Limitations

- **Single process, one worker.** Per-session locks, idempotency and the provider
  semaphore are process-local; multi-worker or multi-host operation would need
  shared transactions and idempotency and is not supported.
- **No authentication.** Session UUIDs identify sessions but are not authorization.
  Bind `127.0.0.1`; **local only** — a public deployment needs auth/session
  ownership, rate limiting, TLS, retention/deletion policy and hardening.
- No real order lookup, refund, replacement or support-team integration.
  "Submitted" means saved locally; the receipt says so explicitly.
- Lexical retrieval over four records only; no semantic/vector retrieval, no
  multilingual or large-corpus claim. Valid citations do not prove entailment: a
  generated answer can still be semantically wrong.
- Voice: server accepts PCM WAV only (no MP3/WebM upload), no streaming,
  interruption or telephony. Reply audio follows `TTS_AUDIO_FORMAT`: MP3
  (`audio/mpeg`) or validated/assembled PCM WAV (`audio/wav`) from the TTS role.
- The operator must verify provider retention policy before using real customer
  data; this demo does not implement redaction beyond excluding transcripts and
  order numbers from logs.
- Demo video and live provider evidence are pending (below).

### Emotion classification limits

- Sentiment and frustration are labels from the **existing** extraction call:
  there is no sentiment API, keyword classifier, confidence score, rationale or
  second model request. The prompt, JSON Schema and few-shot examples carry both
  fields in `json_schema`, `json_object` and `prompt_json` modes.
- The configured model classifies only the current message, using accepted state
  and recent committed messages. Quotation, negation, hypothetical examples,
  mixed valence and ambiguous sarcasm remain difficult; the reviewed cases are a
  small local smoke, not an accuracy benchmark or a guarantee.
- Live local evaluation against the configured model (`DeepSeek-V4.1-Flash-1017`,
  `prompt_json`) on 2026-10-23: **9 of 10** reviewed cases matched. The
  mixed-valence case “I am still frustrated, but thank you for explaining.” was
  labelled `negative/present` instead of the reviewed `uncertain/present`.
  Sanitized record: `reports/logs/ORB-41/emotion-evaluation.json`; reproduce with
  `scripts/evaluate_emotions.py` after exporting the configured LLM environment.
- Labels never supply urgency, category, order facts or intent routing. A
  `present` label alone does not add an acknowledgment in this story; episode
  handling is separate required work.
- Missing, null or invalid labels use the existing bounded repair attempt and
  then fail safely as `invalid_provider_response`; they are never coerced to
  `neutral`. Customer instructions to force a label are treated as untrusted
  content, not classifier instructions.

## Deferred bonuses and potential improvements

Implemented required extensions: frustration classification in the existing
extraction call (Story 2) and English/Spanish support. Remaining required work:
frustration episode acknowledgment (Story 3) and session-memory verification
(implementation-contract.md FEAT-01, FEAT-02 and VERIFY-01).

Still deferred: cross-session memory with identity/consent, streaming/duplex voice, and database-backed
multi-worker operation. Also out of scope: real order integration, ticket-queue
delivery, embeddings/vector retrieval, and a production auth/deployment story.
Potential improvements after core delivery: authenticated session ownership,
shared idempotency for multi-worker deployment, semantic retrieval for a larger
knowledge base, and operator-run live quality evaluation of the chosen models.

## Demo video

Demo video: pending operator recording

The recording must show a normal interview, an invalid answer and correction,
resumed context after restart, the saved JSON output, voice upload/playback, and
the RAG answer with its visible `Sources` details. It must use fictional data only
and never show credentials. No video URL is claimed or invented here; BS §12/R11
remains unfulfilled until the operator records and links it.
