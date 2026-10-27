"""A model's JSON answer is read whatever it wraps around it."""

import json

import pytest

from gateway.providers.wire import InvalidContent, extract_json, parse_extraction
from tests.support.unit_fakes import make_extraction

OBJECT = {"a": 1, "b": {"c": [1, 2]}}


@pytest.mark.parametrize(
    "content",
    [
        '{"a": 1, "b": {"c": [1, 2]}}',
        '  \n{"a": 1, "b": {"c": [1, 2]}}\n ',
        '```json\n{"a": 1, "b": {"c": [1, 2]}}\n```',
        '```\n{"a": 1, "b": {"c": [1, 2]}}\n```',
        '<think>The user wants {"x": 1} maybe.</think>\n{"a": 1, "b": {"c": [1, 2]}}',
        '<THINK>\nmulti\nline\n</THINK>```json\n{"a": 1, "b": {"c": [1, 2]}}\n```',
        'Here is the result:\n{"a": 1, "b": {"c": [1, 2]}}\nHope that helps!',
        'Note {not json} first. {"a": 1, "b": {"c": [1, 2]}}',
    ],
)
def test_the_object_is_found_however_it_is_wrapped(content) -> None:
    assert extract_json(content) == OBJECT


@pytest.mark.parametrize(
    "content",
    [
        "",
        "   ",
        "no json here",
        "[1, 2, 3]",
        '{"unclosed": ',
        "<think>only thoughts</think>",
    ],
)
def test_an_answer_without_a_json_object_is_refused(content) -> None:
    with pytest.raises(ValueError):
        extract_json(content)


def _envelope(content: str) -> str:
    import json

    return json.dumps(
        {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    )


def test_a_fenced_extraction_is_parsed_into_the_schema() -> None:
    import json

    extraction = make_extraction(intent="confirm")
    fenced = "```json\n" + extraction.model_dump_json() + "\n```"

    assert parse_extraction(_envelope(fenced)) == extraction
    assert json.loads(extraction.model_dump_json())["intent"] == "confirm"


@pytest.mark.parametrize(
    "content", ["no json", '{"intent": "confirm"}', "<think>x</think>"]
)
def test_unusable_answers_still_fail_as_invalid_content(content) -> None:
    with pytest.raises(InvalidContent):
        parse_extraction(_envelope(content))


INSIDE_A_STRING = {"description": "tell them <think>x</think>replace the valve", "a": 1}


@pytest.mark.parametrize(
    "wrap",
    [
        "{body}",
        "```json\n{body}\n```",
        '<think>maybe {{"x": 1}}</think>{body}',
        "Result: {body} done",
    ],
)
def test_a_think_tag_inside_a_json_string_is_kept(wrap) -> None:
    content = wrap.format(body=json.dumps(INSIDE_A_STRING))

    assert extract_json(content) == INSIDE_A_STRING


@pytest.mark.parametrize("opener", ["[", '{"a":'], ids=["array", "object"])
def test_a_deeply_nested_answer_is_refused_not_raised(opener) -> None:
    with pytest.raises(ValueError):
        extract_json(opener * 100000)


def test_a_deeply_nested_answer_fails_as_invalid_content() -> None:
    with pytest.raises(InvalidContent):
        parse_extraction(_envelope('{"a":' * 100000))
    with pytest.raises(InvalidContent):
        parse_extraction("[" * 100000)
