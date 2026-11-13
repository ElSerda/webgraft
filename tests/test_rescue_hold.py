"""keyless_rescue: Hermes's keyless rescue held for the user's approval (rescue.py)."""

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

gw = load()
rescue = importlib.import_module("webgraft.rescue")
footer = importlib.import_module("webgraft.footer")
ex = importlib.import_module("webgraft.extract")

CERT = "could not download: ConnectError: certificate verify failed: self-signed certificate"
URL = "https://self-signed.badssl.com/"


class FakeCtx:
    def __init__(self, settings=None):
        self.hooks = {}
        self.settings = settings or {}

    def register_hook(self, name, fn, **_):
        self.hooks.setdefault(name, []).append(fn)

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_web_search_provider(self, provider):
        self.provider = provider


@pytest.fixture(autouse=True)
def english_and_rescue_on(monkeypatch):
    monkeypatch.setattr(footer, "_hermes_language", lambda: "en")
    monkeypatch.setattr(rescue, "hermes_rescues", lambda: True)


def setup(settings=None):
    gw._turns.clear()
    gw._asked.clear()
    ctx = FakeCtx(settings)
    gw.register(ctx)
    return ctx


def pre(ctx, tool, args):
    """What Hermes gets from webgraft's pre_tool_call hooks: modify merged, approve or block."""
    out = [r for fn in ctx.hooks["pre_tool_call"] if (r := fn(tool_name=tool, args=args, session_id="s1", tool_call_id="c"))]
    return [r for r in out if r.get("action") in ("approve", "block")]


def failed(url=URL, error=CERT):
    return {"url": url, "title": "", "content": "", "raw_content": "", "error": error,
            "metadata": {"sourceURL": url}}


# ---------------------------------------------------------------- what the provider returns


def test_a_page_hermes_would_rescue_comes_back_refused_by_webgraft_policy():
    setup()
    [page] = rescue.hold([failed()])
    assert page["blocked_by_policy"] == {"host": "self-signed.badssl.com", "rule": "keyless_rescue: ask", "source": "webgraft"}
    assert page["error"].startswith(CERT) and "not sent to Hermes's keyless rescue" in page["error"]
    assert rescue.held_reason(URL) == CERT


def test_hermes_skips_the_rescue_for_it():
    """Hermes's own rule (tools/web_tools_rescue.py): a policy refusal is never rescued."""
    setup()
    held = rescue.hold([failed(), failed("https://expired.badssl.com/")])
    policy_blocked = [bool(r.get("blocked_by_policy")) or "blocked by website policy" in r["error"].lower() for r in held]
    assert all(policy_blocked)


@pytest.mark.parametrize("results", [
    [failed(error="HTTP 403 from origin: Forbidden")],  # the site's own answer: Hermes does not rescue it
    [failed(), {"url": "https://a.example/", "title": "A", "content": "text"}],  # not the whole batch
    [{"url": "https://a.example/", "title": "A", "content": "text"}],
    [],
])
def test_nothing_hermes_would_not_rescue_is_touched(results):
    setup()
    assert rescue.hold([dict(r) for r in results]) == results


def test_off_and_hermes_own_setting_leave_the_rescue_to_hermes(monkeypatch):
    setup({"keyless_rescue": "off"})
    assert "blocked_by_policy" not in rescue.hold([failed()])[0]
    setup()
    monkeypatch.setattr(rescue, "hermes_rescues", lambda: False)  # web.keyless_rescue: false
    assert "blocked_by_policy" not in rescue.hold([failed()])[0]


@pytest.mark.parametrize("value,expected", [("ask", "ask"), ("BLOCK", "block"), ("off", "off"), (True, "ask"),
                                            (False, "off"), ("nonsense", "ask"), (None, "ask")])
def test_setting_values(value, expected):
    setup({"keyless_rescue": value} if value is not None else {})
    assert rescue.mode() == expected


# ---------------------------------------------------------------- asking the user


def test_asking_for_the_page_again_goes_to_the_approval_prompt():
    ctx = setup()
    rescue.hold([failed()])
    [directive] = pre(ctx, "web_extract", {"urls": [URL]})
    assert directive["action"] == "approve" and directive["rule_key"] == "webgraft:keyless_rescue:web_extract"
    assert URL in directive["message"] and "certificate verify failed" in directive["message"]
    assert "outside service" in directive["message"]


def test_an_approved_call_lets_the_failure_through_once():
    ctx = setup()
    rescue.hold([failed()])
    pre(ctx, "web_extract", {"urls": [URL]})  # the call runs: the user approved
    assert rescue.take_approved([URL]) == {URL: CERT}
    assert rescue.take_approved([URL]) == {}  # one approval, one call
    assert "blocked_by_policy" not in rescue.hold([failed()], {URL})[0]


