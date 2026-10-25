# Strict TDD and quality contract

User requirements added 22 October 2026: unit tests, integration tests, Playwright end-to-end tests, strict TDD, and **framework-generated mutation testing**. All are mandatory. This document defines required checks; README.md and reports/ record observed runs and remaining gaps.

## 1. Non-negotiable workflow

For each behavior slice, write an executable test first → run it and capture the intended assertion failure (RED) → implement the minimum behavior (GREEN) → run the relevant regression suite → refactor while green. Test scaffolding may precede RED; implementation behavior may not. An import error may establish initial scaffolding work, but is insufficient evidence for subsequent behavior slices. Do not write the whole app then backfill tests. Do not weaken assertions or edit expected behavior to turn a failing implementation green. Fix a mistaken test only with an explicit requirement explanation in the evidence ledger.

At each layer, add its test before that layer's behavior: unit tests before pure domain rules, integration tests before route/provider/storage wiring, browser tests before browser flows. Already-green lower-layer code need not be broken artificially to make a new higher-layer test fail. A new browser feature must fail at the browser assertion before the UI implementation is added.

Record each slice in `reports/tdd-ledger.json` with `{id, requirement_ids, test_nodeids, red_command, red_exit_code, red_log, green_command, green_exit_code, green_log, source_revision}`. Log paths are repository-relative artifacts, logs must exist, RED exit is nonzero and GREEN zero. Logs identify the specific expected failing/passing behavior. If Git is available, commit test plus RED evidence before production implementation, then commit GREEN code. If the repository is not yet initialized, initialize it for the assignment source deliverable; do not fabricate historical commits or test output. The verifier inspects ledger, actual logs and diffs; a green suite alone cannot prove TDD history.

## 2. Frameworks and dependencies

Python 3.11 on Linux is the quality reference environment. Windows can run pytest and Playwright, but **mutmut requires WSL/Linux fork support**. Use WSL 2 or Ubuntu CI for mutation; no hand-built substitute.

`requirements-dev.txt` exact direct entries after `-r requirements.txt`:

```text
pytest==9.1.1
pytest-asyncio==1.4.0
pytest-cov==7.1.0
httpx==0.28.1
pytest-playwright==0.10.0
playwright==1.64.1
mutmut==3.8.0; sys_platform != 'win32'
```

Package metadata was inspected during specification. These are not yet a jointly verified lock; installation, `pip check` and the whole suite are acceptance gates. Capture all transitive versions in the resolved lock described by implementation-contract. Install Chromium with `python -m playwright install --with-deps chromium` on Ubuntu/WSL, or `python -m playwright install chromium` on native Windows after dependency installation. No JavaScript test framework or npm toolchain is required.

## 3. Test boundaries and doubles

| Layer | Real components | Replaced dependencies | Never replace |
|---|---|---|---|
| Unit | Pure validators/merge/reply/state decision functions; adapter retry logic | Clock, jitter, provider, failure points as needed | The rule being asserted |
| Integration | FastAPI routes/lifespan, engine, JSON repository, temporary directory; real application HTTP serialization for adapter tests | httpx transport, external AI service | Engine, validation, JSON writes in transaction tests |
| E2E | Chromium, shipped UI, real Uvicorn TCP server, routes, engine, temp JSON store | Server-side AIProvider fake; only the explicitly enumerated environmental-fault fixtures | Successful browser API routes, persistence, confirmation, frontend request logic |
| Live smoke | Real app and real provider | No AI fake | Provider calls and voice playback |
| Mutation | Real mutmut framework executing unit/integration tests against generated variants | Same deterministic external-service doubles | Mutation generator/runner; no model-authored source hacks |

Unit and integration tests must run without any real service credential and must fail immediately if an unexpected external request is attempted. HTTP transport doubles return realistic provider wire responses; mocking `OpenAICompatibleProvider.extract` does not count as an adapter integration test. Test actual request JSON/multipart, model IDs, parsed response, refusal/truncation and error mapping. Use an injected sleep function to avoid real backoff delays. Avoid real provider calls in PR jobs.

