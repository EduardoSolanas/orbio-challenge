"""Locale API, startup validation and catalog-backed bootstrap."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.errors import LOCALIZATION_INVALID_MESSAGE
from app.localization import LOCALES_DIR, load_locales
from app.main import create_app
from tests.integration.support.api import (
    ScriptedProvider,
    build_settings,
    running_client,
)


BOOTSTRAP = re.compile(
    r'<script type="application/json" id="bootstrap-locale">(.*?)</script>', re.DOTALL
)


def shipped(language: str) -> dict[str, str]:
    return dict(load_locales().mapping(language))


def write_locales(directory: Path, english: dict[str, object], spanish: dict[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "en.json").write_text(json.dumps(english, ensure_ascii=False), encoding="utf-8")
    (directory / "es.json").write_text(json.dumps(spanish, ensure_ascii=False), encoding="utf-8")
    return directory


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


async def test_index_embeds_the_english_catalog_without_duplicating_literals(client):
    response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    html = response.text
    assert "__BOOTSTRAP_LOCALE__" not in html
    match = BOOTSTRAP.search(html)
    assert match is not None
    assert json.loads(match.group(1)) == shipped("en")
    # Static labels are keyed, not hard-coded English duplicates.
    assert ">Send<" not in html
    assert ">New conversation<" not in html
    assert 'data-i18n="actions.send"' in html


async def test_index_escapes_json_that_could_close_the_script_element(tmp_path: Path):
    english = {"greeting": "</script><b>&\u2028\u2029", "actions.send": "Send"}
    spanish = {"greeting": "Hola", "actions.send": "Enviar"}
    locales = write_locales(tmp_path / "locales", english, spanish)
    app = create_app(
        settings=build_settings(tmp_path / "sessions"),
        provider=ScriptedProvider(),
        locales_dir=locales,
    )

    async with running_client(app) as client:
        response = await client.get("/")

    html = response.text
    assert "</script><b>" not in html
    assert "\u2028" not in html and "\u2029" not in html
    assert "\\u003c/script\\u003e\\u003cb\\u003e\\u0026" in html
    match = BOOTSTRAP.search(html)
    assert match is not None
    assert json.loads(match.group(1)) == english


@pytest.mark.parametrize(
    "path,media_type",
    [
        ("/static/js/main.js", "javascript"),
        ("/static/js/app.js", "javascript"),
        ("/static/css/base.css", "css"),
        ("/static/css/objects.css", "css"),
        ("/static/css/skins.css", "css"),
    ],
)
async def test_static_assets_revalidate_after_deployment(client, path, media_type):
    response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    # Browsers refuse a module script that is not served with a JavaScript type.
    assert media_type in response.headers["content-type"]


@pytest.mark.parametrize(
    "english,spanish",
    [
        ({"greeting": "Hello"}, {"greeting": "Hola", "actions.send": "Enviar"}),
        ({"speech.play": "Play reply {ordinal}"}, {"speech.play": "Reproducir respuesta"}),
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
    from app.errors import ERRORS

    for code in ERRORS:
        assert body[f"errors.{code}"]
        assert body[f"errors.{code}"] == ERRORS[code].message


async def test_greeting_is_available_in_both_languages(client):
    english = (await client.get("/api/locales/en")).json()
    spanish = (await client.get("/api/locales/es")).json()

    assert english["greeting"].startswith("Hi,")
    assert spanish["greeting"].startswith("¡Hola!")
    assert english["greeting"] != spanish["greeting"]
