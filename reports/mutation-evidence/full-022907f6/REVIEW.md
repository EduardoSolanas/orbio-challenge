# Fresh full mutation result

**Gate passed. Every non-equivalent generated variant was killed.**

| Result | Count |
| --- | ---: |
| Generated and checked | 4,887 / 4,887 |
| Killed by tests | 4,770 |
| Independently reviewed equivalents | 115 |
| Invalid type-only Protocol variants | 2 |
| Unresolved, unexecuted, timed out, suspicious, skipped | 0 |
| Unit and integration baseline | 2,420 passed |

The raw kill rate is **97.6059%**. The contract's non-equivalent mutation score is **100%**: `4770 / (4887 - 115 - 2)`. Equivalents and invalid variants are not kills. The two invalid variants alter defaults on an ellipsis-only `AIProvider` Protocol method, rather than a concrete implementation.

## Execution and scope

Used Python 3.11.17, pinned mutmut 3.8.0, Linux and a fresh cache. The complete configured runtime scope is `gateway/`, `backend_api/`, and `shared/`. Statement-wide regex exclusions were removed because they could also hide meaningful changes. Documented wiring/schema/prompt file exclusions remain; module-level declarations are outside the framework's mutation operator scope. Neither the score threshold nor timeouts were relaxed.

mutmut generated, isolated, selected tests for and executed every variant. No production function was manually mutated and no custom mutation runner or monkeypatch substituted for framework execution. Commands are retained in `run.sh.snapshot`; `environment.json` records actual versions. The full baseline and raw run/results are retained, including all killed lines in `mutation-results-complete.txt`.

An earlier exploratory pass exposed real gaps in booking conflict recovery and implicit text encoding. Eight regression cases were added:

- Four booking cases verify that only an already-booked response for the correct account and exact window can be recovered; a different window, unavailable slot or backend failure preserves the correct error and saved session.
- Four real-locale cases verify UTF-8 safety examples, Spanish translations, named flows and availability JSON when the host's default codec is not UTF-8.

Those focused tests passed, and a separate pinned-framework run killed all ten initially targeted survivors. A new isolated full snapshot containing these tests then passed the 2,420-test baseline and completed all 4,887 variants. The corrected storage probe's six variants were all killed in this full run as well.

## Independent review

All 117 non-killed diffs were freshly captured from the completed framework run and checked against their SHA-256 hashes. `independent-review.json` records each exact argument, source-file and function hash, framework status and reviewer decision. Source context includes fixed schema ASCII checks, positive retry budgets and actual HTTPX defaults in `review-context.json`. Prior arguments were rechecked against unchanged function ASTs and the current application context; their provenance is explicit. New SQL, codec, HTTP and copy-semantics cases were inspected individually. No broad function-level or automatic approvals were used. The verifier's own storage repair has no excluded variants.

`mutation-summary.json` is the output of the real report gate, which returned success with 4,887 generated, 4,770 killed, 115 reviewed equivalents and 2 reviewed invalid variants.

## Provenance and limits

The tested content is the frozen working-tree snapshot `022907f634b5bdfc87a012e15bd2271609c204324fa4cc2e328828268bc4ab04`, not a claim of a clean committed checkout. `source-snapshot.tar.gz`, `snapshot.json` and `workspace-verification.json` preserve the source and hashes of all 345 input files. They matched the current working files before the report/README updates. `source_revision` follows the existing policy of recording the last committed gated-input revision; `source_snapshot_revision` identifies the actual tested dirty content.

The exploratory `full-067910a9` run was stopped after 3,897 checks and was not a complete pass, despite mutmut's zero stopping exit code. Its 990 unexecuted variants remain explicit. None of those partial totals are used here.

Browser journeys, live providers and image builds were not rerun in this task. This report certifies the stated mutation scope and the full unit/integration baseline.
