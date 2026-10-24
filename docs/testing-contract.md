# Strict TDD and quality contract

User requirements added 22 October 2026: unit tests, integration tests, Playwright end-to-end tests, strict TDD, and **framework-generated mutation testing**. All are mandatory. This document specifies future implementation checks, not tests already run on the application.

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
| Integration | FastAPI routes/lifespan, engine, JSON repository, temporary directory; real application HTTP serialization for adapter tests; real private Order API app + gateway `OrderApiClient` over HTTP with a temporary catalogue | httpx transport, external AI service; Order API transport only for wire-failure cases | Engine, validation, JSON writes in transaction tests; Order API catalogue/grant logic in boundary tests |
| E2E | Chromium, shipped UI, real Uvicorn TCP gateway server, real private Order API server, routes, engine, temp JSON store and temp catalogue | Server-side AIProvider fake; only the explicitly enumerated environmental-fault fixtures | Successful browser API routes, persistence, confirmation, frontend request logic, Order API verification |
| Live smoke | Real app and real provider | No AI fake | Provider calls and voice playback |
| Mutation | Real mutmut framework executing unit/integration tests against generated variants | Same deterministic external-service doubles | Mutation generator/runner; no model-authored source hacks |

Unit and integration tests must run without any real service credential and must fail immediately if an unexpected external request is attempted. HTTP transport doubles return realistic provider wire responses; mocking `OpenAICompatibleProvider.extract` does not count as an adapter integration test. Test actual request JSON/multipart, model IDs, parsed response, refusal/truncation and error mapping. Use an injected sleep function to avoid real backoff delays. Avoid real provider calls in PR jobs.

E2E provider fake is a separate test-only implementation in `tests/support/fakes.py`. It receives context and has deterministic responses for each test's exact message/audio fixture, supports a one-shot recoverable failure when configured by fixture, and raises on any unexpected call. It must not duplicate business validation or state decisions. Fake STT returns `Order NS-000123 arrived broken and this is urgent`; fake TTS returns a finite playable MP3 fixture as bytes, and response remains audio/mpeg. Keep it under `tests/fixtures/reply.mp3` with source/licence note and SHA-256 in fixture metadata. Create it once during test-fixture preparation using `ffmpeg -f lavfi -i sine=frequency=440:duration=1:sample_rate=24000 -ac 1 -c:a libmp3lame -b:a 64k tests/fixtures/reply.mp3`. This encoder is fixture-authoring tooling, not an application, CI or runtime requirement once the fixture is committed. Generate a 1-second 16-kHz mono 16-bit WAV with peak amplitude 1000 for valid upload tests using Python wave/struct; generate silence with zeros, and boundary fixtures with the same generator's explicit rate/channel/width/duration parameters. Record both commands/generator and SHA-256s in tests/fixtures/README.md. No WAV-as-MP3 labeling is allowed.


## 4. Integration/E2E coverage authority

The actual test specifications are in **implementation-contract.md section13**: 199 individually identified integration cases across groups IT01–IT15 and eleven complete Playwright journeys J01–J11. They replace the earlier100-browser-case matrix. Unit-test design is the implementing model's responsibility while doing strict TDD; unit/coverage/mutmut gates remain mandatory. Do not recreate100 fullbrowser flows and relabel them integration.

IT15 covers the private Order API boundary (gateway `OrderApiClient` ↔ real `order_api` service, verification and grant checks, outage/timeout/malformed-wire mapping, browser unreachability); J01 extends the intake journey with unknown-order verification, and J11 covers Order API outage recovery.

Use section13 for exact fixture seams, scripted inputs, journey actions/assertions, backend-vs-browser boundaries and required evidence. Integration tests execute real Python API/engine/retrieval/storage with controlled external HTTP, plus the real private Order API app with a temporary catalogue. Playwright journeys execute the real UI/server/engine/Order API path and assert browser-specific draft/media/retry/rendering behavior. Invalid field combinations, state permutations, message-limit edges, codecs, retrieval/ranking/citation failures and protocol/auth/deadline boundaries belong primarily to integration or model-designed units.

