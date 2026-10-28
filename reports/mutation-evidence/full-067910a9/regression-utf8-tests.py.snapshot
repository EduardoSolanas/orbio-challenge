"""Authored UTF-8 configuration must survive a host with a non-UTF-8 locale."""

from __future__ import annotations

import codecs
import json
import locale
from datetime import datetime

import pytest

from backend_api import bookings
from gateway.conversation.flows import load_flows
from gateway.conversation.safety import load_examples
from gateway.localization import load_locales


@pytest.fixture
def non_utf8_locale():
    previous = locale.setlocale(locale.LC_CTYPE)
    try:
        locale.setlocale(locale.LC_CTYPE, "C")
        if codecs.lookup(locale.getpreferredencoding(False)).name == "utf-8":
            pytest.skip("Python UTF-8 mode overrides the process locale")
        yield
    finally:
        locale.setlocale(locale.LC_CTYPE, previous)


def test_spanish_safety_examples_load_under_a_non_utf8_locale(
    tmp_path, non_utf8_locale
):
    document = {"emergency": ["Huele muchísimo a gas"], "ordinary": ["¿Cuánto debo?"]}
    path = tmp_path / "safety.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    examples = load_examples(path)

    assert examples.emergency == tuple(document["emergency"])
    assert examples.ordinary == tuple(document["ordinary"])


def test_spanish_translations_load_under_a_non_utf8_locale(tmp_path, non_utf8_locale):
    for language, greeting in [("en", "Hello"), ("es", "¿Qué tal?")]:
        (tmp_path / f"{language}.json").write_text(
            json.dumps({"greeting": greeting}, ensure_ascii=False), encoding="utf-8"
        )

    catalog = load_locales(tmp_path)

    assert catalog.text("greeting", "en") == "Hello"
    assert catalog.text("greeting", "es") == "¿Qué tal?"


def test_named_flows_load_under_a_non_utf8_locale(tmp_path, non_utf8_locale):
    document = {
        "support_request": {
            "intent": "start_intake",
            "steps": ["collect_details", "review_details"],
        },
        "visita_técnica": {
            "intent": "book_technician",
            "steps": ["ask_account", "verify_account", "offer_windows"],
        },
    }
    path = tmp_path / "flows.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    flows = load_flows(path)

    assert set(flows) == set(document)
    assert flows["visita_técnica"].name == "visita_técnica"


def test_availability_json_loads_under_a_non_utf8_locale(
    tmp_path, non_utf8_locale, monkeypatch
):
    document = {
        "days_ahead": 0,
        "windows": [["09:00", "11:00"]],
        "description": "Visitas técnicas",
    }
    path = tmp_path / "availability.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(bookings, "AVAILABILITY_PATH", path)
    store = bookings.BookingStore(clock=lambda: datetime(2026, 10, 26, 8))
    try:
        assert store.open_slots() == [
            {"starts_at": "2026-10-26T09:00", "ends_at": "2026-10-26T11:00"}
        ]
    finally:
        store.close()
