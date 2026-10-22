# Fresh independent source-to-specification verification

Date: 22 October 2026. Scope: specification audit only.

## Verdict

The specification covers the original assignment and the supplied user additions in substantial detail, but is **not yet an unambiguous implementation handoff**. Resolve finding 1 before implementing review rejection/recovery; correct finding 2 before presenting startup commands as reproducible. Findings 3–4 are concrete hardening/clarity gaps. No application, test suite, framework mutation run, container, live backend, README deliverable or video has been verified as working.

This audit independently extracted and read all three pages of `C:/Users/eduar/Downloads/Core Engineer Case.pdf` using pypdf. It read SPECS.md, build-spec.md, implementation-contract.md, testing-contract.md, rag-contract.md, provider-compatibility.md, startup.md, architecture.html, Dockerfile, compose.yaml, deployment/.env.example, .gitignore and .dockerignore. It did **not** read or rely on docs/spec-verification.md. References below use repository-relative paths with verified one-based lines. They describe contractual behavior, not observed application failures. No live credentials, provider requests or dependency installations were used.

## Original client requirement matrix

“Covered” means specified, never implemented or passed. Page 1's objectives also identify evaluation dimensions; they do not prescribe additional frameworks or production-scale infrastructure.

| PDF page | Explicit requirement, deliverable or expectation | Specification coverage | Assessment |
|---|---|---|---|
| 1 | LLM integration and prompt engineering | implementation-contract §§6–7; provider-compatibility §§3–5 | Covered: literal extraction prompt, structured parsing and retries; live quality pending. |
| 1 | Data extraction and structured output | build-spec R2/R3; implementation-contract §§4–5 | Covered: four-slot schema and independent deterministic validation. |
| 1 | Conversation flow management | implementation-contract §§5,7,9 | Covered, with rejection recovery ambiguity in finding 1. |
| 1 | Error handling and edge cases | build-spec AT3–AT16; implementation-contract §§7,11 | Covered: bounded failures, state preservation and exact safe errors. |
| 1 | Code quality and testing | testing-contract §§1–12 | Covered and greatly expanded by user requirements; execution pending. |
| 1 | System design and scalability considerations | build-spec §§3,9; implementation-contract:74; startup runtime/hosting | Covered: one process, process-local locks, atomic JSON, explicit shared-storage prerequisites for scaling. No scale benchmark claimed. |
| 1 | Optional RAG implementation | rag-contract; build-spec:232,251–253 | Deliberately promoted to mandatory by the user; not a missing client requirement or a client-mandated vector database. |
| 1 | Create conversational AI for a fictional business | build-spec:9–11; implementation-contract:9 | Covered: fictional Northstar Shop support intake. |
| 1 | Short customer interviews to gather information and provide appropriate responses | build-spec §§1–2; implementation-contract §§5–6; rag-contract §5 | Covered: missing-field questions, review, grounded policy responses, limits. Mixed fact/question turns require a second message, an explicit design restriction. |
| 1 | Choice of support, scheduling or another information-gathering function | build-spec:9 | Covered by choosing support; other business functions are examples, not required features. |
| 1 | Natural conversation | build-spec R1, AT1–AT2; implementation-contract §§5–6 | Specified: arbitrary field order, multiple facts, correction and context. Fixed replies alone do not prove naturalness; inspect real conversations. |
| 2 | Collect specific information; support example order/category/description/urgency | build-spec §4; implementation-contract:81–89,119–136 | Covered using those four example fields. NS format, enumerations and lengths are project decisions, not PDF rules. |
| 2 | Extract and validate into structured format | implementation-contract §§4–5; testing-contract unit/integration cases | Covered, including invalid siblings, ambiguity, leading zeros and corrections. |
| 2 | Gracefully handle basic invalid input and errors | build-spec AT4–AT8, AT13–AT16; implementation-contract §11 | Covered; exact infrastructure execution pending. |
| 2 | Maintain context throughout interaction | build-spec R5/AT9; implementation-contract §§4,6–9 | Covered: transcript, slots, pending issues, latest 12 messages plus accepted facts, restart/resume. Bounded prompt context is not full-history prompting. |
| 2 | Store conversations and extracted data as JSON | build-spec §4; implementation-contract §8 | Covered: one validated JSON session, transcript, slots, summary, replay and confirmation. No database required by source. |
| 2 | Basic validation of collected information | implementation-contract:119–136 | Covered. Description check is length/category-label validation, not a semantic guarantee. |
| 2 | Summary with extracted key points | implementation-contract:165; build-spec R7 | Covered: deterministic rendering of all four fields; an additional LLM summary is unnecessary. |
| 2 | Python implementation | implementation-contract:9,41; Dockerfile | Covered as planned Python 3.11 runtime. No Python application exists yet. |
| 2 | Proper error handling | implementation-contract §§7,11; testing-contract §4 | Covered in specification. |
| 2 | LLM of choice, considering use case | provider-compatibility §§1–5; build-spec:227 | Covered: configurable per-role models, README rationale, quality/compatibility smoke. No actual model selection verified. |
| 2 | Conversational AI platform/model for text transcription and TTS | build-spec R9/§8; implementation-contract:192–202; provider-compatibility §§3–5 | Covered conservatively by real upload STT and reply TTS. The source does not require telephony, streaming, microphone recording or duplex voice. |
| 2 | Optional RAG using a provided knowledge base, e.g. common support issues | rag-contract §§1–4 | Covered by a specified bundled fictional four-record corpus; PDF supplies no actual knowledge file. Mandatory status comes from user. |
| 2 | Optional sentiment analysis for frustration | build-spec:43,232 | Explicitly deferred; empathetic wording does not falsely claim sentiment analysis. |
| 2 | Optional multi-turn conversation memory | build-spec:232; implementation-contract §§4,6 | Within-session memory already satisfies mandatory context. Cross-session identity/memory is optional and deferred. |
| 2 | Optional multiple languages | build-spec:232; rag-contract prompt | Deferred; English baseline is permitted. |
| 2 | Source code in a Git repository | build-spec:226; testing-contract:11 | Required future deliverable; source has not been implemented. |
| 2–3 | README setup instructions | build-spec:227; startup.md | Required future README, with exact configuration/start instructions; finding 2 affects one proposed test command. |
| 3 | README system architecture overview | build-spec:227; architecture.html | Required future README; explanatory architecture exists, not the finished application README. |
| 3 | README key design decisions | build-spec:227; contracts | Covered: validation/state ownership, local JSON, fixed voice formats, lexical RAG, backend roles. |
| 3 | README potential improvements | build-spec:227,232 | Covered by explicitly deferred features/scaling limitations. |
| 3 | URL to demo video showing sample conversations and capabilities | build-spec:229; rag-contract:105 | Mandatory future accessible URL; video must show voice and mandatory RAG as well as core intake. No URL or recording verified. |
| 3 | 4–6 hours for core; additional time for bonuses | build-spec:220; implementation-contract:226; testing-contract:181 | Retained as original expectation, explicitly superseded as a delivery budget by added quality/RAG scope. No honest basis to promise the expanded work fits 4–6 hours. |