E2E provider fake is a separate test-only implementation in `tests/support/fakes.py`. It receives context and has deterministic responses for each test's exact message/audio fixture, supports a one-shot recoverable failure when configured by fixture, and raises on any unexpected call. It must not duplicate business validation or state decisions. Fake STT returns `Order NS-000123 arrived broken and this is urgent`; fake TTS returns SynthesizedAudio with finite playable MP3/audio-mpeg or PCM-WAV/audio-wav fixture bytes and the corresponding MIME type; follow the TTS format coverage amendment in implementation-contract.md. Keep it under `tests/fixtures/reply.mp3` with source/licence note and SHA-256 in fixture metadata. Create it once during test-fixture preparation using `ffmpeg -f lavfi -i sine=frequency=440:duration=1:sample_rate=24000 -ac 1 -c:a libmp3lame -b:a 64k tests/fixtures/reply.mp3`. This encoder is fixture-authoring tooling, not an application, CI or runtime requirement once the fixture is committed. Generate a 1-second 16-kHz mono 16-bit WAV with peak amplitude 1000 for valid upload tests using Python wave/struct; generate silence with zeros, and boundary fixtures with the same generator's explicit rate/channel/width/duration parameters. Record both commands/generator and SHA-256s in tests/fixtures/README.md. No WAV-as-MP3 labeling is allowed.


## 4. Integration/E2E coverage authority

The actual test specifications are in **implementation-contract.md section13**: 185 individually identified integration cases across groups IT01–IT14 and ten complete Playwright journeys the ten browser journeys. AUTH-01 adds eleven supplemental integration cases AUTH-I01–AUTH-I11; This layer adds seven supplemental integration cases OA-I01–OA-I07 for the private Backend API boundary. These extend the inventory and do not replace or renumber IT01–IT14. Extend complete_intake for the mock order-verification flow, including Backend API outage and recovery; do not add another full-browser journey. They replace the earlier100-browser-case matrix. Unit-test design for AUTH-01 and is the implementing model's responsibility while doing strict TDD; unit/coverage/mutmut gates remain mandatory. Do not recreate100 fullbrowser flows and relabel them integration.

Use section13 for exact fixture seams, scripted inputs, journey actions/assertions, backend-vs-browser boundaries and required evidence. Integration tests execute real Python API/engine/retrieval/storage with controlled external HTTP; Playwright journeys execute the real UI/server path and assert browser-specific draft/media/retry/rendering behavior. Invalid field combinations, state permutations, message-limit edges, codecs, retrieval/ranking/citation failures and protocol/auth/deadline boundaries belong primarily to integration or model-designed units.

The E2E manifest/evidence checker must read journey IDs the ten browser journeys from implementation-contract.md section13; no gate requires the retired100 IDs. Keep all185 concrete ITxx-yy IDs plus AUTH-I01–AUTH-I11 and OA-I01–OA-I07 collected/executed and mapped separately in the integration requirement mapping. Do not silently skip a journey or backend branch.

The gateway-to-Backend-API boundary is real-boundary coverage: integration must serve the shipped `backend_api.main` app with real Uvicorn on a loopback port and drive it through `BackendApiClient`; stopping and restarting that server must prove the fixed safe outage errors and recovery. Unit tests may use a protocol-faithful `httpx.MockTransport` only for the client's transport/status mapping; an in-process Backend API replacement is not accepted as integration coverage. The gateway origin must be proven not to expose `/internal/v1/...`, and Compose must publish no host port for `backend-api`.

