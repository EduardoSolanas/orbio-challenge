# Testing and evaluation

The details behind the short Testing section of the [README](../README.md): what each
layer covers, the latest results, the real-stack runbook and the model evaluation.

## Results by layer

| Layer | What it covers | Result (2026-10-26) |
|---|---|---|
| Unit and integration | Validation, state machine, storage, HTTP routes, providers over a real socket, RAG, audio | 2,500 unit and integration tests passed together in the latest frozen Linux snapshot. Ruff checks and formatting run in CI first (unused imports, import order, undefined names, `ruff format --check`). Exact retry counts after a timeout are pinned without real timing; the real-socket timeout tests only check that the call returns within its bound, never exceeds the configured attempts and starts no work afterwards. |
| Browser (Playwright) | Eleven Chromium tests of the user journeys, including voice and RAG | 11 passed |
| Frontend (Jest) | The page's classes in jsdom, using the real `index.html` | 312 passed; 97.7% statements, 92% branches (CI gate 90% / 80%) |
| Judge journeys | 31 tests with a scripted provider (29 browser journeys and 2 output-format checks); 26 of the journeys record a conversation that a judge LLM grades against a written rule. The greeting, thanks and capability journeys assert their fixed replies exactly and are not judged. Every intent, including verification before account actions, and frustration hand-off | 31 passed; 26 of 26 judged pass. Replies are mostly templates, so this checks wording rules, not model behaviour. |
| Real Docker end-to-end | 34 tests on the deployed containers against the real LLM, speech services and embeddings, graded by the judge LLM. 21 cover every intent, RAG with abstention, intake edits, angry and nervous customers in both languages, and every flow end to end (booking, installation status, a full support request, a detour from a support request into a booking and back, leaving a flow on a question, booking in Spanish). 13 cover what goes wrong: a gas emergency alone, in the middle of a booking and in Spanish; an unknown account, five wrong codes, malformed values, nonsense, markup and the longest message; and six jailbreaks (prompt leak, role-play refund, staff override, someone else's account, orders hidden in a description, Spanish mid-booking). The jailbreak and emergency tests also check the state the reply leaves behind (no verification, no booking, nothing saved), not only the wording | The 13 scenario tests passed on the demo stack in two full runs; they never touch bookings. The other 21 last passed before the latest refactors and have not been rerun on a dedicated deployment since. The judge is the same DeepSeek model that serves the chat, so it grades itself. Journeys that touch bookings first reset the deployed booking store (`docker compose exec` into the backend of a dedicated Compose project whose name ends in `-e2e` and whose page is the origin under test; see Real-stack end-to-end tests below); one creates a reservation and checks it is reported back, another starts from a known existing visit. Each judged reply keeps its own numbered file under `reports/e2e-docker/judge/`. A reasoning judge needs `E2E_JUDGE_MAX_TOKENS=4096`. |
| Live providers | `scripts/check_backends.py` | 5 of 5 (2026-10-24) |
| Mutation testing | Pinned `mutmut 3.8.0`, fresh Linux cache, all configured eligible functions in `gateway/`, `backend_api/` and `shared/`, including uncovered lines; no statement-wide regex exclusions | Latest frozen working tree: **5,017 / 5,017 checked; 4,898 killed; 117 independently reviewed equivalents; 2 invalid type-only stubs; 0 unresolved or timed out**. **97.63% raw kills; 100% of non-equivalent variants killed; gate passed.** Evidence: `reports/mutation-evidence/full-9e20b0ba/`. |

Mutation review drafts are not approvals. Generate a draft with
`python scripts/build_mutation_review.py --results <framework-results> --diffs <framework-diffs.json> --revision <tested-revision> --run-commit <run-commit> --out reports/mutation-proposals.json`.
The command rejects missing, duplicate or inconsistent evidence. A reviewer must inspect
each exact diff, its source and test evidence before recording an independent approval;
an untested mutant is not automatically an invalid mutation. Historical raw evidence is
retained under `reports/mutation-evidence/6c197dc/` and is not evidence for later code.

The current complete run is recorded in
`reports/mutation-evidence/full-9e20b0ba/REVIEW.md`. Its baseline passed 2,500
unit/integration tests. The parser's existing two-second test budget is now enforced
during execution, so infinite-loop regressions fail a test rather than hang until
mutmut's outer timeout. Two new integration cases cover an aside after successful
credential verification but failed flow continuation. The four looping variants
and two flow variants were all killed in a focused framework run and the fresh full run.

All 119 non-killed diffs were freshly exported and checked against their actual
hashes and source files before independent approval. Equivalents and invalid
variants are reported separately and never counted as kills. The review builder
still generates pending proposals only. The underlying report checker trusts the
manual review; this run additionally verified each fresh diff/source hash explicitly.

The tested uncommitted working tree is bound to an archived 339-file snapshot and
hash manifest, alongside the repository's last committed gated-input revision.
Inputs matched the working files before report documentation was updated. Historical
runs remain under `reports/mutation-evidence/`; superseded partial runs contribute
nothing to the current score.

CI (`.github/workflows/quality.yml`) runs five jobs on every push: unit and integration tests with a 95% coverage gate and Ruff, Jest, the browser journeys with the evidence gate, mutation testing, and an image build with a non-root smoke test.

## Real-stack end-to-end tests

The judged real-stack tests (`tests/e2e/test_docker_e2e.py`, `test_docker_flows.py`, `test_docker_scenarios.py`, marker `docker_e2e`) talk only to the deployed page and API and never import the test fakes. Deploy a disposable stack as its own Compose project with its own volumes and port; the booking journeys empty every account's bookings, so they act only on a project whose name ends in `-e2e` and whose page is the origin under test:

```bash
APP_PUBLISH_PORT=8010 docker compose -p northstar-e2e up -d --build --wait
export E2E_DOCKER_BASE_URL=http://127.0.0.1:8010 E2E_COMPOSE_PROJECT=northstar-e2e
cp tests/e2e/.env.judge.example tests/e2e/.env.judge   # judge model, timeout, max tokens, prompt version
python scripts/run_docker_e2e.py --with-deterministic
docker compose -p northstar-e2e down -v --rmi local    # when finished
```

The runner probes `/health` and the judge's `/models` endpoint first, fails early rather than skipping or falling back to a fake provider, and writes the origin, model and prompt version to `reports/e2e-docker/run-config.json`, JUnit output to `reports/e2e-docker/docker.xml`, and one numbered verdict file per judged reply under `reports/e2e-docker/judge/`. Raw judge output is kept; the deterministic assertions stay authoritative. If the judge needs a key, set `E2E_JUDGE_API_KEY` in the process environment, never in a file. `--with-deterministic` also runs the scripted browser journeys (`-m browser_e2e`) as a separate, labelled suite; call that one "browser workflow E2E (controlled provider)", never plain "full E2E". `python scripts/check_e2e_layers.py` fails if controlled evidence is mislabelled or claims live coverage.

## Evaluation

The scripted tests prove what the app does once it knows the intent. They cannot show
whether the real model picks the right intent or FAQ record, so an operator-run
evaluation checks that against the live services (not part of the automatic run):

```bash
python scripts/evaluate.py            # also: intents, category, safety, frustration, retrieval
```

It uses the labelled utterances in [`tests/eval/utterances.json`](tests/eval/utterances.json)
(99 English and Spanish messages, plus two FAQ question sets). Run on 2026-10-25 with
`DeepSeek-V4.1-Flash-1017` and `nvidia/nemotron-3-embed-1b:free`.

**Intent detection: 85 of 89 at first, 89 of 89 after a prompt-only fix.** The misses
were "I want to report a problem" (and its Spanish and "Let me report a fault"
variants) read as `provide_details`, and "What are the urgency levels?" read as
`capability`. The fix was two prompt rules and three new examples worded differently
from the test messages. The 89 are the messages that exposed the misses, so read 100%
as a regression baseline, not general accuracy.

Manual tests later showed "help me with something" and "I have a problem" being sent to intake, which asked for an account reference before the customer had said what they wanted. Saying only that there is a problem, or asking for help, is now a `capability` turn: the reply lists what the assistant can do and asks what the customer needs. An explicit request to report, raise or log a problem still starts an intake. Ten such messages joined the set, and the score on the live model is 99 of 99 (the explicit report requests are still 8 of 8).

**Safety triage (gas): each check alone, and both together, caught 15 of 15 gas reports with 0 false alarms on 19 look-alikes and 0 on 99 everyday messages.** The set has English and Spanish, indirect wording ("smells like rotten eggs", "hissing by the meter", a carbon monoxide alarm) and look-alikes (a high gas bill, a gas meter installation, "I smelled new paint"). Recall is the number that matters, since a false alarm only shows safety advice. The first run of the embedding check raised one false alarm ("How's it going?"); adding two greeting sentences to the examples file fixed it with no code change. A later end-to-end run showed it also flagged "Tell me a joke about pizza.": for text far from every example, "nearest" is close to a coin flip. The check now also needs a minimum similarity to an emergency example (0.35, picked from the gap between real emergencies at 0.47 or more and unrelated messages at 0.22 or less), and five off-topic messages joined the look-alikes. The examples share nothing with the evaluation messages (a test enforces that). The messages were written by one author and 29 is a small set: read it as a check that both work, not as a guarantee. Adding the safety checks did not move the other scores (intents still 99 of 99, hand-off 9 of 9 and 8 of 8).

**Support area (what the stored category should be): 8 of 27 at first, 27 of 27 after a prompt-only fix.** A manual test showed "i have overcharged in my last bill" saved as "other energy issue". The prompt named the category codes but never said what they meant, so the model put almost everything under `other`: bills, power cuts, wrong readings and broken equipment alike. The fix is one prompt sentence that defines each area (supply, equipment, meter reading, billing, other) and says to leave the category empty when the message does not make it clear. The 27 messages (English and Spanish, five or more per area) were written before the fix; run `python scripts/evaluate.py category`.

**Frustration hand-off: 9 of 9 angry messages handed off, 8 of 8 calm or nervous kept.**
The messages were written by one author and judged one at a time, so milder or
sarcastic complaints may differ. The Docker tests cover it end to end.

**FAQ retrieval.** The *tuning set* chose the similarity floor, so its scores are
optimistic. The *held-out set* was run once at the chosen floor; its first ten
questions were written before any score was seen, the last seven were added with the
new records they test.

On fresh wordings keywords collapse ("I want my money back" shares no keyword with the
records) while embeddings still find the record, and keywords wrongly answered "Do you
ship internationally?" with the delivery record. The 12-record corpus rewrote the shop
sounding records in energy terms and added seven new ones; the floor was not retuned.
Some held-out misses were fixed after being seen, so that gain is partly by
construction, and the questions are few and written by the records' author.

The floor of 0.25 was chosen on the tuning set (0.35 dropped correct paraphrases, 0.20
let "Do you ship internationally?" through), so treat it as a sensible value for this
model, not an optimum; re-run `scripts/evaluate.py retrieval` if the model changes.
Of four free OpenRouter embedding models tried, three tied and the 8B one timed out,
so the first was kept.