No explicit client requirement was found to require public hosting, real order validation, refunds, external tickets, authentication, model training, an agent framework, embeddings, vector storage, a frontend framework, Docker or mutation testing. These are either deliberate project choices, user additions, or out of scope.

## User additions

| User requirement supplied for this audit | Coverage and limits |
|---|---|
| RAG mandatory | rag-contract:3,9–11,93–105; R13/AT17–19/E09–10. Actual retrieved context goes into a separate generation call, with local citation membership checks and abstention. Valid IDs do not establish semantic truth. |
| Strict TDD at unit/integration/Playwright layers | testing-contract:7–11 requires actual assertion RED before behavior and GREEN/regression evidence, logs and source revisions. Green tests alone cannot prove test-first history. |
| Real framework-generated mutation tests | testing-contract:127–149,188–193 requires mutmut 3.8.0 on Linux/WSL, seven-module inventory, independently reviewed exclusions and kills of every non-equivalent generated mutant. No manual mutants/custom engine allowed. |
| Independently selectable LLM/RAG/STT/TTS compatible servers and models | provider-compatibility:7–26,30–56,68–78 and .env.example specify four role profiles, separate clients/keys, atomic optional RAG inheritance and exact HTTP contracts. Local open-source/no-auth use is allowed. Support is profile-specific, not universal compatibility. |
| Eventual full implementation | Explicitly future work in SPECS.md:3 and startup.md:3. This repository is a specification package. This audit grants no implementation signoff. |

