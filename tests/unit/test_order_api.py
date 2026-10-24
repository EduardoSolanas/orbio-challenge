"""Unit tests for the private Order API service and its gateway client.

The Order API owns the fixed fictional fixture and the credential rule; the
gateway only speaks HTTP to it. These tests pin the catalog, the ASCII-only
postcode rule, the identical failure surface for every mismatch, the allowlisted
response shape and the client's protocol-faithful error mapping.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.errors import (
    INVALID_ORDER_API_RESPONSE,
    ORDER_API_TIMEOUT,
    ORDER_API_UNAVAILABLE,
    AppError,
)
from app.order_api import (
    DEMO_VERIFICATION_CODE,
    FIXTURE_ORDER_NUMBER,
    FIXTURE_POSTCODE,
    NOT_FOUND,
    OrderRecord,
    VERIFICATION_FAILED,
    create_order_api_app,
    credentials_match,
    normalize_postcode,
    valid_verification_code,
)
from app.order_client import OrderApiClient

OTHER_ORDER = "NS-000999"
WRONG_CODE = "123456"


def fixture_app():
    return create_order_api_app()


def test_fixture_record_is_exact_and_allowlisted() -> None:
    app = fixture_app()
    client = TestClient(app)
    response = client.get(f"/internal/v1/orders/{FIXTURE_ORDER_NUMBER}")

    assert response.status_code == 200
    assert response.json() == {
        "order_number": "NS-000123",
        "first_name": "Casey",
        "item_names": ["Electricity and gas supply"],
        "status": "active",
        "dispatch_date": None,
        "delivery_date": None,
    }
    assert set(response.json()) == {
        "order_number",
        "first_name",
        "item_names",
        "status",
        "dispatch_date",
        "delivery_date",
    }


def test_unknown_order_returns_not_found_without_record_data() -> None:
    client = TestClient(fixture_app())
    response = client.get(f"/internal/v1/orders/{OTHER_ORDER}")

    assert response.status_code == 404
    assert response.json() == {"error": NOT_FOUND}


def test_health_is_private_and_minimal() -> None:
    client = TestClient(fixture_app())
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_verification_success_returns_only_the_boolean() -> None:
    client = TestClient(fixture_app())
    response = client.post(
        "/internal/v1/order-verifications",
        json={
            "order_number": FIXTURE_ORDER_NUMBER,
            "delivery_postcode": FIXTURE_POSTCODE,
            "verification_code": DEMO_VERIFICATION_CODE,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"verified": True, "error": None}


@pytest.mark.parametrize(
    "order_number,postcode,code",
    [
        (FIXTURE_ORDER_NUMBER, FIXTURE_POSTCODE, WRONG_CODE),
        (FIXTURE_ORDER_NUMBER, "ZZ1 1ZZ", DEMO_VERIFICATION_CODE),
        (OTHER_ORDER, FIXTURE_POSTCODE, DEMO_VERIFICATION_CODE),
        (None, FIXTURE_POSTCODE, DEMO_VERIFICATION_CODE),
        (FIXTURE_ORDER_NUMBER, FIXTURE_POSTCODE, "24681"),
        (FIXTURE_ORDER_NUMBER, FIXTURE_POSTCODE, "٢٤٦٨١٠"),
    ],
)
def test_every_mismatch_returns_the_same_failure_body(
    order_number: str | None, postcode: str, code: str
) -> None:
    client = TestClient(fixture_app())
    response = client.post(
        "/internal/v1/order-verifications",
        json={
            "order_number": order_number,
            "delivery_postcode": postcode,
            "verification_code": code,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"verified": False, "error": VERIFICATION_FAILED}


def test_postcode_normalization_is_ascii_only_trim_and_uppercase() -> None:
    assert normalize_postcode(" sw1a 1aa ") == FIXTURE_POSTCODE
    assert normalize_postcode("SW1A 1AA") == FIXTURE_POSTCODE
    assert normalize_postcode("sw1a  1aa") != FIXTURE_POSTCODE


@pytest.mark.parametrize(
    "value,expected",
    [
        ("246810", True),
        ("999999", True),
        ("000000", True),
        ("24681", False),
        ("2468100", False),
        ("", False),
        ("246 810", False),
        ("٢٤٦٨١٠", False),
        ("24681a", False),
        ("ABC123", False),
    ],
)
def test_verification_code_is_exactly_six_ascii_digits(value: str, expected: bool) -> None:
    assert valid_verification_code(value) is expected


def test_credentials_match_binds_all_three_values() -> None:
    assert credentials_match(FIXTURE_ORDER_NUMBER, " sw1a 1aa ", DEMO_VERIFICATION_CODE) is True
    assert credentials_match(FIXTURE_ORDER_NUMBER, FIXTURE_POSTCODE, WRONG_CODE) is False
    assert credentials_match(OTHER_ORDER, FIXTURE_POSTCODE, DEMO_VERIFICATION_CODE) is False


def test_order_records_are_copies_not_shared_state() -> None:
    app = fixture_app()
    client = TestClient(app)
    first = OrderRecord.model_validate(client.get(f"/internal/v1/orders/{FIXTURE_ORDER_NUMBER}").json())
    second = OrderRecord.model_validate(client.get(f"/internal/v1/orders/{FIXTURE_ORDER_NUMBER}").json())

    assert first == second
    assert first is not second
    assert first.item_names is not second.item_names


def _client(handler) -> OrderApiClient:
    """Real client over a protocol-faithful in-process HTTP transport."""

    return OrderApiClient(
        "http://order-api.test", timeout_seconds=1.0, transport=httpx.MockTransport(handler)
    )


async def test_client_verify_sends_the_exact_internal_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"verified": True, "error": None})

    client = _client(handler)
    result = await client.verify_order(FIXTURE_ORDER_NUMBER, "SW1A 1AA", DEMO_VERIFICATION_CODE)

    assert result.verified is True
    assert result.error is None
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert str(seen[0].url) == "http://order-api.test/internal/v1/order-verifications"
    assert seen[0].headers["Accept"] == "application/json"
    assert json.loads(seen[0].content) == {
        "order_number": FIXTURE_ORDER_NUMBER,
        "delivery_postcode": "SW1A 1AA",
        "verification_code": DEMO_VERIFICATION_CODE,
    }


async def test_client_lookup_returns_the_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "order_number": FIXTURE_ORDER_NUMBER,
                "first_name": "Casey",
                "item_names": ["Electricity and gas supply"],
                "status": "active",
                "dispatch_date": None,
                "delivery_date": None,
            },
        )

    record = await _client(handler).lookup_order(FIXTURE_ORDER_NUMBER)

    assert record is not None
    assert record.order_number == FIXTURE_ORDER_NUMBER


async def test_client_lookup_maps_not_found_to_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": NOT_FOUND})

    assert await _client(handler).lookup_order(OTHER_ORDER) is None


@pytest.mark.parametrize(
    "status,expected_code",
    [
        (503, ORDER_API_UNAVAILABLE),
        (502, ORDER_API_UNAVAILABLE),
        (504, ORDER_API_TIMEOUT),
        (500, INVALID_ORDER_API_RESPONSE),
        (422, INVALID_ORDER_API_RESPONSE),
    ],
)
async def test_client_maps_unsuccessful_statuses(status: int, expected_code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "x"})

    with pytest.raises(AppError) as raised:
        await _client(handler).verify_order(FIXTURE_ORDER_NUMBER, FIXTURE_POSTCODE, DEMO_VERIFICATION_CODE)

    assert raised.value.code == expected_code


@pytest.mark.parametrize(
    "payload,expected_code",
    [
        ({"verified": "yes", "error": None}, INVALID_ORDER_API_RESPONSE),
        ({"verified": False, "error": "other"}, INVALID_ORDER_API_RESPONSE),
        ({"verified": True, "error": "verification_failed"}, INVALID_ORDER_API_RESPONSE),
    ],
)
async def test_client_rejects_malformed_verification_bodies(payload, expected_code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(AppError) as raised:
        await _client(handler).verify_order(FIXTURE_ORDER_NUMBER, FIXTURE_POSTCODE, DEMO_VERIFICATION_CODE)

    assert raised.value.code == expected_code


async def test_client_does_not_accept_a_404_as_a_successful_verification() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"verified": True, "error": None})

    with pytest.raises(AppError) as raised:
        await _client(handler).verify_order(FIXTURE_ORDER_NUMBER, FIXTURE_POSTCODE, DEMO_VERIFICATION_CODE)

    assert raised.value.code == INVALID_ORDER_API_RESPONSE


async def test_client_rejects_non_json_and_unexpected_lookup_fields() -> None:
    def non_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>nope</html>")

    with pytest.raises(AppError) as raised:
        await _client(non_json).verify_order(FIXTURE_ORDER_NUMBER, FIXTURE_POSTCODE, DEMO_VERIFICATION_CODE)
    assert raised.value.code == INVALID_ORDER_API_RESPONSE

    def extra_field(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "order_number": FIXTURE_ORDER_NUMBER,
                "first_name": "Casey",
                "item_names": ["Electricity and gas supply"],
                "status": "active",
                "dispatch_date": None,
                "delivery_date": None,
                "postcode": "SW1A 1AA",
            },
        )

    with pytest.raises(AppError) as raised:
        await _client(extra_field).lookup_order(FIXTURE_ORDER_NUMBER)
    assert raised.value.code == INVALID_ORDER_API_RESPONSE


@pytest.mark.parametrize(
    "error,expected_code",
    [
        (httpx.ConnectError("refused"), ORDER_API_UNAVAILABLE),
        (httpx.ConnectTimeout("slow"), ORDER_API_UNAVAILABLE),
        (httpx.ReadTimeout("slow"), ORDER_API_TIMEOUT),
        (httpx.WriteError("broken pipe"), INVALID_ORDER_API_RESPONSE),
    ],
)
async def test_client_maps_transport_failures(error, expected_code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(AppError) as raised:
        await _client(handler).lookup_order(FIXTURE_ORDER_NUMBER)

    assert raised.value.code == expected_code
    assert raised.value.retryable is True


async def test_client_encodes_the_order_number_in_the_lookup_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, json={"error": NOT_FOUND})

    await _client(handler).lookup_order("NS 000123")

    assert seen[0].method == "GET"
    assert str(seen[0].url) == "http://order-api.test/internal/v1/orders/NS%20000123"


async def test_client_encodes_path_separators_in_the_order_number() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, json={"error": NOT_FOUND})

    await _client(handler).lookup_order("NS/000123")

    assert str(seen[0].url) == "http://order-api.test/internal/v1/orders/NS%2F000123"


async def test_client_has_no_public_gateway_routes() -> None:
    app = fixture_app()

    assert app.title == "Northstar order API"
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    assert {route.path for route in app.routes} == {
        "/internal/v1/order-verifications",
        "/internal/v1/orders/{order_number}",
        "/health",
    }


def test_client_defaults_are_bounded_json_only_and_normalize_the_base_url() -> None:
    default = OrderApiClient("http://order-api.test")
    explicit = OrderApiClient("http://order-api.test/", timeout_seconds=0.25)

    assert default._client.timeout == httpx.Timeout(5.0)
    assert explicit._client.timeout == httpx.Timeout(0.25)
    assert str(explicit._client.base_url) == "http://order-api.test"
    assert explicit._client.headers["Accept"] == "application/json"
