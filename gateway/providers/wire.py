"""Chat Completions and transcription wire format: request payloads and parsers.

Pure functions with no I/O. Every parser raises ``InvalidContent`` when a
successful HTTP response carries unusable content, so the provider adapter can
retry or repair inside its bounded operation (PC §3, IC §11, RAG §4).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from pydantic import BaseModel, ValidationError

from gateway.config import TextProfile
from gateway.errors import app_error
from gateway.knowledge.retrieval import MAX_ANSWER_CHARS, TOP_K
from gateway.schemas import (
    Extraction,
    KnowledgeAnswer,
    KnowledgeContext,
    KnowledgeRecord,
)

REPAIR_LINE = (
    "The previous response did not satisfy the schema. Return a complete schema object."
)
SCHEMA_INSTRUCTION = "Return one JSON object matching this schema: "
MAX_TRANSCRIPT_CHARS = 4000


THINK_TAG = re.compile(r"</?think>", re.IGNORECASE)
# ponytail: a failed decode costs O(its position), so a reply made of braces would take
# quadratic time; the real object is one of the first few "{" outside reasoning.
MAX_OBJECT_ATTEMPTS = 32


class InvalidContent(Exception):
    """Successful HTTP with unusable content."""


def _schema_instruction(schema: type[BaseModel]) -> str:
    return SCHEMA_INSTRUCTION + json.dumps(
        schema.model_json_schema(), ensure_ascii=False
    )


def text_payload(
    profile: TextProfile,
    system_prompt: str,
    user_content: str,
    max_tokens: int,
    schema: type[BaseModel],
    schema_name: str,
    *,
    repair: bool,
) -> dict[str, object]:
    system = system_prompt
    mode = profile.output_mode
    if mode in ("json_object", "prompt_json"):
        system = f"{system}\n\n{_schema_instruction(schema)}"
    if repair:
        system = f"{system}\n{REPAIR_LINE}"
    payload: dict[str, object] = {
        "model": profile.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if profile.reasoning_effort is not None:
        payload["reasoning_effort"] = profile.reasoning_effort
    if mode == "json_schema":
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema.model_json_schema(),
            },
        }
    elif mode == "json_object":
        payload["response_format"] = {"type": "json_object"}
    return payload


def _reasoning_spans(text: str) -> list[tuple[int, int]]:
    """Where the model was thinking: each <think> up to the next </think>, in one pass.

    An opener that is never closed runs to the end, so one malformed reply cannot make
    this rescan the rest of the text for every opener (a regex does, and a long enough
    reply would block the event loop).
    """

    spans: list[tuple[int, int]] = []
    begin: int | None = None
    for tag in THINK_TAG.finditer(text):
        closing = tag.group()[1] == "/"
        if begin is None and not closing:
            begin = tag.start()
        elif begin is not None and closing:
            spans.append((begin, tag.end()))
            begin = None
    if begin is not None:
        spans.append((begin, len(text)))
    return spans


def extract_json(content: str) -> object:
    """The JSON object in a model's answer, however the model wrapped it.

    Models differ in what they put around it: a ``` fence, a <think> block, a lead-in
    line. Reasoning is skipped, never cut out of the text: removing a <think> tag first
    would also remove it from inside a JSON string and change what the customer said.
    Raises ValueError when there is no JSON object to find or it is nested too deeply.
    """

    text = content.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    except RecursionError:
        raise ValueError("JSON nested too deeply") from None
    spans = _reasoning_spans(text)
    decoder = json.JSONDecoder()
    skipped = 0
    attempts = 0
    start = text.find("{")
    while start != -1 and attempts < MAX_OBJECT_ATTEMPTS:
        while skipped < len(spans) and spans[skipped][1] <= start:
            skipped += 1
        if skipped < len(spans) and spans[skipped][0] <= start:
            start = text.find("{", spans[skipped][1])
            continue
        attempts += 1
        try:
            return decoder.raw_decode(text, start)[0]
        except ValueError:
            pass
        except RecursionError:
            raise ValueError("JSON nested too deeply") from None
        start = text.find("{", start + 1)
    raise ValueError("no JSON object in the answer")


def _loads(raw: str) -> object:
    try:
        return json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        raise InvalidContent from None


def _message_content(envelope: object) -> str:
    """Parse the first choice only, requiring an explicit clean ``stop``."""

    if not isinstance(envelope, dict):
        raise InvalidContent
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise InvalidContent
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
        raise InvalidContent
    message = choice.get("message")
    if not isinstance(message, dict):
        raise InvalidContent
    if message.get("refusal") or message.get("tool_calls"):
        raise InvalidContent
    content = message.get("content")
    if not isinstance(content, str):
        raise InvalidContent
    return content


def _parse_model(body: str, model: type[BaseModel]):
    try:
        payload = extract_json(_message_content(_loads(body)))
    except ValueError:
        raise InvalidContent from None
    if not isinstance(payload, dict):
        raise InvalidContent
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise InvalidContent from None


def parse_extraction(body: str) -> Extraction:
    return _parse_model(body, Extraction)


def parse_knowledge_answer(body: str, context: KnowledgeContext) -> KnowledgeAnswer:
    answer = _parse_model(body, KnowledgeAnswer)
    _validate_answer(answer, context.sources)
    return answer


def _validate_answer(
    answer: KnowledgeAnswer, sources: Sequence[KnowledgeRecord]
) -> None:
    """RAG section 4 citation rules, checked inside the bounded operation."""

    if not answer.answerable:
        if answer.answer != "" or answer.cited_ids:
            raise InvalidContent
        return
    if not 1 <= len(answer.answer.strip()) <= MAX_ANSWER_CHARS:
        raise InvalidContent
    cited_ids = answer.cited_ids
    if not 1 <= len(cited_ids) <= TOP_K or len(set(cited_ids)) != len(cited_ids):
        raise InvalidContent
    supplied = {record.id for record in sources}
    if any(cited_id not in supplied for cited_id in cited_ids):
        raise InvalidContent


def parse_transcription(body: str) -> str:
    payload = _loads(body)
    if not isinstance(payload, dict):
        raise InvalidContent
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise app_error("empty_transcription")
    trimmed = text.strip()
    if len(trimmed) > MAX_TRANSCRIPT_CHARS:
        raise app_error("transcript_too_long")
    return trimmed


def parse_embeddings(body: str, expected: int) -> list[list[float]]:
    """Vectors in input order; every vector is a non-empty list of numbers, same length."""

    payload = _loads(body)
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) != expected:
        raise InvalidContent
    vectors: list[list[float]] = []
    for item in sorted(
        items, key=lambda entry: entry.get("index", 0) if isinstance(entry, dict) else 0
    ):
        vector = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(vector, list) or not vector:
            raise InvalidContent
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in vector):
            raise InvalidContent
        vectors.append([float(x) for x in vector])
    if len({len(vector) for vector in vectors}) != 1:
        raise InvalidContent
    return vectors
