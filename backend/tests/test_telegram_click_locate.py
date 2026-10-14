"""telegram_click_button must not need a message id the planner cannot know."""

import asyncio
from types import SimpleNamespace

from backend.app.tools.telegram_user_tools import _locate_button


class _Btn:
    def __init__(self, text):
        self.text = text


class _Msg:
    def __init__(self, id, texts):
        self.id = id
        self.buttons = [[_Btn(t) for t in texts]] if texts else None


class _Client:
    def __init__(self, msgs, newest_first=True):
        self.msgs = sorted(msgs, key=lambda m: m.id, reverse=newest_first)

    async def get_messages(self, entity, ids=None, limit=10):
        if ids is not None:
            return next((m for m in self.msgs if m.id == ids), None)
        return self.msgs[:limit]


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_finds_newest_message_with_button_without_an_id():
    client = _Client([_Msg(1, ["Continue"]), _Msg(2, None), _Msg(3, ["Continue", "Done"])])
    msg, button, _ = _run(_locate_button(client, SimpleNamespace(), "continue", None))
    assert msg.id == 3 and button.text == "Continue"


def test_wrong_hinted_id_falls_back_to_search():
    client = _Client([_Msg(5, None), _Msg(6, ["Continue"])])
    msg, button, _ = _run(_locate_button(client, SimpleNamespace(), "continue", 5))
    assert msg.id == 6 and button is not None


def test_oldest_first_client_order_is_handled():
    client = _Client([_Msg(1, ["Continue"]), _Msg(2, ["Continue"])], newest_first=False)
    msg, _, _ = _run(_locate_button(client, SimpleNamespace(), "continue", None))
    assert msg.id == 2


def test_no_match_reports_available_buttons():
    client = _Client([_Msg(1, ["Join", "Check"])])
    msg, button, flat = _run(_locate_button(client, SimpleNamespace(), "continue", None))
    assert button is None and [b.text for b in flat] == ["Join", "Check"]
