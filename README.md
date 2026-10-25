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
`246810`), then ask for the installation status or to book a technician. Stop with
`docker compose down`; add `--volumes` to also delete saved sessions and bookings.

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
| RAG | Embedding retrieval (or keyword matching when no embedding model is configured) over `app/knowledge/northstar-faq.json`, validated citations, abstention |
| Account actions (extra) | After verifying the demo account: installation status and booking a technician from offered time windows (`app/backend_api.py`, `app/bookings.py`, `app/visits.py`) |
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
| Embeddings (optional) | `nvidia/nemotron-3-embed-1b:free` on OpenRouter, set in `EMBED_*`; any OpenAI-compatible `/v1/embeddings` model works | Semantic retrieval for the FAQ; handles Spanish questions. Free tier, so rate-limited |

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
   └── Backend API client ──► private Backend API (fictional accounts, internal network only)
```

- `app/main.py`, `app/routes.py`: app factory and startup wiring; the HTTP routes. `app/asgi_guards.py` and `app/http_support.py` hold the request-size guards, correlation IDs and request parsing.
- `app/conversation.py`: the turn transaction and state machine (`collecting` → `awaiting_confirmation` → `completed`). It uses `app/intent.py` (what an extraction means), `app/plans.py` (what to reply) and `app/replay.py` (duplicate-request handling).
- `app/validation.py`, `app/schemas.py`: normalization and strict data models.
- `app/providers.py`: one OpenAI-compatible adapter for the four roles, with timeouts, retries and a shared concurrency limit. `app/provider_wire.py` builds requests and parses replies; `app/speech_wav.py` joins spoken chunks into one WAV.
- `app/retrieval.py`: knowledge loading, retrieval and citation checks.
- `app/storage.py`: one JSON file per session, replaced atomically.
- `app/backend_api.py`, `app/backend_client.py`, `app/verification.py`: the private Backend API, the single place for account data: the fictional account check, installation status, technician availability and booking. The gateway reaches it over the internal network only.
- `app/bookings.py`, `app/availability.json`, `app/visits.py`: technician windows and bookings kept in one JSON file (hours and days offered are edited in `availability.json`) and the reply text for installation status and booking. After verification a customer can ask for installation status or to book a technician; the chat shows the open windows as buttons and a click books one.
- `app/static/`: the chat page, with no build step. `js/` is plain ES modules, one class per concern: `ChatState` (what the page knows), `ChatView` (draws it), `ChatPresenter` (applies server responses), `SessionController`, `TurnController` and `VoiceController` (the user's actions), `ApiClient` and `DeadlineHttp` (requests), `I18n` (translations) and `App`, which wires them together. Everything a class needs is passed in, so the tests replace the network, storage and microphone. `css/` follows OOCSS: `objects.css` holds structure only (layout, size, shape), `skins.css` holds colour only, `base.css` the tokens, reset and utilities. No rule is tied to an id and each object looks the same wherever it is placed.

## Key design decisions

- **The model proposes, the code decides.** The LLM only extracts candidate facts and intent. Application code owns validation, accepted state, the next question, and completion, so a model mistake or a prompt injection cannot save bad data or trigger actions.
- **Deterministic replies and summary.** Questions, the review and the summary come from templates, so the saved record always matches what the customer confirmed. Only RAG answers are generated.
- **Explicit confirmation.** Nothing is saved as submitted until the customer confirms a specific review; a correction invalidates the earlier review.
- **Safe failure.** A failed model call, storage write or invalid answer leaves the session untouched and can be retried. Repeated requests are de-duplicated.
- **Simple, local storage.** One JSON file per session with atomic replacement. Technician bookings use one JSON file in the Backend API. No database or vector store, which suits a single-process demo.
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

The fictional corpus has twelve records. Two retrieval modes exist, chosen by configuration:

- **Embeddings** (when `EMBED_*` is set in `.env`): the records are embedded once and
  each question is matched by cosine similarity. The best two records at or above
  `EMBED_MIN_SIMILARITY` go to the model; if none qualifies, the assistant abstains.
  If the embedding service fails, that question falls back to keyword matching.
- **Keywords** (default, no embedding model): each record is scored by the words it
  shares with the question, and the best two go to the model.

Either way the model only sees the retrieved records, and citations are built and
checked by the app (`app/semantic.py`, `app/retrieval.py`).

| ID | Title |
|---|---|
| `faq-delivery` | Delivery times |
| `faq-damaged` | Damaged items |
| `faq-returns` | Returns and refunds |
| `faq-urgency` | Urgency levels |
| `faq-standing-charge` | Standing charges on an energy bill |
| `faq-installation` | Smart meter installation |
| `faq-meter-readings` | Submitting a meter reading |
| `faq-moving-home` | Moving home |
| `faq-bill-queries` | Questions about your statement |
| `faq-power-cut` | Power cuts and loss of supply |
| `faq-gas-safety` | If you smell gas |
| `faq-account-reference` | Finding your account reference |

## Testing

```bash
pip install -r requirements-dev.txt
python -m ruff check app
python -m ruff format --check app
python scripts/test.py unit          # also: integration, e2e, coverage, mutation, all
python scripts/check_backends.py     # live check against the services in your environment
npm ci && npm test                   # frontend unit tests (Jest)
```

Run `python -m ruff format app` from the repository root to format the Python application code.

| Layer | What it covers | Result |
|---|---|---|
| Unit and integration | Validation, state machine, storage, HTTP routes, providers over a real socket, RAG, audio | 1775 unit and 389 integration tests passed on 2026-10-25, run as separate layers (`scripts/test.py unit`, then `integration`). Two wall-clock timeout tests can fail when both layers share one pytest process on a slow machine. |
| Browser (Playwright) | Eleven tests in Chromium covering the user journeys, including voice and RAG | 11 passed on 2026-10-25 |
| Frontend unit tests | Jest (`npm test`) over the page's classes in jsdom, using the real `index.html` | 280 passed on 2026-10-25; 97.6% statements, 91% branches (CI gate: 90% / 80%) |
| Judge journeys | 31 browser tests with a scripted AI provider; 29 record a conversation that a judge LLM grades against a written rule. They cover every intent, including account status, installation status and booking with verification first, and the hand-off of a frustrated customer | 31 passed on 2026-10-25 with `DeepSeek-V4.1-Flash-1017` as the judge: 29 of 29 conversations judged as pass. Replies there are mostly fixed templates, so this checks wording rules, not model behaviour. |
| Real Docker end-to-end | 15 tests on the deployed containers against the real LLM, speech services and embeddings, graded by the judge LLM: greeting, gratitude, capability, off-topic, start of a report, a full intake with correction, rejection and confirmation, RAG with citations and abstention, order status, installation status and booking (each with verification first), angry customers in English and Spanish, an angry customer mid-intake, and a nervous one | 15 passed on 2026-10-25 with the same DeepSeek model as judge, so the model grades itself. The first full run passed 13: one failure was my rule being too narrow and one was real, the off-topic reply never said it would not help, which is now fixed. The booking test books the demo account's one visit, so a rerun reports that visit; reset with `docker compose down --volumes`. A reasoning judge needs `E2E_JUDGE_MAX_TOKENS=4096`: at 1024 it ran out of tokens thinking and returned nothing. |
| Live providers | `scripts/check_backends.py` | 5 of 5 passed (2026-10-24) |
| Live evaluation | `scripts/evaluate.py`: intent detection, frustration hand-off and FAQ retrieval on labelled utterances | 89 of 89 intents (in-sample); frustration: 9 of 9 angry handed off, 8 of 8 calm or nervous kept; FAQ retrieval on held-out questions: embeddings 9 of 10 vs keywords 3 of 10 (see Evaluation) |
| Mutation testing | `mutmut` over `app/`, Linux only; runs in CI | Not re-run since the modules were split. An earlier run killed 2563 of 2677 mutants; the rest were reviewed as equivalent. CI re-runs it. |

## Evaluation

The scripted tests prove what the app does once it is told an intent. They cannot show
whether the real model picks the right intent or the right FAQ record, so there is a
separate, operator-run evaluation against the live services (it is not part of the
automatic test run and gates nothing):

```bash
python scripts/evaluate.py            # also: intents, retrieval
```

It uses the labelled utterances in [`tests/eval/utterances.json`](tests/eval/utterances.json):
89 English and Spanish messages for the ten context-free intents (confirm, reject and
correction need an open review, so the scripted tests cover them) and two sets of FAQ
questions (25 and 22; 4 and 5 of them should get no answer). Run on 2026-10-25 with `DeepSeek-V4.1-Flash-1017`
and `nvidia/nemotron-3-embed-1b:free` on OpenRouter.

### Intent detection

| | Before | After |
|---|---|---|
| Correct intents | 85 of 89 (96%) | 89 of 89 (100%) |
| `start_intake` | 5 of 8 | 8 of 8 |
| `question` | 8 of 9 | 9 of 9 |
| Every other intent | 9 of 9 | 9 of 9 |

The first run missed four messages: "I want to report a problem", "Let me report a fault"
and its Spanish equivalent were read as `provide_details`, and "What are the urgency
levels?" was read as `capability`. The fix was in the prompt only: a rule that reporting
a problem without saying what it is starts an intake, a rule that questions about how
support works are policy questions, and three new examples (an English and a Spanish
fault report, and a response-time question) worded differently from the test messages.
The new intents `installation_status` and `book_technician` scored 9 of 9 each from the
start. The score is on the same messages that exposed the misses, so read 100% as a
regression baseline, not proof of general accuracy; new phrasings should be added to the
file over time.

### Frustration hand-off

The app hands a customer to a simulated human agent on the first message the model
labels as clearly frustrated; a calm or merely anxious message stays in the chat. On 17
labelled messages in English and Spanish, the real model handed off 9 of 9 angry ones
("I'm so pissed off, I want to talk to a human", "I'm furious", "Estoy harto de este
bot") and kept 8 of 8 calm or nervous ones ("I'm a bit nervous, my meter shows a strange
number", "I'm worried about my bill"). The messages were written by one author and the
model labels each message alone, so a milder or sarcastic complaint may be judged
differently; the Docker tests above check the hand-off end to end, including an angry
customer mid-intake and a nervous one who must not be handed off.

### FAQ retrieval

There are two question sets. The **tuning set** was used to choose the similarity floor,
so its scores are optimistic. The **held-out set** was run once at the chosen floor. The
first ten of its questions were written before any score was seen; the last seven were
added later, together with the new records they test, so they are not blind.

| Retrieval | Tuning set: right record in top two | Tuning set: correctly no answer | Held-out: right record in top two | Held-out: correctly no answer |
|---|---|---|---|---|
| **5 records**, keywords | 11 of 13 | 4 of 5 | 3 of 10 | 5 of 5 |
| **5 records**, embeddings, floor 0.25 | 12 of 13 | 5 of 5 | 9 of 10 | 5 of 5 |
| **12 records**, keywords | 21 of 21 | 3 of 4 | 12 of 17 | 5 of 5 |
| **12 records**, embeddings, floor 0.25 | 21 of 21 | 4 of 4 | 17 of 17 | 5 of 5 |

On the tuning set the two are close, because those questions happened to share words
with the hand-written keywords. On fresh wordings keyword matching collapses ("I want my
money back" or "how soon will it be shipped" share no keyword with the records), while
embeddings still find the record. Keywords also answered "Do you ship internationally?"
with the delivery record; embeddings refused it.

The 12-record corpus fixed what the first round exposed. The original records still
sounded like a shop, so the delivery and damaged-equipment records now speak about
smart meters, the damaged record gained "cracked", "dented" and "faulty", and seven
energy records were added (installation, meter readings, moving home, statements,
power cuts, gas safety, account reference). The similarity floor was not retuned. Two
cautions: the held-out misses that disappeared (such as "a dented meter box") were
fixed after being seen, so that improvement is partly by construction; and the
questions are still few and written by the same author as the records.

How the floor was chosen: the first guess of 0.35 was untested and dropped correct
paraphrases (10 of 13 on the 5-record tuning set); 0.30 gave 11, 0.25 gave 12, and 0.20
found all 13 but let "Do you ship internationally?" through to the delivery record. 0.25
was picked on that tuning set, so treat it as a sensible value for this model, not a
proven optimum. The model matters: re-run `scripts/evaluate.py retrieval` after
changing it. Three free OpenRouter embedding models were compared on a first 16-question
set; `nemotron-3-embed-1b`, `nemotron-3.5-embed-1b` and `lfm-2.5-embedding-350m` tied,
and `nemotron-3.5-embed-8b` timed out on the free tier, so the first was kept.

## Limitations

- **Single process, one worker.** Locks and de-duplication are in memory.
- **No authentication.** Session IDs are not credentials. It is for local only use; a public deployment would need authentication, rate limiting, TLS and a retention policy.
- Nothing is sent to a real support team, and "submitted" means saved locally.
- Answers come from twelve short, fictional FAQ records. Keyword matching can miss a differently worded question; embedding retrieval depends on `EMBED_MIN_SIMILARITY`, tuned here for one model on a small question set (see Evaluation), so it needs re-tuning if the model changes. The gas-safety record quotes the UK National Gas Emergency number from memory; check it before relying on it. A citation shows which record the model used; it does not prove the model summarised it correctly.
- Technician bookings are one JSON file: one visit per account, no cancelling or rescheduling, and the offered times disappear from the chat if the page is reloaded (ask again to see them).
- Voice accepts PCM WAV only, with no streaming.
- Frustration and sentiment come from one LLM call and can misclassify sarcasm or mixed feelings.
- Stored category values are older identifiers (`delivery`, `damaged_item`, `wrong_item`, `return_refund`, `other`) that the UI shows as supply, equipment, meter, billing and other.
- Audio and messages go to the configured providers, so check their retention policies before using real customer data.

## Potential improvements

- Authenticated sessions and a shared database, so it can run with several workers.
- A vector store and chunking for a larger knowledge base (the embeddings are held in memory today).
- Streaming voice and barge-in.
- Rename the stored category values to the energy vocabulary, with a migration.
- Cross-session memory with identity and consent.
- A live ticketing handoff instead of the simulated one.

## Demo video

Demo video: pending