The E2E manifest/evidence checker must read journey IDs J01–J11 from implementation-contract.md section13; no gate requires the retired100 IDs. Keep all199 concrete ITxx-yy IDs collected/executed and mapped separately in the integration requirement mapping. Do not silently skip a journey or backend branch.

## 5. E2E server fixture lifecycle

`tests/e2e/conftest.py` uses a function-scoped fixture to start two real Uvicorn servers in background threads, each with an already-bound loopback socket on port 0. Pass each socket to `Server.run(sockets=[socket])` so there is no free-port race. One serves the private `order-api` service (`create_order_api(settings=test_order_settings, catalog_path=temp_catalogue)`); the other serves `create_app(settings=test_settings, provider=test_fake)` with `ORDER_API_URL` pointing at the Order API's loopback address and the shared test token. Wait for both `/health` endpoints using bounded polling (10-second startup maximum); on teardown set each `server.should_exit`, join within 10 seconds and fail cleanup if either remains alive. Each test gets a fresh browser context, isolated temporary SESSION_DIR and catalogue, and independent app/Order-API/server/fake instances. J11 stops and restarts only the Order API server on its previously bound port while the gateway and page stay loaded. Run test cases serially. A case may open the two pages or overlapping provider requests explicitly required by its row, using the same controlled app fixture.

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
source = ["app", "order_api"]
omit = ["app/main.py", "app/schemas.py", "app/prompts.py", "app/errors.py", "order_api/main.py", "order_api/schemas.py", "order_api/errors.py"]

[tool.coverage.json]
show_contexts = true

[tool.coverage.report]
fail_under = 95
show_missing = true

[tool.mutmut]
source_paths = ["app/", "order_api/"]
do_not_mutate = ["app/__init__.py", "app/main.py", "app/schemas.py", "app/prompts.py", "app/errors.py", "order_api/__init__.py", "order_api/main.py", "order_api/schemas.py", "order_api/errors.py"]
pytest_add_cli_args_test_selection = ["tests/unit", "tests/integration"]
also_copy = ["tests/", "pyproject.toml"]
mutate_only_covered_lines = false
```

Mutate all eligible functions in **conversation.py, validation.py, storage.py, audio.py, providers.py, config.py, retrieval.py, order_client.py, order_api/catalog.py, order_api/grants.py, order_api/config.py**, including uncovered lines. Main wiring/errors/schema declarations/prompt literals remain covered by integration assertions and are explicitly outside mutmut scope. Do not describe those excluded files as mutation-tested. mutmut 3's mutation operator scope does not prove module-level schema/prompt constants; write direct assertions for their contracts. A coverage percentage is not proof of test strength.

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
python -m pytest tests/unit tests/integration --cov=app --cov=order_api --cov-branch --cov-context=test --cov-report=json:reports/coverage.json --cov-report=term-missing --cov-report=xml:reports/coverage.xml --junitxml=reports/unit-integration.xml
python -m playwright install --with-deps chromium
python -m pytest tests/e2e --browser chromium --tracing retain-on-failure --screenshot only-on-failure --video retain-on-failure --output reports/playwright --junitxml=reports/e2e.xml
python scripts/check_e2e_inventory.py --spec docs/implementation-contract.md --manifest tests/e2e/cases.json --collection reports/e2e-collection.json --results reports/e2e-results.json
mutmut run --max-children 2
mutmut results > reports/mutation-results.txt
python scripts/check_mutation_report.py --results reports/mutation-results.txt --run-log reports/mutation-run.txt --review reports/mutation-review.json
```