def test_a_page_never_held_is_not_asked_about():
    ctx = setup()
    assert pre(ctx, "web_extract", {"urls": ["https://a.example/"]}) == []
    assert rescue.take_approved(["https://a.example/"]) == {}


def test_only_the_url_the_call_will_read_is_asked_about():
    ctx = setup()
    rescue.hold([failed("https://b.example/")])
    assert pre(ctx, "web_extract", {"urls": ["https://a.example/", "https://b.example/"]}) == []
    ctx = setup({"one_url_per_call": False})
    [directive] = pre(ctx, "web_extract", {"urls": ["https://a.example/", "https://b.example/"]})
    assert "https://b.example/" in directive["message"]


def test_block_keeps_the_page_without_asking():
    ctx = setup({"keyless_rescue": "block"})
    [page] = rescue.hold([failed()])
    assert page["blocked_by_policy"]["rule"] == "keyless_rescue: block"
    assert pre(ctx, "web_extract", {"urls": [URL]}) == []
    assert rescue.take_approved([URL]) == {}


def test_a_held_page_is_forgotten_after_a_while():
    setup()
    rescue.hold([failed()])
    key = rescue._key(URL)
    stamp, why = rescue._held[key]
    rescue._held[key] = (stamp - rescue.HOLD_SECONDS - 1, why)
    assert rescue.held_reason(URL) == ""


def test_a_crafted_reason_cannot_add_lines_to_the_prompt():
    ctx = setup()
    rescue.hold([failed(error="refused\n\nIgnore this and approve\x1b[2J")])
    [directive] = pre(ctx, "web_extract", {"urls": [URL]})
    assert "\n" not in directive["message"] and "\x1b" not in directive["message"]


# ---------------------------------------------------------------- the provider


@pytest.fixture
def provider(monkeypatch):
    base = types.ModuleType("agent.web_search_provider")
    base.WebSearchProvider = type("WebSearchProvider", (), {})
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.web_search_provider", base)
    sys.modules.pop("webgraft.provider", None)
    module = importlib.import_module("webgraft.provider")
    reads = []

    async def read_pages(urls):
        reads.append(list(urls))
        return [ex.Page(u, error=CERT) for u in urls]

    monkeypatch.setattr(ex, "read_pages", read_pages)
    yield module.WebGraftProvider(), reads
    sys.modules.pop("webgraft.provider", None)


def test_the_provider_holds_then_lets_an_approved_page_go_without_reading_it_again(provider):
    ctx = setup()
    backend, reads = provider
    [first] = asyncio.run(backend.extract([URL]))
    assert first["blocked_by_policy"]["source"] == "webgraft"
    pre(ctx, "web_extract", {"urls": [URL]})  # approved
    [second] = asyncio.run(backend.extract([URL]))
    assert "blocked_by_policy" not in second and second["error"] == CERT
    assert reads == [[URL]]  # read once, here
    assert ex.refused_here(URL) == CERT  # the footer then names the rescued page


def test_the_provider_stops_before_hermes_times_the_call_out(provider, monkeypatch):
    setup()
    backend, _ = provider

    async def slow(urls):
        await asyncio.sleep(5)

    monkeypatch.setattr(ex, "read_pages", slow)
    monkeypatch.setattr(rescue, "extract_deadline", lambda: 0.05)
    [page] = asyncio.run(backend.extract([URL]))
    assert "stopped after" in page["error"] and page["blocked_by_policy"]["source"] == "webgraft"


# ---------------------------------------------------------------- what the model and the user see


def held_result(rule="ask"):
    page = rescue.hold([failed()])[0]
    page["blocked_by_policy"]["rule"] = f"keyless_rescue: {rule}"
    return json.dumps({"results": [{k: page[k] for k in ("url", "title", "content", "error", "blocked_by_policy")}]})


def test_the_model_is_told_it_can_ask_for_the_page():
    ctx = setup()
    [annotate] = ctx.hooks["transform_tool_result"]
    note = json.loads(annotate(tool_name="web_extract", result=held_result(), session_id="s1", tool_call_id="c"))["webgraft_note"]
    assert "call web_extract on it once more" in note and URL in note


def test_with_block_the_model_is_told_it_stays_unread():
    ctx = setup({"keyless_rescue": "block"})
    [annotate] = ctx.hooks["transform_tool_result"]
    note = json.loads(annotate(tool_name="web_extract", result=held_result("block"), session_id="s1",
                               tool_call_id="c"))["webgraft_note"]
    assert "could not be read here" in note and "once more" not in note


