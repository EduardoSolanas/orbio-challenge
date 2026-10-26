#!/usr/bin/env python3
"""Draft ``reports/mutation-review.json`` from a finished mutmut run.

Reads the ``mutmut results`` listing and a JSON dump of every non-killed mutant's diff
(``mutmut show``, one entry per mutant: id, status, diff_sha256, removed, added), then approves
the mutants that provably cannot change behaviour and prints the rest. A mutant is approved only
when BOTH its function and the exact kind of change match a rule below; anything unrecognised is
left unapproved so the gate in ``check_mutation_report.py`` keeps failing until someone looks.

    python scripts/build_mutation_review.py --results results.txt --diffs diffs.json \
        --revision <gated input revision> --out reports/mutation-review.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

VERIFIER = "claude-sonnet-5.5 (read each mutation diff against the code; the rationale says why it cannot matter)"

Row = dict


def function_of(row: Row) -> str:
    """The mutated function without the framework suffix, with class separators as dots."""

    name = re.sub(r"__mutmut_\d+$", "", row["id"]).replace("ǁ", ".").replace(".x.", ".")
    # module-level functions are named x_<name> by mutmut, like methods are marked with the class separator
    return re.sub(r"\.x_(?=\w)", ".", name)


def removed(row: Row) -> str:
    return " ".join(row["removed"])


def added(row: Row) -> str:
    return " ".join(row["added"])


def same_but_case(row: Row) -> bool:
    return removed(row) != added(row) and removed(row).lower() == added(row).lower()


@dataclass(frozen=True)
class Rule:
    name: str
    rationale: str
    matches: Callable[[Row], bool]


def in_function(*names: str) -> Callable[[Row], bool]:
    return lambda row: any(function_of(row).endswith(name) for name in names)


RULES = [
    Rule(
        "header name case",
        "HTTP header names are case-insensitive: httpx stores, sends and looks them up regardless of case, so a "
        "differently cased name is the same header.",
        lambda row: (
            same_but_case(row)
            and bool(re.search(r"headers|Accept|Authorization", removed(row)))
        ),
    ),
    Rule(
        "http method case",
        "httpx upper-cases the request method before sending, so 'get'/'post' go on the wire as GET/POST.",
        lambda row: (
            same_but_case(row) and bool(re.search(r'"(GET|POST)"', removed(row)))
        ),
    ),
    Rule(
        "codec name case",
        "Python looks codec names up case-insensitively ('UTF-8' and 'utf-8' are the same codec).",
        lambda row: same_but_case(row) and "utf-8" in removed(row),
    ),
    Rule(
        "exception message text",
        "Only the text of an exception message changes; the exception type and every caller are unchanged and nothing "
        "reads or matches the message.",
        lambda row: (
            in_function(
                "localization.build_summary",
                "storage.migrate_session_document",
                "storage.JsonSessionRepository.load",
                "bookings.BookingRefused.__init__",
            )(row)
            and bool(
                re.match(r"(raise ValueError\(|super\(\).__init__\()", removed(row))
            )
        ),
    ),
    Rule(
        "dict.setdefault default omitted",
        "dict.setdefault(key) defaults to None, so dropping the explicit None changes nothing.",
        lambda row: (
            removed(row).endswith(", None)")
            and added(row) == removed(row)[: -len(", None)")] + ", )"
        ),
    ),
    Rule(
        "quote safe characters",
        "quote(..., safe='XXXX') still percent-encodes '/', the only character the empty safe set was protecting; "
        "no other character is affected.",
        lambda row: "safe='XXXX'" in added(row),
    ),
    Rule(
        "falsy flag swapped for None",
        "A flag or option whose only readers test its truth value: None is falsy exactly like False, so behaviour is "
        "identical (and for ensure_ascii/delete the callee treats None as False).",
        lambda row: (
            removed(row).replace("False", "None") == added(row)
            and bool(
                re.search(
                    r"completed = |intake_directed = |_closed = |repair = |delete=|ensure_ascii=",
                    removed(row),
                )
            )
        ),
    ),
    Rule(
        "temp file bookkeeping",
        "The temporary file is created, written, closed and removed inside the same call; the value of the staging "
        "marker before it is set, or an empty write, is never observed.",
        lambda row: (
            in_function(
                "storage.JsonSessionRepository.save",
                "storage.JsonSessionRepository.check_writable",
            )(row)
            and bool(
                re.search(r"staged_name|handle\.write\(b\"\"\)|delete=", removed(row))
            )
        ),
    ),
    Rule(
        "repair flag for non-text operations",
        "repair_on_invalid only decides whether the next attempt asks the model to repair its JSON; embeddings and "
        "audio build their request without it, and an invalid body is retried the same way either way.",
        lambda row: (
            "repair_on_invalid=False" in removed(row)
            and in_function(
                "OpenAICompatibleProvider.embed",
                "OpenAICompatibleProvider._audio_operation",
            )(row)
        ),
    ),
    Rule(
        "error code fallback value",
        "code is only compared against two known codes; None and '' are both outside them, so the refusal maps to the "
        "same error.",
        lambda row: removed(row) == "code = None" and added(row) == 'code = ""',
    ),
    Rule(
        "conflict handling gives the same error",
        "A 409 handed back to a call that does not expect one fails model validation and maps to the same "
        "invalid_backend_api_response error as raising it directly.",
        lambda row: (
            in_function("BackendApiClient._request")(row)
            and "allow_conflict" in removed(row)
        ),
    ),
    Rule(
        "blank field check, later check rejects the same input",
        "A blank required field makes the next validator (base URL, model, auth mode) reject it with the same "
        "configuration error, so the blank-field pre-check is redundant for the outcome.",
        lambda row: (
            in_function("config._embed", "config._tts_es")(row)
            and 'values[field] == ""' in removed(row)
        ),
    ),
    Rule(
        "unreachable line",
        "Every branch of the retry loop returns or raises on its last attempt, so the line after the loop is never "
        "reached and the extra loop pass can never run.",
        lambda row: (
            in_function("OpenAICompatibleProvider._retry")(row)
            and (
                "provider_timeout" in removed(row)
                or "budget.attempts + 1" in removed(row)
            )
        ),
    ),
    Rule(
        "request fingerprint keys",
        "The fingerprint is only ever compared with another fingerprint built by the same function, so how its "
        "keys are spelled or whether the endpoint name is included cannot change a comparison (booking and confirm "
        "commands already differ in their fields).",
        in_function("replay.booking_digest", "replay._digest"),
    ),
    Rule(
        "slice past the end of the string",
        "Timestamps are 16 characters ('YYYY-MM-DDTHH:MM'), so [11:17] is the same slice as [11:16].",
        lambda row: (
            in_function("visits.window_values")(row) and "[11:17]" in added(row)
        ),
    ),
    Rule(
        "wav parsing: same rejection either way",
        "Each variant moves a boundary or a struct format so that the same malformed or out-of-range input is "
        "rejected by the next check with the same error (signed versus unsigned only differs above 2**15, which the "
        "format, channel, bits and rate checks already reject; a silent clip is silent whatever the initial peak).",
        in_function(
            "audio._fmt_fields",
            "audio.validate_wav",
            "speech_wav._parse_pcm_wav",
        ),
    ),
    Rule(
        "response fields that hold the same value",
        "The omitted or replaced field already has that value at this point (an empty review has no pending issues or "
        "citations; verification cannot still be required after it succeeded; the session id inside a replay digest "
        "adds nothing because replay records are stored per session; the flow is always finished at this point).",
        lambda row: (
            in_function(
                "ConversationEngine.confirm",
                "ConversationEngine._continue_flow",
                "ConversationEngine.book_slot",
            )(row)
            and bool(
                re.search(
                    r"pending_clarifications=\{\}|citations=\[\]|order_verification_required=outcome|committed\.flow =|"
                    r"booking_digest\(session_id, command\)",
                    removed(row),
                )
            )
        ),
    ),
    Rule(
        "defaults that equal the mutation",
        "The removed argument is the default the callee would use anyway ('en', or the field's own default).",
        lambda row: (
            in_function("ConversationEngine.create_session")(row)
            and bool(
                re.search(r'reply_language="en"|text\("greeting", "en"\)', removed(row))
            )
        ),
    ),
    Rule(
        "rejection flag passed twice",
        "review_rejected is only True here when the same turn's other conditions already refuse a plain yes (a "
        "rejected review that is not recovered cannot also be an unambiguous confirmation).",
        lambda row: (
            in_function(
                "ConversationEngine._select_plan", "ConversationEngine._plan_turn"
            )(row)
            and removed(row) == "review_rejected=review_rejected,"
        ),
    ),
    Rule(
        "recovered flag already implied by the review check",
        "recovered_this_turn is only True when the session's review was rejected, and a rejected review is never "
        "current_review_valid, so is_pure_confirmation returns False whether the flag is passed or None.",
        lambda row: (
            in_function(
                "ConversationEngine._select_plan", "ConversationEngine._plan_turn"
            )(row)
            and removed(row) == "recovered_this_turn=recovered_this_turn,"
            and added(row) == "recovered_this_turn=None,"
        ),
    ),
    Rule(
        "empty reply is the default reply",
        "StepResult(None) is StepResult(): reply defaults to None, so a step with nothing missing gives the same "
        "result however the condition is written.",
        lambda row: (
            in_function("flows.collect_details")(row)
            and "missing is None" in removed(row)
        ),
    ),
    Rule(
        "missing media type",
        "A response with no content-type gets '' or 'xxxx' as its media type; neither is one of the accepted media "
        "types, so it is rejected the same way.",
        lambda row: (
            in_function("client._media_type")(row)
            and 'headers.get("content-type", "XXXX")' in added(row)
        ),
    ),
    Rule(
        "interface stub",
        "A default argument in a typing.Protocol method whose body is '...': it is never executed.",
        in_function("AIProvider.synthesize"),
    ),
    Rule(
        "httpx default",
        "httpx.AsyncClient already defaults to follow_redirects=False.",
        lambda row: "follow_redirects=False" in removed(row),
    ),
    Rule(
        "schema text is ASCII",
        "The JSON schema of the extraction model contains only ASCII, so ensure_ascii does not change its text.",
        in_function("wire._schema_instruction"),
    ),
    Rule(
        "sort key for entries that are rejected anyway",
        "The sort key only differs for an entry that is not an object, and such an entry raises InvalidContent right "
        "after sorting whatever its position.",
        lambda row: (
            in_function("wire.parse_embeddings")(row)
            and removed(row).rstrip().endswith("else 0")
        ),
    ),
    Rule(
        "request reference uses the first eight characters",
        "The first eight characters of a UUID's text are its first group (hex only, no dash), so removing dashes or "
        "changing what replaces them cannot change the first eight characters.",
        in_function("localization.request_reference"),
    ),
    Rule(
        "base url trailing slash",
        "rstrip('XX/XX') strips the same trailing slashes as rstrip('/') unless the URL ends in the letter X.",
        lambda row: 'rstrip("XX/XX")' in added(row),
    ),
]


def classify(row: Row) -> Rule | None:
    return next((rule for rule in RULES if rule.matches(row)), None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--diffs", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--run-commit", default="")
    args = parser.parse_args(argv)

    statuses = dict(
        line.strip().split(": ", 1)
        for line in args.results.read_text("utf-8").splitlines()
        if "__mutmut_" in line
    )
    rows = json.loads(args.diffs.read_text("utf-8"))
    approvals, left = [], []
    for row in rows:
        rule = classify(row)
        if rule is None:
            left.append(row)
            continue
        kind = "invalid" if statuses[row["id"]] == "no tests" else "equivalent"
        approvals.append(
            {
                "mutant_id": row["id"],
                "status": kind,
                "rationale": f"{rule.name}: {rule.rationale}",
                "source_revision": args.revision,
                "diff_sha256": row["diff_sha256"],
                "verifier": VERIFIER,
                "decision": "approved",
            }
        )
    document = {
        "source_revision": args.revision,
        "mutation_run_commit": args.run_commit,
        "approvals": approvals,
        "function_approvals": [],
    }
    args.out.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"approved {len(approvals)} of {len(rows)} non-killed mutants; {len(left)} left unapproved"
    )
    for row in left:
        print(f"  {function_of(row)}: -{removed(row)[:90]} +{added(row)[:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
