# Independent specification verification

Reviewed 22 October 2026 by the separately spawned verifier agent.

**Decision: ready to guide implementation. No unresolved requirement blocker was found in the revised specification.** This is a review of the specification, not approval of a working application. No application, provider integration, automated test result, or demo recording was available for implementation verification. The verifier was spawned and briefed by the specification's author, so this is not an arm's-length audit; a human reviewer should sign off before the specification is treated as final.

## Evidence and scope

I independently extracted and read all three pages of `C:\Users\eduar\Downloads\Core Engineer Case.pdf`, read the full initial and revised `docs/build-spec.md`, and discussed the findings below with its author. The author revised the specification; I did not edit it. I checked the final prompt-context and assistant-message-ID additions after the full reread.

The PDF is assignment source material. The human's request authorizes detailed specifications and verifier discussion. It does not ask this task to implement the application or perform the PDF's eventual delivery actions.

## Requirement coverage

All evidence in this table is **planned specification coverage**, not observed runtime behavior.

| Requirement | Specification coverage | Implementation evidence still required |
|---|---|---|
| R1 Natural interview | Sections 2, 5, 6; AT1, AT2 | Real model conversation and demo |
| R2 Specific information | Section 4 defines four fields and business rules | Happy path and correction tests |
| R3 Extraction and validation | Strict extraction boundary; deterministic normalization; persistent clarification | AT2–AT5 with fake provider plus live correction |
| R4 Error handling | Bounded retry/deadline, sanitized failures, unchanged failed state | AT4–AT8 and voice failure checks |
| R5 Context | Accepted slots, saved transcript, persisted unresolved issues, restart contract | AT3, AT9, long clarification scenario |
| R6 JSON storage | Atomic replacement, copy-on-write publication, deduplication, single-process locks | AT7, AT9–AT11, corrupt-file handling |
| R7 Summary | Deterministic summary of accepted fields at review/completion | Compare saved fields, review, final summary |
| R8 Python and LLM | Python architecture and configurable provider adapter | Fresh setup, pinned dependencies, live interview |
| R9 Transcription and TTS | Validated PCM WAV upload, editable transcript, reply playback | AT12–AT14 and real STT/TTS smoke |
| R10 Git and README | Explicit source and documentation checklist | Repository and README inspection |
| R11 Video URL | Required accessible recording with fictional examples | Actual accessible URL and watched recording |
| R12 Time expectation | Five-hour sequential plan plus one-hour contingency; optional work deferred | Actual time and honest incomplete-item reporting |

## Findings discussed and resolved

| Finding | Risk in the draft | Agreed revision |
|---|---|---|
| Voice treated as optional by accident | Missing a technical requirement | Upload-based STT and TTS remain core; microphone capture was optional here but is now required by user decision (22 Oct 2026, client-side WAV encoding); streaming stays optional |
| Unresolved contradiction not stored | An old valid value could be confirmed after an ambiguous replacement | Persist `pending_clarifications`; block review/confirmation until explicitly resolved; provide issues to the extractor |
| Review token unclear | A correction or unrelated question could confirm the wrong snapshot | Define review revision separately from current revision; check both and invalidate on corrections |
| Last-turn confirmation trap | A review produced at turn 50 could never complete | Exempt final confirmation from the collection-turn cap and record the UI action as a synthetic transcript pair |
| Persistence retry could publish phantom state | Failed save might still appear committed in memory | Construct a separate copy; publish state and deduplication only after atomic persistence succeeds |
| Mixed valid/invalid candidates | One bad field could erase useful facts or permit malformed extraction | Preserve valid sibling fields only after the extraction object parses; whole malformed extraction commits nothing |
| Media dependency burden | Three audio formats and external probing threaten the timebox | PCM WAV baseline using standard-library validation; optional additional formats deferred |
| Speech endpoint integration gap | UI lacked an assistant message identifier for playback | Return assistant `message_id` from committed turns |

## Implementation gates to retain

- At the turn limit, a valid existing review must still accept an unambiguous confirmation. A correction or unrelated message at the limit must be rejected without mutation; classification does not itself commit a turn.
- A pending clarification must survive restart and a digression exceeding the 12-message prompt window. Neither UI confirmation nor a bare “yes” may bypass it.
- A question may advance session revision while preserving the review token. Confirmation must use the fresh session revision and the unchanged review token.
- Failed persistence must leave both disk and in-memory state unchanged. Retrying the same request after a lost successful response must return the committed response once.
- STT must not commit a customer turn before transcript submission. TTS failure must not roll back or duplicate a successful text turn.
- WAV validation must examine actual frame bytes, enforce size during upload, and reject unsupported/truncated/overlength files before a paid provider call.
- Mocked tests cannot establish LLM extraction quality or working voice integration. The live smoke and accessible video remain required before claiming the assignment complete.

## Accepted limits and feasibility

The architecture is proportionate for a local demonstration: one Python process, local JSON, no autonomous external actions, deterministic local RAG over a bundled corpus (no embedding service or vector store), no cross-session identity, and a narrow voice path. The five-hour estimate is plausible for an experienced builder with credentials and dependencies ready, but it is not guaranteed. Provider setup, live extraction quality, media behavior, and recording delivery can consume the contingency. The implementation must report unmet core items instead of calling them optional.

No provider/model availability, pricing, real-customer privacy policy, throughput, or production readiness has been verified. The specification explicitly defers current provider selection to implementation-time documentation checks and describes public deployment as separate work.

## Startup and diagram review addendum