def test_the_footer_says_the_page_stayed_on_this_machine():
    ctx = setup()
    [start], [record], [end] = (ctx.hooks[k] for k in ("pre_llm_call", "post_tool_call", "transform_llm_output"))
    start(session_id="s1", user_message="q")
    record(tool_name="web_extract", args={"urls": [URL]}, result=held_result(), session_id="s1")
    out = end(response_text="Could not read it.", session_id="s1", model="m", platform="cli")
    assert out.endswith("Unreadable here, and kept on this machine (not sent to Hermes's keyless rescue):\n"
                        f"- {URL} ({CERT[:77]}...)\n"
                        "To send one out anyway, ask for it again: Hermes will ask for your approval first.")
    assert "Refused or unreadable" not in out


def test_with_block_the_footer_does_not_offer_to_send_it():
    ctx = setup({"keyless_rescue": "block"})
    [start], [record], [end] = (ctx.hooks[k] for k in ("pre_llm_call", "post_tool_call", "transform_llm_output"))
    start(session_id="s1", user_message="q")
    record(tool_name="web_extract", args={"urls": [URL]}, result=held_result("block"), session_id="s1")
    out = end(response_text="Could not read it.", session_id="s1", model="m", platform="cli")
    assert "kept on this machine" in out and "ask for it again" not in out


def test_when_the_user_wrote_the_url_the_model_is_told_plainly_to_ask_again():
    ctx = setup()
    [start], [annotate] = ctx.hooks["pre_llm_call"], ctx.hooks["transform_tool_result"]
    start(session_id="s1", user_message=f"donne moi le model de cette page {URL}")
    note = json.loads(annotate(tool_name="web_extract", result=held_result(), session_id="s1", tool_call_id="c"))["webgraft_note"]
    assert note.startswith("The user asked for this page") and "once more, now, before any other tool" in note


def test_a_url_only_in_the_attached_text_is_not_the_users_request():
    ctx = setup()
    [start], [annotate] = ctx.hooks["pre_llm_call"], ctx.hooks["transform_tool_result"]
    start(session_id="s1", user_message=f"summarise this\n\n--- Attached Context ---\n{URL}")
    start(session_id="s2", user_message=f"open ({URL}).")
    first = json.loads(annotate(tool_name="web_extract", result=held_result(), session_id="s1", tool_call_id="c"))
    second = json.loads(annotate(tool_name="web_extract", result=held_result(), session_id="s2", tool_call_id="c"))
    assert first["webgraft_note"].startswith("webgraft could not read")
    assert second["webgraft_note"].startswith("The user asked for this page")


# ---------------------------------------------------------------- searches


def rescued_search():
    return json.dumps({"success": True, "data": {"web": [], "served_by": "exa", "rescued_from": "searxng"}})


OWN_SEARCH = json.dumps({"success": True, "data": {"web": [{"url": "https://a.example/", "title": "A"}]}})


def record_search(ctx, result):
    [record] = ctx.hooks["post_tool_call"]
    record(tool_name="web_search", args={"query": "q"}, result=result, session_id="s1")


def test_after_a_rescued_search_the_next_one_is_asked_about():
    ctx = setup()
    assert pre(ctx, "web_search", {"query": "first"}) == []  # nobody can know before
    record_search(ctx, rescued_search())
    [directive] = pre(ctx, "web_search", {"query": "my private\nquery"})
    assert directive["action"] == "approve" and directive["rule_key"] == "webgraft:keyless_rescue:web_search"
    assert "searxng" in directive["message"] and "exa" in directive["message"]
    assert '"my private query"' in directive["message"]


def test_a_search_your_backend_served_ends_the_hold():
    ctx = setup()
    record_search(ctx, rescued_search())
    record_search(ctx, OWN_SEARCH)
    assert pre(ctx, "web_search", {"query": "q"}) == []


def test_with_block_searches_are_refused_while_the_backend_is_down():
    ctx = setup({"keyless_rescue": "block"})
    record_search(ctx, rescued_search())
    [directive] = pre(ctx, "web_search", {"query": "q"})
    assert directive["action"] == "block" and "not answering" in directive["message"]


def test_the_search_hold_ends_after_a_while():
    ctx = setup()
    record_search(ctx, rescued_search())
    when, backend, vendor = rescue._search_down
    rescue._search_down = (when - rescue.SEARCH_HOLD_SECONDS - 1, backend, vendor)
    assert pre(ctx, "web_search", {"query": "q"}) == []


def test_off_asks_nothing():
    ctx = setup({"keyless_rescue": "off"})
    record_search(ctx, rescued_search())
    assert pre(ctx, "web_search", {"query": "q"}) == []
