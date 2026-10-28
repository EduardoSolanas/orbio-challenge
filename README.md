# Northstar Energy Customer Support

A conversational support assistant for a fictional energy provider, built for the
Core Engineer assignment. It interviews a customer about a service problem,
extracts and validates four fields, asks the customer to confirm, and saves the
result as JSON with a summary. It also answers energy questions from a small
knowledge base (RAG) with citations, and works by text or voice.

It is a **demo**: it never contacts a real support team, and the account data and
knowledge base are invented.

## Fictional scenario

A customer reports a damaged electricity meter, asks what a standing charge covers,
reviews the captured issue, and confirms it. The assistant answers the policy
question from the bundled knowledge base with a citation, then saves the confirmed
support request locally. The knowledge base is **not real energy-provider policy**.

## Quick start

You need Docker with Compose v2 and access to an LLM, a speech-to-text (STT) and a
text-to-speech (TTS) service that speak the OpenAI-compatible API.

```bash
cp .env.example .env        # then fill in the endpoints, models and keys
docker compose up --build -d --wait --wait-timeout 120
```

Open http://127.0.0.1:8000. To try the verified-account flow, use the demo values
shown in the "Test customer" card beside the chat, each with a copy button (fictional account `NS-000123`,
postcode `SW1A 1AA`, code `246810`; phones see the same values in the verification
form), then ask for the installation status or to book a technician. The card's
"Clear local storage" button forgets the conversation this browser saved, so you can start
again from a fresh state. The request
you made before verifying is answered as soon as the account is verified, even if
you typed only the account number in between. Stop with
`docker compose down`; add `--volumes` to also delete saved sessions and bookings.

## How it meets the assignment

| Requirement | Where |
|---|---|
| Natural conversation, one question at a time, several answers per message | `gateway/conversation/engine.py`, `gateway/providers/prompts.py` |
| Collect order/account reference, category, description, urgency (normal unless the customer says otherwise) | `gateway/conversation/validation.py`, `gateway/schemas.py` |
| Extract and validate into structured output | LLM extraction with strict schema, then deterministic validators |
| Error handling and edge cases | Bounded retries, clear error codes, state never changes on a failed turn |
| Conversation context | Saved transcript plus accepted fields; sessions resume after a restart |
| JSON storage and summary | `gateway/storage.py`, [`examples/session.example.json`](examples/session.example.json) |
| Speech-to-text and text-to-speech | Upload or record audio, edit the transcript, play replies (`gateway/providers/`) |
| RAG | Embedding retrieval (keyword matching if no embedding model is set) over `gateway/knowledge/northstar-faq.json`, validated citations, abstention |
| Account actions (extra) | After verifying the demo account: installation status, booking a technician and changing the date of the booked visit (`backend_api/`, `gateway/conversation/visits.py`) |
| Sentiment (bonus) | The extraction call also labels sentiment and frustration; clear frustration hands off to a simulated agent |
| Multiple languages (bonus) | English and Spanish. A new chat greets in the browser's language (`Accept-Language`, English if it is neither); after that each reply follows the language of the customer's message |
| Multi-turn memory (bonus) | Within a session |

## Models used

All three services are configured independently in `.env`; nothing is hard-coded.

| Role | Model | Why |
|---|---|---|
| LLM (extraction, RAG answers) | Any OpenAI-compatible chat model, set by `LLM_BASE_URL` and `LLM_MODEL` (used so far: `DeepSeek-V4.1-Flash-1017` and `GLM-5.3-Flash-EXL3`, both self-hosted on the Spark server, and `deepseek-v4.1-flash` on OpenCode Go, which is what the chat runs on now; the evaluation numbers below were measured on the first; JSON via `prompt_json`) | Fast and cheap enough for one call per turn; no provider-side structured output needed. To swap models change those variables, recreate the gateway and run `python scripts/evaluate.py`; the gateway reads JSON wrapped in code fences or `<think>` blocks, and the judged tests use the same server unless `E2E_JUDGE_*` is set |
| Speech-to-text | `whisper-large-v3-turbo` on Groq | Low latency |
| Text-to-speech | `eleven_multilingual_v2` on ElevenLabs, one voice for English and Spanish, mp3 | One multilingual voice for both languages; replies are one request each |
| Spanish text-to-speech | `TTS_ES_*`, set to the same ElevenLabs model and voice (`TTS_ES_AUTH_MODE=elevenlabs`) | Spanish replies only get audio when this block is set; it can point at a different voice |
| RAG answers | The same LLM as above (`RAG_*` left blank) | One model to run; the answer is checked against the retrieved records |
| Embeddings (optional) | `nvidia/nemotron-3-embed-1b:free` on OpenRouter (`EMBED_*`); any OpenAI-compatible `/v1/embeddings` model works | Semantic FAQ retrieval, handles Spanish; free tier, so rate-limited |

