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
    assert ctx.hooks["pre_tool_call"](tool_name="terminal", args={}, session_id="", task_id="t8", tool_call_id="c1") is None
    ctx.hooks["post_tool_call"](tool_name="web_extract", args={}, result=PAGE, session_id="", task_id="t9")
    out = ctx.hooks["pre_tool_call"](tool_name="terminal", args={}, session_id="", task_id="t9", tool_call_id="c1")
    assert out["action"] == "approve"
    # another task no conversation is known for: it cannot be told apart, so it is guarded too
    out = ctx.hooks["pre_tool_call"](tool_name="terminal", args={}, session_id="", task_id="t8", tool_call_id="c1")
    assert out["action"] == "approve"
    # a known conversation that read nothing stays free
    assert pre(ctx, "terminal", session="clean") is None


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


# ---------------------------------------------------------------- the mark survives a restart


@pytest.fixture
def marks(tmp_path, monkeypatch):
    path = tmp_path / "plugin-data" / "webgraft" / "web_sessions.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(gw, "_marks_file", lambda: path)
    return path


def restart():
    """A new Hermes process: the plugin's memory is empty, its plugin-data file is not."""
    gw._web_sessions.clear()
    gw._task_sessions.clear()


def test_a_marked_conversation_stays_guarded_after_a_restart(marks):
    ctx = setup()
    read_a_page(ctx)
    restart()
    assert json.loads(marks.read_text(encoding="utf-8")) == ["s1"]  # the id only, no content
    assert pre(ctx, "terminal")["action"] == "approve"
    assert pre(ctx, "terminal", session="s2") is None


def test_a_compacted_history_after_a_restart_is_still_guarded(marks):
    # Seen live (2026-11-13): /compress replaced the web_extract result with a summary; after a
    # gateway restart the resumed session ran terminal.
    ctx = setup()
    read_a_page(ctx, session="s9")
    restart()
    compacted = [{"role": "user", "content": "Open https://example.com"},
                 {"role": "assistant", "content": "[CONTEXT COMPACTION - REFERENCE ONLY] Earlier turns were compacted."}]
    ctx.hooks["pre_llm_call"](session_id="s9", user_message="run echo", conversation_history=compacted)
    assert pre(ctx, "terminal", session="s9")["action"] == "approve"


def test_the_saved_list_is_bounded_and_written_once_per_conversation(marks):
    ctx = setup()
    for i in range(gw.MAX_WEB_SESSIONS + 5):
        read_a_page(ctx, session=f"s{i}")
    read_a_page(ctx, session="s7")  # already marked: no rewrite needed
    ids = json.loads(marks.read_text(encoding="utf-8"))
    assert len(ids) == gw.MAX_WEB_SESSIONS and ids[-1] == f"s{gw.MAX_WEB_SESSIONS + 4}"


def test_script_task_keys_are_not_saved(marks):
    ctx = setup()
    ctx.hooks["post_tool_call"](tool_name="web_extract", args={}, result=PAGE, session_id="", task_id="t1")
    assert not marks.exists() or "task:t1" not in marks.read_text(encoding="utf-8")


@pytest.mark.parametrize("content", ["not json", '{"s1": 1}', "[1, 2]", ""])
def test_a_damaged_file_counts_as_empty(marks, content):
    marks.write_text(content, encoding="utf-8")
    ctx = setup()
    assert pre(ctx, "terminal") is None
    read_a_page(ctx)
    assert pre(ctx, "terminal")["action"] == "approve"


def test_an_unwritable_folder_keeps_the_mark_in_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "_marks_file", lambda: tmp_path / "missing" / "deeper" / "web_sessions.json")
    ctx = setup()
    read_a_page(ctx)
    assert pre(ctx, "terminal")["action"] == "approve"


def test_a_continuation_or_child_session_inherits_the_parent_mark(marks):
    ctx = setup()
    read_a_page(ctx, session="parent")
    restart()
    ctx.hooks["pre_llm_call"](session_id="child", user_message="go on", conversation_history=[],
                              parent_session_id="parent")
    assert pre(ctx, "terminal", session="child")["action"] == "approve"
    ctx.hooks["pre_llm_call"](session_id="other", user_message="go on", conversation_history=[],
                              parent_session_id="clean-parent")
    assert pre(ctx, "terminal", session="other") is None