The real workflow must capture `mutmut run` to `reports/mutation-run.txt` while preserving exit status; the unredirected line above illustrates the tool command only. For CI use `mutmut run --max-children 2 2>&1 | tee reports/mutation-run.txt` under `bash` with pipefail. Create reports directory before all report-writing commands. Mutation review file starts with an empty approvals list, never implicit approval of survivors. `scripts/check_mutation_report.py` also writes mutation-summary.json with counts. Its exact flags above are mandatory. Unit/integration run excludes E2E so mutmut never launches browsers. E2E runs separately against the real server fixture.

CI on push and pull_request, Ubuntu 24.04, Python 3.11:

1. **unit-integration:** install pinned dev dependencies, pip check, run coverage/JUnit command; fail on failed tests or aggregate measured coverage <95%.
2. **e2e:** install same dependencies and pinned Chromium, run the eleven J01–J11 complete journeys plus the no-skipping collection/result gate; no retries that mask flaky failures.
3. **mutation:** after unit-integration succeeds, fresh checkout/install, run full framework and report gate; 30-minute job cap, timeout means failure/incomplete rather than exemption.
4. **image-build:** build runtime Dockerfile without secrets; ensure non-root user and boot with injected test configuration through `tests/support/image_smoke.py`, not a fake-provider production switch. Run `docker build -f docs/deployment/Dockerfile -t northstar-test .` then `docker run --rm --entrypoint python -v "$PWD/tests:/checks:ro" northstar-test /checks/support/image_smoke.py`. Harness uses only stdlib plus installed runtime packages, adds `/checks` to import path for test fake, asserts `os.getuid()!=0`, starts a real app/server with injected fake and TemporaryDirectory, checks /health and a session/turn roundtrip, stops server, then exits nonzero on any failure. Do not mount secrets or the live named volume. Real provider access remains separately verified.

Upload reports on failure and success, including browser trace/screenshots/video, red/green logs, coverage, framework output, manifests and requirement matrix. Retain 14 days. PR quality checks have no provider key and cannot call paid APIs. Do not automatically rerun failing browser tests as the success criterion. Fix flaky fixtures or a real race. Branch protection configuration depends on repository permissions; report if required checks cannot actually be enforced.

Live tests are outside automatic CI: require explicit `RUN_LIVE_TESTS=1`, separately authorized credentials, and invocation `python -m pytest tests/live -m live -q`. Without opt-in, collect and skip with a clear reason. They verify a real extraction, STT response and TTS audio, including current account/model access, and save only sanitized evidence. A skipped live check means live integration remains unverified, even if deterministic gates pass.

## 9. Definition of done

The verifier must find: test-first RED/GREEN evidence for each behavior slice; all AT/E2E mappings backed by tests; green unit/integration, coverage and browser jobs; completed full framework mutation run meeting the gate; exact resolved dependency evidence; successful container build/start/resume and live text/voice smoke; README and accessible demo recording. No missing gate is converted into optional work to meet the original time estimate. Passing documentation checks on this repository does not constitute any of those application gates.

