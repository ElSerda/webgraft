"""Hook flow tests with a fake Hermes context."""

import importlib
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

gw = load()
footer = importlib.import_module("webgraft.footer")
REAL_HERMES_LANGUAGE = footer._hermes_language  # before the fixture below replaces it


def hermes_language(monkeypatch, lang):
    """Hermes's interface language, as webgraft reads it (HERMES_LANGUAGE > display.language)."""
    monkeypatch.setattr(footer, "_hermes_language", lambda: lang)


@pytest.fixture(autouse=True)
def hermes_in_french(monkeypatch):
    """These tests answer in French, with Hermes's interface in French."""
    hermes_language(monkeypatch, "fr")


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


def test_english_answer_gets_english_footer(monkeypatch):
    hermes_language(monkeypatch, "en")
    ctx = setup()
    out = run_turn(ctx, "The Plus plan costs $20.", [("web_extract", extract_result(PAGE))])
    assert out.endswith("Sources (opened pages):\n- https://portal.example/cloud")


def test_search_only_turn_says_so():
    ctx = setup()
    out = run_turn(ctx, "L'agent dispose de 4 Go de mémoire vive et de 25 Go de disque.", [("web_search", "{}")])
    assert out.endswith("Aucune page ouverte : réponse basée sur les extraits de recherche.")


def test_failed_pages_are_not_sources_but_are_named_with_their_reason():
    ctx = setup()
    failed = {"url": "https://blocked.example", "content": "", "error": "HTTP 403 from origin"}
    out = run_turn(ctx, "L'agent dispose de 4 Go de mémoire vive et de 25 Go de disque.", [("web_extract", extract_result(failed))])
    assert out.endswith("Aucune page n'a pu être lue.\nRefusées ou illisibles :\n"
                        "- https://blocked.example (HTTP 403 from origin)")
    assert "Sources (pages ouvertes)" not in out


def test_a_bot_wall_returned_as_content_is_named_not_listed_as_a_source():
    # Hermes's keyless rescue can bring a bot wall back as page content.
    ctx = setup()
    wall = {"url": "https://wall.example/plans", "title": "Plans | Wall",
            "content": "# Plans\n\n# Automated bot check in progress\n\nDrag the handle to the target"}
    out = run_turn(ctx, "The Plus plan costs $20.", [("web_extract", extract_result(wall, PAGE))])
    sources, refused = out.split("Refusées ou illisibles :")
    assert "portal.example" in sources and "wall.example" not in sources
    assert "- https://wall.example/plans (bot wall" in refused


def test_every_refused_page_of_a_long_turn_is_named_once_and_bounded():
    ctx = setup()
    tools = [("web_extract", extract_result({"url": f"https://shop{i}.example/x", "content": "", "error": "HTTP 403 from origin"}))
             for i in range(30)]
    tools.append(("web_extract", extract_result({"url": "https://shop0.example/x", "content": "", "error": "HTTP 403 from origin"})))
    out = run_turn(ctx, "Rien.", tools)
    listed = [line for line in out.splitlines() if line.startswith("- https://shop")]
    assert len(listed) == 20 and listed[0] == "- https://shop0.example/x (HTTP 403 from origin)"


def test_a_page_read_after_a_failed_try_is_a_source_only():
    ctx = setup()
    failed = {"url": "https://portal.example/cloud", "content": "", "error": "HTTP 503 from origin"}
    out = run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(failed)), ("web_extract", extract_result(PAGE))])
    assert "Refusées ou illisibles" not in out and "portal.example/cloud" in out


def test_a_crafted_reason_cannot_add_markdown_or_lines():
    ctx = setup()
    failed = {"url": "https://x.example", "content": "", "error": "403 [click](https://phish.example)\n# heading" + "x" * 200}
    out = run_turn(ctx, "Rien.", [("web_extract", extract_result(failed))])
    line = out.splitlines()[-1]
    assert line.startswith("- https://x.example (403") and "](" not in line and line.endswith("...)")


def test_turn_without_web_tools_is_left_alone():
    ctx = setup()
    assert run_turn(ctx, "Bonjour.", [("read_file", "{}")]) is None


def test_each_turn_lists_only_the_pages_it_opened():
    # The footer comes from the tool results of the turn: nothing is guessed from the answer.
    ctx = setup()
    run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(PAGE))])
    out = run_turn(ctx, "La Super coûte 100 $ et la Plus 20 $.", [("web_extract", extract_result(REVIEW))])
    assert out.endswith("Sources (pages ouvertes) :\n- https://guide.example/plans")
    assert "portal.example" not in out