## Prioritized actionable findings

### 1. [P1] Rejection recovery has conflicting reply precedence

Evidence: `docs/implementation-contract.md:138–142`. Rule 2 handles **any** actual field change, pending issue or non-null candidate, then creates a fresh review if complete. Rule 3, below it, says an accepted explicit correction while `review_rejected=true` clears that flag. The same line also forbids review/confirmation while that flag remains true.

Concrete case: collect all fields → review → customer rejects → persisted rejected flag becomes true → customer explicitly changes urgency to high. This meets rule 2 before rule 3. A literal priority-based implementation can either emit a review with a still-rejected flag or refuse review indefinitely. Both conflict with intended rejection recovery. A reaffirmation returning equal candidates also meets rule 2 rather than the lower-priority reset instruction.

Small fix: specify flag recovery as a state transition **before** reply selection, with explicit cases for accepted corrections, invalid corrections, equal-value reaffirmation and no-fact reaffirmation. Then choose a reply subject to the resulting flag. Add a test that rejects, changes a field, receives a fresh review and completes only on a later confirmation. This is a same-document conflict; transport/RAG override precedence does not resolve it.

### 2. [P2] The advertised container “mocked tests” command collects unsupported suites

Evidence: `docs/startup.md:58` invokes `python -m pytest /checks -q` after installing development Python packages into the runtime image. `docs/deployment/Dockerfile:1–11` installs runtime packages only and no browser/system dependencies. `docs/testing-contract.md:36–39,79–89` requires real Chromium E2E; `docs/testing-contract.md:159` separately installs Chromium with system dependencies. Passing `/checks` explicitly bypasses the default unit/integration testpaths restriction at `docs/testing-contract.md:105` and discovers tests/e2e as well.

Concrete case: follow that startup command after implementing the specified tree. It collects browser tests but has no installed Chromium/browser dependencies, so the proposed command does not reproduce the claimed mocked test path. It also inherits the real Compose env/volume despite the adjacent isolation warning.

Small fix: make this command explicitly run `/checks/unit /checks/integration` with isolated injected settings, or remove it in favor of the authoritative quality commands. Keep E2E in its dedicated browser-enabled environment. Do not present this as a successful run; none was attempted here.

### 3. [P2] Generic non-audio response/request byte bounds are unspecified

Evidence: `docs/provider-compatibility.md:37,44–54` defines LLM/RAG JSON and STT response parsing but no transport byte cap; its explicit bounded streaming rule at line 56 applies to TTS. `docs/implementation-contract.md:78` checks text length after JSON parsing, while lines 192–194 prescribe a pre-buffer upload byte guard only for multipart audio. Line 280 says bounded response reading remains required without supplying a JSON-response bound.

Concrete case: a configured compatible server returns a huge `choices` envelope or huge STT `text` before the application validates content, or a local caller sends a huge JSON body whose text is ultimately rejected. A straightforward buffered HTTP/JSON implementation can allocate far beyond the stated small-demo limits before validation. Model max_tokens and wall-clock deadlines are not HTTP byte limits.

Small fix: choose explicit finite byte limits for incoming JSON and provider JSON/error bodies, enforce while reading before parsing, define safe error/retry behavior and add chunked/missing-Content-Length tests. This is an underspecification/hardening risk, not an observed exploit or an original PDF-specific mandate.

### 4. [P2] Stale operative clauses still make the handoff depend on distant overrides

