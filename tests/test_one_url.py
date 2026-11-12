"""One URL per web_extract call: the first URL is read, the result lists the others."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

gw = load()


class FakeCtx:
    def __init__(self, settings=None):
        self.hooks = {}
        self.settings = settings or {}

    def register_hook(self, name, fn, **_):
        self.hooks[name] = fn

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


def setup(settings=None):
    gw._sessions.clear()
    gw._turns.clear()
    ctx = FakeCtx(settings)
    gw.register(ctx)
    return ctx


def test_several_urls_read_the_first_and_the_result_lists_the_others():
    ctx = setup()
    urls = ["https://a.example/1", "https://b.example/2", "https://c.example/3"]
    out = ctx.hooks["pre_tool_call"](tool_name="web_extract", session_id="s1", tool_call_id="c1", args={"urls": urls})
    assert out == {"action": "modify", "args": {"urls": ["https://a.example/1"]}}
    result = json.dumps({"results": [{"url": urls[0], "content": "page"}]})
    noted = json.loads(ctx.hooks["transform_tool_result"](tool_name="web_extract", result=result,
                                                          session_id="s1", tool_call_id="c1"))
    assert noted["results"][0]["url"] == urls[0]
    assert "https://b.example/2, https://c.example/3" in noted["webgraft_note"]
    # once only, and only for that call
    assert ctx.hooks["transform_tool_result"](tool_name="web_extract", result=result, session_id="s1", tool_call_id="c1") is None


def test_one_url_passes_and_other_tools_are_untouched():
    ctx = setup()
    assert ctx.hooks["pre_tool_call"](tool_name="web_extract", args={"urls": ["https://a.example/1"]}, session_id="s1") is None
    assert ctx.hooks["pre_tool_call"](tool_name="terminal", args={"urls": ["a", "b"]}, session_id="s1") is None
    assert ctx.hooks["transform_tool_result"](tool_name="web_extract", result="{}", session_id="s1", tool_call_id="x") is None


def test_the_guard_can_be_turned_off():
    ctx = setup({"one_url_per_call": False})
    assert ctx.hooks["pre_tool_call"](tool_name="web_extract", args={"urls": ["https://a.example/1", "https://b.example/2"]},
                                      session_id="s1") is None
