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
                                  "delegate_task", "memory", "send_message", "some_future_tool",
                                  "session_search", "mcp_github_create_issue"])
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


@pytest.mark.parametrize("tool", ["web_search", "x_search", "browser_cdp", "browser_exec", "mcp_fetch_fetch"])
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


# ---------------------------------------------------------------- reads after web content


@pytest.fixture
def home(tmp_path, monkeypatch):
    for parts in (("cache", "web"), ("cache", "spillover"), (".ssh",)):
        tmp_path.joinpath(*parts).mkdir(parents=True)
    monkeypatch.setattr(gw, "_hermes_home", lambda: tmp_path)
    return tmp_path


def test_reads_anywhere_are_free_before_web_content(home):
    ctx = setup()
    assert pre(ctx, "read_file", args={"path": str(home / ".ssh" / "id_ed25519")}) is None
    assert pre(ctx, "search_files", args={"path": str(home), "pattern": "key"}) is None


def test_after_web_content_reading_hermes_saved_pages_stays_free(home):
    ctx = setup()
    read_a_page(ctx)
    assert pre(ctx, "read_file", args={"path": str(home / "cache" / "web" / "site-1c75.md"), "offset": 150}) is None
    assert pre(ctx, "read_file", args={"path": str(home / "cache" / "spillover" / "p5AL.txt")}) is None
    assert pre(ctx, "search_files", args={"path": str(home / "cache" / "web"), "pattern": "price"}) is None


@pytest.mark.parametrize("tool, where", [
    ("read_file", (".ssh", "id_ed25519")),
    ("read_file", ("config.yaml",)),
    ("search_files", ("cache", "spillover")),   # other tools' and sessions' output
    ("search_files", ()),                       # the whole Hermes folder
])
def test_after_web_content_other_reads_go_to_the_approval_gate(home, tool, where):
    ctx = setup()
    read_a_page(ctx)
    path = str(home.joinpath(*where))
    out = pre(ctx, tool, args={"path": path})
    assert out["action"] == "approve" and path in out["message"]
    assert out["rule_key"] == f"webgraft:tool_guard:{tool}"


@pytest.mark.parametrize("path", ["cache/web/site.md", "", None])
def test_relative_or_missing_paths_are_not_taken_for_the_cache(home, path):
    ctx = setup()
    read_a_page(ctx)
    assert pre(ctx, "read_file", args={"path": path} if path is not None else {})["action"] == "approve"


def test_a_path_that_climbs_out_of_the_cache_is_not_free(home):
    ctx = setup()
    read_a_page(ctx)
    sneaky = str(home / "cache" / "web" / ".." / ".." / ".ssh" / "id_ed25519")
    assert pre(ctx, "read_file", args={"path": sneaky})["action"] == "approve"


def test_a_similar_folder_name_is_not_the_cache(home):
    ctx = setup()
    read_a_page(ctx)
    assert pre(ctx, "read_file", args={"path": str(home / "cache" / "web-private" / "x")})["action"] == "approve"


def test_the_prompt_shows_the_path_without_control_characters(home):
    ctx = setup()
    read_a_page(ctx)
    out = pre(ctx, "read_file", args={"path": "/etc/passwd\n\x1b[2Jfake approved\r"})
    assert "\n" not in out["message"] and "\x1b" not in out["message"] and "/etc/passwd" in out["message"]


def test_outside_hermes_no_folder_is_free(monkeypatch):
    monkeypatch.setattr(gw, "_hermes_home", lambda: None)
    ctx = setup()
    read_a_page(ctx)
    assert pre(ctx, "read_file", args={"path": "/anything/cache/web/x.md"})["action"] == "approve"


def test_block_mode_blocks_reads_outside_the_cache_too(home):
    ctx = setup({"tool_guard": "block"})
    read_a_page(ctx)
    assert pre(ctx, "read_file", args={"path": str(home / ".ssh" / "id_ed25519")})["action"] == "block"
    assert pre(ctx, "read_file", args={"path": str(home / "cache" / "web" / "page.md")}) is None


# ---------------------------------------------------------------- images, videos, MCP, earlier sessions


@pytest.mark.parametrize("tool, key", [("vision_analyze", "image_url"), ("video_analyze", "video_url")])
def test_an_image_or_video_from_a_url_marks_the_conversation(tool, key):
    ctx = setup()
    ctx.hooks["post_tool_call"](tool_name=tool, args={key: " HTTPS://img.example/a.png", "question": "?"},
                                result="a cat", session_id="s1")
    assert pre(ctx, "terminal")["action"] == "approve"


@pytest.mark.parametrize("source", ["C:/Users/me/photo.png", "file:///tmp/a.png", "data:image/png;base64,AAAA", ""])
def test_a_local_or_inline_image_does_not_mark_it(source):
    ctx = setup()
    ctx.hooks["post_tool_call"](tool_name="vision_analyze", args={"image_url": source, "question": "see https://x.example"},
                                result="a cat", session_id="s1")
    assert pre(ctx, "terminal") is None


@pytest.mark.parametrize("tool, key", [("vision_analyze", "image_url"), ("video_analyze", "video_url")])
def test_after_web_content_images_from_urls_or_inline_stay_free(tool, key):
    ctx = setup()
    read_a_page(ctx)
    assert pre(ctx, tool, args={key: "https://img.example/a.png"}) is None
    assert pre(ctx, tool, args={key: "data:image/png;base64,AAAA"}) is None