`scripts/check_backends.py` ran against the services of that date (DeepSeek as the LLM, before the
switch above) on 2026-10-24 and passed all
five calls (extraction, supported and unsupported RAG questions, transcription,
synthesis), at roughly 0.5–1.7 s each.

## Provider setup used

Copy `.env.example` to `.env` and fill in the blocks you need. This is the setup the demo ran on.

**Groq: speech-to-text.** Whisper is multilingual, so it transcribes English and Spanish. Create a key at <https://console.groq.com/keys>.

```bash
STT_BASE_URL=https://api.groq.com/openai/v1
STT_MODEL=whisper-large-v3-turbo
STT_AUTH_MODE=bearer
STT_API_KEY=<groq key>
```

**ElevenLabs: the voice, for English and Spanish.** One multilingual model and voice speaks both, so the English block (`TTS_*`) and the Spanish block (`TTS_ES_*`) hold the same values. The Spanish block is what turns Spanish audio on.
1. Create an API key at <https://elevenlabs.io/app/settings/api-keys>. Text to speech is the only permission it needs.
2. Get a voice id from <https://elevenlabs.io/app/voices> (My Voices, the voice's menu, "Copy voice ID"), or make one at <https://elevenlabs.io/app/voice-lab>. A free plan gets HTTP 402 for voices taken from the public library, so use a voice in your own list.

```bash
TTS_BASE_URL=https://api.elevenlabs.io/v1
TTS_MODEL=eleven_multilingual_v2
TTS_AUTH_MODE=elevenlabs     # xi-api-key header and ElevenLabs' own route, mp3 only
TTS_API_KEY=<elevenlabs key>
TTS_VOICE=<voice id>
TTS_AUDIO_FORMAT=mp3
TTS_MAX_INPUT_CHARS=4000
# the same seven values again for Spanish:
TTS_ES_BASE_URL=https://api.elevenlabs.io/v1
TTS_ES_MODEL=eleven_multilingual_v2
TTS_ES_AUTH_MODE=elevenlabs
TTS_ES_API_KEY=<elevenlabs key>
TTS_ES_VOICE=<voice id>
TTS_ES_AUDIO_FORMAT=mp3
TTS_ES_MAX_INPUT_CHARS=4000
```

**OpenCode Go: the chat model.** Subscribe and create a key as described in <https://opencode.ai/docs/go/>. Its chat endpoint is OpenAI-compatible but also needs a session header, which `LLM_HEADERS` sends on every request (any role accepts `<ROLE>_HEADERS`).

```bash
LLM_BASE_URL=https://opencode.ai/zen/go/v1
LLM_MODEL=deepseek-v4.1-flash          # list the ids with: GET https://opencode.ai/zen/go/v1/models
LLM_AUTH_MODE=bearer
LLM_API_KEY=<opencode key>
LLM_HEADERS='{"x-opencode-session":"northstar"}'
LLM_OUTPUT_MODE=prompt_json
```

**OpenRouter: embeddings (optional).** Create a key at <https://openrouter.ai/keys>, then set `EMBED_BASE_URL=https://openrouter.ai/api/v1`, `EMBED_MODEL=nvidia/nemotron-3-embed-1b:free`, `EMBED_AUTH_MODE=bearer` and `EMBED_API_KEY`.

After editing `.env`, run `docker compose up -d --build --force-recreate --wait`. Keys stay in `.env`, which is git-ignored.

## Built with

The code was written with two AI coding assistants, **Codex** and **Claude Code**, working in the same
repository and reviewed by the author. The app itself was run and tested against two self-hosted chat
models, **DeepSeek-V4.1-Flash-1017** and **GLM-5.3-Flash-EXL3**; either one can fill the LLM role
(see the table above).

## Architecture

![Architecture: browser, web (nginx), gateway, backend-api, shared models and the AI services](docs/architecture.svg)

The page and each service live in their own folder, with their own Dockerfile:

