"""The judge keeps one file per judged reply, not one per test."""

from __future__ import annotations

import json
from pathlib import Path

from tests.e2e import judge_config
from tests.e2e.judge_config import write_artifact

NODE = "tests/e2e/test_x.py::test_journey[chromium]"


def names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def test_each_judged_reply_of_a_test_keeps_its_own_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(judge_config, "_checkpoints", {})

    write_artifact(tmp_path, NODE, {"verdict": "first"})
    write_artifact(tmp_path, NODE, {"verdict": "second"})

    files = names(tmp_path)
    assert (
        len(files) == 2
        and files[0].endswith("__01.json")
        and files[1].endswith("__02.json")
    )
    assert [
        json.loads((tmp_path / name).read_text("utf-8"))["verdict"] for name in files
    ] == ["first", "second"]


def test_a_new_run_replaces_the_files_of_the_previous_run(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(judge_config, "_checkpoints", {})
    for verdict in ("a", "b", "c"):
        write_artifact(tmp_path, NODE, {"verdict": verdict})
    (tmp_path / "tests_e2e_test_x.py_test_journey_chromium_.json").write_text(
        "{}", "utf-8"
    )  # the old single-file name

    monkeypatch.setattr(judge_config, "_checkpoints", {})  # a new process
    write_artifact(tmp_path, NODE, {"verdict": "only"})

    files = names(tmp_path)
    assert (
        len(files) == 1
        and json.loads((tmp_path / files[0]).read_text("utf-8"))["verdict"] == "only"
    )


def test_two_tests_do_not_touch_each_others_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(judge_config, "_checkpoints", {})

    write_artifact(tmp_path, NODE, {"verdict": "one"})
    write_artifact(
        tmp_path, "tests/e2e/test_x.py::test_other[chromium]", {"verdict": "two"}
    )

    assert len(names(tmp_path)) == 2