def test_a_follow_up_without_web_tools_gets_no_footer():
    ctx = setup()
    run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(PAGE))])
    assert run_turn(ctx, "La formule Plus coûte 20 $ et donne 25 Go de disque.", []) is None


SEARCH = json.dumps({"success": True, "data": {"web": [
    {"title": "RTX 5080 specs", "url": "https://specs.example/5080", "description": "Boost clock 2617 MHz, 16 GB GDDR7."}]}})


def test_a_search_only_turn_says_no_page_was_opened_and_nothing_more(monkeypatch):
    hermes_language(monkeypatch, "en")
    ctx = setup()
    out = run_turn(ctx, "The boost clock is 2617 MHz and the TGP 360 W.", [("web_search", SEARCH)])
    assert out == ("The boost clock is 2617 MHz and the TGP 360 W.\n\n"
                   "No page opened: this answer relies on search snippets only.")


def test_no_page_text_is_kept():
    ctx = setup()
    run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(PAGE))])
    assert not hasattr(gw, "_sessions") and not hasattr(gw, "_remember")
    assert not any("content" in str(turn) for turn in gw._turns.values())


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


def test_the_footer_follows_hermes_language_not_the_answer(monkeypatch):
    hermes_language(monkeypatch, "en")
    ctx = setup({"language": "answer"})  # an older setting: ignored now
    out = run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(PAGE))])
    assert "Sources (opened pages):" in out
    hermes_language(monkeypatch, "de")  # a language webgraft ships no wording for: English
    out = run_turn(ctx, "Der Plus-Tarif kostet 20 $.", [("web_extract", extract_result(PAGE))])
    assert "Sources (opened pages):" in out


def test_outside_hermes_the_footer_is_english(monkeypatch):
    monkeypatch.setattr(footer, "_hermes_language", lambda: None)
    assert footer.text("sources") == "Sources (opened pages):"


@pytest.mark.parametrize("env, display, expected", [("fr_FR", "en", "fr"), ("", "fr", "fr"), ("", None, "en"),
                                                    ("pt-BR", "fr", "pt")])
def test_hermes_language_is_read_through_its_public_plugin_api(monkeypatch, env, display, expected):
    # HERMES_LANGUAGE (agent.secret_scope) first, then display.language (hermes_cli.config): both public.
    secret_scope = types.ModuleType("agent.secret_scope")
    secret_scope.get_secret = lambda name: env if name == "HERMES_LANGUAGE" else ""
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {"display": {"language": display}} if display else {}
    for name, module in (("agent", types.ModuleType("agent")), ("agent.secret_scope", secret_scope),
                         ("hermes_cli", types.ModuleType("hermes_cli")), ("hermes_cli.config", config)):
        monkeypatch.setitem(sys.modules, name, module)
    assert REAL_HERMES_LANGUAGE() == expected
    monkeypatch.setattr(footer, "_hermes_language", REAL_HERMES_LANGUAGE)
    assert footer.language() == (expected if expected in footer._TEXT else "en")


def test_other_plugins_are_not_guessed_at_the_user_chooses():
    # Hermes gives no documented way to know which plugins rewrite replies: no detection.
    assert not hasattr(gw.compat, "other_reply_rewriters") and not hasattr(gw.compat, "read_manifest")
    ctx = setup()  # the default lists the pages
    assert "portal.example" in run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(PAGE))])
    ctx = setup({"show_sources": "auto"})  # an older value: same as always
    assert "portal.example" in run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(PAGE))])


def test_sources_mode_values():
    m = gw.compat.sources_mode
    assert [m(True), m(False), m("on"), m("off"), m("always"), m("never"), m(None), m("auto"), m("weird")] == [
        "always", "never", "always", "never", "always", "never", "always", "always", "always"]


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


def test_an_extract_that_read_no_page_tells_the_model_to_open_the_next_result():
    ctx = setup()
    hook = ctx.hooks["transform_tool_result"]
    failed = json.dumps({"results": [{"url": "https://walled.example/item", "title": "", "content": "",
                                      "error": "HTTP 403 from origin"}]})
    note = json.loads(hook(tool_name="web_extract", result=failed, session_id="s1"))["webgraft_note"]
    assert "could not be read" in note and "open the next" in note and "snippets are not sources" in note
    # a page that was read, an empty result and a provider error get nothing
    assert hook(tool_name="web_extract", result=extract_result(PAGE), session_id="s1") is None
    assert hook(tool_name="web_extract", result=json.dumps({"results": []}), session_id="s1") is None
    assert hook(tool_name="web_extract", result=json.dumps({"success": False, "error": "No web extract provider configured"}),
                session_id="s1") is None