```
web/           the chat page (index.html, css/, js/) and the nginx config that serves it
gateway/       chat service   routes/  conversation/  providers/  knowledge/  locales/  flows.json
backend_api/   Backend API    main.py  bookings.py  availability.json
shared/        the models and error codes both services use
tests/         unit/{gateway,backend_api,tooling}  integration/  e2e/  js/
```

Docker runs three containers: `web` (nginx, the only published port: it serves the page and forwards `/api` and `/health` to the gateway), `gateway`, and the private `backend-api`. The gateway and the Backend API import only `shared/`, never each other.

- `gateway/conversation/engine.py` is the turn transaction and state machine (`collecting` → `awaiting_confirmation` → `completed`); `intent.py`, `plans.py` and `replay.py` decide what an extraction means, what to reply and how duplicates are handled.
- `gateway/providers/client.py` is one OpenAI-compatible adapter for every role, with timeouts, retries and a concurrency limit.
- `backend_api/` is the only place that holds account data: the account check, installation status, technician windows (hours and days are edited in `availability.json`) and bookings, kept in one SQLite file. The gateway reaches it over the internal network only.
- `web/` is the chat page with no build step: plain ES modules, everything injected so tests replace the network and microphone, and CSS split into `objects.css` (structure), `skins.css` (colour) and `base.css` (tokens). It is one centred column with four quick-option buttons under the greeting, so a customer can start without typing.

## Key design decisions

- **The model proposes, the code decides.** The LLM only extracts candidate facts and intent. Application code owns validation, state, the next question and completion, so a model mistake or prompt injection cannot save bad data or trigger actions. The real-stack tests try to jailbreak it and check that verification, bookings and saved data stayed untouched.
- **Deterministic replies and summary.** Questions, the review and the summary come from templates, so the saved record matches what the customer confirmed. Only RAG answers are generated.
- **Flows are data, the runner is code.** Installation status and technician booking are lists of step ids in [`gateway/flows.json`](gateway/flows.json): ask for the account, verify it, then show the status or offer windows. The model only starts a flow and supplies what a step asks for; the code moves between steps and saves the current one in the session. The support request is listed there too, but its saved slots and `status` stay the source of truth, since the customer can give the four details in any order and leave for a booking and come back. An unknown step id stops the gateway at startup.
- **Safety comes first.** "I smell gas" is a statement, so the intent label can send it to intake. Two independent checks run before intake, and either one is enough: the model's `safety_concern` flag, and an embedding check that asks whether the message is closer to a labelled emergency example or to an ordinary one. The examples are plain sentences in [`gateway/knowledge/safety-examples.json`](gateway/knowledge/safety-examples.json), so teaching it new wording means adding a sentence, not code. The embedding check runs beside the model call and still works when the model is down. The customer gets the gas-safety record at once (if the model is down, the record text as written), and a gas report wins over the angry-customer hand-off.
- **Explicit confirmation.** Nothing is saved as submitted until the customer confirms a specific review; a correction invalidates it.
- **Safe failure.** A failed model call, write or invalid answer leaves the session untouched and can be retried; repeated requests are de-duplicated.
- **Simple storage.** One JSON file per session with atomic replacement, as the assignment asks. Technician bookings live in one SQLite file (standard library, no server); the database itself stops two customers taking the same window or one account holding two visits. No vector store.
- **Grounded answers.** The assistant answers only from retrieved records, cites them, and says when it has no information. Citations are built by the app, never taken from the model.
- **Provider-agnostic.** Any OpenAI-compatible server can fill any role.

## Example session

[`examples/session.example.json`](examples/session.example.json) is a sanitized,
hand-written example: a greeting, one intake turn, one cited policy answer, the
confirmation and the completed summary.

## Voice

Upload a WAV file or record in the page. The audio is checked (PCM WAV, up to 60 s
and 10 MB) before transcription. The transcript appears as an editable draft and is
only sent when the customer presses send. Recording or uploading again replaces a dictated draft; text the customer typed is never overwritten, so it blocks both. Each assistant reply has an optional play
button. Audio is never stored.

## Knowledge base

The fictional corpus has twelve records. Retrieval depends on configuration:

- **Embeddings** (when `EMBED_*` is set): records are embedded once and each question
  is matched by cosine similarity. The best two records at or above
  `EMBED_MIN_SIMILARITY` go to the model; if none qualifies the assistant abstains. If
  the embedding service fails, that question falls back to keywords.
