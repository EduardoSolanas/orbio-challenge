# Independent OpenAI-compatible service backends

Current transport/configuration contract, 22 October 2026. RAG, text extraction, STT, TTS and every quality gate remain mandatory. This is a design contract, not an assertion of tested server interoperability.

## 1. Four independently configurable roles

| Prefix | Service | Used by |
|---|---|---|
| `LLM_` | Structured extraction and intent | AIProvider.extract |
| `RAG_` | Grounded answer generation | AIProvider.answer_knowledge |
| `STT_` | File transcription | AIProvider.transcribe |
| `TTS_` | Speech synthesis | AIProvider.synthesize |

Each role has its own **BASE_URL, MODEL, AUTH_MODE and API_KEY**. Text roles additionally have OUTPUT_MODE. TTS additionally has VOICE. All roles can point to one compatible server, four different servers, or any combination. Open-source local servers and external compatible services are both supported by this contract. The application itself still has one container and worker; model-server provisioning, model weights, licences and hardware belong to the operator and are not silently installed by app startup.

Support means meeting the wire contracts below and passing per-role compatibility checks, not that every service branded “OpenAI-compatible” implements every endpoint. A text-only server can handle extraction/RAG while separate servers handle audio. All four roles must be configured for a complete assignment demo. No missing audio endpoint silently becomes a fake or disabled feature.

## 2. Exact configuration and inheritance

Use `AI_PROVIDER=openai_compatible` (only production adapter in this revision). Required for LLM/STT/TTS: `<ROLE>_BASE_URL`, `<ROLE>_MODEL`, `<ROLE>_AUTH_MODE`. Auth mode is `none` or `bearer`. For bearer, that role's API_KEY must be nonempty; for none, API_KEY must be empty and **no Authorization header** is sent. Server-side keys are never exposed to the browser. There is no global PROVIDER_API_KEY or automatic OPENAI_API_KEY lookup.

RAG inheritance is deliberately atomic: if **all** RAG profile fields are absent/empty, copy the entire resolved LLM profile, including output mode/auth/token, into a separate immutable RAG settings object. If **any** RAG field is supplied, require the complete RAG profile (BASE_URL, MODEL, AUTH_MODE, OUTPUT_MODE and a key iff bearer); do not inherit any field or credential. This prevents a local or alternate RAG host accidentally receiving the LLM host's key. The same URL with different model/auth is allowed by explicit complete configuration. No other role inherits anything.

BASE_URL is an operator-provided absolute http/https URL ending at the API prefix, e.g. `http://host.docker.internal:8001/v1`. Strip only trailing slashes. Reject missing host, credentials in URL, query, fragment, or path traversal segments. Model names are nonblank opaque strings ≤200 characters: do not hardcode model allowlists or silently rewrite IDs. API_KEY maximum 4096 characters; do not log values. VOICE is a required opaque nonblank string for TTS. LLM_OUTPUT_MODE defaults to `json_schema`; explicitly configured RAG requires its own OUTPUT_MODE. Modes are exactly `json_schema|json_object|prompt_json`. No automatic fallback between modes, endpoints, hosts or models. All settings validate offline at lifespan startup; there is no paid network probe during startup/health.

Do not require hosted OpenAI credentials for auth=none local servers. If a user selects OpenAI's hosted service, provisioning/reuse of its real credential follows the secure key workflow. There is no universal model-expiration startup cutoff for compatible servers. Provider/model deprecation information belongs to a named deployment profile and compatibility evidence, not to generic adapter code.

## 3. Transport and endpoint contracts

