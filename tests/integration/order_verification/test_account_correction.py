"""Correcting the account, or typing the credentials loosely, while a booking waits for verification.

Found by talking to the running chat: "sorry, I meant NS-000123" used to turn the waiting booking
into a support request and lose it, and a postcode typed as ``sw1a1aa`` or a code with a trailing
space failed verification. Real gateway and Backend API; only the AI provider is scripted.
"""

from __future__ import annotations

from backend_api.main import FIXTURE_ORDER_NUMBER
from gateway.localization import text
from tests.integration.order_verification.support import (
    create_session,
    send_turn,
    verify,
)
from tests.integration.support.api import make_extraction, read_session
from tests.integration.support.rag import grounded

OTHER_ACCOUNT = "NS-000456"


def booking_for(account: str):
    return make_extraction(
        intent="book_technician", candidates={"order_number": account}
    )


async def waiting_booking(
    client, provider, session_dir, account: str = OTHER_ACCOUNT
) -> tuple[str, int]:
    session_id = await create_session(client)
    provider.script_extraction(booking_for(account))
    first = (
        await send_turn(client, session_id, "Book a technician for NS-000456", 0)
    ).json()
    assert first["order_verification_required"] is True
    assert read_session(session_dir, session_id)["flow"]["name"] == "book_technician"
    return session_id, first["revision"]


async def test_correcting_the_account_keeps_the_booking_waiting_for_verification(
    client, provider, session_dir
):
    session_id, revision = await waiting_booking(client, provider, session_dir)
    provider.script_extraction(
        make_extraction(
            intent="correction",
            explicit_corrections=["order_number"],
            candidates={"order_number": FIXTURE_ORDER_NUMBER},
        )
    )

    corrected = (
        await send_turn(client, session_id, "sorry, I meant NS-000123", revision)
    ).json()

    assert corrected["order_verification_required"] is True
    assert corrected["reply"] == text("auth.verification_prompt", "en")
    assert corrected["slots"]["order_number"] is None  # no support request was started
    assert read_session(session_dir, session_id)["flow"]["name"] == "book_technician"

    verified = (await verify(client, session_id)).json()

    assert verified["verified"] is True
    assert verified["turn"]["slot_options"], verified["turn"]["reply"]


async def test_a_correction_with_other_facts_is_not_a_bare_account_number(
    client, provider, session_dir
):
    session_id, revision = await waiting_booking(client, provider, session_dir)
    provider.script_extraction(
        make_extraction(
            intent="correction",
            explicit_corrections=["order_number"],
            candidates={
                "order_number": FIXTURE_ORDER_NUMBER,
                "description": "my meter display is blank",
            },
        )
    )

    body = (
        await send_turn(
            client, session_id, "I meant NS-000123, my meter display is blank", revision
        )
    ).json()

    assert read_session(session_dir, session_id)["flow"] is None
    assert body["slots"]["description"] == "my meter display is blank"


async def test_credentials_typed_loosely_still_verify(client, provider, session_dir):
    for postcode, code in [
        ("sw1a1aa", "246810"),
        ("  SW1A   1AA ", "246810"),
        ("sw1a 1aa", " 246810 "),
        ("SW1A1AA", "246810\n"),
    ]:
        session_id = await create_session(client)
        provider.script_extraction(booking_for(FIXTURE_ORDER_NUMBER))
        await send_turn(client, session_id, "Book a technician for NS-000123", 0)

        response = (await verify(client, session_id, postcode, code)).json()

        assert response["verified"] is True, (postcode, code)


async def test_wrong_credentials_still_fail_after_the_spacing_is_tidied(
    client, provider, session_dir
):
    session_id = await create_session(client)
    provider.script_extraction(booking_for(FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, "Book a technician for NS-000123", 0)

    for postcode, code in [
        ("SW1A 1AB", "246810"),
        ("sw1a1ab", "246810"),
        ("SW1A 1AA", "246811"),
        ("SW1A 1AA", "24 6810"),
    ]:
        response = (await verify(client, session_id, postcode, code)).json()
        assert response == {
            "verified": False,
            "error": "verification_failed",
        }, (
            postcode,
            code,
        )


# --- an aside while the verification form is open ---------------------------------------


async def test_a_question_while_verification_is_open_keeps_the_booking_waiting(
    client, provider, session_dir
):
    session_id, revision = await waiting_booking(
        client, provider, session_dir, FIXTURE_ORDER_NUMBER
    )
    provider.script_extraction(make_extraction(intent="question", has_question=True))
    provider.script_answer(
        grounded("A standing charge is a fixed daily amount.", ["faq-standing-charge"])
    )

    asked = (
        await send_turn(client, session_id, "what are your standing charges?", revision)
    ).json()

    assert asked["citations"] and asked["order_verification_required"] is True
    assert read_session(session_dir, session_id)["flow"]["name"] == "book_technician"
    verified = (await verify(client, session_id)).json()
    assert verified["verified"] is True
    assert verified["turn"]["slot_options"], verified["turn"]["reply"]


async def test_thanks_or_small_talk_keeps_the_booking_waiting(
    client, provider, session_dir
):
    session_id, revision = await waiting_booking(client, provider, session_dir)
    provider.script_extraction(make_extraction(intent="gratitude"))

    body = (await send_turn(client, session_id, "thanks", revision)).json()

    assert body["order_verification_required"] is True
    assert read_session(session_dir, session_id)["flow"]["name"] == "book_technician"


async def test_a_gas_report_ends_the_waiting_booking(client, provider, session_dir):
    session_id, revision = await waiting_booking(client, provider, session_dir)
    provider.script_extraction(
        make_extraction(intent="question", has_question=True, safety_concern=True)
    )
    provider.script_answer(
        grounded("Open the windows and call 0800 111 999.", ["faq-gas-safety"])
    )

    body = (await send_turn(client, session_id, "I smell gas", revision)).json()

    assert read_session(session_dir, session_id)["flow"] is None
    assert (
        body["order_verification_required"] is False
    )  # the form closes with the dropped request


async def test_a_question_that_carries_facts_is_not_an_aside(
    client, provider, session_dir
):
    session_id, revision = await waiting_booking(client, provider, session_dir)
    provider.script_extraction(
        make_extraction(
            intent="question",
            has_question=True,
            candidates={"description": "my meter display is blank"},
        )
    )
    provider.script_answer(
        grounded("A standing charge is a fixed daily amount.", ["faq-standing-charge"])
    )

    await send_turn(
        client,
        session_id,
        "my meter display is blank, what is a standing charge?",
        revision,
    )

    assert read_session(session_dir, session_id)["flow"] is None