- **Keywords** (default): records are scored by the words they share with the question.

Either way the model only sees the retrieved records, and the app checks the citations
(`gateway/knowledge/`).

Each record also has a Spanish title and body (`es` in `northstar-faq.json`). They are shown to the
model and cited when the reply is Spanish. Embedding retrieval reads both texts and a record scores
as its better match; keyword retrieval reads the English text plus a fixed Spanish word map. The ids and
numbers are the same. The Spanish text is a translation written for this demo, not reviewed policy.

Records:
`faq-delivery` Delivery times ·
`faq-damaged` Damaged items ·
`faq-returns` Returns and refunds ·
`faq-urgency` Urgency levels ·
`faq-standing-charge` Standing charges on an energy bill ·
`faq-installation` Smart meter installation ·
`faq-meter-readings` Submitting a meter reading ·
`faq-moving-home` Moving home ·
`faq-bill-queries` Questions about your statement ·
`faq-power-cut` Power cuts and loss of supply ·
`faq-gas-safety` If you smell gas ·
`faq-account-reference` Finding your account reference

## Testing

```bash
pip install -r requirements-dev.txt
python -m ruff check . && python -m ruff format --check .
python scripts/test.py unit          # also: integration, e2e, coverage, mutation, all
python scripts/check_backends.py     # live check against the services in your environment
npm ci && npm test                   # frontend unit tests (Jest)
```

Unit, integration, browser, Jest, judged real-stack and mutation tests run in CI
(`.github/workflows/quality.yml`). Latest recorded results (2026-10-26): about 2,500 unit and
integration tests, 11 Playwright journeys, 312 Jest tests, and mutation testing with every
non-equivalent variant killed. The per-layer table, the real-stack runbook and the
model evaluation (intent, safety, category, frustration, retrieval) are in
[docs/TESTING.md](docs/TESTING.md).

## Limitations

- **Single process, one worker.** Locks and de-duplication are in memory.
- **No authentication.** Session IDs are not credentials. It is for local only use; a public deployment would need authentication, rate limiting, TLS and a retention policy.
- Nothing is sent to a real support team; "submitted" means saved locally.
- The receipt shows a short reference (`REQ-` and the first eight characters of the session id, for example `REQ-2B0737BE`), not the full id. The session file is named after the full id, so the reference is enough to find it, but eight characters are not unique at scale.
- Answers come from twelve short fictional records. Keyword matching can miss a differently worded question; embedding retrieval depends on `EMBED_MIN_SIMILARITY`, tuned for one model on a small set. The gas-safety record quotes the UK National Gas Emergency number from memory; check it before relying on it. A citation shows which record was used, not that the model summarised it correctly.
- Bookings (SQLite): one visit per account. Asking to book again shows the existing visit with the free times to move it to; choosing one moves the visit in a single step and keeps its reference. There is no cancelling, and the offered times disappear if the page is reloaded (ask again).
- Only gas (a smell, a leak or a carbon monoxide alarm) gets the immediate safety answer. Other emergencies (sparks, smoke) are not in the knowledge base, so the assistant treats them like any other message. Both checks can miss: the model's flag, and the embedding check, which needs the embedding service and is switched off when none is configured (the model's flag alone then applies). Both were measured on 29 messages (see [docs/TESTING.md](docs/TESTING.md)).
- Jailbreak resistance is tested with six attempts and three gas reports on the real model; that shows the obvious attacks fail, not that it is secure.
- Voice accepts PCM WAV only, with no streaming.
- Frustration and sentiment come from one LLM call and can misread sarcasm or mixed feelings.
- Stored category values are older identifiers (`delivery`, `damaged_item`, `wrong_item`, `return_refund`, `other`) that the UI shows as supply, equipment, meter, billing and other.
- Audio and messages go to the configured providers; check their retention policies before using real customer data.

## Potential improvements

- Authenticated sessions and a shared database, so it can run with several workers.
- A vector store and chunking for a larger knowledge base (embeddings are held in memory today).
- Streaming voice and barge-in.
- Rename the stored category values to the energy vocabulary, with a migration.
- Cross-session memory with identity and consent.
- Order the quick-option buttons by the most common intents in the last 10 minutes, so the page offers what customers are asking for right now.
- A live ticketing handoff instead of the simulated one.

## Demo video

Demo video: pending