References: [mutmut requirements/configuration](https://mutmut.readthedocs.io/en/latest/), [Playwright pytest setup](https://playwright.dev/python/docs/intro), [Playwright CI](https://playwright.dev/python/docs/ci).


## 10. Mutation reachability and inventory gate

mutmut's default discovery focuses on called functions. `mutate_only_covered_lines=false` alone does not prove uncalled functions were tested. Before mutation, produce `reports/mutation-inventory.json` from an AST inventory of concrete functions/methods defined in the ten required source files (the seven `app/` modules plus `order_api/catalog.py`, `order_api/grants.py` and `order_api/config.py`), excluding AIProvider Protocol stubs and bodies consisting only of pass/ellipsis. Every concrete function must map to an executed unit/integration test and framework mutation IDs, or a verifier-reviewed “no applicable mutation operator” reason with exact source hash. A concrete function with no executed test is a blocker, even if overall coverage exceeds 95% and other functions have many kills.

Implement `scripts/check_mutation_inventory.py` test-first: read the ten modules with ast, the unit/integration coverage JSON with test contexts (the quality command includes --cov-context=test and JSON reporting), and the framework-generated inventory/result files. Use qualified module/class/function names; nested functions must be accounted for, not silently dropped. Require executed body-line coverage for each function and a test-node mapping verified against measured coverage test contexts; declaration/import line coverage alone is insufficient. Framework IDs must correspond to the current source hash. Store generated functions with no variants explicitly and require independent review rather than assuming success. Unit tests of this gate include an uncalled helper, a wholly uncovered function, a function with no applicable operator and a missing module. Run it before the final mutation-report gate; either nonzero exit blocks delivery. This is an inventory/evidence checker, not a mutation engine; only mutmut creates or executes altered code.


Inventory checker CLI is exactly `python scripts/check_mutation_inventory.py --coverage reports/coverage.json --inventory reports/mutation-inventory.json --review reports/mutation-review.json`. The inventory JSON is `{ "source_revision": string, "functions": [{ "qualified_name": string, "path": string, "source_sha256": string, "test_nodeids": [string], "mutant_ids": [string], "no_operator_reason": string|null }] }`. It must cover every AST-discovered concrete function exactly once. IDs must come from the installed mutmut state, never invented by the model. Generate this evidence through a read-only exporter using the pinned framework's own source-file mutation data; exporter is prohibited from changing source or running alternate mutants. Export and check this manifest after the framework run, before the final report gate. Missing/unknown fields, duplicate entries, missing functions, no executed body lines, stale source hashes or missing independent approvals fail the gate. Include the exporter and checker in ordinary TDD unit tests; they are evidence tooling, not replacements for mutmut.


## 11. Mandatory RAG coverage

[rag-contract.md](rag-contract.md) defines required R13 and AT17–AT19; implementation-contract section13 assigns visible RAG coverage to J08–J09 and backend cases to IT10–IT11. Extend unit/integration/Playwright/live cases, strict TDD evidence, provider fake and httpx HTTP transport mocks accordingly. Add app/retrieval.py to required mutation inventory and retain it in source_paths=app/ (not do_not_mutate). All ten concrete-module inventories must be checked. The real retriever and corpus run in E2E; only AI responses are faked.


## 12. Independent service backend compatibility

Follow provider-compatibility.md for all per-role transport/configuration cases. Test real application HTTP serialization with httpx MockTransport for four independent profiles, three text output modes, atomic RAG inheritance, no cross-role secret forwarding and per-role failure isolation (IT12–IT14 plus J07/J10 browser checkpoints). No hosted OpenAI key is required for deterministic gates or auth=none local backends. Mutation inventory still includes providers.py/config.py, order_client.py, order_api/catalog.py, order_api/grants.py and order_api/config.py plus all new profile-selection/auth/parsing functions. All unit/integration/Playwright/TDD/mutmut gates remain mandatory.

The private Order API boundary adds its own required regressions: IT15's fourteen cases plus J01/J11 browser checkpoints. The Order API app runs for real in integration and E2E; only its transport is doubled for wire-failure cases, and the Compose template's port/network exposure is asserted through Docker Compose itself.

Required guard regressions under unit/integration: preparse JSON body64KiB/+1 (whitespace/unknown keys count), large single ASGI message, chunked/missing/understated Content-Length, success provider JSON1MiB/+1, error body64KiB/+1 preserving status-derived nonretryable401/403/etc, unexpected Content-Encoding with no decompression, close/cancel cleanup within deadlines, and distinct multipart/audio caps. Test rejection recovery before reply selection for valid correction, invalid correction, no-candidate/equal-value reaffirmation, mixed valid/invalid fields and recovery-without-same-turn-completion. These cases are required, not optional hardening.

The inventory checker must validate each claimed test_nodeid against coverage contexts on executable body lines (pytest-cov contexts ending |run); a hand-authored mapping or aggregate line hit alone is insufficient. Retain context-bearing coverage JSON as evidence.
