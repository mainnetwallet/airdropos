"""The model often wraps its JSON. The console must still read it."""

from __future__ import annotations

import pytest

from backend.app.llm.client import FixedLLMClient, LLMError

parse = FixedLLMClient.parse_json

OBJECT = '{"reply": "Task created.", "tool_calls": [], "create_task": {"title": "LockOn", "priority": 5}}'


def test_plain_object():
    assert parse(OBJECT)["reply"] == "Task created."


def test_prose_then_fenced_block_is_the_case_from_the_console():
    text = "I'll create a task to execute the campaign.\n\n```json\n" + OBJECT + "\n```\n"
    parsed = parse(text)
    assert parsed["create_task"]["title"] == "LockOn"


def test_fence_only_and_unlabelled_fence():
    assert parse("```json\n" + OBJECT + "\n```")["reply"] == "Task created."
    assert parse("```\n" + OBJECT + "\n```")["reply"] == "Task created."


def test_text_after_the_block_is_ignored():
    assert parse("Here you go:\n```json\n" + OBJECT + "\n```\nLet me know!")["reply"] == "Task created."


def test_object_embedded_in_prose_without_a_fence():
    assert parse("Sure. " + OBJECT + " Done.")["tool_calls"] == []


def test_raw_newlines_inside_strings_are_accepted():
    text = '{"reply": "line one\nline two", "tool_calls": []}'
    assert parse(text)["reply"] == "line one\nline two"


def test_braces_inside_strings_do_not_confuse_extraction():
    text = 'note {not json} then {"reply": "use {x} here", "tool_calls": []}'
    assert parse(text)["reply"] == "use {x} here"


def test_prefers_the_agent_contract_over_an_unrelated_object():
    text = '{"example": 1}\n```json\n' + OBJECT + "\n```"
    assert "reply" in parse(text)


@pytest.mark.parametrize("bad", ["", "   ", "just words, no json", "[1, 2, 3]", "```json\n[1,2]\n```", '{"reply": '])
def test_no_object_still_fails_loudly(bad):
    with pytest.raises(LLMError):
        parse(bad)
