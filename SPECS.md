# Builder instructions

This repository contains the application, tests, deployment configuration and implementation contracts. Start with README.md for root-relative execution instructions and recorded verification limits. These contracts define required behavior; their existence does not prove a runtime or test gate has passed.

RAG is mandatory under the user-added R13 requirement. Read [rag-contract.md](docs/rag-contract.md), which defines the knowledge corpus, retrieval, generation, citations, state/budget rules and additional quality gates.

Read [provider-compatibility.md](docs/provider-compatibility.md) first for independent extraction/RAG/STT/TTS backend settings and exact HTTP contracts.

Read in order:

1. [Exact implementation contract](docs/implementation-contract.md): fixed choices, file ownership, signatures, schemas, state/reply rules, prompt, server assembly, HTTP calls, text/voice transactions and errors.
2. [Strict testing contract](docs/testing-contract.md): test-first procedure, unit/integration/Playwright E2E suites, fixtures, mutmut framework, survivor/inventory gates, CI and evidence.
3. [Assignment and acceptance criteria](docs/build-spec.md): source traceability, core AT1–AT16, deliverables and boundaries.
4. [Startup contract](docs/startup.md) and [deployment configuration](compose.yaml).

The contracts describe the current design. The original PDF estimate does not waive user-added RAG or quality requirements. If a conflict remains, state the exact conflicting clauses and resolve it with the verifier before implementing that behavior. Do not invent an unrecorded architecture decision.

Strict TDD is mandatory at each layer. Write and run failing tests before production behavior; retain real RED/GREEN evidence. Use pytest for unit/integration, pytest-playwright for real-browser E2E, and mutmut for mutation generation/execution in Linux/WSL. The implementing model must not manually alter source to imitate mutations or write a custom mutation engine. Framework report/inventory checkers may only inspect evidence. Never waive a failing or incomplete gate to meet the PDF's original time estimate.

Credentials/account access, a resolved dependency set, current provider availability, actual Docker execution and live model quality remain external/runtime verification gates. Do not fabricate secrets, logs, test results, video URLs, mutant kills or approval. No hosted model or TTS shutdown date is hardcoded in the generic adapter; compatibility is verified per configured backend.

Use the [HTML architecture guide](docs/architecture.html) to see the three containers, what each owns and how one customer turn flows through them. It is an explanatory view of the contracts above.
