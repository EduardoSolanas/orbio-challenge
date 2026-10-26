"""What the saved chat records about each customer message: its language and the labels on it."""

from __future__ import annotations

from tests.integration.order_verification.support import create_session, send_turn
from tests.integration.support.api import make_extraction, read_session


async def saved_customer_message(client, provider, session_dir, extraction):
    session_id = await create_session(client)
    provider.script_extraction(extraction)
    assert (await send_turn(client, session_id, "Hola", 0)).status_code == 200
    messages = read_session(session_dir, session_id)["messages"]
    return [message for message in messages if message["role"] == "user"][-1]


async def test_a_spanish_message_is_saved_as_spanish_with_the_labels_the_model_gave(
    client, provider, session_dir
):
    message = await saved_customer_message(
        client,
        provider,
        session_dir,
        make_extraction(
            intent="social",
            detected_language="es",
            sentiment="negative",
            frustration="present",
        ),
    )

    assert message["language"] == "es"
    assert (message["sentiment"], message["frustration"]) == ("negative", "present")


async def test_an_unsupported_language_message_gets_uncertain_labels_and_the_session_language(
    client, provider, session_dir
):
    message = await saved_customer_message(
        client,
        provider,
        session_dir,
        make_extraction(
            intent="provide_details",
            detected_language="unsupported",
            sentiment="negative",
            frustration="present",
        ),
    )

    assert message["language"] == "en"
    assert (message["sentiment"], message["frustration"]) == ("uncertain", "uncertain")