# ---------------------------------------------------------------- a script's web read (pre_tool_call only)


def test_a_web_read_made_by_a_script_marks_the_conversation_without_a_post_event():
    # Live trace (2026-11-13, user's TUI): the script's web_extract reached pre_tool_call with task_id and
    # no session id, and never post_tool_call (Hermes suppresses it inside execute_code); the next
    # terminal then ran with no prompt.
    ctx = setup()
    sid = "local-637b24d7"
    hook = ctx.hooks["pre_tool_call"]
    assert hook(tool_name="execute_code", args={"code": "import base64 ..."}, session_id=sid, task_id=sid,
                tool_call_id="Gi8S") is None
    assert hook(tool_name="web_extract", args={"urls": ["https://example.com"]}, session_id="", task_id=sid,
                tool_call_id="") is None
    ctx.hooks["post_tool_call"](tool_name="execute_code", args={}, result="{}", session_id=sid, task_id=sid)
    assert pre(ctx, "terminal", session=sid)["action"] == "approve"


def test_a_fetching_command_is_not_held_back_by_its_own_mark():
    ctx = setup()
    assert pre(ctx, "terminal", args={"command": "curl -s https://example.com"}) is None
    assert pre(ctx, "terminal", args={"command": "echo next"})["action"] == "approve"


def test_a_web_call_that_is_refused_does_not_mark_by_itself():
    ctx = setup({"tool_guard": "block"})
    read_a_page(ctx, session="s1")
    # a refused curl in another conversation brings nothing in
    assert pre(ctx, "terminal", session="s2", args={"command": "echo hi"}) is None


# ---------------------------------------------------------------- Kanban workers (nobody to approve)


@pytest.fixture
def worker(monkeypatch):
    """A worker the Kanban dispatcher spawned for card t_child on board research."""
    def on(task="t_child", board="research"):
        monkeypatch.setenv("HERMES_KANBAN_TASK", task)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    on()
    return on


def first_turn(ctx, session, message):
    ctx.hooks["pre_llm_call"](session_id=session, user_message=message, conversation_history=[])


def card_message(task="t_child", parents=(), parent_section=True):
    text = f"# Kanban task {task}: Research\n\nAssignee: default\n\n## Body\nResearch it.\n"
    if parents or not parent_section:
        return text + "\n## Parent task results\n_Handoffs from upstream tasks._\n" + "".join(
            f"### {p} (completed 2m ago)\nSummary of {p}.\n\n" for p in parents) + "## Comment thread\n"
    return text


def new_worker_process():
    restart()
    gw._kanban_cards.clear()
    gw._handoff_files.clear()


def live_message(attachments, recent=("t_40bbbe48", "t_6d43fd20")):
    """The first message Hermes v0.23.1 gave the writer card of the 2026-11-17 proof run (t_f676b487),
    shortened, with its attachment folder replaced."""
    a1 = str(attachments / "t_0850f487" / "brief.txt")
    a2 = str(attachments / "t_e99785e8" / "Hermes_models_brief.md")
    meta = lambda path, sid: json.dumps({"artifacts": [path], "worker_session_id": sid}, ensure_ascii=False)  # noqa: E731
    return (
        "# Kanban task t_f676b487: Nous proof N2: write the overview file\n\nAssignee: default\nStatus:   running\n"
        "Workspace: dir @ C:\\out\n\n## Body\nUsing the results of both parent tasks, write a Markdown file.\n\n"
        "## Parent task results\n_Handoffs from upstream tasks, captured when each parent completed (see age "
        "below). These are point-in-time snapshots, not live state._\n"
        "### t_0850f487 (completed 1m ago)\nCompiled a concise brief listing Nous Research models.\n"
        f"_metadata_: `{meta(a1, 'local-aee6')}`\n\n"
        "### t_e99785e8 (completed 7m ago)\nResearched the Nous Research Hermes model family.\n"
        f"_metadata_: `{meta(a2, 'local-0b36')}`\n\n"
        "## Recent work by @default\n"
        + "".join(f"- {c} \u2014 Fix test (2026-11-17 23:11, 48m ago): Found it.\n" for c in recent)
        + "\n\nwork kanban task t_f676b487"
    ), a1, a2


