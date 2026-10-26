"""Locale API and startup validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.errors import LOCALIZATION_INVALID_MESSAGE
from gateway.localization import load_locales
from gateway.main import create_app
from tests.integration.support.api import (
    ScriptedProvider,
    build_settings,
    running_client,
)
from tests.support.locale_files import write_locales


def shipped(language: str) -> dict[str, str]:
    return dict(load_locales().mapping(language))


async def test_locale_route_serves_only_the_selected_dictionary(client):
    english = await client.get("/api/locales/en")
    spanish = await client.get("/api/locales/es")

    assert english.status_code == 200
    assert spanish.status_code == 200
    assert english.json() == shipped("en")
    assert spanish.json() == shipped("es")
    assert set(english.json()) == set(spanish.json())


async def test_locale_route_revalidates_cached_responses(client):
    response = await client.get("/api/locales/en")
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize(
    "path",
    [
        "/api/locales/fr",
        "/api/locales/en.json",
        "/api/locales/EN",
        "/api/locales/%2e%2e%2fetc%2fpasswd",
        "/api/locales/..%2f..%2fetc%2fpasswd",
    ],
)
async def test_unknown_or_traversal_locale_paths_return_sanitized_404(client, path):
    response = await client.get(path)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert "passwd" not in response.text
    assert "en.json" not in response.text


@pytest.mark.parametrize(
    "english,spanish",
    [
        ({"greeting": "Hello"}, {"greeting": "Hola", "actions.send": "Enviar"}),
        (
            {"speech.play": "Play reply {ordinal}"},
            {"speech.play": "Reproducir respuesta"},
        ),
        ({"greeting": 1}, {"greeting": "Hola"}),
        ({"greeting": "Hello"}, {"greeting": "Hola", "actions.send": "   "}),
    ],
)
async def test_invalid_dictionaries_fail_startup(tmp_path: Path, english, spanish):
    locales = write_locales(tmp_path / "locales", english, spanish)
    provider = ScriptedProvider()
    app = create_app(
        settings=build_settings(tmp_path / "sessions"),
        provider=provider,
        locales_dir=locales,
    )

    with pytest.raises(RuntimeError) as caught:
        async with running_client(app):
            pytest.fail("startup must not complete with invalid translations")

    assert str(caught.value) == LOCALIZATION_INVALID_MESSAGE
    assert provider.close_count == 1


async def test_malformed_dictionary_fails_startup(tmp_path: Path):
    locales = tmp_path / "locales"
    locales.mkdir()
    (locales / "en.json").write_text("{not json", encoding="utf-8")
    (locales / "es.json").write_text('{"greeting": "Hola"}', encoding="utf-8")
    provider = ScriptedProvider()
    app = create_app(
        settings=build_settings(tmp_path / "sessions"),
        provider=provider,
        locales_dir=locales,
    )

    with pytest.raises(RuntimeError) as caught:
        async with running_client(app):
            pytest.fail("startup must not complete with malformed translations")

    assert str(caught.value) == LOCALIZATION_INVALID_MESSAGE
    assert provider.close_count == 1


async def test_missing_dictionary_file_fails_startup(tmp_path: Path):
    locales = tmp_path / "locales"
    locales.mkdir()
    (locales / "en.json").write_text('{"greeting": "Hello"}', encoding="utf-8")
    provider = ScriptedProvider()
    app = create_app(
        settings=build_settings(tmp_path / "sessions"),
        provider=provider,
        locales_dir=locales,
    )

    with pytest.raises(RuntimeError) as caught:
        async with running_client(app):
            pytest.fail("startup must not complete without both dictionaries")

    assert str(caught.value) == LOCALIZATION_INVALID_MESSAGE
    assert provider.close_count == 1


async def test_shipped_dictionaries_cover_every_error_code(client):
    body = (await client.get("/api/locales/en")).json()
    from gateway.errors import ERRORS

    for code in ERRORS:
        assert body[f"errors.{code}"]
        assert body[f"errors.{code}"] == ERRORS[code].message


async def test_greeting_is_available_in_both_languages(client):
    english = (await client.get("/api/locales/en")).json()
    spanish = (await client.get("/api/locales/es")).json()

    assert english["greeting"].startswith("Hi,")
    assert spanish["greeting"].startswith("¡Hola!")
    assert english["greeting"] != spanish["greeting"]
