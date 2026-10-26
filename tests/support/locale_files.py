"""Write a pair of locale files into a directory."""

from __future__ import annotations

import json
from pathlib import Path


def write_locales(
    directory: Path, english: dict[str, object], spanish: dict[str, object]
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "en.json").write_text(
        json.dumps(english, ensure_ascii=False), encoding="utf-8"
    )
    (directory / "es.json").write_text(
        json.dumps(spanish, ensure_ascii=False), encoding="utf-8"
    )
    return directory