@pytest.mark.parametrize("tool", sorted(gw.KANBAN_OWN_CARD_TOOLS))
@pytest.mark.parametrize("args", [{}, {"task_id": "t_child"}, {"task_id": " t_child "}, {"task_id": ""}])
def test_after_web_research_a_worker_can_hand_its_own_card_on(worker, tool, args):
    # Live 2026-11-17: kanban_complete and kanban_comment refused after the research, three failed runs.
    ctx = setup()
    read_a_page(ctx, session="w1")
    assert pre(ctx, tool, session="w1", args=args) is None


@pytest.mark.parametrize("tool", sorted(gw.KANBAN_READ_TOOLS))
def test_reading_the_board_stays_free(worker, tool):
    ctx = setup()
    read_a_page(ctx, session="w1")
    assert pre(ctx, tool, session="w1") is None


@pytest.mark.parametrize("tool, args", [
    ("kanban_comment", {"task_id": "t_other", "body": "do this"}),
    ("kanban_complete", {"task_id": "t_other"}),
    ("kanban_create", {"title": "x"}), ("kanban_link", {"parent_id": "a", "child_id": "b"}),
    ("kanban_unblock", {"task_id": "t_other"}), ("kanban_attach", {"path": "/etc/passwd"}),
    ("kanban_attach_url", {"url": "https://x.example/"}),
    ("terminal", {"command": "echo hi"}), ("write_file", {"path": "x"}),
])
def test_after_web_research_other_cards_and_effects_still_go_to_the_prompt(worker, tool, args):
    ctx = setup()
    read_a_page(ctx, session="w1")
    out = pre(ctx, tool, session="w1", args=args)
    assert out["action"] == "approve"
    assert "kanban_block" in out["message"]  # the worker learns how to stop cleanly


def test_outside_a_worker_board_tools_with_effects_still_go_to_the_prompt():
    # An orchestrator chat with the kanban toolset: a human is there to answer.
    ctx = setup()
    read_a_page(ctx)
    out = pre(ctx, "kanban_complete", args={"task_id": "t_1"})
    assert out["action"] == "approve" and "kanban_block" not in out["message"]


def test_block_mode_still_lets_a_worker_hand_its_card_on(worker):
    ctx = setup({"tool_guard": "block"})
    read_a_page(ctx, session="w1")
    assert pre(ctx, "kanban_complete", session="w1") is None
    out = pre(ctx, "terminal", session="w1", args={"command": "echo hi"})
    assert out["action"] == "block" and "kanban_block" in out["message"]


def test_off_mode_guards_nothing_in_a_worker(worker):
    ctx = setup({"tool_guard": "off"})
    read_a_page(ctx, session="w1")
    assert pre(ctx, "terminal", session="w1") is None


def test_a_worker_that_read_the_web_records_its_card_by_id_only(worker, tmp_path):
    ctx = setup()
    first_turn(ctx, "w1", card_message())
    assert not gw._card_recorded("research", "t_child")
    read_a_page(ctx, session="w1")
    folder = tmp_path / "plugin-data" / "webgraft" / "kanban_cards"
    assert [p.name for p in folder.iterdir()] == ["research__t_child"]
    assert (folder / "research__t_child").read_bytes() == b""  # no content


def test_a_retry_of_that_card_starts_guarded_in_a_new_process(worker):
    ctx = setup()
    read_a_page(ctx, session="run1")
    new_worker_process()
    first_turn(ctx, "run2", card_message() + "\n## Prior attempts on this task\nWorker's last output: ...\n")
    assert pre(ctx, "terminal", session="run2")["action"] == "approve"
    assert pre(ctx, "kanban_complete", session="run2") is None


def test_a_child_of_a_card_that_read_the_web_starts_guarded(worker):
    ctx = setup()
    worker(task="t_parent")
    read_a_page(ctx, session="parent-run")
    assert pre(ctx, "kanban_complete", session="parent-run") is None
    new_worker_process()
    worker(task="t_child")
    first_turn(ctx, "child-run", card_message(parents=["t_clean", "t_parent"]))
    assert pre(ctx, "terminal", session="child-run")["action"] == "approve"
    assert pre(ctx, "kanban_complete", session="child-run") is None
    assert gw._card_recorded("research", "t_child")  # in turn, for the grandchildren


