"""Runner-generated E2E evidence (TC §5, IC §13.4).

These pytest hooks observe the real collection and the real setup/call/teardown
phases of every `tests/e2e` node and write `reports/e2e-collection.json` and
`reports/e2e-results.json`. Nothing is filled in by hand: a journey that did not
run has no outcome, a journey that stopped early reports only the checkpoints it
reached, and a rerun is counted from repeated setup phases.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

E2E_PREFIX = "tests/e2e/"
COLLECTION_FILE = "e2e-collection.json"
RESULTS_FILE = "e2e-results.json"

_state: dict[str, Any] = {
    "phases": {},
    "setups": {},
    "checkpoints": {},
    "durations": {},
    "failures": {},
    "deselected": [],
    "collection_errors": [],
    "case_ids": {},
}


def _is_e2e(nodeid: str) -> bool:
    return nodeid.startswith(E2E_PREFIX)


def source_revision(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def browser_name(config: Any) -> str:
    browsers = config.getoption("browser", default=None) or ["chromium"]
    return browsers[0]


def _case_id(item: Any) -> str | None:
    callspec = getattr(item, "callspec", None)
    return callspec.params.get("case_id") if callspec is not None else None


def _write(config: Any, name: str, payload: dict[str, Any]) -> None:
    target = Path(config.rootpath) / "reports" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def pytest_deselected(items: list[Any]) -> None:
    _state["deselected"].extend(item.nodeid for item in items if _is_e2e(item.nodeid))


def pytest_collectreport(report: Any) -> None:
    if report.failed and _is_e2e(report.nodeid or E2E_PREFIX):
        _state["collection_errors"].append(
            {"nodeid": report.nodeid, "error": str(report.longrepr)[:500]}
        )


def pytest_collection_finish(session: Any) -> None:
    items = [item for item in session.items if _is_e2e(item.nodeid)]
    for item in items:
        _state["case_ids"][item.nodeid] = _case_id(item)
    config = session.config
    _write(
        config,
        COLLECTION_FILE,
        {
            "source_revision": source_revision(Path(config.rootpath)),
            "browser": browser_name(config),
            "collected": [
                {
                    "nodeid": item.nodeid,
                    "case_id": _case_id(item),
                    "browser": browser_name(config),
                    "outcome": "collected",
                    "markers": sorted(mark.name for mark in item.iter_markers()),
                }
                for item in items
            ],
            "deselected": list(_state["deselected"]),
            "errors": list(_state["collection_errors"]),
        },
    )


def _phase_outcome(report: Any) -> str:
    if hasattr(report, "wasxfail"):
        return "xpassed" if report.passed else "xfailed"
    return report.outcome


def pytest_runtest_logreport(report: Any) -> None:
    if not _is_e2e(report.nodeid):
        return
    phases = _state["phases"].setdefault(report.nodeid, {})
    if report.when == "setup":
        _state["setups"][report.nodeid] = _state["setups"].get(report.nodeid, 0) + 1
        phases.clear()
    phases[report.when] = _phase_outcome(report)
    _state["durations"][report.nodeid] = _state["durations"].get(report.nodeid, 0.0) + float(
        report.duration
    )
    for name, value in report.user_properties:
        if name == "checkpoint":
            seen = _state["checkpoints"].setdefault(report.nodeid, [])
            if value not in seen:
                seen.append(value)
    if report.failed:
        crash = getattr(report.longrepr, "reprcrash", None)
        message = getattr(crash, "message", None) or str(report.longrepr)
        _state["failures"][report.nodeid] = f"{report.when}: {message}"[:600]


def _overall(phases: dict[str, str]) -> str:
    for phase in ("setup", "call", "teardown"):
        result = phases.get(phase)
        if result is None:
            return "not run"
        if result != "passed":
            return result if result in {"failed", "skipped", "xfailed", "xpassed"} else "failed"
    return "passed"


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    config = session.config
    if config.option.collectonly or not _state["phases"]:
        return
    outcomes = []
    for nodeid, phases in _state["phases"].items():
        outcomes.append(
            {
                "nodeid": nodeid,
                "case_id": _state["case_ids"].get(nodeid),
                "browser": browser_name(config),
                "outcome": _overall(phases),
                "phases": {phase: phases.get(phase, "not run") for phase in ("setup", "call", "teardown")},
                "reruns": _state["setups"].get(nodeid, 1) - 1,
                "checkpoints": list(_state["checkpoints"].get(nodeid, [])),
                "duration_seconds": round(_state["durations"].get(nodeid, 0.0), 3),
                "failure": _state["failures"].get(nodeid),
            }
        )
    _write(
        config,
        RESULTS_FILE,
        {
            "source_revision": source_revision(Path(config.rootpath)),
            "browser": browser_name(config),
            "exit_status": int(exitstatus),
            "outcomes": outcomes,
        },
    )