Independently reviewed `startup.md`, deployment Compose/Dockerfile/configuration templates, `.dockerignore`, and `architecture.html` on 22 October 2026. **Verdict: coherent proposed deployment contract; no unresolved review blocker.**

Three blockers were discussed and verified resolved: the diagram now connects the conversation engine to provider adapters instead of making storage call the model; the unintended validation-to-configuration arrow is removed; and the container test recipe mounts development requirements under `/app`, preserving relative runtime-requirements resolution. The voice route also avoids crossing the validation box.

The contract consistently specifies one non-root Python container/worker, local-only host port, a retained JSON volume, server-side credentials, external AI capabilities, and unimplemented application prerequisites. The builder reports browser checks of view switching, component selection, narrow-screen overflow, JavaScript errors, and desktop/startup screenshots; I did not independently repeat those browser checks.

Docker is unavailable in this environment. Image build, Compose startup, volume permissions, health behavior, test installation, and live application/provider operation remain unverified. The templates are explicitly not runnable until application source, pinned requirements, and valid provider configuration exist.

## Detailed wiring and strict-quality review addendum

Independently reviewed the implementation and testing contracts, the HTML build/quality views, the root `SPECS.md` reading order, and the updated deployment settings. **Verdict: ready to guide a separate implementing model; no unresolved blocker identified in this review.** This does not promise that every omission has been found.

The expanded contract now explains dependency construction, hosted-model SDK calls, schemas, deterministic merge/reply rules, text transactions, STT drafts, saved-reply TTS, browser controls, failure ownership, and implementation increments. It explicitly distinguishes writing the Python conversation engine from training or serving a model. The dated TTS baseline has an explicit shutdown boundary and requires contract revision rather than silent substitution.

Review discussion resolved these additional blockers:

- JSON UUID handling is explicit rather than relying on blanket strict validation that can reject valid wire strings.
- Every concrete function in the mutation scope needs executed-test and framework-inventory evidence, preventing a positive mutant total from hiding uncalled functions.
- Independently approved equivalent and invalid mutants have consistent, separate accounting; neither is counted as a kill.
- Error messages, retry flags, startup failures, lock acquisition limits, and cancellation behavior are specified.
- Uvicorn's 40-second graceful shutdown and Compose's 45-second stop grace accommodate the 35-second STT budget.
- TTS uses the SDK streaming-response context for bounded chunk reads before returning complete MP3 bytes to the browser; the byte cap is enforceable during download.

Strict test-first RED/GREEN evidence, pytest unit/integration tests, real-server Playwright E2E, and Linux/WSL **mutmut-generated** mutation tests are mandatory. The implementing model must not hand-edit production code to imitate mutants or build a replacement mutation engine. Report/inventory scripts inspect framework evidence only. The original assignment estimate cannot waive these expanded gates.

No application quality gate has run in this specification task: no dependency set was jointly installed, no application unit/integration/E2E suite or mutmut run executed, and no Docker/live-provider integration verified. Those remain delivery blockers until real evidence exists. Package metadata and document review are not substitutes for execution.

## Mandatory RAG review addendum

Reviewed `rag-contract.md` and its implementation/testing/startup/HTML cross-references. **Verdict: no unresolved specification blocker identified.** RAG is user-added mandatory R13; the PDF's optional-bonus wording does not limit the user's request.

The contract specifies actual retrieved-context generation: a bundled versioned corpus, deterministic top-two lexical retrieval, a separate structured model answer, validated source IDs, persisted citation snapshots, and explicit abstention. It preserves intake fields, review tokens and summaries, and shares one outer deadline across extraction and answering. No embedding service or additional container is implied.

One blocking ambiguity was resolved in discussion: a single extraction intent could not distinguish an ordinary intake statement from facts mixed with a question. Mandatory strict `has_question` now drives explicit pure-question/mixed-message routing; confirmations require it to be false. Prompt examples, fakes and tests must adopt the extended schema.

AT17–AT19, E09–E10, real-provider RAG smoke, and retrieval-function mutation/inventory checks are required alongside the existing strict TDD gates. Citation membership validates provenance, not semantic entailment; real answers still require policy-fact and abstention evaluation. No RAG implementation, tests, model call or quality gate has run in this task. This review does not certify runtime grounding or promise zero remaining omissions.

## Independent backend compatibility review addendum

Reviewed `provider-compatibility.md`, the per-role environment example, and corresponding builder-entry/startup/HTML changes. **Verdict: no blocking specification contradiction identified.** This controlling revision supersedes historical hosted-model names, SDK methods, shared-key assumptions and the universal TTS cutoff; earlier review approval of that dated baseline is historical only.

Extraction, grounded generation, transcription and speech have independent URL/model/auth profiles. Atomic all-or-nothing RAG inheritance avoids leaking an LLM credential to a newly configured answer host. The generic httpx contracts define explicit structured-output modes, endpoint/payload parsing, audio limits, no redirects or silent fallback, offline startup and capability-specific compatibility evidence. These support compatible implementations that satisfy the contract; they do not certify every server or modality marketed as OpenAI-compatible.

One smoke-test ambiguity was discussed and resolved: compatibility evidence must include at least two RAG cases, a supported seeded policy and a retrieved-but-unsupported question requiring abstention. The strict TDD, unit/integration, Playwright, framework mutation, RAG and audio gates remain mandatory across this revision.

The builder reports six-view HTML interaction and narrow-screen checks, no JavaScript errors, and visual inspection of the independent-RAG diagram; I did not independently repeat those browser checks. No actual backend calls or interoperability tests were run. Endpoint availability, served model quality, audio decodability, credentials, deployment profiles, dependency resolution and application quality gates remain unverified.