def test_a_chain_that_never_read_the_web_stays_free(worker):
    ctx = setup()
    first_turn(ctx, "child-run", card_message(parents=["t_clean"]))
    assert pre(ctx, "terminal", session="child-run", args={"command": "echo hi"}) is None


def test_a_parent_section_without_readable_card_ids_counts_as_web(worker):
    ctx = setup()
    first_turn(ctx, "child-run", card_message(parent_section=False))
    assert pre(ctx, "terminal", session="child-run")["action"] == "approve"


def test_the_same_card_id_on_another_board_is_not_confused(worker):
    ctx = setup()
    worker(task="t_parent", board="research")
    read_a_page(ctx, session="parent-run")
    new_worker_process()
    worker(task="t_child", board="ops")
    first_turn(ctx, "child-run", card_message(parents=["t_parent"]))
    assert pre(ctx, "terminal", session="child-run") is None


def test_showing_a_card_that_read_the_web_marks_the_conversation(worker):
    ctx = setup()
    worker(task="t_parent")
    read_a_page(ctx, session="parent-run")
    new_worker_process()
    worker(task="t_child")
    first_turn(ctx, "child-run", card_message())  # no parent section in this message
    assert pre(ctx, "terminal", session="child-run") is None
    shown = json.dumps({"task": {"id": "t_child", "body": "b"}, "parents": ["t_parent"], "children": [],
                        "runs": [{"id": 1, "summary": None}]})
    ctx.hooks["post_tool_call"](tool_name="kanban_show", args={}, result=shown, session_id="child-run")
    assert pre(ctx, "terminal", session="child-run")["action"] == "approve"


def test_a_chat_listing_a_card_that_read_the_web_is_marked(worker, monkeypatch):
    # An orchestrator chat (no dispatcher environment) that reads the board: any board matches.
    worker(task="t_parent")
    ctx = setup()
    read_a_page(ctx, session="parent-run")
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_BOARD")
    listed = json.dumps({"tasks": [{"id": "t_other"}, {"id": "t_parent", "result": "summary"}]})
    ctx.hooks["post_tool_call"](tool_name="kanban_list", args={}, result=listed, session_id="chat")
    assert pre(ctx, "terminal", session="chat")["action"] == "approve"


def test_card_ids_cannot_leave_the_records_folder(worker, tmp_path):
    ctx = setup()
    worker(task="../../evil", board="..\\up")
    read_a_page(ctx, session="w1")
    folder = tmp_path / "plugin-data" / "webgraft" / "kanban_cards"
    names = [p.name for p in folder.iterdir()]
    assert len(names) == 1 and "/" not in names[0] and "\\" not in names[0]
    assert gw._card_recorded("..\\up", "../../evil")


def test_the_card_records_are_bounded(worker, monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "MAX_KANBAN_CARDS", 3)
    ctx = setup()
    for i in range(5):
        worker(task=f"t_{i}")
        read_a_page(ctx, session=f"w{i}")
    folder = tmp_path / "plugin-data" / "webgraft" / "kanban_cards"
    assert len(list(folder.iterdir())) == 3


def test_outside_hermes_cards_are_kept_in_memory(worker, monkeypatch):
    monkeypatch.setattr(gw, "_cards_dir", lambda: None)
    ctx = setup()
    read_a_page(ctx, session="w1")
    assert gw._card_recorded("research", "t_child")


# ---------------------------------------------------------------- a card's output in its workspace


@pytest.fixture
def workspace(worker, tmp_path, monkeypatch):
    """The card's workspace (HERMES_KANBAN_WORKSPACE), as the dispatcher sets it."""
    folder = tmp_path / "boards" / "research" / "workspaces" / "t_child"
    folder.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(folder))
    return folder


@pytest.mark.parametrize("tool, extra", [
    ("write_file", {"content": "# Route"}),
    ("patch", {"old_string": "a", "new_string": "b"}),
    ("patch", {"old_string": "a", "new_string": "b", "mode": "replace"}),
])
@pytest.mark.parametrize("name", ["route.md", "guides/levels/route.md"])
def test_after_web_research_a_worker_writes_its_output_in_its_workspace(workspace, tool, extra, name):
    ctx = setup()
    read_a_page(ctx, session="w1")
    assert pre(ctx, tool, session="w1", args={"path": str(workspace / name), **extra}) is None