Use pinned **httpx==0.28.1** as the runtime async HTTP client. Production code uses httpx rather than a provider SDK. `OpenAICompatibleProvider` implements the four-method AIProvider protocol plus close() using one separate `httpx.AsyncClient` per resolved role, constructed/closed by app lifespan. Disable redirects (`follow_redirects=False`), set `trust_env=False`, verify TLS, send Accept-Encoding: identity on every request, and keep secrets out of request logs. None auth sends no Authorization; bearer sends only that role's header. Clients may share a host but never mutable headers. Connection/read timeouts and the outer deadlines remain the existing project budgets. Global provider semaphore stays shared across roles. Follow implementation-contract section 12 for preparse limits: JSON requests64KiB, successful provider JSON1MiB, error bodies64KiB, TTS5MiB; raw streamed reads, no compressed success responses, status-first error mapping, and existing deadlines including reads/cleanup.

Build URLs as `base_url.rstrip('/') + endpoint_suffix`; never add a second `/v1`. Allowed suffixes:

| Role | Method/suffix | Input | Required output |
|---|---|---|---|
| LLM/RAG | POST `/chat/completions` | JSON below | choices[0].message.content containing one JSON object |
| STT | POST `/audio/transcriptions` | multipart file=(upload.wav, validated bytes, audio/wav), model, response_format=json | JSON object with nonempty `text` string |
| TTS | POST `/audio/speech` | JSON `{model, voice, input, response_format:"mp3"}` | Nonempty MP3 bytes, bounded to 5 MiB while streaming |

No Responses API, tool calling, streaming chat, embeddings or model-list endpoint is required. Model listing is not a reliable capability test. Extra response metadata may be ignored, but required content must validate locally. 3xx is a nonretryable configuration error, never follow redirects with credentials. HTTP 400/401/403/404/405/415/422 indicates a profile/capability/configuration error, not permission to switch server; 429/5xx/connection failures retain bounded retry behavior. Successful HTTP with invalid content maps invalid_provider_response. Unsupported response format/mode is a clear nonretryable provider_configuration error. Sanitized messages from implementation-contract still apply.

### Text request

JSON keys are exactly `model`, `messages`, `stream:false`, `temperature:0`, `max_tokens` (1200 for extraction, 700 for RAG) and optional response_format. Messages are the existing system instructions and a user string containing JSON-encoded context. The RAG prompt and grounding/citation validators are unchanged. Do not send OpenAI-specific `store`, `text_format`, `previous_response_id`, `reasoning`, or tool fields to generic servers.

- `json_schema`: response_format is `{ "type":"json_schema", "json_schema": { "name":"extraction" or "knowledge_answer", "strict":true, "schema":<Pydantic model_json_schema()> } }`. Server must support the supplied schema or fail compatibility validation; do not silently downgrade.
- `json_object`: response_format is `{ "type":"json_object" }`; append the exact JSON Schema to the system prompt prefixed `Return one JSON object matching this schema: `. Local Pydantic validation remains mandatory.
- `prompt_json`: omit response_format; append the same schema instruction. This accommodates servers without structured-output extensions; malformed JSON uses the existing bounded repair attempt and never reaches accepted state.

Parse first choice only; require finish_reason=`stop`, non-null string message.content, no tool_calls and no nonempty refusal field. Reject missing/empty choices, truncation (`length`), reasoning-only or multipart-content-only responses. Run `json.loads` over the complete trimmed content: no markdown fences, substring extraction or repair-by-regex. Validate with Extraction/KnowledgeAnswer; bounded repair is a model retry under the existing shared deadline. HTTP-mocked tests assert exact requests for all modes. Lower structured-output support can lower quality; passing a transport check is not proof that a model can extract/ground reliably.

### Audio request

STT keeps exact validated PCM WAV upload behavior and editable draft. Send no browser transcription or server-specific extras. Require JSON-compatible response; ignore extra fields but reject nonstring/empty/overlength text under existing error rules. File and multipart handles close on every exit.

TTS uses `async with client.stream("POST", url, json=payload)` and raw chunks processed in at most65536-byte pieces with no unexpected Content-Encoding; enforce the existing 5 MiB cap before retaining another chunk, validate nonempty result and Content-Type audio/mpeg or audio/mp3 (case-insensitive, ignoring parameters), return application audio/mpeg/no-store. Reject JSON/text or other audio formats for this baseline. A service that only emits WAV is not MP3-compatible until a separately specified transcoder/adapter is added. Do not relabel incompatible bytes. Tests and browser playback verify finite decodable MP3 data.

