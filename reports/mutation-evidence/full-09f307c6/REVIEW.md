# Superseded diagnostic pass

This is incomplete evidence, not a mutation pass. The full-scope run checked 4,405 of 5,017 variants before being superseded: 4,297 killed, 102 survived, 2 had no tests and 4 timed out; 612 were not checked. Its framework stop returned zero, which does not make the quality gate pass.

Investigation led to an enforced existing two-second parser-test budget and two integration cases covering failed continuation after successful credential verification. The separate fresh focused run killed all six affected variants, with its limited test selection recorded in the command snapshots. No production source was manually mutated.

The subsequent fresh complete run, `../full-9e20b0ba/REVIEW.md`, checked every variant and passed. None of these partial totals were merged into that result.