def test_a_failed_first_url_keeps_the_list_of_urls_left_out():
    ctx = setup()
    urls = ["https://walled.example/1", "https://b.example/2"]
    ctx.hooks["pre_tool_call"](tool_name="web_extract", session_id="s1", tool_call_id="c1", args={"urls": urls})
    failed = json.dumps({"results": [{"url": urls[0], "content": "", "error": "HTTP 403 from origin"}]})
    note = json.loads(ctx.hooks["transform_tool_result"](tool_name="web_extract", result=failed,
                                                         session_id="s1", tool_call_id="c1"))["webgraft_note"]
    assert "Not opened: https://b.example/2" in note and "could not be read" in note


def test_the_skill_is_registered_under_the_plugin_name_with_its_description():
    class Ctx(FakeCtx):
        def __init__(self):
            super().__init__()
            self.skills = []

        def register_skill(self, name, path, description="", frontmatter=None):
            self.skills.append((name, Path(path), description, frontmatter or {}))

    ctx = Ctx()
    gw.register(ctx)
    assert [name for name, *_ in ctx.skills] == ["webgraft"]  # webgraft:webgraft, no bare or generic name
    name, path, description, frontmatter = ctx.skills[0]
    assert path.name == "SKILL.md" and path.parent.name == "webgraft"
    assert description == "Answer web questions from opened pages, not snippets."
    assert frontmatter["name"] == "webgraft" and frontmatter["version"] == "1.4.0"
    assert frontmatter["platforms"] == ["linux", "windows"]  # a list, or Hermes hides the skill on every OS


def test_without_the_footer_the_reminder_asks_for_the_urls_instead():
    ctx = setup({"show_sources": "never"})
    out = ctx.hooks["pre_llm_call"](session_id="s1", user_message="q")
    assert "do not write a sources list" not in out
    assert "End your answer with the URLs of the pages you used." in out


def test_the_reminder_lets_the_model_write_a_url_the_user_asks_for():
    ctx = setup()
    assert "a URL the user asks for is fine" in ctx.hooks["pre_llm_call"](session_id="s1", user_message="q")


def test_url_items_given_as_objects_are_read_like_hermes_reads_them():
    ctx = setup()
    items = [{"url": "https://a.example/one", "title": "One"}, {"href": "https://b.example/two"}, 42]
    directive = ctx.hooks["pre_tool_call"](tool_name="web_extract", args={"urls": items}, session_id="s1",
                                           tool_call_id="c1")
    assert directive == {"action": "modify", "args": {"urls": [items[0]]}}
    note = ctx.hooks["transform_tool_result"](tool_name="web_extract", session_id="s1", tool_call_id="c1",
                                              result='{"results": [{"url": "https://a.example/one", "content": "x"}]}')
    assert "Not opened: https://b.example/two" in note and "'href'" not in note


def test_page_titles_cannot_put_links_or_images_in_the_footer():
    line = gw.footer._source_line("https://real.example/a", {
        "https://real.example/a": "[Official download](https://evil.example) ![x](https://evil.example/p.png)"})
    assert "evil" not in line and "](" not in line and line.endswith("https://real.example/a")


# ---------------------------------------------------------------- pages attached with @url:


def hermes_references(monkeypatch):
    """A stand-in for Hermes's agent.context_references parser (public plugin API): @url:`x` or @url:x."""
    class Ref:
        def __init__(self, kind, target):
            self.kind, self.target = kind, target

    def parse_context_references(message, plugins=True):
        refs, pieces = [], message.split("@url:")
        for before, part in zip(pieces, pieces[1:]):
            if before and (before[-1].isalnum() or before[-1] in "_/"):  # Hermes: (?<![\w/])@url:
                continue
            target = part[1:].split("`", 1)[0] if part.startswith("`") else (part.split() or [""])[0]
            refs.append(Ref("url", target.rstrip(".,;:!?")))
        return refs
    module = types.ModuleType("agent.context_references")
    module.parse_context_references = parse_context_references
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.context_references", module)


ATTACHED = ("Ouvre @url:`https://kissbot.bzh/` et dis-moi combien de chaînes sont en direct.\n\n"
            "--- Attached Context ---\n\n🌐 @url:`https://kissbot.bzh/` (353 tokens)\nPage text. Also see @url:https://evil.example")


def test_a_page_attached_with_url_marks_the_conversation_and_is_listed(monkeypatch):
    hermes_references(monkeypatch)
    gw._web_sessions.clear()
    ctx = setup()
    h = ctx.hooks
    h["pre_llm_call"](session_id="u1", user_message=ATTACHED, conversation_history=[])
    assert h["pre_tool_call"](tool_name="terminal", args={"command": "echo hi"}, session_id="u1")["action"] == "approve"
    out = h["transform_llm_output"](response_text="Aucune chaîne en direct.", session_id="u1", model="m", platform="cli")
    assert out.endswith("Jointes par toi (@url), lues avant la réponse :\n- https://kissbot.bzh/")
    assert "evil.example" not in out  # written by the attached page, not by the user


