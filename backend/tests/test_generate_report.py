"""generate_report must never report success with empty section bodies."""
import asyncio

from backend.app.tools.notification_tools import GenerateReportTool


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def parse_report(markdown: str):
    """Read a rendered report back into {heading: body}. This is the 'read it back' check."""
    sections, current = {}, None
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def gen(sections, title="T"):
    return run(GenerateReportTool().execute({"title": title, "sections": sections}))


def test_standard_sections_render_non_empty_bodies_and_read_back():
    res = gen([{"heading": "Findings", "body": "Two claims verified."},
               {"heading": "Risks", "body": "Wallet not connected."}])
    assert res.ok
    back = parse_report(res.data["report_markdown"])
    assert back == {"Findings": "Two claims verified.", "Risks": "Wallet not connected."}
    assert all(back.values())


def test_alternate_body_keys_are_not_silently_dropped():
    # Models commonly send content/text instead of body. Previously this rendered "" and succeeded.
    for key in ("content", "text"):
        res = gen([{"heading": "H", key: "real content"}])
        assert res.ok, key
        assert parse_report(res.data["report_markdown"])["H"] == "real content"


def test_list_and_dict_bodies_are_rendered_not_repr():
    res = gen([{"heading": "L", "body": ["alpha", "beta"]},
               {"heading": "D", "body": {"Chain": "Base", "Status": "ok"}}])
    assert res.ok
    back = parse_report(res.data["report_markdown"])
    assert back["L"] == "- alpha\n- beta"
    assert "- Chain: Base" in back["D"] and "- Status: ok" in back["D"]
    assert "[" not in back["L"] and "{" not in back["D"]


def test_empty_body_is_a_failure_not_a_success():
    res = gen([{"heading": "Findings", "body": ""}])
    assert not res.ok
    assert "Findings" in (res.error or "")


def test_unrecognised_keys_only_is_a_failure_naming_the_section():
    res = gen([{"heading": "Findings", "details_blob": "x"}])
    assert not res.ok
    assert "Findings" in (res.error or "")


def test_one_empty_section_among_good_ones_fails_whole_report():
    res = gen([{"heading": "Good", "body": "ok"}, {"heading": "Bad"}])
    assert not res.ok and "Bad" in (res.error or "")


def test_no_sections_is_a_failure():
    assert not gen([]).ok


def test_plain_string_section_is_used_as_body():
    res = gen(["just text"])
    assert res.ok
    assert list(parse_report(res.data["report_markdown"]).values()) == ["just text"]


def test_missing_heading_gets_numbered_placeholder_not_identical_names():
    res = gen([{"body": "a"}, {"body": "b"}])
    assert res.ok
    assert len(parse_report(res.data["report_markdown"])) == 2


def test_section_count_and_lengths_reported():
    res = gen([{"heading": "A", "body": "12345"}])
    assert res.data["section_count"] == 1
    assert res.data["sections"][0]["body_chars"] == 5
