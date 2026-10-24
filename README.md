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
support request locally. Account data and policies are invented test data, and the
knowledge base is **not real energy-provider policy**.

## Quick start

You need Docker with Compose v2 and access to an LLM, a speech-to-text (STT) and a
text-to-speech (TTS) service that speak the OpenAI-compatible API.

```bash
cp .env.example .env        # then fill in the endpoints, models and keys
docker compose up --build -d --wait --wait-timeout 120
```

Open http://127.0.0.1:8000. To try the verified-account flow, use the demo values
shown in the page (fictional account `NS-000123`, postcode `SW1A 1AA`, code
`246810`). Stop with `docker compose down`; add `--volumes` to also delete saved
sessions.

## How it meets the assignment

| Requirement | Where |
|---|---|
| Natural conversation, one question at a time, several answers per message | `app/conversation.py`, `app/prompts.py` |
| Collect order/account reference, category, description, urgency | `app/validation.py`, `app/schemas.py` |
| Extract and validate into structured output | LLM extraction with strict schema, then deterministic validators |
| Error handling and edge cases | Bounded retries, clear error codes, state never changes on a failed turn |
| Conversation context | Saved transcript plus accepted fields; sessions resume after a restart |
| JSON storage and summary | `app/storage.py`, [`examples/session.example.json`](examples/session.example.json) |
| Speech-to-text and text-to-speech | Upload or record audio, edit the transcript, play replies (`app/audio.py`, `app/providers.py`) |
| RAG | Lexical retrieval over `app/knowledge/northstar-faq.json`, validated citations, abstention |
| Sentiment (bonus) | The extraction call also labels sentiment and frustration; clear frustration hands off to a simulated agent |
| Multiple languages (bonus) | English and Spanish |
| Multi-turn memory (bonus) | Within a session |

## Models used

All three services are configured independently in `.env`; nothing is hard-coded.

| Role | Model | Why |
|---|---|---|
| LLM (extraction and RAG answers) | `DeepSeek-V4.1-Flash-1017` on a self-hosted server, JSON via `prompt_json` | Fast and cheap enough for one call per turn; works without provider-side structured output |
| Speech-to-text | `whisper-large-v3-turbo` on Groq | Low latency transcription |
| Text-to-speech | `canopylabs/orpheus-v1-english` on Groq, voice `troy`, WAV | Natural English voice; replies are split into chunks and joined |

A live check (`scripts/check_backends.py`) against these exact services passed all
five calls on 2026-10-24: extraction, a supported RAG question, an unsupported RAG
question that correctly abstained, transcription of `tests/fixtures/audio/spoken.wav`,
and speech synthesis. Latencies were roughly 0.5–1.7 s per call.

## Architecture

```
Browser (static chat + voice UI)
   │  HTTP/JSON
   ▼
FastAPI app ── conversation engine ── validators ── JSON session store (atomic writes)
   │                 │
   │                 ├── provider adapter ──► LLM / STT / TTS services
   │                 └── knowledge base retrieval (local, lexical)
   └── Order API client ──► private Order API (fictional accounts, internal network only)
```

- `app/main.py`: routes, request limits, error translation.
- `app/conversation.py`: the state machine (`collecting` → `awaiting_confirmation` → `completed`).
- `app/validation.py`, `app/schemas.py`: normalization and strict data models.
- `app/providers.py`: one OpenAI-compatible adapter for the four roles, with timeouts, retries and a shared concurrency limit.
- `app/retrieval.py`: knowledge loading, retrieval and citation checks.
- `app/storage.py`: one JSON file per session, replaced atomically.
- `app/order_api.py`, `app/order_client.py`, `app/verification.py`: a separate private service for the fictional account check.

## Key design decisions

