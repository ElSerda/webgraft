"""search_demote: off-query web_search hits move to the tail, the consensus order is untouched."""

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
    ctx = FakeCtx(settings)
    gw.register(ctx)
    return ctx


def data_of(*hits):
    return {"data": {"web": [dict(h, position=i) for i, h in enumerate(hits, 1)]}}


def search_payload(*hits):
    return json.dumps({"success": True, **data_of(*hits)}, ensure_ascii=False)


def annotate(ctx, payload, query="Nous Research Hermes models latest"):
    return ctx.hooks["transform_tool_result"](
        tool_name="web_search", result=payload, args={"query": query}, session_id="s1")


def titles(result):
    return [h["title"] for h in json.loads(result)["data"]["web"]]


ON_TOPIC = {"title": "Hermes AI Agent by Nous Research", "url": "https://hermes-agent.nousresearch.com/"}
RELEASES = {"title": "Releases - NOUS RESEARCH", "url": "https://nousresearch.com/releases"}
NOUS_WIKI = {"title": "Noûs — Wikipédia", "url": "https://fr.wikipedia.org/wiki/No%C3%BBs"}
NOUSLIB = {"title": "Nouslib site de rencontres sans filtre", "url": "https://www.nouslib.com/"}
SCIENCEGATE = {"title": "prediction models Latest Research Papers", "url": "http://www.sciencegate.app/keyword/84811"}
QUERY = "Nous Research Hermes models latest"


# --- unit: _demote_zero_match ---

def test_zero_match_hits_move_to_the_tail():
    d = data_of(NOUS_WIKI, ON_TOPIC, NOUSLIB, RELEASES)
    assert gw._demote_zero_match(d, QUERY) is True
    assert titles(json.dumps(d)) == [ON_TOPIC["title"], RELEASES["title"], NOUS_WIKI["title"], NOUSLIB["title"]]


def test_consensus_order_is_preserved_within_groups():
    d = data_of(RELEASES, NOUS_WIKI, ON_TOPIC, SCIENCEGATE, NOUSLIB)
    gw._demote_zero_match(d, QUERY)
    assert titles(json.dumps(d)) == [
        RELEASES["title"], ON_TOPIC["title"], SCIENCEGATE["title"], NOUS_WIKI["title"], NOUSLIB["title"]]


def test_positions_are_renumbered():
    d = data_of(NOUS_WIKI, ON_TOPIC, RELEASES)
    gw._demote_zero_match(d, QUERY)
    assert [h["position"] for h in d["data"]["web"]] == [1, 2, 3]


def test_a_title_that_carries_query_words_keeps_its_native_rank():
    # ScienceGate matches models/latest/research: lexical, not semantic — it is kept, not promoted.
    d = data_of(SCIENCEGATE, ON_TOPIC)
    assert gw._demote_zero_match(d, QUERY) is False


def test_description_boilerplate_does_not_save_a_hit():
    hit = dict(NOUSLIB, description="CGU · Cookies et vie privée · Contactez-nous")
    d = data_of(hit, ON_TOPIC)
    assert gw._demote_zero_match(d, QUERY) is True
    assert d["data"]["web"][0]["title"] == ON_TOPIC["title"]


def test_stopword_only_query_is_a_noop():
    d = data_of(NOUS_WIKI, ON_TOPIC)
    assert gw._demote_zero_match(d, "le la de nous") is False


def test_diacritics_are_not_folded():
    accent = {"title": "Café de la Gare", "url": "https://gare.example/"}
    plain = {"title": "Cafe Restaurant", "url": "https://restaurant.example/"}
    d = data_of(accent, plain)
    assert gw._demote_zero_match(d, "cafe restaurant") is True
    assert d["data"]["web"][0]["title"] == "Cafe Restaurant"
    assert d["data"]["web"][-1]["title"] == "Café de la Gare"


def test_all_match_is_a_noop():
    assert gw._demote_zero_match(data_of(ON_TOPIC, RELEASES), "Nous Research Hermes") is False


def test_none_match_is_a_noop():
    assert gw._demote_zero_match(data_of(NOUS_WIKI, NOUSLIB), "Nous Research Hermes") is False


def test_empty_query_is_a_noop():
    assert gw._demote_zero_match(data_of(NOUS_WIKI, ON_TOPIC), "") is False


@pytest.mark.parametrize("bad", [
    {}, {"data": {}}, {"data": None}, {"data": {"web": []}}, {"data": {"web": [ON_TOPIC]}},
    {"data": {"web": [None, 42]}}, {"data": {"web": "x"}},
])
def test_malformed_payloads_never_crash(bad):
    assert gw._demote_zero_match(bad, "Nous Research Hermes") is False


# --- integration: the transform_tool_result hook ---

def test_hook_demotes_when_setting_on():
    ctx = setup({"search_demote": True})
    out = annotate(ctx, search_payload(NOUS_WIKI, ON_TOPIC, NOUSLIB, RELEASES))
    assert out is not None
    assert titles(out) == [ON_TOPIC["title"], RELEASES["title"], NOUS_WIKI["title"], NOUSLIB["title"]]


def test_hook_is_a_noop_when_setting_off():
    ctx = setup({"search_demote": False})
    assert annotate(ctx, search_payload(NOUS_WIKI, ON_TOPIC, NOUSLIB)) is None


def test_hook_default_is_off():
    ctx = setup()
    assert annotate(ctx, search_payload(NOUS_WIKI, ON_TOPIC, NOUSLIB)) is None


def test_hook_without_args_is_safe():
    ctx = setup({"search_demote": True})
    out = ctx.hooks["transform_tool_result"](
        tool_name="web_search", result=search_payload(NOUS_WIKI, ON_TOPIC, NOUSLIB), session_id="s1")
    assert out is None


def test_empty_search_note_is_preserved():
    ctx = setup({"search_demote": True})
    out = annotate(ctx, json.dumps({"success": True, "data": {"web": []}}))
    assert out is not None and "webgraft_note" in json.loads(out)


def test_failed_search_is_untouched():
    ctx = setup({"search_demote": True})
    assert annotate(ctx, json.dumps({"success": False})) is None


@pytest.mark.parametrize("bad", [None, "", "not json", "{}",
                                 json.dumps({"success": True, "data": {"web": [None, 42]}})])
def test_hook_never_crashes_on_malformed_result(bad):
    ctx = setup({"search_demote": True})
    assert annotate(ctx, bad) is None
