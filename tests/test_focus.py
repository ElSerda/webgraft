"""One URL per web_extract call, and long pages with the passages about the question first."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

gw = load()
import webgraft.extract as ex  # noqa: E402
import webgraft.focus as fo  # noqa: E402

FILLER = "Every Hermes plan includes the hosted tools, the model catalog and the Portal dashboard."


def long_page(table_at: int = 120) -> str:
    """A guide-like document: a table of contents, the free plan first, the asked plan deep inside,
    its table split by a running page header."""
    blocks = ["Nous Portal plans guide", "Contents\nIntroduction......3\nFree......14\nUltra......49"]
    blocks += [f"Section {i}. {FILLER} The Free plan comes first." for i in range(table_at)]
    blocks += [
        "The Ultra plan is the largest. Its cloud agent is compared below.",
        "|Plan|Super|Ultra|\n|---|---|---|\n|Price|$100/month|$200/month|\n|RAM|4 GB|8 GB|",
        "APPENDIX B: Ultra",
        "|Disk|100 GB|100 GB|\n|Monthly credits|$110|$220|",
    ]
    blocks += [f"Closing {i}. {FILLER}" for i in range(40)]
    return "\n\n".join(blocks)


def model_view(text: str, window: int = 15_000) -> str:
    """What Hermes shows the model of a long page: about the first 75% of the window (plus a tail)."""
    return text[: int(window * 0.75)]


# ---------------------------------------------------------------- focus.py


def test_terms_put_numbers_first_and_drop_stop_words_and_operators():
    terms = fo.terms_of("trouve moi les formules de Nous Portal pour Hermes 0.22.7",
                        "Nous Portal plans site:nousresearch.com filetype:pdf")
    assert terms[0] == "0.22.7"
    assert "portal" in terms and "formules" in terms
    assert not {"trouve", "moi", "les", "site", "nousresearch.com", "pdf", "filetype"} & set(terms)


def test_short_page_is_unchanged():
    text = "Short page about the Ultra plan.\n\nIt has 8 GB of RAM."
    assert fo.arrange(text, ["ultra"], 15_000) == text


def test_long_page_puts_the_asked_product_where_the_model_sees_it():
    text = long_page()
    assert "$200/month" not in model_view(text)  # the asked plan is in the hidden middle
    out = fo.arrange(text, fo.terms_of("what does the Ultra plan include?"), 15_000)
    seen = model_view(out)
    assert "|Price|$100/month|$200/month|" in seen
    assert "|Monthly credits|$110|$220|" in seen  # the table's second half, past the page header
    assert "[webgraft: this page is long." in seen


def test_nothing_is_removed():
    text = long_page()
    out = fo.arrange(text, ["ultra"], 15_000)
    blocks = [b for b in text.split("\n\n")]
    assert all(b in out for b in blocks)


def test_table_of_contents_and_common_words_do_not_move():
    text = long_page()
    out = fo.arrange(text, ["ultra", "hermes"], 15_000)
    note = out.index("[webgraft: this page is long.")
    first_part = out[note: out.index("[webgraft: rest of the page")]
    assert "Introduction......3" not in first_part
    assert "Section 5." not in first_part  # "hermes" is on every filler line: it weighs nothing


def test_page_without_the_terms_is_unchanged():
    text = long_page()
    assert fo.arrange(text, ["zz9000"], 15_000) == text


def test_expected_url_matches_despite_slash_and_case():
    fo.expect("https://WWW.Example.com/plans/", ["ultra"], 9000)
    assert fo.take("https://www.example.com/plans") == (["ultra"], 9000)


# ---------------------------------------------------------------- hooks and provider


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
    gw._questions.clear()
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
    ctx = setup({"one_url_per_call": False, "focus": False})
    assert ctx.hooks["pre_tool_call"](tool_name="web_extract", args={"urls": ["https://a.example/1", "https://b.example/2"]},
                                      session_id="s1") is None


def test_question_and_search_queries_reach_the_reader():
    ctx = setup()
    h = ctx.hooks
    h["pre_llm_call"](session_id="s1", user_message="Que comprend la formule Ultra de Nous Portal ?")
    h["post_tool_call"](tool_name="web_search", args={"query": "Nous Portal Ultra plan cloud agent"}, result="{}", session_id="s1")
    h["pre_tool_call"](tool_name="web_extract", args={"urls": ["https://portal.example/plans-guide.pdf"], "char_limit": 12000},
                       session_id="s1")
    terms, window = fo.take("https://portal.example/plans-guide.pdf")
    assert window == 12000
    assert {"ultra", "portal", "agent"} <= set(terms)


def test_the_page_handed_back_has_the_relevant_part_first():
    # What provider.extract() does with each page (the provider needs Hermes's base class).
    url = "https://portal.example/plans-guide.pdf"
    fo.expect(url, ["ultra"], 15_000)
    page = ex.Page(url, "Plans guide", long_page(), engine="anydoc")
    result = page.as_result(*(fo.take(url) or (None, 0)))
    assert "|Price|$100/month|$200/month|" in model_view(result["content"])
    assert len(result["content"]) <= ex.MAX_CONTENT_CHARS
    assert result["raw_content"] == result["content"]
    json.dumps(result)


def test_without_a_question_the_page_is_only_cut():
    page = ex.Page("https://maker.example/x", "X", "a" * 50_000)
    assert page.as_result()["content"] == "a" * ex.MAX_CONTENT_CHARS


def test_a_page_without_blank_lines_is_cut_into_passages():
    # trafilatura's text of an HTML page has no blank lines: one block of the whole page.
    lines = [f"setting_{i}: value {i} # explained here" for i in range(3000)]
    lines[2500] = "keyless_rescue: true  # one-shot retry on the keyless ring"
    text = "\n".join(lines)
    assert len(fo.passages(text)) > 50
    out = fo.arrange(text, fo.terms_of("what does web.keyless_rescue do?"), 15_000)
    assert "keyless_rescue: true" in model_view(out)


def test_dotted_names_also_give_their_last_part():
    assert fo.terms_of("web.keyless_rescue")[:2] == ["web.keyless_rescue", "keyless_rescue"]
    assert "0.22.7" in fo.terms_of("Hermes 0.22.7") and "22" not in fo.terms_of("Hermes 0.22.7")
