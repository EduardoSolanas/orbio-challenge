# Builder instructions

This repository is currently specifications and deployment templates, not a working application. The user's task in this phase is to make the specification sufficient for a separate implementing model. Do not report the application as implemented or any application test as passing because these documents exist.

RAG is mandatory under the user-added R13 requirement. Read [rag-contract.md](docs/rag-contract.md), which defines the knowledge corpus, retrieval, generation, citations, state/budget rules and additional quality gates.

Read [provider-compatibility.md](docs/provider-compatibility.md) first for independent extraction/RAG/STT/TTS backend settings and exact HTTP contracts.

Read in order:

1. [Exact implementation contract](docs/implementation-contract.md): fixed choices, file ownership, signatures, schemas, state/reply rules, prompt, server assembly, HTTP calls, text/voice transactions and errors.
2. [Strict testing contract](docs/testing-contract.md): test-first procedure, unit/integration/Playwright E2E suites, fixtures, mutmut framework, survivor/inventory gates, CI and evidence.
3. [Assignment and acceptance criteria](docs/build-spec.md): source traceability, core AT1–AT16 and AT20, deliverables and boundaries.
4. [Startup contract](docs/startup.md) and [deployment templates](docs/deployment/compose.yaml). Two services: the browser-facing gateway and the private Order API (no host port, internal network only).
5. [Independent verifier findings](docs/spec-verification.md).

The contracts describe the current design. The original PDF estimate does not waive user-added RAG or quality requirements. If a conflict remains, state the exact conflicting clauses and resolve it with the verifier before implementing that behavior. Do not invent an unrecorded architecture decision.

Strict TDD is mandatory at each layer. Write and run failing tests before production behavior; retain real RED/GREEN evidence. Use pytest for unit/integration, pytest-playwright for real-browser E2E, and mutmut for mutation generation/execution in Linux/WSL. The implementing model must not manually alter source to imitate mutations or write a custom mutation engine. Framework report/inventory checkers may only inspect evidence. Never waive a failing or incomplete gate to meet the PDF's original time estimate.

Credentials/account access, a resolved dependency set, current provider availability, actual Docker execution and live model quality remain external/runtime verification gates. Do not fabricate secrets, logs, test results, video URLs, mutant kills or approval. No hosted model or TTS shutdown date is hardcoded in the generic adapter; compatibility is verified per configured backend.

Use the [HTML architecture guide](docs/architecture.html) to inspect wiring, component dependencies, startup, and TDD/mutation gates. It is an explanatory view of the contracts above.

The two-service boundary contract is guarded by `scripts/check_spec_consistency.py` (standard library plus the Docker Compose CLI): it parses `docs/deployment/compose.yaml` through Docker Compose and asserts that every contract document agrees on the private Order API. Run `python3 scripts/check_spec_consistency.py` after editing any contract file; a nonzero exit means the documents disagree.
