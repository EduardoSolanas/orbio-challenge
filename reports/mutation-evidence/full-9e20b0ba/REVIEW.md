# Fresh full mutation verification

**Gate passed: all non-equivalent generated variants were killed.**

| Result | Count |
| --- | ---: |
| Generated and checked | 5,017 / 5,017 |
| Killed by actual tests | 4,898 |
| Independently reviewed equivalents | 117 |
| Invalid type-only Protocol variants | 2 |
| Unresolved, unexecuted, timed out, suspicious, skipped | 0 |
| Full unit/integration baseline | 2,500 passed |

Raw kill rate: **97.6281%**. The contract's non-equivalent mutation score is **100%**: `4898 / (5017 - 117 - 2)`. Equivalent and invalid variants are never counted as kills.

## Execution

Python 3.11.17 and pinned mutmut 3.8.0 ran on Linux from an empty cache. The framework generated, isolated, selected tests for and executed every variant using 24 workers. Required scope remained all configured eligible functions in `gateway/`, `backend_api/` and `shared/`, including uncovered lines. Statement-wide regex exclusions remain empty; the documented routing/helper, schema and prompt file exclusions and the framework's module-level declaration limits remain explicit. Framework timeout settings and the score threshold were not relaxed. Commands are in `run.sh.snapshot`; versions are in `environment.json`.

Production functions were not hand-mutated and no custom mutation engine or monkeypatch substituted for execution. The integration fault is an ordinary deterministic failure of the external backend dependency.

## What the first pass exposed

The superseded `full-09f307c6` pass found four parser variants that loop forever. Its elapsed-time assertions ran after the call and could not fail a loop that never returned. The parser unit module now enforces its existing two-second requirement during execution with a POSIX interval timer that fails the active test. Handler/timer state is restored. This is a test failure on a real latency requirement, not a framework timeout reclassified as a kill. Windows retains the existing direct elapsed-time checks; mutmut execution is Linux-only.

Two other survivors affected verification state. Credentials can succeed while continuing the requested flow fails to fetch backend data; the verified grant then exists while the stored flow remains. After an intervening turn ends that old request, another verification must not revive it. Two public-HTTP integration cases cover installation and booking, using a temporary backend-fetch failure. They preserve successful verification and check that the old action does not reappear.

A fresh focused framework run killed all six variants: `extract_json` 21, 22, 31 and 33, and `_awaiting_verification` 3 and 5. Its actual diffs and raw results are retained in `full-09f307c6/`. The final complete run in this directory independently killed all six again. The verifier-authored storage probe also killed all six of its variants.

## Exact review and hash checks

All 119 non-killed diffs were freshly exported after the full run. Each raw diff SHA-256 was checked against the captured framework output; each source-file hash was checked against the frozen snapshot manifest. `independent-review.json` stores every exact decision, rationale, function/source hash and evidence path. Prior arguments were re-evaluated against current callers and error handling, rather than carrying over stale approvals. Changed summary-error paths and the new parser comparison have explicit fresh arguments.

For example, a reasoning span START is at `<`, whereas an object candidate is at `{`; their positions cannot be equal, so `<` versus `<=` there is equivalent. A span END can equal the next object position: that distinct variant could loop and is killed by the new deadline test. The review does not classify all comparison changes alike.

Summary diagnostics are reviewed against the published HTTP/state contract and current fixed unexpected-error response; private diagnostic wording is not a business value. This argument does not assume `Slots` uses Literal validation: its fields are StrictStr. Fixed-schema ASCII checks, HTTPX behavior and positive retry budgets are captured in `review-context.json`.

The two invalid cases modify defaults on the ellipsis-only `AIProvider.synthesize` typing stub. No concrete implementation body changes. No other untested variant is approved by that reasoning. No broad function-level approvals or automatic classifiers were used; the review builder still emits pending drafts only.

The existing report checker trusts the review manifest and does not itself regenerate/compare diffs. This run separately performed those fresh diff/source hash checks before invoking the actual gate. `mutation-summary.json` is the successful gate's generated output, not hand-entered totals.

## Provenance and limits

Tested content: `snapshot:9e20b0ba06ee69b3d759455dc231fc6d0b29ff6b1f5943bb39267ea33d8bb27b`, based on HEAD `1c64a32fc61a1da014cc12c105877a878ff70579` plus the test repairs. `source-snapshot.tar.gz` and `snapshot.json` preserve all 339 input files. All matched the working files before report documentation was updated, as recorded in `workspace-verification.json`.

`source_revision` follows the repository policy of naming the last committed gated-input revision; `source_snapshot_revision` identifies the actual tested dirty contents. This is not a claim that the archive equals a clean checkout of that commit.

The earlier pass was stopped with 612 variants unchecked and four framework timeouts, despite mutmut's zero stopping exit code. Its incomplete results contribute nothing to the score above.

Browser journeys, live-provider checks and image builds were not rerun. This report covers the stated mutation scope and full unit/integration baseline.
