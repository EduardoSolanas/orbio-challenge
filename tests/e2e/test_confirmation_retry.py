"""Confirmation retry replays an uncertain command after a lost response."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import body_of
from tests.support.fakes import FULL, receipt_for, summary_text, x_full

pytestmark = pytest.mark.browser_e2e  # controlled-provider browser workflow, never live Docker/provider proof

SUMMARY = summary_text("NS-000123", "damaged_item", "My order arrived broken", "high")


@pytest.mark.parametrize("case_id", [pytest.param("RETRY", id="RETRY")])
def test_confirmation_lost_response_retries_same_command(case_id, ui, server, provider):
    provider.script_extract(FULL, x_full())
    ui.open()
    sid = ui.current_session_id()
    assert ui.send(FULL).status == 200
    expect(ui.review).to_be_visible()

    first_body = None

    def drop_first_response(route):
        nonlocal first_body
        first_body = route.request.post_data
        route.fetch()
        route.abort("failed")

    ui.page.route("**/api/sessions/*/confirm", drop_first_response)
    ui.button("Confirm details").click()
    expect(ui.button("Retry")).to_be_visible()
    assert first_body

    ui.page.unroute("**/api/sessions/*/confirm", drop_first_response)
    with ui.page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/confirm")
    ) as pending:
        ui.button("Retry").click()
    assert pending.value.status == 200

    confirms = ui.confirm_posts()
    assert len(confirms) == 2
    expected_body = first_body.encode() if isinstance(first_body, str) else first_body
    assert confirms[0]["request_body"] == confirms[1]["request_body"] == expected_body
    assert body_of(confirms[0])["request_id"] == body_of(confirms[1])["request_id"]
    saved = ui.disk(sid)
    assert saved["status"] == "completed"
    receipt = receipt_for(sid)
    assert [m["text"] for m in saved["messages"]].count(receipt) == 1
    assert saved["summary"] == SUMMARY