The separate LLM semantic-judge suite is not a substitute for the ten journeys. It must contain 23 judge-asserted browser checks: seven baseline routing cases (greeting, thanks, social-prefaced facts, social-prefaced policy question, capability, unsupported question and memory-aware explicit resume after an issue, FAQ detour and page refresh), ten FAQ interruptions (partial issue intake, after category/before urgency, pending review, rejected review, pending verification, wrong verification code, verified/before protected lookup, after protected order result, partial intake after reload, and unsupported FAQ during verification), and six frustration-to-handoff cases (during issue intake, during a supported FAQ, during pending verification, at review, on an unrelated/off-topic turn, and in Spanish). Every FAQ case asserts deterministic response, citation/abstention, relevant UI state and `order_verification_required`; each handoff case asserts the fixed localized demo-only transfer disclosure, terminal `handed_off` status, preserved existing slots, locked composer and no RAG/order lookup. The resume judge case additionally verifies the original session ID, persisted transcript/slots/revision across refresh, and that the resumed model context includes prior issue and FAQ turns. The LLM judge assesses the visible final response only. Keep the rubric/prompt version in artifacts and do not count these as extra J journeys.

## 5. E2E server fixture lifecycle

`tests/e2e/conftest.py` uses a function-scoped fixture to start a real Uvicorn server in a background thread with an already-bound loopback socket on port 0. Pass the socket to `Server.run(sockets=[socket])` so there is no free-port race. It serves `create_app(settings=test_settings, provider=test_fake)`. Wait for `/health` using bounded polling (10-second startup maximum); on teardown set `server.should_exit`, join within 10 seconds and fail cleanup if it remains alive. Each test gets a fresh browser context, isolated temporary SESSION_DIR, and independent app/server/fake instance. Run test cases serially. A case may open the two pages or overlapping provider requests explicitly required by its row, using the same controlled app fixture.

The tests/e2e pytest hook writes actual collection and setup/call/teardown outcomes to the two e2e evidence JSON files used by the inventory gate; include browser name, exact nodeid/case ID, statuses and rerun count. These are runner-generated observations, not manually filled completion flags. No production environment flag exposes the fake provider; dependency injection happens in the test fixture only. `localStorage` is isolated per browser context. Bound all HTTP calls and test waits; E2E timeout maximum 30 seconds per browser assertion. Restart/resume tests stop and recreate the server against the same temp directory while preserving the browser context.

## 6. Pytest and mutmut configuration

Write this configuration in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests/unit", "tests/integration"]
asyncio_mode = "auto"
addopts = "--strict-markers --strict-config"
markers = ["live: paid provider smoke, requires explicit opt-in"]

[tool.coverage.run]
branch = true
source = ["app"]
omit = ["gateway/main.py", "gateway/routes/endpoints.py", "gateway/routes/http_support.py", "gateway/routes/asgi_guards.py", "gateway/schemas.py", "gateway/providers/prompts.py", "gateway/errors.py"]

[tool.coverage.json]
show_contexts = true

[tool.coverage.report]
fail_under = 95
show_missing = true

