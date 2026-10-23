"""Unit tests for the in-memory session schema migration (ORB-40).

Version-1 documents and pre-extension version-2 documents are migrated in
memory only: loading never rewrites the file, and the migrated document is a
valid version-2 ``Session`` with null legacy emotion labels and a false
``frustration_active`` flag. Unknown versions and invalid labels stay errors.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.errors import AppError
from app.schemas import Session
from app.storage import JsonSessionRepository, migrate_session_document

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sessions"
V1_FIXTURES = [
    "v1-collecting.json",
    "v1-clarification.json",
    "v1-rejected.json",
    "v1-review.json",
    "v1-completed.json",
]
ADDED_RESPONSE_KEYS = {"frustration_active", "reply_language"}


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def write_document(directory: Path, document: dict) -> Path:
    path = directory / f"{document['session_id']}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.mark.parametrize("name", V1_FIXTURES)
def test_version_one_document_migrates_to_version_two(name):
    migrated = migrate_session_document(load_fixture(name))

    assert migrated["schema_version"] == 2
    assert migrated["frustration_active"] is False
    assert migrated["reply_language"] == "en"
    for message in migrated["messages"]:
        assert message["language"] == "en"
        assert message["sentiment"] is None
        assert message["frustration"] is None
    for record in migrated["requests"]:
        assert record["response"]["reply_language"] == "en"
        assert record["response"]["frustration_active"] is False


@pytest.mark.parametrize("name", V1_FIXTURES)
def test_version_one_migration_preserves_identity_state_and_transcript(name):
    original = load_fixture(name)
    migrated = migrate_session_document(original)

    for key in (
        "session_id",
        "revision",
        "created_at",
        "updated_at",
        "status",
        "slots",
        "pending_clarifications",
        "pending_review_revision",
        "review_rejected",
        "collection_turn_count",
        "summary",
        "confirmation",
    ):
        assert migrated[key] == original[key]
    assert [
        (message["id"], message["role"], message["text"], message["created_at"])
        for message in migrated["messages"]
    ] == [
        (message["id"], message["role"], message["text"], message["created_at"])
        for message in original["messages"]
    ]
    assert len(migrated["requests"]) == len(original["requests"])
    for migrated_record, original_record in zip(
        migrated["requests"], original["requests"]
    ):
        assert migrated_record["request_id"] == original_record["request_id"]
        assert migrated_record["input_digest"] == original_record["input_digest"]
        assert {
            key: value
            for key, value in migrated_record["response"].items()
            if key not in ADDED_RESPONSE_KEYS
        } == original_record["response"]


@pytest.mark.parametrize("name", [*V1_FIXTURES, "v2-pre-extension.json"])
def test_migration_does_not_mutate_the_loaded_document(name):
    original = load_fixture(name)
    snapshot = copy.deepcopy(original)

    migrate_session_document(original)

    assert original == snapshot


def test_version_two_document_without_the_new_metadata_is_filled_in_memory():
    original = load_fixture("v2-pre-extension.json")
    migrated = migrate_session_document(original)

    assert migrated["schema_version"] == 2
    assert migrated["frustration_active"] is False
    assert migrated["reply_language"] == "en"
    for message in migrated["messages"]:
        assert message["language"] == "en"
        assert message["sentiment"] is None
        assert message["frustration"] is None
    for record in migrated["requests"]:
        assert record["response"]["reply_language"] == "en"
        assert record["response"]["frustration_active"] is False
    for key in (
        "session_id",
        "revision",
        "created_at",
        "updated_at",
        "status",
        "slots",
        "pending_clarifications",
        "pending_review_revision",
        "review_rejected",
        "collection_turn_count",
        "summary",
        "confirmation",
    ):
        assert migrated[key] == original[key]


def test_complete_version_two_document_is_returned_unchanged():
    original = load_fixture("v2-active-frustration-es.json")

    assert migrate_session_document(original) == original


@pytest.mark.parametrize(
    "name", ["v1-completed.json", "v2-pre-extension.json"], ids=["v1", "v2"]
)
def test_migration_leaves_non_dict_entries_for_validation(name):
    document = load_fixture(name)
    document["messages"] = ["not-a-message", *document["messages"]]
    document["requests"] = ["not-a-record", *document["requests"]]

    migrated = migrate_session_document(document)

    assert migrated["messages"][0] == "not-a-message"
    assert migrated["requests"][0] == "not-a-record"
    assert migrated["messages"][1]["sentiment"] is None
    assert migrated["messages"][1]["frustration"] is None
    assert migrated["requests"][1]["response"]["frustration_active"] is False


@pytest.mark.parametrize(
    "name", ["v1-completed.json", "v2-pre-extension.json"], ids=["v1", "v2"]
)
async def test_load_rejects_non_dict_message_entries_as_corruption(tmp_path, name):
    document = load_fixture(name)
    document["messages"] = ["not-a-message", *document["messages"]]
    write_document(tmp_path, document)
    repository = JsonSessionRepository(tmp_path)

    with pytest.raises(AppError) as excinfo:
        await repository.load(document["session_id"])

    assert excinfo.value.code == "storage_corrupt"


def test_complete_version_two_document_keeps_its_stored_labels():
    migrated = migrate_session_document(load_fixture("v2-active-frustration-es.json"))

    user_message = migrated["messages"][1]
    assert migrated["frustration_active"] is True
    assert migrated["reply_language"] == "es"
    assert user_message["sentiment"] == "negative"
    assert user_message["frustration"] == "present"
    assert user_message["language"] == "es"


@pytest.mark.parametrize(
    "version", [0, 3, -1, True, False, "2", None, 2.0], ids=str
)
def test_unknown_schema_versions_are_rejected(version):
    document = load_fixture("v2-active-frustration-es.json")
    document["schema_version"] = version

    with pytest.raises(ValueError):
        migrate_session_document(document)


def test_missing_schema_version_is_rejected():
    document = load_fixture("v2-active-frustration-es.json")
    del document["schema_version"]

    with pytest.raises(ValueError):
        migrate_session_document(document)


@pytest.mark.parametrize("name", V1_FIXTURES)
async def test_load_migrates_version_one_without_rewriting_the_file(tmp_path, name):
    document = load_fixture(name)
    path = write_document(tmp_path, document)
    before = path.read_bytes()
    repository = JsonSessionRepository(tmp_path)

    session = await repository.load(document["session_id"])

    assert session.schema_version == 2
    assert session.frustration_active is False
    assert session.reply_language == "en"
    assert all(message.sentiment is None for message in session.messages)
    assert all(message.frustration is None for message in session.messages)
    assert all(message.language == "en" for message in session.messages)
    assert all(record.response.frustration_active is False for record in session.requests)
    assert path.read_bytes() == before
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [path.name]


async def test_load_fills_pre_extension_version_two_without_rewriting(tmp_path):
    document = load_fixture("v2-pre-extension.json")
    path = write_document(tmp_path, document)
    before = path.read_bytes()
    repository = JsonSessionRepository(tmp_path)

    session = await repository.load(document["session_id"])

    assert session.schema_version == 2
    assert session.frustration_active is False
    assert session.reply_language == "en"
    assert all(message.sentiment is None for message in session.messages)
    assert all(message.frustration is None for message in session.messages)
    assert session.requests[0].response.frustration_active is False
    assert path.read_bytes() == before


async def test_load_preserves_stored_labels_of_a_complete_version_two_file(tmp_path):
    document = load_fixture("v2-active-frustration-es.json")
    path = write_document(tmp_path, document)
    before = path.read_bytes()
    repository = JsonSessionRepository(tmp_path)

    session = await repository.load(document["session_id"])

    assert session.frustration_active is True
    assert session.reply_language == "es"
    assert session.messages[1].sentiment == "negative"
    assert session.messages[1].frustration == "present"
    assert session.messages[0].sentiment is None
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "sentiment", ["furious", "Negative", " negative", 1, True, []], ids=str
)
async def test_load_rejects_an_invalid_stored_sentiment_label(tmp_path, sentiment):
    document = load_fixture("v2-active-frustration-es.json")
    document["messages"][1]["sentiment"] = sentiment
    path = write_document(tmp_path, document)
    before = path.read_bytes()
    repository = JsonSessionRepository(tmp_path)

    with pytest.raises(AppError) as excinfo:
        await repository.load(document["session_id"])

    assert excinfo.value.code == "storage_corrupt"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "frustration", ["low", "Present", "present ", 0, False], ids=str
)
async def test_load_rejects_an_invalid_stored_frustration_label(tmp_path, frustration):
    document = load_fixture("v2-active-frustration-es.json")
    document["messages"][1]["frustration"] = frustration
    write_document(tmp_path, document)
    repository = JsonSessionRepository(tmp_path)

    with pytest.raises(AppError) as excinfo:
        await repository.load(document["session_id"])

    assert excinfo.value.code == "storage_corrupt"


async def test_load_rejects_labels_on_an_assistant_message(tmp_path):
    document = load_fixture("v2-active-frustration-es.json")
    document["messages"][2]["sentiment"] = "neutral"
    write_document(tmp_path, document)
    repository = JsonSessionRepository(tmp_path)

    with pytest.raises(AppError) as excinfo:
        await repository.load(document["session_id"])

    assert excinfo.value.code == "storage_corrupt"


async def test_load_rejects_a_version_two_file_with_an_unknown_message_key(tmp_path):
    document = load_fixture("v2-pre-extension.json")
    document["messages"][1]["emotion_score"] = 0.9
    write_document(tmp_path, document)
    repository = JsonSessionRepository(tmp_path)

    with pytest.raises(AppError) as excinfo:
        await repository.load(document["session_id"])

    assert excinfo.value.code == "storage_corrupt"


async def test_load_rejects_a_future_schema_version(tmp_path):
    document = load_fixture("v2-active-frustration-es.json")
    document["schema_version"] = 3
    path = write_document(tmp_path, document)
    before = path.read_bytes()
    repository = JsonSessionRepository(tmp_path)

    with pytest.raises(AppError) as excinfo:
        await repository.load(document["session_id"])

    assert excinfo.value.code == "storage_corrupt"
    assert path.read_bytes() == before


async def test_load_rejects_a_corrupt_document(tmp_path):
    path = tmp_path / "88888888-8888-4888-8888-888888888888.json"
    path.write_text("{not json", encoding="utf-8")
    repository = JsonSessionRepository(tmp_path)

    with pytest.raises(AppError) as excinfo:
        await repository.load("88888888-8888-4888-8888-888888888888")

    assert excinfo.value.code == "storage_corrupt"
    assert path.read_text(encoding="utf-8") == "{not json"


def test_migrated_documents_validate_as_version_two_sessions():
    for name in V1_FIXTURES:
        session = Session.model_validate(migrate_session_document(load_fixture(name)))
        assert session.schema_version == 2
