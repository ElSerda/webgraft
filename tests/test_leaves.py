"""`hermes webgraft status`: what can leave this machine, read from Hermes's config only."""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

load()
leaves = importlib.import_module("webgraft.leaves")
cli = importlib.import_module("webgraft.cli")

LOCAL = {
    "model": {"provider": "lmstudio", "base_url": "http://127.0.0.1:1234/v1", "default": "google/gemma-4-e4b"},
    "web": {"search_backend": "searxng", "extract_backend": "webgraft"},
    "tts": {"provider": "piper"},
    "stt": {"provider": "local"},
}


def rows(config, url="http://serveur.local/searxng", mode="ask"):
    return {r.what: r for r in leaves.report(config, url, mode)}


def test_where_tells_this_machine_your_network_and_outside():
    assert leaves.where("http://127.0.0.1:1234/v1")[0] == leaves.HERE
    assert leaves.where("http://localhost:11434")[0] == leaves.HERE
    assert leaves.where("http://192.168.1.20:1234/v1")[0] == leaves.NETWORK
    assert leaves.where("http://serveur.local/searxng")[0] == leaves.NETWORK
    assert leaves.where("http://nas/searxng")[0] == leaves.NETWORK
    assert leaves.where("https://openrouter.ai/api/v1")[0] == leaves.OUTSIDE
    assert leaves.where("https://8.8.8.8/")[0] == leaves.OUTSIDE
    assert leaves.where("not a url")[0] == leaves.OUTSIDE


def test_a_local_setup_with_webgraft_shows_nothing_going_out_but_the_search():
    r = rows(LOCAL)
    assert r["Model"].kind == leaves.HERE and "gemma-4-e4b" in r["Model"].where
    assert r["Search"].kind == leaves.NETWORK
    assert r["Page reading"].kind == leaves.HERE
    assert r["Keyless rescue"].kind == leaves.HELD and "first search" in r["Keyless rescue"].where
    assert r["Spoken replies"].kind == leaves.HERE
    assert r["Voice input"].kind == leaves.HERE
    assert not any(row.kind == leaves.OUTSIDE for row in r.values())


def test_hermes_defaults_are_named_where_they_send_data():
    r = rows({"model": {"provider": "lmstudio"}, "web": {"search_backend": "ddgs"}}, url="", mode="off")
    assert r["Model"].kind == leaves.HERE  # LM Studio's default address is this machine
    assert r["Search"].kind == leaves.OUTSIDE and "DuckDuckGo" in r["Search"].where
    assert r["Page reading"].kind == leaves.AUTO
    assert rows({"web": {"search_backend": "searxng"}}, url="")["Search"].kind == leaves.AUTO  # unknown, not guessed
    assert r["Keyless rescue"].kind == leaves.OUTSIDE and "Exa" in r["Keyless rescue"].where
    assert r["Spoken replies"].kind == leaves.OUTSIDE and "Microsoft" in r["Spoken replies"].where  # edge
    assert "piper" in r["Spoken replies"].hint.lower()
    assert r["Voice input"].kind == leaves.AUTO  # unset: Hermes decides at run time, not guessed here


def test_keyless_rescue_follows_hermes_then_webgraft():
    off = dict(LOCAL, web={**LOCAL["web"], "keyless_rescue": False})
    assert rows(off, mode="off")["Keyless rescue"].kind == leaves.OFF
    assert rows(LOCAL, mode="block")["Keyless rescue"].kind == leaves.HELD
    other = dict(LOCAL, web={"search_backend": "searxng", "extract_backend": "firecrawl"})
    assert "searches only" in rows(other)["Keyless rescue"].where
    assert rows(other)["Page reading"].kind == leaves.OUTSIDE


def test_cloud_models_fallbacks_and_side_models_are_listed():
    config = dict(LOCAL, model={"provider": "nous", "default": "x"},
                  fallback_providers=[{"provider": "openrouter", "model": "y"}],
                  auxiliary={"vision": {"provider": "openrouter", "model": "z"}, "compression": {"provider": "auto"}})
    r = leaves.report(config, "http://serveur.local/searxng", "ask")
    kinds = {row.what: row.kind for row in r}
    assert kinds["Model"] == leaves.OUTSIDE
    assert kinds["Fallback model"] == leaves.OUTSIDE
    assert kinds["Side model (vision)"] == leaves.OUTSIDE
    assert "Side model (compression)" not in kinds  # auto: the main model, already listed


def test_a_command_voice_and_disabled_input_are_reported_as_such():
    config = dict(LOCAL, tts={"provider": "windows", "providers": {"windows": {"command": "powershell ..."}}},
                  stt={"enabled": False})
    r = rows(config)
    assert r["Spoken replies"].kind == leaves.AUTO and "your own command" in r["Spoken replies"].where
    assert r["Voice input"].kind == leaves.OFF


def test_status_prints_the_section_and_changes_nothing(monkeypatch, capsys):
    config = {"model": {"provider": "lmstudio"}, "web": {"extract_backend": "webgraft"}}
    before = repr(config)
    monkeypatch.setattr(cli, "_config", lambda: config)
    monkeypatch.setattr(cli, "_secret", lambda name: "")
    cli.print_leaves()
    out = capsys.readouterr().out
    assert "What can leave this machine" in out and "Spoken replies" in out and "Microsoft" in out
    assert repr(config) == before