## 4. Engine and deployment wiring

Resolve ServiceProfiles → construct OpenAICompatibleProvider(profiles) → validate/load local KB → construct existing engine(repository, provider, knowledge_base). Provider selects the right profile solely from the invoked method, never from customer content or a browser-provided URL. Extraction uses LLM; grounded generation uses RAG; local lexical retrieval does not call an embedding server. Separate RAG model is optional configuration, but RAG behavior is mandatory.

Expose no backend secrets/URLs through public health. `/health` checks process, storage and loaded KB only. Shared extraction+RAG outer deadline remains 25 seconds even if they use different hosts. Per-operation retry counts, max four attempts per RAG turn, audio budgets, single-process session locking and atomic persistence do not change. TTS failures never roll back a saved text reply. `close()` closes all four clients (including inherited-RAG profile's distinct client).

The Compose template runs the application only, not arbitrary GPU services. App-to-model traffic uses configured addresses. A localhost URL **inside the container** refers to that container, not the host. For models on the Docker host, use host.docker.internal with Docker Desktop, or add the explicit Compose host-gateway mapping documented in startup.md. For models in the same Docker network use their service DNS name; remotely hosted services use their actual HTTPS API prefix. No hardcoded host substitution. Server model IDs and deployment URLs are required operator inputs; documentation examples are not claims that those servers are running.

## 5. Compatibility evidence and mandatory tests

Implement `scripts/check_backends.py` as an explicit operator-invoked live smoke, never a startup hook. It uses the same resolved profiles/adapter as the app, exits nonzero on any role failure, and writes sanitized `reports/backend-compatibility.json`: UTC checked_at, source revision, role, model, scheme/hostname/port (no key or full URL), auth_mode, output_mode where applicable, pass/fail, error code, observed latency. Test one structured Extraction and at least two KnowledgeAnswer cases (one supported seeded policy and one retrieved-but-unsupported question requiring abstention) against seeded contexts, transcribe a short spoken WAV fixture, and synthesize/play or decode a finite reply. A valid shape alone is insufficient: expected order/fields and supported/unsupported policy answers must match the fixture assertions. Audio smoke requires real spoken fixture and decodability checks; provider stubs do not establish compatibility.

Required strict-TDD cases:

- Unit: complete/partial/atomic RAG inheritance, no cross-host key inheritance, invalid URL/model/auth combinations, no-auth header absence, output-mode validation, endpoint joining, schema/JSON parsing, deadline/cancellation and client cleanup.
- Integration: four distinct httpx MockTransport endpoints receive only their assigned calls and credentials; all three output modes; no redirects/fallback; each unsupported endpoint fails clearly; both shared-server and split-server profiles; shared RAG deadline and citations remain correct. Verify real application HTTP serialization against controlled transport responses.
- Playwright: preserve all existing journeys using the same server-side fake AIProvider; cover backend failure isolation in J07/J10 and IT12–IT14: STT/TTS errors leave text/RAG usable and saved state intact. Do not add UI controls for operator URLs or credentials.
- Mutation: providers.py/config.py remain in the required mutmut scope; kill mutations that swap LLM/RAG/STT/TTS profiles, attach a wrong auth header, enable hidden fallback, skip parsing or share credentials incorrectly. New functions enter inventory/coverage gates.
- Live: run compatibility smoke against the actual selected profiles; list each backend+model combination verified. Unavailable endpoints mark integration incomplete, not silently skipped or universally supported.

Use vLLM and llama.cpp as examples of implementations documenting OpenAI-compatible interfaces, not a compatibility guarantee for every version/model/modality. Their exact server versions, model weights and launch commands must be captured in a deployment profile before claiming reproducible live setup. No single generic application spec can truthfully promise that every third-party endpoint works without those checks.

References checked 22 October 2026: [vLLM compatible server](https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/), [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