@pytest.mark.parametrize("tool, key", [("vision_analyze", "image_url"), ("video_analyze", "video_url")])
def test_after_web_content_a_local_image_or_video_goes_to_the_approval_gate(home, tool, key):
    ctx = setup()
    read_a_page(ctx)
    path = str(home / "cache" / "web" / "shot.png")  # no free folder for images
    out = pre(ctx, tool, args={key: "file://" + path})
    assert out["action"] == "approve" and f"{tool} on {path}" in out["message"]
    assert pre(ctx, tool, args={})["action"] == "approve"


def test_block_mode_blocks_local_images_and_earlier_sessions():
    ctx = setup({"tool_guard": "block"})
    read_a_page(ctx)
    assert pre(ctx, "vision_analyze", args={"image_url": "/home/me/id.png"})["action"] == "block"
    assert pre(ctx, "session_search", args={"query": "password"})["action"] == "block"
    assert pre(ctx, "vision_analyze", args={"image_url": "https://img.example/a.png"}) is None


def test_the_prompt_says_mcp_results_count():
    ctx = setup()
    ctx.hooks["post_tool_call"](tool_name="mcp_mail_read", args={}, result="mail text", session_id="s1")
    assert "MCP" in pre(ctx, "terminal")["message"]


@pytest.mark.parametrize("call, result_name", [
    ({"name": "vision_analyze", "arguments": json.dumps({"image_url": "https://img.example/a.png"})}, None),
    ({"name": "mcp_fetch_fetch", "arguments": "{}"}, None),
    ({"name": "something", "arguments": "{}"}, "mcp_fetch_fetch"),  # the name on the tool message counts
])
def test_a_resumed_conversation_with_an_image_url_or_mcp_result_is_marked(call, result_name):
    ctx = setup()
    tool_message = {"role": "tool", "tool_call_id": "t1", "content": "text"}
    if result_name:
        tool_message["name"] = result_name
    history = [{"role": "assistant", "tool_calls": [{"id": "t1", "function": call}]}, tool_message]
    ctx.hooks["pre_llm_call"](session_id="s9", user_message="go on", conversation_history=history)
    assert pre(ctx, "terminal", session="s9")["action"] == "approve"


def test_a_resumed_conversation_with_a_local_image_is_not_marked():
    ctx = setup()
    history = [{"role": "assistant", "tool_calls": [{"id": "t1", "function": {
                   "name": "vision_analyze", "arguments": json.dumps({"image_url": "/home/me/a.png"})}}]},
               {"role": "tool", "tool_call_id": "t1", "content": "a cat"}]
    ctx.hooks["pre_llm_call"](session_id="s9", user_message="go on", conversation_history=history)
    assert pre(ctx, "terminal", session="s9") is None


# ---------------------------------------------------------------- history as Hermes stores it


def dispatched(call_id, name, arguments=None):
    """An assistant message as Hermes 0.23 stores a call made through its tool_call dispatcher
    (seen live, 2026-11-13: the call is named tool_call, the result carries tool_name)."""
    inner = json.dumps({"calls": [{"name": name, "arguments": arguments or {}}]})
    return {"role": "assistant", "tool_calls": [{"id": call_id, "call_id": call_id, "type": "function",
                                                 "function": {"name": "tool_call", "arguments": inner}}]}


@pytest.mark.parametrize("name", ["mcp__wgtest__get_note", "web_extract", "web_search", "browser_navigate"])
def test_a_resumed_conversation_in_hermes_stored_form_is_marked(name):
    ctx = setup()
    history = [{"role": "user", "content": "go"}, dispatched("call_1", name),
               {"role": "tool", "tool_call_id": "call_1", "tool_name": name,
                "content": f'<untrusted_tool_result source="{name}">\nnote\n</untrusted_tool_result>'},
               {"role": "assistant", "content": "done"}]
    ctx.hooks["pre_llm_call"](session_id="s9", user_message="now run echo", conversation_history=history)
    assert pre(ctx, "terminal", session="s9")["action"] == "approve"


def test_a_stored_result_without_tool_name_is_named_by_the_dispatched_call():
    ctx = setup()
    history = [dispatched("call_1", "mcp__mail__read"), {"role": "tool", "tool_call_id": "call_1", "content": "mail"}]
    ctx.hooks["pre_llm_call"](session_id="s9", user_message="go on", conversation_history=history)
    assert pre(ctx, "terminal", session="s9")["action"] == "approve"


@pytest.mark.parametrize("name, args", [
    ("terminal", {"command": "curl -s https://example.com"}),
    ("vision_analyze", {"image_url": "https://img.example/a.png", "question": "?"}),
])
def test_a_stored_dispatched_call_that_fetches_is_marked(name, args):
    ctx = setup()
    history = [dispatched("call_1", name, args), {"role": "tool", "tool_call_id": "call_1", "tool_name": name, "content": "x"}]
    ctx.hooks["pre_llm_call"](session_id="s9", user_message="go on", conversation_history=history)
    assert pre(ctx, "terminal", session="s9")["action"] == "approve"


def test_a_stored_history_of_local_tools_only_is_not_marked():
    ctx = setup()
    history = [dispatched("call_1", "terminal", {"command": "echo hi"}),
               {"role": "tool", "tool_call_id": "call_1", "tool_name": "terminal", "content": "hi"},
               dispatched("call_2", "vision_analyze", {"image_url": "C:/img.png"}),
               {"role": "tool", "tool_call_id": "call_2", "tool_name": "vision_analyze", "content": "a cat"}]
    ctx.hooks["pre_llm_call"](session_id="s9", user_message="go on", conversation_history=history)
    assert pre(ctx, "terminal", session="s9") is None
