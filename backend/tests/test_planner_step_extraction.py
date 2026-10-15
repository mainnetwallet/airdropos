"""The planner accepts the step list in the shapes models actually produce."""

from backend.app.planning.planner import extract_raw_steps


def test_contract_shape():
    assert extract_raw_steps({"steps": [{"description": "a"}]}) == [{"description": "a"}]


def test_other_key_nested_and_bare_list():
    assert extract_raw_steps({"plan": [{"description": "a"}]})[0]["description"] == "a"
    assert extract_raw_steps({"plan": {"steps": [{"description": "a"}]}})[0]["description"] == "a"
    assert extract_raw_steps([{"description": "a"}])[0]["description"] == "a"


def test_steps_keyed_by_number_and_plain_strings():
    got = extract_raw_steps({"steps": {"1": {"description": "b"}, "0": {"description": "a"}}})
    assert [s["description"] for s in got] == ["a", "b"]
    assert extract_raw_steps({"steps": ["open the bot", "press start"]})[1]["description"] == "press start"


def test_description_taken_from_action_or_title():
    got = extract_raw_steps({"steps": [{"action": "join channel"}, {"title": "submit address"}]})
    assert [s["description"] for s in got] == ["join channel", "submit address"]


def test_nothing_usable_is_empty():
    assert extract_raw_steps({"rationale": "x"}) == []
    assert extract_raw_steps({"steps": []}) == []
    assert extract_raw_steps(None) == []
