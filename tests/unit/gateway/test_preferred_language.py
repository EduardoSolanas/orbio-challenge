"""The welcome language comes from the browser's Accept-Language header."""

import pytest

from gateway.routes.http_support import preferred_language


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, "en"),
        ("", "en"),
        ("en-US,en;q=0.9", "en"),
        ("es", "es"),
        ("es-MX,es;q=0.9,en;q=0.8", "es"),
        ("ES-es", "es"),
        ("fr-FR,fr;q=0.9,es;q=0.5", "es"),
        ("en;q=0.4,es;q=0.9", "es"),
        ("fr-FR,de;q=0.8", "en"),
        ("es;q=0", "en"),
        ("es;q=oops,en", "en"),
        ("*", "en"),
    ],
)
def test_the_first_supported_language_wins_by_quality(header, expected) -> None:
    assert preferred_language(header) == expected
