# Real Docker E2E gate

The fake-provider journeys and the real deployment gate are separate suites. The Docker gate never imports `tests.support.fakes` and only talks to the shipped UI/API.

## Setup

1. Deploy a dedicated, disposable Northstar stack as its own Compose project with its own volumes and port; never point this at production or at your demo stack. The booking journeys empty every account's bookings, so they act only on a project whose name ends in `-e2e` and whose published page is the origin under test:

   ```sh
   APP_PUBLISH_PORT=8010 docker compose -p northstar-e2e up -d --build --wait
   export E2E_DOCKER_BASE_URL=http://127.0.0.1:8010 E2E_COMPOSE_PROJECT=northstar-e2e
   ```

   Remove it afterwards with `docker compose -p northstar-e2e down -v`.
2. Copy `tests/e2e/.env.docker.example` to `tests/e2e/.env.docker` and set `E2E_DOCKER_BASE_URL` to the application origin.
3. Copy `tests/e2e/.env.judge.example` to `tests/e2e/.env.judge`; set the exact judge model and all required numeric/prompt settings. If the judge requires authentication, set `E2E_JUDGE_API_KEY` in the process environment; never commit it to the file.
4. Run:

```sh
python scripts/run_docker_e2e.py --with-deterministic
```

The command probes `/health` and the judge `/models` endpoint before launching Chromium. It rejects URLs with paths, query strings, fragments, or embedded credentials, and verifies that the configured model is advertised. It writes the non-secret origin/model/prompt metadata to `reports/e2e-docker/run-config.json`, Playwright output to `reports/e2e-docker/playwright`, JUnit output to `reports/e2e-docker/docker.xml`, and judge artifacts under `reports/e2e-docker/judge/`, one numbered file per judged reply (`<test>__01.json`, `__02.json`, ...). Raw judge output is retained in those artifacts; deterministic assertions remain authoritative.

`--with-deterministic` labels and runs the existing fake-provider suite separately (`-m browser_e2e`). The Docker suite is expected to fail early when either local configuration or either deployed service is unavailable; it must not skip or fall back to a fake provider.

## Test-layer matrix

| Layer | Suite | Provider boundary | Proves |
|---|---|---|---|
| Unit | `tests/unit` | Doubles only | Pure domain rules |
| Integration (real application boundaries) | `tests/integration` | Real FastAPI routes/engine/storage, controlled HTTP doubles | Wiring and error mapping |
| Browser workflow E2E (controlled provider) | the ten browser journeys + confirmation retry, marker `browser_e2e`, `scripts/run_browser_e2e.py` | Controlled provider fake, local Uvicorn fixture | Real browser workflow: JavaScript, HTTP, storage, keyboard, mobile, media, retry, timeout, stale-state |
| Real Docker E2E (real provider) | `tests/e2e/test_docker_e2e.py` (single turns, RAG, intake edits, hand-off) and `tests/e2e/test_docker_flows.py` (booking, installation status, the support request, detours, Spanish), marker `docker_e2e`, `scripts/run_docker_e2e.py` | Real deployed UI/API and real provider; no `tests.support.fakes` import | Shipped UI/API path against the real deployment |
| LLM semantic judge | `intent_judge` marker + per-node Docker judge artifacts | Judge config only (`tests/e2e/.env.judge`), kept separate from the application provider config (`LLM_*`/`STT_*`/`TTS_*`) | Relevance/grounding of responses; deterministic assertions stay authoritative |

## What the controlled-provider suite proves and does not prove

Proves: the complete browser workflow renders and behaves correctly —
intake, correction, resume, retry, two-tab conflict, voice success/failure,
RAG questions with citations, keyboard/mobile layout, and stale-voice
recovery — against deterministic provider responses.

Does not prove: a live provider (no real LLM/STT/TTS call is made), a
deployed Docker system (the app runs in a local thread fixture, not the
Compose stack), or production configuration (judge/app provider settings are
not exercised here).

## Evidence rules

- Controlled reports (`reports/e2e-collection.json`, `reports/e2e-results.json`,
  `reports/e2e-controlled/run-config.json`) record
  `environment_type: controlled-provider`, `provider_mode: controlled-fake`,
  the local fixture URL, browser, test node IDs, deterministic outcomes, and
  checkpoints. No judge model or verdict is recorded because no judge runs.
- Real-Docker evidence (`reports/e2e-docker/run-config.json`,
  `reports/e2e-docker/docker.xml`, `reports/e2e-docker/judge/*.json`) records
  `environment_type: real-docker`, `provider_mode: real-provider`, the Docker
  application origin, judge model, prompt version, per-node IDs, deterministic
  results, and judge verdicts.
- Never include secrets in any report: the judge API key stays in the process
  environment and is never written to disk.
- `scripts/check_e2e_layers.py` enforces the separation: it fails when
  controlled evidence is mislabelled, claims live Docker/provider coverage,
  uses the unqualified name "full E2E" for the controlled suite, carries
  secrets, or when `--require-docker` is passed without real-Docker
  run-config and JUnit evidence. Run it after every E2E run (see README).
- Never call the controlled-provider suite "full E2E" without qualification.
  The qualified forms are "browser workflow E2E (controlled provider)" and
  "real Docker E2E (real provider)".