Evidence: `docs/testing-contract.md:36,41,70–71` still demands SDK serialization and a TTS expiry boundary, while `docs/testing-contract.md:203` and `docs/provider-compatibility.md:26,30,73` remove those requirements. `docs/rag-contract.md:72,98` still names a fixed Responses call/store=false test, while provider-compatibility:40,44 forbids Responses/store fields. `docs/build-spec.md:184` says all automated tests use a fake provider, while testing-contract:36 and provider-compatibility:73 require actual adapter HTTP serialization. `docs/build-spec.md:241` still adds PROVIDER_API_KEY while provider-compatibility:20 forbids global-key lookup. `docs/startup.md:13,25` says external HTTPS calls and suggests optional async-only testing dependencies despite mandatory split local HTTP backends/full test pins. `docs/architecture.html:19,141,162` retains hosted-call/optional-test/HTTPS-only explanatory wording.

The latest overrides make most intended decisions recoverable; these are **precedence traps**, not evidence that hosted OpenAI is still the intended architecture. A weak builder that copies a local “exact” section can nevertheless write forbidden SDK calls/tests or reject valid local servers. The large final addenda do not erase that practical risk.

Small fix: consolidate the current canonical signatures, schemas, request serialization, test boundaries and settings inline; remove superseded executable examples/prohibitions or mark each directly obsolete at its location. Preserve historical rationale separately. Do not drop a mandatory user quality gate while doing so.

## Exact core behavior the implementation must deliver

### Conversation and interface

One Python FastAPI service serves a same-origin accessible HTML/JS chat. Four required validated fields: normalized NS- plus six ASCII digits, five defined categories, 10–2000-character problem description and explicit low/normal/high urgency. Python owns accepted state, clarification persistence, field corrections, review tokens, immutable completion and deterministic summary. The model proposes facts/intent and cannot perform business actions. Every successful turn atomically stores user/reply/state/replay/revision; failed extraction, RAG or storage does not partially commit. Refresh restores persisted context; retry reuses the original payload and ID. Review confirmation, local-only receipt, New intake, visible errors, safe literal rendering and 390-pixel layout are required.

### Voice

Required baseline is **file upload STT plus opt-in saved-reply TTS**, not streaming conversation. Accept exactly one validated PCM WAV: 16-bit, 1–2 channels, 8–48 kHz, positive duration ≤60 seconds, ≤10 MiB file and ≤10 MiB+64 KiB multipart total. Validate frame completeness and silence threshold (<128 peak absolute sample is silent) before provider access. STT output is an editable draft; sending uses the same intake path and no earlier session mutation. Play synthesizes an existing assistant message, never arbitrary supplied text. MP3 is ≤5 MiB, audio/mpeg/no-store, finite/decodable and actually plays in browser tests. STT failure leaves chat available; TTS failure preserves saved text. Handles/object URLs are cleaned up; voice is disclosed as AI-generated. Microphone recording, MP3/WebM upload, conversion, interruption and telephony are optional future work.

### Mandatory RAG

The exact four-record fictional JSON corpus ships in the image and fails startup if invalid. Deterministic local NFKC/casefold tokenization, fixed stop words, keyword qualification, specified score and ID tie-break retrieve at most two records. Only a pure extracted question with no candidate facts/corrections/ambiguities routes to grounded generation. Mixed fact/question turns process intake and explicitly ask for a separate policy question. No-match questions abstain without a generation call; retrieved-but-unsupported questions require model abstention. Answers cite only retrieved IDs; snapshots store title, whole body and KB version. Citations survive replay/resume, do not replace the intake summary and are visibly inspectable. Policy questions preserve an existing review token, while revision/count still advance. The model must never invent missing policy or promise an action. Citation validation proves membership, not entailment.

### Backend roles and budgets

LLM extraction and RAG generation use independently configured Chat Completions servers/models; each has json_schema/json_object/prompt_json mode. STT uses /audio/transcriptions; TTS uses /audio/speech requesting MP3. Each role independently selects URL/model/auth; only all-empty RAG atomically inherits the complete LLM profile. No shared-key fallback, endpoint/model/output-mode fallback, redirect following, model allowlist, arbitrary expiry or mandatory hosted account. Role choice never comes from customer input. Local HTTP/no-auth servers are permitted; compatible audio remains an explicit capability requirement.

