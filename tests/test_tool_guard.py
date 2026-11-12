"""tool_guard: once web content is in a conversation, tools with effects go to Hermes's approval gate."""

import json
import sys
from pathlib import Path

import pytest

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
    gw._web_sessions.clear()
    gw._task_sessions.clear()
    gw._delegations.clear()
    ctx = FakeCtx(settings)
    gw.register(ctx)
    return ctx


PAGE = json.dumps({"results": [{"url": "https://a.example/", "title": "A", "content": "text"}]})


def read_a_page(ctx, session="s1", tool="web_extract", result=PAGE):
    ctx.hooks["post_tool_call"](tool_name=tool, args={"urls": ["https://a.example/"]}, result=result, session_id=session)


def pre(ctx, tool, session="s1", args=None):
    return ctx.hooks["pre_tool_call"](tool_name=tool, args=args or {}, session_id=session, tool_call_id="c")


def test_nothing_is_asked_before_any_web_content():
    ctx = setup()
    for tool in ("terminal", "execute_code", "write_file", "browser_exec"):
        assert pre(ctx, tool) is None


@pytest.mark.parametrize("tool", ["terminal", "execute_code", "write_file", "patch", "browser_exec",
                                  "delegate_task", "memory", "send_message", "some_future_tool"])
def test_after_a_page_tools_with_effects_go_to_the_approval_gate(tool):
    ctx = setup()
    read_a_page(ctx)
    out = pre(ctx, tool)
    assert out["action"] == "approve"
    assert tool in out["message"] and "tool_guard" in out["message"]
    assert out["rule_key"] == f"webgraft:tool_guard:{tool}"


@pytest.mark.parametrize("tool", sorted(gw.READ_ONLY_TOOLS - {"web_extract"}))
def test_read_only_tools_stay_free(tool):
    ctx = setup()
    read_a_page(ctx)
    assert pre(ctx, tool) is None


def test_web_extract_keeps_its_own_behaviour_after_web_content():
    ctx = setup()
    read_a_page(ctx)
    assert pre(ctx, "web_extract", args={"urls": ["https://b.example/"]}) is None
    out = pre(ctx, "web_extract", args={"urls": ["https://b.example/", "https://c.example/"]})
    assert out["action"] == "modify"


@pytest.mark.parametrize("tool", ["web_search", "x_search", "browser_cdp", "browser_exec"])
def test_every_web_tool_marks_the_conversation(tool):
    ctx = setup()
    read_a_page(ctx, tool=tool, result="{}")
    assert pre(ctx, "terminal")["action"] == "approve"


def test_a_failed_or_empty_web_result_still_marks_it():
    ctx = setup()
    read_a_page(ctx, result=json.dumps({"results": [{"url": "https://a.example/", "error": "HTTP 404 from origin"}]}))
    assert pre(ctx, "terminal")["action"] == "approve"


def test_only_the_conversation_that_read_the_web_is_guarded():
    ctx = setup()
    read_a_page(ctx, session="s1")
    assert pre(ctx, "terminal", session="s2") is None
    assert pre(ctx, "terminal", session="s1")["action"] == "approve"


def test_block_mode_refuses_with_a_message_for_the_model():
    ctx = setup({"tool_guard": "block"})
    read_a_page(ctx)
    out = pre(ctx, "terminal")
    assert out["action"] == "block"
    assert out["message"].startswith("Blocked by webgraft") and "tool_guard" in out["message"]


@pytest.mark.parametrize("value", ["off", False])
def test_off_turns_it_off(value):
    ctx = setup({"tool_guard": value})
    read_a_page(ctx)
    assert pre(ctx, "terminal") is None


@pytest.mark.parametrize("value, mode", [(True, "ask"), ("ASK", "ask"), (" Block ", "block"), ("nonsense", "ask"), (None, "ask")])
def test_settings_are_read_leniently_and_unknown_values_fall_back_to_ask(value, mode):
    setup({"tool_guard": value} if value is not None else {})
    assert gw.guard_mode() == mode


