# Independent survivor review

Reviewed all 111 non-killed mutants from the real full run at source revision `6c197dc2d54b6bcc754c08497b4b237a76a61037`. Reviewer: Codex, reviewing the original Claude-authored runtime snapshot. This does not certify later production changes.

| Decision | Count | Evidence |
| --- | ---: | --- |
| Equivalent under the shipped application contracts | 90 | Exact per-mutant arguments, source hashes and context hashes in `independent-review.json` |
| Invalid generated variants | 2 | Mutations of defaults in the type-only `AIProvider.synthesize` Protocol stub; no concrete implementation body was mutated |
| Behavior-changing variants | 19 | Not approved; regression tests killed 18 in actual framework runs |
| Still unresolved | 1 | `JsonSessionRepository.check_writable__mutmut_9` |

## Real execution

Used Python 3.11.17 and pinned mutmut 3.8.0 on Linux in isolated copies of the historical source, with ordinary regression tests added. No production function was manually mutated, no monkeypatch substituted for a mutation run, and no custom mutation engine was used.

The first targeted run executed the 111 originally non-killed mutants: **15 killed, 94 survived, 2 no tests**, exit code 0. The second isolated run added Spanish fallback and startup diagnostic cases and executed three exact mutants: **3 killed**, exit code 0. The selected set therefore has **18 killed, 90 reviewed equivalent survivors, 2 invalid stub variants, and 1 unapproved survivor**. A framework execution exit code of zero is not a mutation quality pass.

The original source hashes match for all 14 reviewed source files. All 111 full unified diffs match the historical hashes after replacing only mutmut's changing status header with its original status. Raw diffs and both hashes are retained in `exact-diffs/` and `diff-verification.json`; the three second-run diffs are checked separately.

The new tests cover:

- URL prefixes ending in X and HTTP failures carrying schema-valid success bodies.
- Legitimate model/voice values named XXXX.
- Replaying saved version 2 requests after restart without repeating work. The fixture was serialized by the real unmutated application; it preserves pre-existing request hashes.
- Cancelling pending extraction when a turn is cancelled during safety triage.
- Keeping Spanish abstention replies in Spanish, including a retrieved but unanswerable question.
- Accurate startup errors for empty or ambiguous flow configuration.
- Rejecting malformed embedding entries before a later numeric overflow can escape.

Current focused validation: **89 affected application tests passed**, plus **31 mutation-tooling tests passed**. Ruff passed for the changed test modules. Historical snapshot baselines and real framework logs/results are stored alongside this report.

## Remaining storage gap

The write probe changes `write(b"")` to `write(b"XXXX")`. These differ under a filesystem/process limit that permits creating empty files but rejects data writes. A real Linux child process with `RLIMIT_FSIZE=0` successfully wrote zero bytes and rejected four bytes with errno 27. See `quota-probe.json` and the probe snapshot. This proves a counterexample; it is **not** a framework kill. The variant remains unapproved, pending an explicit readiness contract and its regression test.

## Gate status

`reports/mutation-review.json` contains 92 exact historical approvals and 19 unapproved behavior-changing variants, with follow-up statuses. The historical full-run gate correctly fails with 19 unapproved results; its generated summary is retained as `historical-gate-summary.json`. Targeted reruns do not rewrite historical full-run totals. A fresh complete run against the latest source, followed by review of its exact diffs, is still required. No current whole-project mutation pass is claimed.

## Subsequent storage fix

The remaining gap is now resolved in later source. The probe performs a real one-byte write using an automatically removed temporary file and maps denied writes to `storage_unavailable`. Real POSIX size-limit tests verify both blocked writes and successful minimal writes, including cleanup. **All six variants of the repaired function were killed by mutmut 3.8.0**, with no approvals or exclusions. The full frozen Linux unit/integration suite passed **2,417 tests**. See `../storage-readiness-fix/summary.json`. The historical counts above are unchanged; this targeted run does not certify the full latest project.

## Completed full follow-up

The subsequent fresh full run in `reports/mutation-evidence/full-022907f6/` passed: 4,887 variants checked, 4,770 killed, 115 independently reviewed equivalents, 2 invalid Protocol variants, and zero unresolved results. All 2,420 unit/integration tests passed. These are separate newer results; the historical evidence above remains unchanged.
