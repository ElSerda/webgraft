"""Hook flow tests with a fake Hermes context."""

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


def extract_result(*pages):
    return json.dumps({"results": [dict(p) for p in pages]})


PAGE = {"url": "https://portal.example/cloud", "content": "Plus plan: $20/month. Cloud agent: 4 GB RAM, 25 GB disk."}
REVIEW = {"url": "https://guide.example/plans", "content": "Super plan: $100/month, 100 GB disk, median latency 250 ms."}


def run_turn(ctx, answer, tools, session="s1"):
    h = ctx.hooks
    h["pre_llm_call"](session_id=session, user_message="q")
    for name, result in tools:
        h["post_tool_call"](tool_name=name, args={}, result=result, session_id=session)
    return h["transform_llm_output"](response_text=answer, session_id=session, model="m", platform="cli")


def setup(settings=None):
    gw._sessions.clear()
    gw._turns.clear()
    ctx = FakeCtx(settings)
    gw.register(ctx)
    return ctx


def test_footer_lists_the_opened_pages_with_titles():
    ctx = setup()
    page = dict(PAGE, title="Cloud plans | Portal")
    answer = "La formule Plus coûte 20 $ et l'agent a 25 Go de disque."
    out = run_turn(ctx, answer, [("web_search", "{}"), ("web_extract", extract_result(page))])
    assert out == answer + "\n\nSources (pages ouvertes) :\n- Cloud plans | Portal: https://portal.example/cloud"


def test_english_answer_gets_english_footer():
    ctx = setup()
    out = run_turn(ctx, "The Plus plan costs $20.", [("web_extract", extract_result(PAGE))])
    assert out.endswith("Sources (opened pages):\n- https://portal.example/cloud")


def test_search_only_turn_says_so():
    ctx = setup()
    out = run_turn(ctx, "L'agent dispose de 4 Go de mémoire vive et de 25 Go de disque.", [("web_search", "{}")])
    assert out.endswith("Aucune page ouverte : réponse basée sur les extraits de recherche.")


def test_failed_pages_are_not_listed():
    ctx = setup()
    failed = {"url": "https://blocked.example", "content": "", "error": "403"}
    out = run_turn(ctx, "L'agent dispose de 4 Go de mémoire vive et de 25 Go de disque.", [("web_extract", extract_result(failed))])
    assert "blocked.example" not in out
    assert out.endswith("Aucune page n'a pu être lue.")


def test_bot_wall_returned_as_content_is_not_a_source():
    # Hermes's keyless rescue can bring a bot wall back as page content.
    ctx = setup()
    wall = {"url": "https://wall.example/plans", "title": "Plans | Wall",
            "content": "# Plans\n\n# Automated bot check in progress\n\nDrag the handle to the target"}
    out = run_turn(ctx, "The Plus plan costs $20.", [("web_extract", extract_result(wall, PAGE))])
    assert "wall.example" not in out
    assert "portal.example" in out


def test_turn_without_web_tools_is_left_alone():
    ctx = setup()
    assert run_turn(ctx, "Bonjour.", [("read_file", "{}")]) is None


def test_earlier_page_is_listed_only_when_the_answer_uses_it():
    ctx = setup()
    run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(PAGE))])
    out = run_turn(ctx, "La Super coûte 100 $ et la Plus 20 $.", [("web_extract", extract_result(REVIEW))])
    assert "Sources (pages ouvertes) :\n- https://guide.example/plans" in out
    assert out.endswith("Aussi utilisées, ouvertes plus tôt dans la conversation :\n- https://portal.example/cloud")
    out = run_turn(ctx, "La Super coûte 100 $.", [("web_extract", extract_result(REVIEW))])
    assert "portal.example" not in out


def test_follow_up_without_web_tools_lists_the_earlier_page_it_uses():
    # Found in testing: "and how much disk does it get?" answered from the page opened before.
    ctx = setup()
    run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(PAGE))])
    out = run_turn(ctx, "La formule Plus coûte 20 $ et donne 25 Go de disque.", [])
    assert out.endswith("Aussi utilisées, ouvertes plus tôt dans la conversation :\n- https://portal.example/cloud")
    assert "Sources (pages ouvertes)" not in out
    assert run_turn(ctx, "Avec plaisir.", []) is None  # no value from the page: nothing added


