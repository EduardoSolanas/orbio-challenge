# Storage readiness fix

The previous probe wrote zero bytes, which can succeed even when storage rejects data writes. It also risked leaving a named probe file behind when a buffered flush failed.

The probe now writes and flushes one byte using `tempfile.TemporaryFile`, which automatically removes the file. Storage failures still become the existing retryable `storage_unavailable` error used by startup and health checks. Startup test injection targets the actual temporary-file operation.

Two real POSIX filesystem-limit regressions prove that a zero-byte file-size limit reports unavailable storage, a one-byte limit permits the minimal write, and neither outcome leaves a file behind. Limits and signal handlers are restored after each case. The tests skip only on platforms without POSIX resource limits; both ran successfully in Linux.

Validation in an isolated snapshot with Python 3.11.17 and pinned mutmut 3.8.0:

- Full unit and integration suite: **2,417 passed**.
- Every generated variant of `JsonSessionRepository.check_writable`: **6 killed out of 6 (100%)**.
- No surviving, untested, timed-out, skipped or approved variants in this targeted function run.
- Changed storage/startup modules passed Ruff checks and formatting checks.

Raw framework output, all results (including unrelated not-checked variants), exact generated diffs, baseline output, source/test snapshots and hashes are retained here. `summary.json` distinguishes the six executed variants from 4,807 generated for the project. No project-wide mutation score is claimed from this targeted run. The historical behavior-changing variant was superseded by correcting the source; it was not reclassified as equivalent or falsely recorded as killed.

## Completed full follow-up

The subsequent fresh full run in `reports/mutation-evidence/full-022907f6/` passed: 4,887 variants checked, 4,770 killed, 115 independently reviewed equivalents, 2 invalid Protocol variants, and zero unresolved results. All 2,420 unit/integration tests passed. These are separate newer results; the historical evidence above remains unchanged.