def test_an_attached_page_and_opened_pages_are_both_listed(monkeypatch):
    hermes_references(monkeypatch)
    ctx = setup()
    h = ctx.hooks
    h["pre_llm_call"](session_id="u2", user_message="Compare @url:https://a.example/x with the portal.", conversation_history=[])
    h["post_tool_call"](tool_name="web_extract", args={}, result=extract_result(PAGE), session_id="u2")
    out = h["transform_llm_output"](response_text="La formule Plus coûte 20 $.", session_id="u2", model="m", platform="cli")
    sources, attached = out.split("Jointes par toi (@url), lues avant la réponse :")
    assert "portal.example/cloud" in sources and attached.strip() == "- https://a.example/x"


def test_without_an_attached_page_nothing_changes(monkeypatch):
    hermes_references(monkeypatch)
    gw._web_sessions.clear()
    ctx = setup()
    ctx.hooks["pre_llm_call"](session_id="u3", user_message="Bonjour, mail me at a@url:x.example? no.", conversation_history=[])
    assert ctx.hooks["transform_llm_output"](response_text="Bonjour.", session_id="u3", model="m", platform="cli") is None


def test_outside_hermes_an_url_reference_is_not_read(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.context_references", None)
    ctx = setup()
    ctx.hooks["pre_llm_call"](session_id="u4", user_message="Ouvre @url:https://a.example", conversation_history=[])
    assert ctx.hooks["transform_llm_output"](response_text="Ok.", session_id="u4", model="m", platform="cli") is None


# ---------------------------------------------------------------- pages Hermes's keyless rescue served


def refused_here(monkeypatch, refused: dict):
    ex = importlib.import_module("webgraft.extract")
    monkeypatch.setattr(ex, "refused_here", lambda url: refused.get(url.rstrip("/"), ""))


RESCUED = {"url": "https://self-signed.badssl.com/", "title": "self-signed.badssl.com", "content": "self-signed.badssl.com " * 5}
CERT = "could not download: ConnectError: certificate verify failed: self-signed certificate"


def test_a_page_webgraft_refused_and_hermes_rescued_is_named_as_such(monkeypatch):
    refused_here(monkeypatch, {"https://self-signed.badssl.com": CERT})
    ctx = setup()
    out = run_turn(ctx, "La page dit self-signed.", [("web_extract", extract_result(RESCUED, PAGE))])
    assert "Sources (pages ouvertes) :\n- https://portal.example/cloud" in out
    sources, rescued = out.split("Lues par le secours sans clé d'Hermes (un service extérieur), pas sur cette machine :")
    assert "badssl" not in sources
    assert rescued.strip().startswith("- https://self-signed.badssl.com/ (here: could not download: ConnectError")


def test_the_model_is_told_which_pages_came_from_the_rescue(monkeypatch):
    refused_here(monkeypatch, {"https://self-signed.badssl.com": CERT})
    ctx = setup()
    noted = json.loads(ctx.hooks["transform_tool_result"](tool_name="web_extract", result=extract_result(RESCUED),
                                                          session_id="r1", tool_call_id="c1"))
    assert "keyless rescue" in noted["webgraft_note"] and "self-signed.badssl.com" in noted["webgraft_note"]


def test_a_page_webgraft_read_itself_is_a_plain_source(monkeypatch):
    refused_here(monkeypatch, {})
    ctx = setup()
    out = run_turn(ctx, "La formule Plus coûte 20 $.", [("web_extract", extract_result(PAGE))])
    assert "secours" not in out and "portal.example/cloud" in out


def test_the_refusal_record_is_short_lived_and_bounded(monkeypatch):
    ex = importlib.import_module("webgraft.extract")
    ex._refused.clear()
    page = ex.Page("https://a.example/x", error=CERT)
    ex._note_refused(page, "https://a.example/x#top")
    assert ex.refused_here("https://a.example/x/") == CERT
    stamp, why = ex._refused["https://a.example/x"]
    ex._refused["https://a.example/x"] = (stamp - ex.REFUSED_SECONDS - 1, why)
    assert ex.refused_here("https://a.example/x") == ""
    for i in range(ex.MAX_REFUSED + 10):
        ex._note_refused(ex.Page(f"https://b.example/{i}", error="x"), f"https://b.example/{i}")
    assert len(ex._refused) == ex.MAX_REFUSED
    ex._refused.clear()