Global provider capacity defaults to four, acquired per logical operation; per-session lock acquisition is bounded to two seconds. Extraction and answer share a 25-second outer deadline, at most two attempts each/four total, ten seconds maximum per text attempt. STT has two 15-second attempts within 35 seconds; TTS one 20-second attempt. Retries/reads/jitter consume budgets. Shutdown allows 40 seconds with Compose grace of 45 seconds. Exact selected backend/model/voice and quality must pass explicit live compatibility checks; generic interface branding is insufficient.

### Tests and hosting

Strict assertion-level RED/GREEN evidence at each layer is mandatory. Unit/integration tests use actual domain/storage/routes and controlled external transport, with no real network. Playwright E01–E11 use Chromium, real TCP Uvicorn, actual UI/API/engine/retrieval/JSON and only a server-side AI fake. Playback assertions exceed HTTP 200. Unit/integration coverage target is 95% for the configured measured files. mutmut 3.8.0 exclusively generates/runs mutations on conversation, validation, storage, audio, providers, config and retrieval; inventory catches uncalled functions and all meaningful mutants must be killed or independently adjudicated. Excluded wiring/schemas/prompts are not falsely called mutation-tested. CI and evidence tooling remain future implementation work.

Hosting is localhost, one application container/process/worker and persistent /data/sessions named volume. Compose publishes 127.0.0.1:8000; Uvicorn binds 0.0.0.0 inside the container. Four model roles are external services managed separately, optionally on the Docker host via host.docker.internal (Linux host-gateway mapping documented), same-network DNS, or remote HTTPS. No model-serving container is automatically provisioned. Health probes process/writable storage/loaded KB without model calls; it cannot establish backend readiness. Public production deployment requires a separate authentication/ownership/security design and is not part of the client core.

## Feasibility and remaining underspecification

- The original 4–6 hours does not cover a mandated multi-layer TDD history, seven-module 100% meaningful mutation gate, evidence exporters/checkers, container verification, live four-role compatibility, RAG evaluation and video. Re-estimate after the first tested slice; report actual time rather than weaken gates. A 30-minute mutation CI cap (`testing-contract.md:172`) may fail on a large implementation; there is no evidence yet it is sufficient. Failure must remain incomplete, not be waived.
- The exact runtime/dev pins are explicitly not jointly resolved (`implementation-contract.md:41`, `testing-contract.md:29`). Dependency installation and pip check are real future gates. This audit did not repeat package metadata checks or promise current backend availability.
- No actual backend server versions, model weights, launch profiles, voices or audio decoder validation method have been selected. The contract correctly assigns these to deployment compatibility evidence, but reproducible live setup remains blocked until supplied/tested. Requiring only MP3 compatible TTS narrows eligible open-source backends; it does not mean all open-source voice servers work.
- Mutation inventory records test-node mappings, but aggregate coverage JSON alone cannot prove which test executed each function (`testing-contract.md:188–193`). The implementing evidence exporter should capture actual test execution/coverage contexts or equivalent framework evidence, rather than accept self-authored mappings as proof. Independent review remains essential.
- Model quality is inherently still a live acceptance question: extraction may misclassify a customer assertion, and a well-shaped cited answer may hallucinate. The specified seeded/adversarial cases are appropriate checks, not a universal correctness guarantee.
- Four lexical FAQ records are adequate for this bounded exercise; embeddings, vector databases, queues, agent frameworks, model training, public hosting and multiple workers would add unnecessary scope. The detailed reviewer/inventory tooling is additional cost chosen for the user's strict quality request, not a client requirement. Do not silently expand it beyond the stated evidence needs.

## Remaining blockers and audit limits

Specification handoff: resolve finding 1, correct the known startup test command, and preferably consolidate conflicting operative text before handing this to a weak implementing model. Choose the missing JSON byte bounds explicitly during that cleanup. The source/client requirements are otherwise substantially represented; optional bonuses are correctly distinguished from user additions.

Application delivery: **all implementation/runtime gates remain open**. There is no app/, requirements.txt, real test evidence, complete resolved dependency set, image execution, profile-specific live quality/audio proof, finished README/source deliverable or accessible demo video in the reviewed specification inventory. The deployment files are templates that intentionally reference future files. Nothing in this report certifies application behavior, TDD history, mutation completeness, model availability, public hosting or production safety. No specs or previous verification report were modified by this audit.