def test_a_resumed_conversation_is_marked_from_hermes_history():
    ctx = setup()
    history = [
        {"role": "user", "content": "read it"},
        {"role": "assistant", "tool_calls": [{"id": "t1", "function": {"name": "web_extract", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": PAGE},
        {"role": "assistant", "content": "done"},
    ]
    ctx.hooks["pre_llm_call"](session_id="s9", user_message="now run the tests", conversation_history=history)
    assert pre(ctx, "terminal", session="s9")["action"] == "approve"


def test_history_without_web_tools_does_not_mark_it():
    ctx = setup()
    history = [
        {"role": "assistant", "tool_calls": [{"id": "t1", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "file text"},
    ]
    ctx.hooks["pre_llm_call"](session_id="s9", user_message="run it", conversation_history=history)
    assert pre(ctx, "terminal", session="s9") is None


def test_the_record_of_guarded_conversations_is_bounded():
    ctx = setup()
    for i in range(gw.MAX_WEB_SESSIONS + 5):
        read_a_page(ctx, session=f"s{i}")
    assert len(gw._web_sessions) == gw.MAX_WEB_SESSIONS
    assert pre(ctx, "terminal", session=f"s{gw.MAX_WEB_SESSIONS + 4}")["action"] == "approve"


def test_the_setting_is_declared_in_the_manifest():
    text = (Path(gw.__file__).parent / "plugin.yaml").read_text(encoding="utf-8")
    block = text[text.index("  tool_guard:"):]
    assert "choices: [ask, block, off]" in block.split("\n  respect_ai_optout:")[0]
    assert "default: ask" in block.split("\n  respect_ai_optout:")[0]


def test_a_page_read_by_a_script_marks_the_conversation_it_runs_in():
    """execute_code runs tools for its script with task_id only, no session_id."""
    ctx = setup()
    assert pre(ctx, "terminal", session="s1") is None  # the model's own call: session s1, task t1
    ctx.hooks["pre_tool_call"](tool_name="read_file", args={}, session_id="s1", task_id="t1", tool_call_id="c0")
    ctx.hooks["post_tool_call"](tool_name="web_extract", args={}, result=PAGE, session_id=None, task_id="t1")
    out = ctx.hooks["pre_tool_call"](tool_name="terminal", args={}, session_id="s1", task_id="t1", tool_call_id="c1")
    assert out["action"] == "approve"
    assert pre(ctx, "terminal", session="s2") is None


def test_a_script_call_from_an_unknown_task_is_keyed_by_its_task():
    ctx = setup()
    ctx.hooks["post_tool_call"](tool_name="web_extract", args={}, result=PAGE, session_id="", task_id="t9")
    out = ctx.hooks["pre_tool_call"](tool_name="terminal", args={}, session_id="", task_id="t9", tool_call_id="c1")
    assert out["action"] == "approve"
    assert ctx.hooks["pre_tool_call"](tool_name="terminal", args={}, session_id="", task_id="t8", tool_call_id="c1") is None


def delegate(ctx, reads_web):
    ctx.hooks["pre_tool_call"](tool_name="delegate_task", args={}, session_id="parent", task_id="tp", tool_call_id="d1")
    if reads_web:  # the subagent runs in its own session
        read_a_page(ctx, session="child")
    ctx.hooks["post_tool_call"](tool_name="delegate_task", args={}, result="done", session_id="parent",
                                task_id="tp", tool_call_id="d1")


def test_a_subagent_that_read_the_web_marks_the_parent():
    ctx = setup()
    delegate(ctx, reads_web=True)
    assert pre(ctx, "terminal", session="parent")["action"] == "approve"


def test_a_subagent_that_read_nothing_from_the_web_leaves_the_parent_free():
    ctx = setup()
    delegate(ctx, reads_web=False)
    assert pre(ctx, "terminal", session="parent") is None


def test_a_delegation_whose_start_was_not_seen_counts_as_web():
    ctx = setup()
    ctx.hooks["post_tool_call"](tool_name="delegate_task", args={}, result="done", session_id="parent", tool_call_id="d7")
    assert pre(ctx, "terminal", session="parent")["action"] == "approve"


@pytest.mark.parametrize("tool, args", [
    ("terminal", {"command": "curl -s https://example.com/page"}),
    ("terminal", {"command": "git clone https://github.com/someone/repo"}),
    ("process_manage", {"action": "start", "command": "wget http://example.com/x"}),
    ("execute_code", {"code": "import urllib.request\nurllib.request.urlopen('https://example.com').read()"}),
])
def test_a_command_or_script_with_a_url_marks_the_conversation(tool, args):
    ctx = setup()
    ctx.hooks["post_tool_call"](tool_name=tool, args=args, result="output", session_id="s1")
    assert pre(ctx, "write_file")["action"] == "approve"


@pytest.mark.parametrize("tool, args", [
    ("terminal", {"command": "pytest -q tests"}),
    ("execute_code", {"code": "print(sum(range(10)))"}),
    ("write_file", {"path": "notes.md", "content": "see https://example.com"}),  # writing a URL fetches nothing
])
def test_commands_without_a_url_and_other_tools_do_not(tool, args):
    ctx = setup()
    ctx.hooks["post_tool_call"](tool_name=tool, args=args, result="ok", session_id="s1")
    assert pre(ctx, "terminal") is None


def test_a_resumed_conversation_with_a_fetching_command_is_marked():
    ctx = setup()
    history = [
        {"role": "assistant", "tool_calls": [{"id": "t1", "function": {
            "name": "terminal", "arguments": json.dumps({"command": "curl https://example.com"})}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "<html>...</html>"},
    ]
    ctx.hooks["pre_llm_call"](session_id="s9", user_message="go on", conversation_history=history)
    assert pre(ctx, "terminal", session="s9")["action"] == "approve"
