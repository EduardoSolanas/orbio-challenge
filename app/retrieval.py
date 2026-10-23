"""Immutable versioned knowledge base, lexical retrieval and citation validation.

Implements RAG sections 1-4 with the standard library only: the bundled JSON
corpus is validated before it is parsed beyond the documented byte cap, loaded
records are frozen for the app lifespan, retrieval is deterministic lexical
scoring, and generated answers are checked against the retrieved records before
any citation snapshot is built.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ConfigDict

from app.errors import INVALID_PROVIDER_RESPONSE, KNOWLEDGE_INVALID_MESSAGE, app_error
from app.schemas import Citation, KnowledgeAnswer, KnowledgeRecord

KB_SCHEMA_VERSION = 1
MAX_KB_BYTES = 256 * 1024
MIN_RECORDS = 1
MAX_RECORDS = 100
MAX_VERSION_CHARS = 40
MAX_TITLE_CHARS = 100
MAX_BODY_CHARS = 2000
MAX_KEYWORDS = 20
MAX_ANSWER_CHARS = 1200

TOP_LEVEL_KEYS = frozenset({"schema_version", "version", "records"})
RECORD_KEYS = frozenset({"id", "title", "keywords", "body"})

_ID_PATTERN = re.compile(r"[a-z0-9-]{1,64}")
_KEYWORD_PATTERN = re.compile(r"[a-z0-9]+")
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")

STOP_WORDS = frozenset(
    (
        "a an the i my me we you your is are was were be been do does did can could "
        "would should will what how when where why who to of for in on at by with and "
        "or but it its this that as have has had order item items"
    ).split()
)

TOP_K = 2
KEYWORD_WEIGHT = 3

MISSING_REASON = "missing"
FILE_TOO_LARGE_REASON = "file_too_large"
INVALID_DOCUMENT_REASON = "invalid_document"


class KnowledgeInvalid(RuntimeError):
    """The bundled KB is unusable; the message is the exact RAG section 1 string."""

    def __init__(self, reason: str) -> None:
        super().__init__(KNOWLEDGE_INVALID_MESSAGE)
        self.reason = reason


class ImmutableKnowledgeRecord(KnowledgeRecord):
    """A knowledge record that cannot be reassigned after load."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def tokenize(text: str) -> set[str]:
    """NFKC normalise, casefold, split on ``[a-z0-9]+`` and drop the fixed stop words."""

    normalised = unicodedata.normalize("NFKC", text).casefold()
    return {token for token in _TOKEN_PATTERN.findall(normalised) if token not in STOP_WORDS}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value: object, minimum: int, maximum: int) -> bool:
    return isinstance(value, str) and minimum <= len(value) <= maximum


def _validate_document(document: object) -> tuple[str, tuple[ImmutableKnowledgeRecord, ...]]:
    if not isinstance(document, dict) or set(document) != TOP_LEVEL_KEYS:
        raise KnowledgeInvalid(INVALID_DOCUMENT_REASON)
    if not _is_int(document["schema_version"]) or document["schema_version"] != KB_SCHEMA_VERSION:
        raise KnowledgeInvalid(INVALID_DOCUMENT_REASON)
    version = document["version"]
    if not _is_text(version, 1, MAX_VERSION_CHARS):
        raise KnowledgeInvalid(INVALID_DOCUMENT_REASON)
    raw_records = document["records"]
    if not isinstance(raw_records, list) or not MIN_RECORDS <= len(raw_records) <= MAX_RECORDS:
        raise KnowledgeInvalid(INVALID_DOCUMENT_REASON)

    records: list[ImmutableKnowledgeRecord] = []
    seen_ids: set[str] = set()
    for raw_record in raw_records:
        if not isinstance(raw_record, dict) or set(raw_record) != RECORD_KEYS:
            raise KnowledgeInvalid(INVALID_DOCUMENT_REASON)
        record_id = raw_record["id"]
        if (
            not isinstance(record_id, str)
            or _ID_PATTERN.fullmatch(record_id) is None
            or record_id in seen_ids
        ):
            raise KnowledgeInvalid(INVALID_DOCUMENT_REASON)
        seen_ids.add(record_id)
        if not _is_text(raw_record["title"], 1, MAX_TITLE_CHARS):
            raise KnowledgeInvalid(INVALID_DOCUMENT_REASON)
        if not _is_text(raw_record["body"], 1, MAX_BODY_CHARS):
            raise KnowledgeInvalid(INVALID_DOCUMENT_REASON)
        keywords = raw_record["keywords"]
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= MAX_KEYWORDS:
            raise KnowledgeInvalid(INVALID_DOCUMENT_REASON)
        if len(set(keywords)) != len(keywords):
            raise KnowledgeInvalid(INVALID_DOCUMENT_REASON)
        if any(
            not isinstance(keyword, str) or _KEYWORD_PATTERN.fullmatch(keyword) is None
            for keyword in keywords
        ):
            raise KnowledgeInvalid(INVALID_DOCUMENT_REASON)
        records.append(
            ImmutableKnowledgeRecord(
                id=record_id,
                title=raw_record["title"],
                keywords=list(keywords),
                body=raw_record["body"],
            )
        )
    return version, tuple(records)


