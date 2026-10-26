"""Which booking store the Backend API app uses."""

from __future__ import annotations

import httpx

from backend_api.bookings import BookingStore
from backend_api.main import DATA_PATH_ENV, create_backend_api_app


async def post_booking(app, order_number: str, starts_at: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backend"
    ) as client:
        return await client.post(
            "/internal/v1/bookings",
            json={"order_number": order_number, "starts_at": starts_at},
        )


def test_the_store_comes_from_the_data_path_setting(monkeypatch, tmp_path) -> None:
    path = tmp_path / "data" / "bookings.db"
    monkeypatch.setenv(DATA_PATH_ENV, str(path))

    create_backend_api_app()

    assert path.exists()


async def test_an_injected_store_is_the_one_that_takes_the_booking(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(DATA_PATH_ENV, str(tmp_path / "other.db"))
    store = BookingStore(None)
    window = store.open_slots()[0]["starts_at"]

    response = await post_booking(
        create_backend_api_app(bookings=store), "NS-000123", window
    )

    assert response.status_code == 200
    assert store.appointment("NS-000123")["starts_at"] == window
    assert (
        not (tmp_path / "other.db").exists()
        or BookingStore(str(tmp_path / "other.db")).appointment("NS-000123") is None
    )