def test_resumed_conversation_gets_its_earlier_pages_from_the_history():
    ctx = setup()  # a new process: the plugin has no memory of the conversation
    history = [
        {"role": "user", "content": "specs?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "web_extract", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "<untrusted_tool_result>\n" + extract_result(PAGE) + "\n</untrusted_tool_result>"},
        {"role": "tool", "tool_call_id": "c2", "content": extract_result(REVIEW)},  # not web_extract: ignored
        {"role": "assistant", "content": "It costs $20."},
    ]
    h = ctx.hooks
    h["pre_llm_call"](session_id="s9", user_message="and the disk?", conversation_history=history)
    out = h["transform_llm_output"](response_text="La formule Plus coûte 20 $ et la latence est de 250 ms.", session_id="s9", model="m", platform="cli")
    assert out.endswith("Aussi utilisées, ouvertes plus tôt dans la conversation :\n- https://portal.example/cloud")
    assert "guide.example" not in out


SEARCH = json.dumps({"success": True, "data": {"web": [
    {"title": "RTX 5080 specs", "url": "https://specs.example/5080", "description": "Boost clock 2617 MHz, 16 GB GDDR7."}]}})


def test_values_taken_from_search_snippets_are_named():
    ctx = setup()
    out = run_turn(ctx, "The boost clock is 2617 MHz and the TGP 360 W.", [("web_search", SEARCH)])
    assert out.endswith("No page opened: this answer relies on search snippets only.\n"
                        "From search snippets, not from an opened page: 2617 MHz")
    # A value read on an opened page is not blamed on the snippets, even if a snippet also holds it.
    page = {"url": "https://nvidia.example/5080", "content": "Boost Clock: 2617 MHz. Total Graphics Power: 360 W."}
    out = run_turn(ctx, "Boost 2617 MHz, 360 W.", [("web_search", SEARCH), ("web_extract", extract_result(page))], session="s2")
    assert "From search snippets" not in out
    # A search that found nothing the answer uses adds no line.
    out = run_turn(ctx, "The card costs $999.", [("web_search", SEARCH)], session="s3")
    assert "From search snippets" not in out


def test_reminder_asks_the_model_not_to_write_the_list():
    ctx = setup()
    assert "do not write a sources list" in ctx.hooks["pre_llm_call"](session_id="s1", user_message="q")


def test_reminder_is_marked_as_a_note_not_the_request():
    ctx = setup()
    out = ctx.hooks["pre_llm_call"](session_id="s1", user_message="q")
    assert out.startswith("[webgraft note to the assistant, not part of the user's message]")


def test_a_message_with_a_url_asks_for_the_page_not_a_search():
    ctx = setup()
    h = ctx.hooks["pre_llm_call"]
    out = h(session_id="s1", user_message="https://kissbot.bzh/llms.txt")
    assert out.startswith("[webgraft note to the assistant, not part of the user's message] Open the URL in the user's message")
    assert "If you search the web for this message" not in out
    assert "If it cannot be read, say so" in out and "snippets are not sources" in out
    assert "do not write a sources list" in out
    out = h(session_id="s1", user_message="what does HTTP://Example.org/a say about pricing?")
    assert "Open the URL in the user's message" in out
    assert "snippets are not sources" in h(session_id="s1", user_message="price of example.org plus plan")


def test_sessions_do_not_share_pages():
    ctx = setup()
    run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(PAGE))], session="a")
    out = run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(REVIEW))], session="b")
    assert "portal.example" not in out


def test_reminder_is_added_to_each_turn_and_can_be_turned_off():
    ctx = setup()
    assert "snippets are not sources" in ctx.hooks["pre_llm_call"](session_id="s1", user_message="q")
    ctx = setup({"remind": False})
    assert ctx.hooks["pre_llm_call"](session_id="s1", user_message="q") is None


def test_footer_can_follow_the_answer_language():
    ctx = setup({"language": "answer"})
    out = run_turn(ctx, "The Plus plan costs $20.", [("web_extract", extract_result(PAGE))])
    assert "Sources (opened pages):" in out


def test_steps_aside_for_another_reply_rewriter(monkeypatch):
    monkeypatch.setattr(gw.compat, "other_reply_rewriters", lambda own, has_plugin: ["other-footer"])
    ctx = setup()
    assert run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(PAGE))]) is None
    ctx = setup({"show_sources": "always"})
    assert "portal.example" in run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(PAGE))])


def test_sources_mode_values():
    m = gw.compat.sources_mode
    assert [m(True), m(False), m("on"), m("off"), m("always"), m("never"), m(None), m("weird")] == [
        "auto", "never", "always", "never", "always", "never", "auto", "auto"]


def test_never_turns_the_footer_off():
    ctx = setup({"show_sources": "never"})
    assert run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(PAGE))]) is None


def test_model_is_told_the_footer_under_its_earlier_answers_is_not_its_own():
    ctx = setup()
    history = [{"role": "user", "content": "specs?"},
               {"role": "assistant", "content": "It costs $20.\n\nSources (opened pages):\n- https://portal.example/cloud"}]
    context = ctx.hooks["pre_llm_call"](session_id="s1", user_message="and the disk?", conversation_history=history)
    assert 'were added by webgraft, not written by you' in context
    assert '"Sources (opened pages):"' in context
    plain = ctx.hooks["pre_llm_call"](session_id="s2", user_message="hi", conversation_history=[])
    assert "not written by you" not in plain
    ctx = setup({"remind": False})
    assert "not written by you" in ctx.hooks["pre_llm_call"](session_id="s3", user_message="q", conversation_history=history)


def test_an_empty_search_gets_a_note_and_a_search_with_results_does_not():
    ctx = setup()
    hook = ctx.hooks["transform_tool_result"]
    out = hook(tool_name="web_search", result=json.dumps({"success": True, "data": {"web": []}}), session_id="s1")
    assert "does not show that something does not exist" in json.loads(out)["webgraft_note"]
    assert hook(tool_name="web_search", result=SEARCH, session_id="s1") is None
    assert hook(tool_name="web_search", result=json.dumps({"success": False, "error": "SearXNG unreachable"}), session_id="s1") is None
    assert hook(tool_name="web_extract", result=extract_result(PAGE), session_id="s1") is None