class KnowledgeBase:
    """Immutable knowledge base: one validated version and its frozen records."""

    def __init__(self, version: str, records: tuple[ImmutableKnowledgeRecord, ...]) -> None:
        self.version = version
        self.records = tuple(records)
        self._keywords = {record.id: frozenset(record.keywords) for record in self.records}
        self._text_tokens = {
            record.id: tokenize(record.title + " " + record.body) for record in self.records
        }

    def retrieve(self, question: str) -> list[ImmutableKnowledgeRecord]:
        """Rank records by the RAG section 3 score and return at most two."""

        query = tokenize(question)
        ranked: list[tuple[int, str, ImmutableKnowledgeRecord]] = []
        for record in self.records:
            keyword_hits = len(query & self._keywords[record.id])
            if keyword_hits == 0:
                continue
            score = KEYWORD_WEIGHT * keyword_hits + len(query & self._text_tokens[record.id])
            ranked.append((score, record.id, record))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [record for _, _, record in ranked[:TOP_K]]


def validate_knowledge_answer(
    answer: KnowledgeAnswer,
    retrieved: Sequence[KnowledgeRecord],
    kb_version: str,
) -> tuple[Citation, ...]:
    """Validate a generated answer and build snapshots from the retrieved records only."""

    if not answer.answerable:
        if answer.answer != "" or answer.cited_ids:
            raise app_error(INVALID_PROVIDER_RESPONSE)
        return ()
    if not 1 <= len(answer.answer.strip()) <= MAX_ANSWER_CHARS:
        raise app_error(INVALID_PROVIDER_RESPONSE)
    cited_ids = answer.cited_ids
    if not 1 <= len(cited_ids) <= TOP_K or len(set(cited_ids)) != len(cited_ids):
        raise app_error(INVALID_PROVIDER_RESPONSE)
    by_id = {record.id: record for record in retrieved}
    if any(cited_id not in by_id for cited_id in cited_ids):
        raise app_error(INVALID_PROVIDER_RESPONSE)
    return tuple(
        Citation(
            source_id=cited_id,
            title=by_id[cited_id].title,
            excerpt=by_id[cited_id].body,
            kb_version=kb_version,
        )
        for cited_id in cited_ids
    )


def load_knowledge(path: str | Path) -> KnowledgeBase:
    """Load and validate the bundled KB, rejecting it with the RAG section 1 string."""

    kb_path = Path(path)
    try:
        size = kb_path.stat().st_size
    except OSError:
        raise KnowledgeInvalid(MISSING_REASON) from None
    if size > MAX_KB_BYTES:
        raise KnowledgeInvalid(FILE_TOO_LARGE_REASON)
    try:
        raw = kb_path.read_bytes()
    except OSError:
        raise KnowledgeInvalid(MISSING_REASON) from None
    try:
        document: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise KnowledgeInvalid(INVALID_DOCUMENT_REASON) from None
    version, records = _validate_document(document)
    return KnowledgeBase(version, records)
