# Real Docker E2E gate

The fake-provider journeys and the real deployment gate are separate suites. The Docker gate never imports `tests.support.fakes` and only talks to the shipped UI/API.

## Setup

1. Deploy an isolated, disposable Orbio stack; do not point this at production. The compose `sessions` volume is test data and should be removed after the run (`docker compose down -v`).
2. Copy `tests/e2e/.env.docker.example` to `tests/e2e/.env.docker` and set `E2E_DOCKER_BASE_URL` to the application origin.
3. Copy `tests/e2e/.env.judge.example` to `tests/e2e/.env.judge`; set the exact judge model and all required numeric/prompt settings. If the judge requires authentication, set `E2E_JUDGE_API_KEY` in the process environment; never commit it to the file.
4. Run:

```sh
python scripts/run_docker_e2e.py --with-deterministic
```

The command probes `/health` and the judge `/models` endpoint before launching Chromium. It rejects URLs with paths, query strings, fragments, or embedded credentials, and verifies that the configured model is advertised. It writes the non-secret origin/model/prompt metadata to `reports/e2e-docker/run-config.json`, Playwright output to `reports/e2e-docker/playwright`, JUnit output to `reports/e2e-docker/docker.xml`, and per-node judge artifacts under `reports/e2e-docker/judge/`. Raw judge output is retained in those artifacts; deterministic assertions remain authoritative.

`--with-deterministic` labels and runs the existing fake-provider suite separately (`not intent_judge and not docker_e2e`). The Docker suite is expected to fail early when either local configuration or either deployed service is unavailable; it must not skip or fall back to a fake provider.