- **The model proposes, the code decides.** The LLM only extracts candidate facts and intent. Application code owns validation, accepted state, the next question, and completion, so a model mistake or a prompt injection cannot save bad data or trigger actions.
- **Deterministic replies and summary.** Questions, the review and the summary come from templates, so the saved record always matches what the customer confirmed. Only RAG answers are generated.
- **Explicit confirmation.** Nothing is saved as submitted until the customer confirms a specific review; a correction invalidates the earlier review.
- **Safe failure.** A failed model call, storage write or invalid answer leaves the session untouched and can be retried. Repeated requests are de-duplicated.
- **Simple, local storage.** One JSON file per session with atomic replacement. No database or vector store, which suits a single-process demo.
- **Grounded answers.** The assistant answers only from the retrieved records, cites them, and says when it has no information. The app builds citations itself and never trusts model-supplied titles or links.
- **Provider-agnostic.** Any OpenAI-compatible server can fill any role, so the models can be swapped without code changes.

## Example session

[`examples/session.example.json`](examples/session.example.json) is a sanitized,
hand-written example: a greeting, one intake turn, one cited policy answer, the
confirmation and the completed summary.

```json
{
  "status": "completed",
  "slots": {
    "order_number": "NS-000123",
    "category": "damaged_item",
    "description": "My electricity meter casing is cracked and it will not switch on.",
    "urgency": "high"
  },
  "summary": "Account reference: NS-000123\nEnergy support area: equipment issue\nIssue: My electricity meter casing is cracked and it will not switch on.\nUrgency: high",
  "confirmation": { "confirmed_at": "2026-10-22T12:02:30Z", "review_revision": 1 }
}
```

## Voice

Upload a WAV file or record in the page. The audio is checked (PCM WAV, up to 60 s
and 10 MB) before it is sent for transcription. The transcript appears as an
editable draft and is only sent as a message when the customer presses send. Each
assistant reply has an optional play button. Audio is never stored.

## Knowledge base

The fictional corpus has five records. Retrieval is simple keyword matching, with no
embeddings.

| ID | Title |
|---|---|
| `faq-delivery` | Delivery times |
| `faq-damaged` | Damaged items |
| `faq-returns` | Returns and refunds |
| `faq-urgency` | Urgency levels |
| `faq-standing-charge` | Standing charges on an energy bill |

## Testing

```bash
pip install -r requirements-dev.txt
python scripts/test.py unit          # also: integration, e2e, coverage, mutation, all
python scripts/check_backends.py     # live check against the services in your environment
```

| Layer | What it covers | Result |
|---|---|---|
| Unit and integration | Validation, state machine, storage, HTTP routes, providers over a real socket, RAG, audio | 1713 unit and 370 integration tests passed on 2026-10-24, run as separate layers (`scripts/test.py unit`, then `integration`). Two wall-clock timeout tests can fail when both layers share one pytest process on a slow machine. |
| Browser (Playwright) | Twelve journeys in Chromium, including voice and RAG | 12 passed at the last full run; not repeated in the final pass |
| Real Docker end-to-end | Deployed container against the real LLM, with an LLM judge for intent routing | 3 passed at the last full run; not repeated in the final pass |
| Live providers | `scripts/check_backends.py` | 5 of 5 passed (2026-10-24) |
| Mutation testing | `mutmut` over `app/`, Linux only; runs in CI | Not re-run on the final code. Earlier runs killed 2563 of 2677 mutants; the rest were reviewed as equivalent. |

## Limitations

- **Single process, one worker.** Locks and de-duplication are in memory.
- **No authentication.** Session IDs are not credentials. It is for local only use; a public deployment would need authentication, rate limiting, TLS and a retention policy.
- Nothing is sent to a real support team, and "submitted" means saved locally.
- Retrieval is keyword-based over five records. A cited answer can still be wrong.
- Voice accepts PCM WAV only, with no streaming.
- Frustration and sentiment come from one LLM call and can misclassify sarcasm or mixed feelings.
- Stored category values are older identifiers (`delivery`, `damaged_item`, `wrong_item`, `return_refund`, `other`) that the UI shows as supply, equipment, meter, billing and other.
- Audio and messages go to the configured providers, so check their retention policies before using real customer data.

## Potential improvements

- Authenticated sessions and a shared database, so it can run with several workers.
- Embedding-based retrieval for a larger knowledge base.
- Streaming voice and barge-in.
- Rename the stored category values to the energy vocabulary, with a migration.
- Cross-session memory with identity and consent.
- A live ticketing handoff instead of the simulated one.

## Demo video

Demo video: pending