def test_a_child_of_a_web_card_can_still_write_the_final_file(workspace, worker):
    ctx = setup()
    worker(task="t_parent")
    read_a_page(ctx, session="parent-run")
    new_worker_process()
    worker(task="t_child")
    first_turn(ctx, "child-run", card_message(parents=["t_parent"]))
    assert pre(ctx, "write_file", session="child-run", args={"path": str(workspace / "route.md")}) is None
    assert pre(ctx, "terminal", session="child-run")["action"] == "approve"


@pytest.mark.parametrize("where", ["outside", "relative", "climbs", "similar", "folder"])
def test_writes_outside_the_workspace_still_go_to_the_prompt(workspace, tmp_path, where):
    path = {"outside": str(tmp_path / "elsewhere" / "x.md"), "relative": "route.md",
            "climbs": str(workspace / ".." / "t_other" / "x.md"),
            "similar": str(workspace) + "-evil" + "/x.md", "folder": str(workspace)}[where]
    ctx = setup()
    read_a_page(ctx, session="w1")
    out = pre(ctx, "write_file", session="w1", args={"path": path, "content": "x"})
    assert out["action"] == "approve"
    assert str(workspace) in out["message"] and "kanban_block" in out["message"]


@pytest.mark.parametrize("args", [
    {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: /etc/hosts\n*** End Patch"},
    {"path": "PLACEHOLDER", "old_string": "a", "new_string": "b", "patch": "*** Add File: /tmp/x"},
    {"path": "PLACEHOLDER", "content": "x", "cross_profile": True},
])
def test_multi_file_patches_and_cross_profile_writes_are_not_free(workspace, args):
    args = {k: (str(workspace / "route.md") if v == "PLACEHOLDER" else v) for k, v in args.items()}
    ctx = setup()
    read_a_page(ctx, session="w1")
    tool = "write_file" if "content" in args else "patch"
    assert pre(ctx, tool, session="w1", args=args)["action"] == "approve"


def test_a_link_inside_the_workspace_that_leads_out_is_not_free(workspace, tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    try:
        (workspace / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("this system does not let the test create a link")
        import _winapi  # a junction needs no privilege on Windows
        _winapi.CreateJunction(str(outside), str(workspace / "link"))
    ctx = setup()
    read_a_page(ctx, session="w1")
    assert pre(ctx, "write_file", session="w1", args={"path": str(workspace / "link" / "x")})["action"] == "approve"


def test_block_mode_still_lets_a_worker_write_in_its_workspace(workspace):
    ctx = setup({"tool_guard": "block"})
    read_a_page(ctx, session="w1")
    assert pre(ctx, "write_file", session="w1", args={"path": str(workspace / "route.md")}) is None
    assert pre(ctx, "write_file", session="w1", args={"path": "/tmp/x"})["action"] == "block"


def test_without_a_workspace_writes_go_to_the_prompt(worker, tmp_path):
    ctx = setup()
    read_a_page(ctx, session="w1")
    out = pre(ctx, "write_file", session="w1", args={"path": str(tmp_path / "x.md")})
    assert out["action"] == "approve" and "workspace" not in out["message"]


def test_outside_a_worker_the_workspace_variable_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    ctx = setup()
    read_a_page(ctx)
    assert pre(ctx, "write_file", args={"path": str(tmp_path / "x.md")})["action"] == "approve"


# ---------------------------------------------------------------- reading what the parent cards handed on


@pytest.fixture
def writer(workspace, worker, tmp_path):
    """The writer card of the proof run: its parents read the web and handed on one file each."""
    ctx = setup()
    attachments = tmp_path / "attachments"
    for parent in ("t_0850f487", "t_e99785e8"):
        worker(task=parent)
        read_a_page(ctx, session=f"run-{parent}")
    new_worker_process()
    worker(task="t_f676b487")
    message, a1, a2 = live_message(attachments)
    for path in (a1, a2):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("brief", encoding="utf-8")
    first_turn(ctx, "writer", message)
    return ctx, a1, a2, attachments


def test_hermes_first_message_is_read_as_hermes_writes_it(tmp_path):
    message, a1, a2 = live_message(tmp_path)
    ids, files, unreadable = gw._handoff(message)
    assert ids == ["t_0850f487", "t_e99785e8", "t_40bbbe48", "t_6d43fd20"]
    assert files == [a1, a2] and not unreadable


def test_the_writer_starts_guarded_and_reads_what_its_parents_handed_on(writer):
    # Live 2026-11-18: read_file, a terminal `type` and search_files on these files were all refused.
    ctx, a1, a2, _ = writer
    assert pre(ctx, "terminal", session="writer")["action"] == "approve"
    for path in (a1, a2):
        assert pre(ctx, "read_file", session="writer", args={"path": path}) is None


def test_other_files_beside_them_and_searches_there_still_go_to_the_prompt(writer):
    ctx, a1, _, attachments = writer
    other = attachments / "t_0850f487" / "attached-by-a-person.env"
    out = pre(ctx, "read_file", session="writer", args={"path": str(other)})
    assert out["action"] == "approve"
    assert a1 in out["message"] and "kanban_block" in out["message"]  # the worker learns what it may read
    assert pre(ctx, "search_files", session="writer",
               args={"path": str(attachments / "t_0850f487"), "pattern": "x"})["action"] == "approve"
    assert pre(ctx, "read_file", session="writer",
               args={"path": str(attachments / "t_other" / "x.md")})["action"] == "approve"


def test_the_writer_reads_and_searches_its_own_workspace(writer, workspace):
    ctx = writer[0]
    assert pre(ctx, "read_file", session="writer", args={"path": str(workspace / "draft.md")}) is None
    assert pre(ctx, "search_files", session="writer", args={"path": str(workspace), "pattern": "x"}) is None
    assert pre(ctx, "search_files", session="writer",
               args={"path": str(workspace / "sub"), "pattern": "x"}) is None


def test_a_recent_card_of_the_same_profile_that_read_the_web_marks_the_worker(worker):
    ctx = setup()
    worker(task="t_40bbbe48")
    read_a_page(ctx, session="d1")
    new_worker_process()
    worker(task="t_new")
    first_turn(ctx, "w", card_message() + "\n## Recent work by @default\n- t_40bbbe48 — D1 (1h ago): Found it.\n")
    assert pre(ctx, "terminal", session="w")["action"] == "approve"


def test_recent_cards_that_never_read_the_web_leave_the_worker_free(worker):
    ctx = setup()
    first_turn(ctx, "w", card_message() + "\n## Recent work by @default\n- t_clean — C (1h ago): echo.\n")
    assert pre(ctx, "terminal", session="w", args={"command": "echo hi"}) is None


def test_metadata_cut_by_hermes_size_cap_hands_no_file_on(worker, tmp_path):
    ctx = setup()
    worker(task="t_p")
    read_a_page(ctx, session="p")
    new_worker_process()
    worker(task="t_child")
    cut = (card_message(parents=["t_p"]).replace(
        "Summary of t_p.\n", 'Summary of t_p.\n_metadata_: `{"artifacts": ["' + str(tmp_path / "f.md") + '"], "x": "cut…`\n'))
    first_turn(ctx, "w", cut)
    assert gw._handoff_files == set()
    assert pre(ctx, "read_file", session="w", args={"path": str(tmp_path / "f.md")})["action"] == "approve"


def test_outside_a_worker_parent_files_are_not_free(tmp_path):
    message, a1, _ = live_message(tmp_path)
    ctx = setup()
    first_turn(ctx, "chat", message)  # a chat that pastes such a message gets nothing from it
    read_a_page(ctx, session="chat")
    assert pre(ctx, "read_file", session="chat", args={"path": a1})["action"] == "approve"


@pytest.mark.skipif(sys.platform != "win32", reason="Git Bash paths are a Windows form")
def test_a_git_bash_path_to_the_workspace_counts_on_windows(workspace):
    # Seen live 2026-11-17: a worker wrote /c/Users/.../workspaces/t_33a5d75a/output.txt.
    drive, rest = str(workspace).split(":", 1)
    msys = f"/{drive.lower()}{rest.replace(chr(92), '/')}/route.md"
    ctx = setup()
    read_a_page(ctx, session="w1")
    assert pre(ctx, "write_file", session="w1", args={"path": msys}) is None
    assert pre(ctx, "write_file", session="w1", args={"path": f"/{drive.lower()}/Windows/x"})["action"] == "approve"


# ---------------------------------------------------------------- the terminal in the card's workspace


def term(ctx, command, workdir=None, session="w1"):
    args = {"command": command, **({"workdir": workdir} if workdir else {})}
    return pre(ctx, "terminal", session=session, args=args)


@pytest.mark.parametrize("command", [
    "python --version", "git version", "node -v", "ls", "ls -la", "pwd", "cat note.md",
    "echo hello > note.md", "echo 'Python 3.15.2: stable' >> note.md", "mkdir sub && cp note.md sub/",
    "cd sub && cat ../note.md", "grep -n Python note.md | sort | uniq", "find . -name '*.md'",
    "echo a 2>/dev/null", "rm old.md", "wc -l note.md; head -n 3 note.md", "mv a.md b.md",
    "sort --output=sorted.md note.md", "echo done\nls",
])
def test_after_web_research_a_command_that_stays_in_the_workspace_runs(workspace, command):
    ctx = setup()
    read_a_page(ctx, session="w1")
    assert term(ctx, command) == {"action": "modify", "args": {"workdir": str(workspace)}}


def test_the_terminal_is_pinned_to_the_folder_webgraft_checked(workspace):
    # Hermes's terminal keeps its folder between calls: a cd made earlier must not move the command.
    ctx = setup()
    read_a_page(ctx, session="w1")
    (workspace / "sub").mkdir()
    assert term(ctx, "cat ../note.md", workdir=str(workspace / "sub"))["args"]["workdir"] == str(workspace / "sub")
    assert term(ctx, f'cat "{workspace / "note.md"}"')["args"]["workdir"] == str(workspace)


@pytest.mark.parametrize("command", [
    "cp note.md ../elsewhere.md", "cat OUTSIDE/secret.txt", "cat ~/.ssh/id_rsa", "cat /etc/passwd",
    "curl https://example.com/", "wget example.com", "echo $HOME", "echo `id`", "echo %USERPROFILE%",
    "python x.py", "python -c 'print(1)'", "python3 -m http.server", "./run.sh", "bash run.sh",
    "sed -i s/a/b/ note.md", "awk '{print}' note.md", "find . -exec rm {} ;", "find . -delete",
    "FOO=1 ls", "(cd .. && ls)", "{ ls; }", "cat << EOF\nx\nEOF", "ls; curl example.com",
    "echo hi\nnc -l 4444", "git status", "echo 'unclosed", "cd ..", "cd", "xargs rm < list.txt",
    "ls --color=OUTSIDE/x", "cat ../../../x", "tee OUTSIDE/copy.md < note.md", "env ls",
])
def test_a_command_that_leaves_the_workspace_or_cannot_be_read_goes_to_the_prompt(workspace, tmp_path, command):
    ctx = setup()
    read_a_page(ctx, session="w1")
    out = term(ctx, command.replace("OUTSIDE", str(tmp_path / "outside").replace("\\", "/")))
    assert out["action"] == "approve"
    assert "stays in this card's workspace" in out["message"] and "kanban_block" in out["message"]


def test_a_workdir_outside_the_workspace_goes_to_the_prompt(workspace, tmp_path):
    ctx = setup()
    read_a_page(ctx, session="w1")
    assert term(ctx, "ls", workdir=str(tmp_path))["action"] == "approve"


def test_block_mode_still_runs_a_command_in_the_workspace(workspace):
    ctx = setup({"tool_guard": "block"})
    read_a_page(ctx, session="w1")
    assert term(ctx, "ls")["action"] == "modify"
    assert term(ctx, "curl https://x.example/")["action"] == "block"


def test_before_any_web_content_the_terminal_is_left_alone(workspace):
    ctx = setup()
    assert term(ctx, "curl https://x.example/ -o x") is None


def test_outside_a_worker_the_terminal_still_goes_to_the_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    ctx = setup()
    read_a_page(ctx)
    out = term(ctx, "ls", session="s1")
    assert out["action"] == "approve" and "workspace" not in out["message"]


def test_execute_code_refusals_point_to_the_free_ways(workspace):
    ctx = setup()
    read_a_page(ctx, session="w1")
    out = pre(ctx, "execute_code", session="w1", args={"code": "open('x','w')"})
    assert out["action"] == "approve" and "write_file" in out["message"]


# ---------------------------------------------------------------- a tool_call that reaches the hook still wrapped
# Hermes unwraps a well-formed tool_call before pre_tool_call (agent/tool_executor.py, _unwrap_tool_search_call):
# the hook sees the real tool. The wrapper still reaches it when Hermes cannot resolve the call (it then answers
# with its own error and runs nothing) and for a connector batch (tools/connectors/dispatch.py runs each entry
# through the hooks on its own). Seen live (2026-11-18): Gemma 4 leaked its template into a call named tool_call,
# and webgraft answered "requires approval", which a Kanban worker cannot get, instead of Hermes's own error.


@pytest.fixture
def hermes_bridge(monkeypatch):
    """Hermes's tools.tool_search, reduced to the contract webgraft uses: resolve_underlying_call returns
    (name, arguments, None), (CONNECTOR_BATCH_SENTINEL, {"calls": ...}, None), or (None, {}, error)."""
    import types
    fake = types.ModuleType("tools.tool_search")
    fake.CONNECTOR_BATCH_SENTINEL = "connectors__execute"

    def resolve_underlying_call(args, session_tool_names=None):
        calls = args.get("calls")
        if not isinstance(calls, list) or not calls or not all(isinstance(c, dict) and c.get("name") for c in calls):
            return None, {}, "tool_call requires 'calls' (an array of {name, arguments})"
        if calls[0]["name"].startswith("connectors__"):
            return fake.CONNECTOR_BATCH_SENTINEL, {"calls": calls}, None
        return calls[0]["name"], calls[0].get("arguments") or {}, None

    fake.resolve_underlying_call = resolve_underlying_call
    package = types.ModuleType("tools")
    package.tool_search = fake
    monkeypatch.setitem(sys.modules, "tools", package)
    monkeypatch.setitem(sys.modules, "tools.tool_search", fake)
    return fake


@pytest.mark.parametrize("args", [{}, {"name": ""}, {"calls": "<|tool_call>call:web_search{}"}, {"calls": [{}]}])
def test_a_tool_call_hermes_cannot_resolve_gets_hermes_own_error(hermes_bridge, args):
    ctx = setup()
    read_a_page(ctx)
    assert pre(ctx, "tool_call", args=args) is None


def test_a_connector_batch_is_guarded_entry_by_entry_not_as_a_wrapper(hermes_bridge):
    ctx = setup()
    read_a_page(ctx)
    batch = {"calls": [{"name": "connectors__mail__send", "arguments": {"to": "x"}}]}
    assert pre(ctx, "tool_call", args=batch) is None
    assert pre(ctx, "connectors__mail__send", args={"to": "x"})["action"] == "approve"  # what Hermes then sends


def test_a_wrapped_call_hermes_can_run_is_guarded_as_its_real_tool(hermes_bridge):
    ctx = setup()
    read_a_page(ctx)
    out = pre(ctx, "tool_call", args={"calls": [{"name": "mcp__mail__send", "arguments": {"to": "x"}}]})
    assert out["action"] == "approve" and out["rule_key"] == "webgraft:tool_guard:mcp__mail__send"
    assert "mcp__mail__send" in out["message"]


def test_a_wrapped_change_of_arguments_becomes_a_question(hermes_bridge, workspace):
    # webgraft pins a worker's terminal by changing its arguments; on a wrapper Hermes would change the
    # wrapper's arguments, which the tool never sees.
    ctx = setup()
    read_a_page(ctx, session="w1")
    out = pre(ctx, "tool_call", session="w1", args={"calls": [{"name": "terminal", "arguments": {"command": "ls"}}]})
    assert out["action"] == "approve" and out["rule_key"] == "webgraft:tool_guard:terminal"


def test_a_wrapped_call_reads_the_web_like_the_real_tool(hermes_bridge):
    ctx = setup()
    assert pre(ctx, "tool_call", args={"calls": [{"name": "web_extract", "arguments": {"urls": ["https://a.example/"]}}]}) is None
    assert pre(ctx, "terminal", args={"command": "echo hi"})["action"] == "approve"


def test_without_hermes_resolver_the_wrapper_is_still_guarded(monkeypatch):
    monkeypatch.setitem(sys.modules, "tools.tool_search", None)  # import fails: older Hermes, or outside it
    ctx = setup()
    read_a_page(ctx)
    out = pre(ctx, "tool_call", args={})
    assert out["action"] == "approve" and out["rule_key"] == "webgraft:tool_guard:tool_call"