[tool.mutmut]
source_paths = ["gateway/", "backend/"]
do_not_mutate = ["gateway/__init__.py", "gateway/main.py", "gateway/routes/endpoints.py", "gateway/routes/http_support.py", "gateway/routes/asgi_guards.py", "gateway/schemas.py", "gateway/providers/prompts.py", "gateway/errors.py"]
pytest_add_cli_args_test_selection = ["tests/unit", "tests/integration"]
also_copy = ["tests/", "pyproject.toml"]
mutate_only_covered_lines = false
```

Mutate all eligible functions in **conversation.py, intent.py, plans.py, replay.py, validation.py, storage.py, audio.py, providers.py, provider_wire.py, speech_wav.py, config.py, retrieval.py**, including uncovered lines. Main wiring/errors/schema declarations/prompt literals remain covered by integration assertions and are explicitly outside mutmut scope. Do not describe those excluded files as mutation-tested. mutmut 3's mutation operator scope does not prove module-level schema/prompt constants; write direct assertions for their contracts. A coverage percentage is not proof of test strength.

## 7. Mutation framework gate

The model must never hand-edit production functions to generate mutations, create a custom mutation runner, use monkeypatch as a substitute, or manually claim a mutant was killed. **mutmut 3.8.0 owns mutation generation, isolation, test selection and execution.** Test mocks are allowed only for ordinary deterministic dependencies. Do not run `mutmut apply` in the working source tree as part of routine mutation testing; use the framework's isolated execution and `mutmut show` to inspect generated diffs.

Run from repository root in Linux/WSL after the baseline suite is green:

```bash
mkdir -p reports
python -m pytest tests/unit tests/integration -q
mutmut run --max-children 2
mutmut results
mutmut show <framework-generated-mutant-id>
```

Capture `mutmut run` output and `mutmut results` output under reports; on CI tee commands with `set -o pipefail` so a failed command cannot become green through logging. `mutmut results` is a report, not an assumed failing-quality exit code. Implement `scripts/check_mutation_report.py` only as a **gate over framework output**, never as a mutation generator: parse status lines `<id>: <status>` from the captured report, reject malformed/nonempty unrecognized output, fail if any non-killed status is not either a verifier-approved equivalent survivor or verifier-approved invalid/incompetent generated variant in the review manifest. Invalid variants are reported separately and excluded from the meaningful-mutation denominator, never counted as killed. Also reject an empty/zero-generated run by checking the run summary has a positive generated/executed count; save that evidence. Implement parser tests first with fixtures from the installed pinned framework's actual output, not guessed summaries. If the framework changes its output, fail closed and revise the adapter tests.

Gate: **100% of non-equivalent generated mutants in the required scope must be killed by tests**. No unreviewed surviving, uncovered/untested, timed-out, suspicious, skipped, crashed, interrupted, or unexecuted mutant counts as a kill or a pass. A timeout must be investigated/rerun; it cannot improve the score. Invalid/incompetent generated mutations must be listed separately with framework ID/diff/tool evidence and independent verifier approval; never silently remove them from totals.

Equivalent survivors require a written argument that no observable behavior changes for any allowed input, exact framework ID/diff hash, source revision, test evidence, and verifier decision in `reports/mutation-review.json`. Builder may propose equivalence but cannot approve its own exclusion. Non-equivalent survivors require a meaningful behavioral test, then rerun that framework mutant and the affected suite. Never add assertions about source text or implementation internals just to kill a mutant. Never add blanket `pragma: no mutate`, shrink mutation scope, alter thresholds or increase timeouts merely to obtain green. A necessary change in scope requires explicit requirement rationale and verifier review.

Fresh CI checkouts have no mutation cache. Local changed-test/config runs must invalidate old mutation results or rerun in a fresh isolated working copy; do not reuse stale green results. Save framework version, required paths, full run log, non-killed report, reviewed exclusions, final generated/killed/equivalent/invalid/unresolved counts and source revision in `reports/mutation-summary.json`. This summary is derived from framework evidence and does not replace it. If infrastructure/time budget prevents a full run, mark the mutation gate incomplete and keep delivery blocked.

## 8. Exact commands and CI gates

After dependency install, from repository root:

```bash
# Linux/WSL quality reference
python -m pip check
python -m pytest tests/unit tests/integration --cov=app --cov-branch --cov-context=test --cov-report=json:reports/coverage.json --cov-report=term-missing --cov-report=xml:reports/coverage.xml --junitxml=reports/unit-integration.xml
python -m playwright install --with-deps chromium
python -m pytest tests/e2e --browser chromium --tracing retain-on-failure --screenshot only-on-failure --video retain-on-failure --output reports/playwright --junitxml=reports/e2e.xml
python scripts/check_e2e_inventory.py --spec docs/implementation-contract.md --manifest tests/e2e/cases.json --collection reports/e2e-collection.json --results reports/e2e-results.json
mutmut run --max-children 2
mutmut results > reports/mutation-results.txt
python scripts/check_mutation_report.py --results reports/mutation-results.txt --run-log reports/mutation-run.txt --review reports/mutation-review.json
```

The real workflow must capture `mutmut run` to `reports/mutation-run.txt` while preserving exit status; the unredirected line above illustrates the tool command only. For CI use `mutmut run --max-children 2 2>&1 | tee reports/mutation-run.txt` under `bash` with pipefail. Create reports directory before all report-writing commands. Mutation review file starts with an empty approvals list, never implicit approval of survivors. `scripts/check_mutation_report.py` also writes mutation-summary.json with counts. Its exact flags above are mandatory. Unit/integration run excludes E2E so mutmut never launches browsers. E2E runs separately against the real server fixture.

Evidence revision policy (commit-safe, shared by all three evidence checkers): an evidence file's `source_revision` is the last commit that touched a gated input — `app`, `tests`, `scripts`, `pyproject.toml`, `docs/implementation-contract.md`, `docs/testing-contract.md` — computed by `scripts/evidence_revision.py`. The E2E manifest `tests/e2e/cases.json` is itself evidence and is excluded from that set, otherwise the commit that stamps it would move the expected revision. Every evidence file must agree on that revision: regenerated evidence still passes after the commit that stores it, while any later commit touching a gated input makes it stale and fails the gate. Outside a git checkout the checkers keep their previous behaviour.

CI on push and pull_request, Ubuntu 24.04, Python 3.11:

1. **unit-integration:** install pinned dev dependencies, pip check, run coverage/JUnit command; fail on failed tests or aggregate measured coverage <95%.
2. **e2e:** install same dependencies and pinned Chromium, run the ten complete journeys plus the no-skipping collection/result gate; no retries that mask flaky failures.
3. **mutation:** after unit-integration succeeds, fresh checkout/install, run full framework and report gate; 30-minute job cap, timeout means failure/incomplete rather than exemption.
4. **image-build:** build the three Dockerfiles (`gateway/`, `backend_api/`, `web/`) without secrets; ensure non-root user and boot with injected test configuration through `tests/support/image_smoke.py`, not a fake-provider production switch. Run `docker build -f gateway/Dockerfile -t northstar-test .` (the smoke runs in the gateway image), then `docker run --rm --entrypoint python -v "$PWD/tests:/checks:ro" northstar-test /checks/support/image_smoke.py`. Harness uses only stdlib plus installed runtime packages, adds `/checks` to import path for test fake, asserts `os.getuid()!=0`, starts a real app/server with injected fake and TemporaryDirectory, checks /health and a session/turn roundtrip, stops server, then exits nonzero on any failure. Do not mount secrets or the live named volume. Real provider access remains separately verified.

Upload reports on failure and success, including browser trace/screenshots/video, red/green logs, coverage, framework output, manifests and requirement matrix. Retain 14 days. PR quality checks have no provider key and cannot call paid APIs. Do not automatically rerun failing browser tests as the success criterion. Fix flaky fixtures or a real race. Branch protection configuration depends on repository permissions; report if required checks cannot actually be enforced.

Live tests are outside automatic CI: require explicit `RUN_LIVE_TESTS=1`, separately authorized credentials, and invocation `python -m pytest tests/live -m live -q`. Without opt-in, collect and skip with a clear reason. They verify a real extraction, STT response and TTS audio, including current account/model access, and save only sanitized evidence. A skipped live check means live integration remains unverified, even if deterministic gates pass.

## 9. Definition of done

The verifier must find: test-first RED/GREEN evidence for each behavior slice; all AT/E2E mappings backed by tests; green unit/integration, coverage and browser jobs; completed full framework mutation run meeting the gate; exact resolved dependency evidence; successful container build/start/resume and live text/voice smoke; README and accessible demo recording. No missing gate is converted into optional work to meet the original time estimate. Passing documentation checks on this repository does not constitute any of those application gates.

References: [mutmut requirements/configuration](https://mutmut.readthedocs.io/en/latest/), [Playwright pytest setup](https://playwright.dev/python/docs/intro), [Playwright CI](https://playwright.dev/python/docs/ci).


## 10. Mutation reachability and inventory gate

mutmut's default discovery focuses on called functions. `mutate_only_covered_lines=false` alone does not prove uncalled functions were tested. Before mutation, produce `reports/mutation-inventory.json` from an AST inventory of concrete functions/methods defined in the seven required source files, excluding AIProvider Protocol stubs and bodies consisting only of pass/ellipsis. Every concrete function must map to an executed unit/integration test and framework mutation IDs, or a verifier-reviewed “no applicable mutation operator” reason with exact source hash. A concrete function with no executed test is a blocker, even if overall coverage exceeds 95% and other functions have many kills.

Implement `scripts/check_mutation_inventory.py` test-first: read the seven modules with ast, the unit/integration coverage JSON with test contexts (the quality command includes --cov-context=test and JSON reporting), and the framework-generated inventory/result files. Use qualified module/class/function names; nested functions must be accounted for, not silently dropped. Require executed body-line coverage for each function and a test-node mapping verified against measured coverage test contexts; declaration/import line coverage alone is insufficient. Framework IDs must correspond to the current source hash. Store generated functions with no variants explicitly and require independent review rather than assuming success. Unit tests of this gate include an uncalled helper, a wholly uncovered function, a function with no applicable operator and a missing module. Run it before the final mutation-report gate; either nonzero exit blocks delivery. This is an inventory/evidence checker, not a mutation engine; only mutmut creates or executes altered code.


Inventory checker CLI is exactly `python scripts/check_mutation_inventory.py --coverage reports/coverage.json --inventory reports/mutation-inventory.json --review reports/mutation-review.json`. The inventory JSON is `{ "source_revision": string, "functions": [{ "qualified_name": string, "path": string, "source_sha256": string, "test_nodeids": [string], "mutant_ids": [string], "no_operator_reason": string|null }] }`. It must cover every AST-discovered concrete function exactly once. IDs must come from the installed mutmut state, never invented by the model. Generate this evidence through a read-only exporter using the pinned framework's own source-file mutation data; exporter is prohibited from changing source or running alternate mutants. Export and check this manifest after the framework run, before the final report gate. Missing/unknown fields, duplicate entries, missing functions, no executed body lines, stale source hashes or missing independent approvals fail the gate. Include the exporter and checker in ordinary TDD unit tests; they are evidence tooling, not replacements for mutmut.


## 11. Mandatory RAG coverage

[rag-contract.md](rag-contract.md) defines required R13 and AT17–AT19; implementation-contract section13 assigns visible RAG coverage to policy_questions and keyboard_mobile and backend cases to IT10–IT11. Extend unit/integration/Playwright/live cases, strict TDD evidence, provider fake and httpx HTTP transport mocks accordingly. Add gateway/knowledge/retrieval.py to required mutation inventory and retain it in source_paths=app/ (not do_not_mutate). All seven concrete-module inventories must be checked. The real retriever and corpus run in E2E; only AI responses are faked.


## 12. Independent service backend compatibility

Follow provider-compatibility.md for all per-role transport/configuration cases. Test real application HTTP serialization with httpx MockTransport for four independent profiles, three text output modes, atomic RAG inheritance, no cross-role secret forwarding and per-role failure isolation (IT12–IT14 plus voice_failure/stale_voice_results browser checkpoints). No hosted OpenAI key is required for deterministic gates or auth=none local backends. Mutation inventory still includes providers.py/config.py and all new profile-selection/auth/parsing functions. All unit/integration/Playwright/TDD/mutmut gates remain mandatory.

Required guard regressions under unit/integration: preparse JSON body64KiB/+1 (whitespace/unknown keys count), large single ASGI message, chunked/missing/understated Content-Length, success provider JSON1MiB/+1, error body64KiB/+1 preserving status-derived nonretryable401/403/etc, unexpected Content-Encoding with no decompression, close/cancel cleanup within deadlines, and distinct multipart/audio caps. Test rejection recovery before reply selection for valid correction, invalid correction, no-candidate/equal-value reaffirmation, mixed valid/invalid fields and recovery-without-same-turn-completion. These cases are required, not optional hardening.

The inventory checker must validate each claimed test_nodeid against coverage contexts on executable body lines (pytest-cov contexts ending |run); a hand-authored mapping or aggregate line hit alone is insufficient. Retain context-bearing coverage JSON as evidence.


## 13. Real provider HTTP integration layer

Unit tests and the existing IT08/IT12/IT13 cases double the provider transport with `httpx.MockTransport`. This layer adds the layer between those and browser E2E: the shipped `OpenAICompatibleProvider` with its real `httpx.AsyncClient` talks over a real TCP socket to `tests/integration/support/provider_http.py::LocalProviderServer`, a threaded loopback HTTP service with per-path scripted responses. No provider object is injected into `create_app`; the lifespan builds the adapter from settings, so routes, schemas, engine, JSON repository, retriever, adapter and HTTP client all run as shipped. The harness is test-only; there is no production fake-provider mode.

The server records each request path, headers and raw body, and each response status/headers/outcome, including bytes written and client abort (`broken pipe`) on a chunked stream. Tests therefore assert exact wire requests, retry/repair counts, bounded response consumption and the absence of detached provider work after a timeout.

| Boundary | Case |
|----------|------|
| Exact extraction/RAG requests, parsed results, per-role bearer auth, `Accept-Encoding: identity` | `providers/test_provider_http.py::test_extraction_and_answer_requests_cross_the_real_socket_exactly`, `::test_role_tokens_never_cross_the_wire` |
| `json_schema`, `json_object`, `prompt_json` payloads at the socket | `::test_all_three_output_modes_are_transported_on_the_wire` and `rag/test_rag_http.py::test_output_mode_drives_a_real_turn_end_to_end` |
| Redirect not followed (302, one request, no target hit) | `::test_redirect_is_not_followed` |
| 1 MiB raw success cap: exact limit accepted; 8 MiB chunked body aborted mid-stream and retried | `::test_success_body_exactly_at_the_limit_is_accepted`, `::test_oversized_chunked_success_is_bounded_and_retried` |
| Compressed success rejected without decompression | `::test_gzip_encoded_success_is_rejected_without_decompression` |
| Truncated (`finish_reason=length`) response repaired once | `::test_truncated_success_is_invalid_and_repaired_once` |
| 503 retried then `provider_unavailable`; 400 not retried | `::test_provider_503_is_retried_then_unavailable`, `::test_provider_400_is_not_retried_and_maps_to_configuration` |
| Read timeout is bounded, exactly two scripted attempts occur, and no late result changes application state | `::test_read_timeout_is_bounded_and_leaves_no_detached_work` |
| Connection refused maps to `provider_unavailable` | `::test_connection_refused_maps_to_unavailable` |
| Shared extraction+generation deadline, 504, no partial write, and no late application request/state commit | `rag/test_rag_http.py::test_it10_12_shared_deadline_cuts_generation_without_detached_work` |
| Multi-turn RAG: slots/citations/revision after refresh-equivalent reload, old accepted fact outside the latest 12 messages | `::test_multi_turn_rag_keeps_state_and_old_facts_outside_the_window` |
| Rollback after reload leaves persisted bytes untouched, same revision accepts a later turn | `::test_rollback_after_reload_leaves_the_persisted_session_untouched` |
| Two application instances and sessions isolated (separate servers, directories, revisions, citations) | `::test_two_app_instances_and_sessions_are_isolated` |
| Speech over real HTTP: exact synthesis request, saved-message-only, arbitrary text/unknown/user message rejected with no provider call and no session write | `::test_speech_synthesizes_only_a_saved_message_over_real_http`, `::test_speech_rejects_unsaved_or_arbitrary_input_without_provider_call` |

IT10 matrix coverage: `rag/test_rag.py` keeps IT10-01..IT10-16 at the scripted-AIProvider and `httpx.MockTransport` layers; `rag/test_rag_http.py` re-proves the transport-dependent rows over the real socket (01 grounded answer, 02 no-match/no generation, 03 retrieved-but-unanswerable, 04 two ordered sources, 05 unknown citation, 06 duplicate citation, 07 unretrieved KB citation, 08 false-with-text, 11 outage rollback, 12 shared deadline). Rows 09, 10, 13, 14, 15 and 16 remain covered by their existing `rag/test_rag.py` cases; the module docstring records the exact mapping.

Timeout cases shorten the real adapter budget (`TEXT_BUDGET`/`ROLE_BUDGETS`) and the engine deadline before construction so wall-clock stays bounded; retry, repair, validation and commit/rollback logic are unchanged. Commands:

```bash
.venv/bin/python -m pytest tests/integration/test_provider_http.py tests/integration/test_rag_http.py -q
.venv/bin/python -m pytest tests/unit tests/integration -q
```

No Playwright case is added or changed by this change.

Existing suites remain authoritative for boundaries this layer does not re-prove: route and request/response schema validation (IT01–IT05), session storage locking, atomic replacement, migration and restart persistence (IT06, IT13, IT14, IT16), transcription transport (IT08), and speech transport variants (IT09, IT09b). This layer adds the missing real-socket provider layer rather than duplicating those cases.


## 14. Private Backend API boundary

The gateway owns no catalog, demo code or credential rule. Verification and lookup cross a real HTTP boundary to the shipped `backend_api.main:app` served by Uvicorn on a loopback port (`tests/integration/support/backend_api.py::LocalBackendApiServer`); the same server can be stopped and restarted on the same origin to prove outage and recovery. `BackendApiClient` transport/status mapping is also pinned in `tests/unit/backend_api/test_backend_api.py` with a protocol-faithful `httpx.MockTransport`; an in-process Backend API replacement is not accepted for integration coverage.

| Backend API case | Test nodeids |
|---|---|
| OA-I01 fixture and allowlisted record | `tests/unit/backend_api/test_backend_api.py::test_fixture_record_is_exact_and_allowlisted`, `::test_unknown_order_returns_not_found_without_record_data`; over the real socket `tests/integration/order_verification/test_backend_api_boundary.py::test_normalized_postcode_and_demo_code_grant_one_allowlisted_lookup`, `::test_missing_fixture_record_clears_the_grant_and_fails_generically` |
| OA-I02 health and private surface | `tests/unit/backend_api/test_backend_api.py::test_health_is_private_and_minimal`, `::test_client_has_no_public_gateway_routes` |
| OA-I03 gateway browser surface and no host port | `tests/integration/order_verification/test_backend_api_boundary.py::test_gateway_origin_does_not_expose_the_private_backend_api`, `tests/unit/tooling/test_compose_backend_api.py::test_backend_api_publishes_no_host_port_and_is_only_exposed_internally`, `::test_gateway_waits_for_a_healthy_backend_api_and_calls_it_by_service_dns` |
| OA-I04 verification while the Backend API is down | `tests/integration/order_verification/test_backend_api_boundary.py::test_backend_api_outage_during_verification_is_safe_and_recovers` |
| OA-I05 lookup while the Backend API is down | `::test_backend_api_outage_during_lookup_preserves_the_grant_and_recovers` |
| OA-I06 restart on the same origin recovers without a gateway restart | the `LocalBackendApiServer.restart()` phase of both OA-I04/I05 tests |
| OA-I07 slow, malformed and 5xx replies | `::test_unusable_backend_api_replies_map_to_fixed_safe_errors` (`OA-I07-timeout`, `OA-I07-malformed`, `OA-I07-5xx` ids) against a real loopback Uvicorn service |

complete_intake carries the browser journey extension; the outage/recovery checkpoints are `complete_intake.backend-api-outage-safe-error` and `complete_intake.demo-code-success-enables-composer`, alongside the failed-verification checkpoint `complete_intake.wrong-code-localized-error` and the successful lookup checkpoints `complete_intake.verified-allowlisted-order-summary` and `complete_intake.verified-order-review`.

```bash
.venv/bin/python -m pytest tests/integration/order_verification tests/unit/backend_api/test_backend_api.py tests/unit/tooling/test_compose_backend_api.py -q
.venv/bin/python scripts/test.py e2e
```
