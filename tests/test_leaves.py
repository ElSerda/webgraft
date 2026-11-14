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


# Each case below follows a rule read in Hermes's own code (v0.23.1), named in the comment.

def test_names_are_compared_as_hermes_does():
    # Hermes lower-cases and strips provider and backend names (tts_tool.py, web_tools.py).
    config = dict(LOCAL, tts={"provider": " Piper "}, web={"search_backend": "SearXNG", "extract_backend": "WebGraft"})
    r = rows(config)
    assert r["Spoken replies"].kind == leaves.HERE
    assert r["Search"].kind == leaves.NETWORK
    assert r["Page reading"].kind == leaves.HERE


def test_a_command_never_replaces_a_built_in_voice():
    # tts_command_provider.py: built-in names are reserved, so tts.providers.openai.command still means OpenAI.
    config = dict(LOCAL, tts={"provider": "openai", "providers": {"openai": {"command": "my-local-tts"}}})
    assert rows(config)["Spoken replies"].kind == leaves.OUTSIDE


def test_command_voices_follow_hermes_rules():
    legacy = dict(LOCAL, tts={"provider": "mine", "mine": {"command": "say {input_path}"}})  # tts.<name>
    assert rows(legacy)["Spoken replies"].kind == leaves.AUTO
    not_a_command = dict(LOCAL, tts={"provider": "mine", "providers": {"mine": {"type": "http", "command": "x"}}})
    unknown = rows(not_a_command)["Spoken replies"]
    # Neither built-in nor command: a voice plugin of that name, else Hermes's Edge default.
    assert unknown.kind == leaves.AUTO and "Microsoft" in unknown.where and "piper" in unknown.hint.lower()
    blank = dict(LOCAL, tts={"provider": "mine", "providers": {"mine": {"command": "   "}}})
    assert "plugin" in rows(blank)["Spoken replies"].where
    nous = rows(dict(LOCAL, tts={"provider": "nous"}))["Spoken replies"]
    assert nous.kind == leaves.OUTSIDE and "Nous" in nous.where


def test_keyless_rescue_off_switches_read_as_hermes_reads_them():
    # web_tools_rescue.py: `not web.keyless_rescue` turns it off, and so does web.keyless_fallback: false.
    for value in (False, 0, None):
        assert rows(dict(LOCAL, web={**LOCAL["web"], "keyless_rescue": value}))["Keyless rescue"].kind == leaves.OFF
    fallback_off = rows(dict(LOCAL, web={**LOCAL["web"], "keyless_fallback": False}), mode="off")["Keyless rescue"]
    assert fallback_off.kind == leaves.OFF and "keyless_fallback" in fallback_off.where
    # The string "false" is truthy for Hermes there: the rescue stays on, so it is not reported off.
    assert rows(dict(LOCAL, web={**LOCAL["web"], "keyless_rescue": "false"}), mode="off")["Keyless rescue"].kind \
        == leaves.OUTSIDE


def test_voice_input_follows_hermes():
    # stt.enabled goes through is_truthy_value (utils.py): only 1, true, yes, on count as on.
    for value in (False, "false", "no", "off", 0):
        assert rows(dict(LOCAL, stt={"enabled": value, "provider": "groq"}))["Voice input"].kind == leaves.OFF
    assert rows(dict(LOCAL, stt={"enabled": "yes", "provider": "groq"}))["Voice input"].kind == leaves.OUTSIDE
    assert rows(dict(LOCAL, stt={"provider": "local_command"}))["Voice input"].kind == leaves.HERE
    cloud = rows(dict(LOCAL, stt={"provider": "openai"}))["Voice input"]
    assert cloud.kind == leaves.OUTSIDE and "local" in cloud.hint


def test_model_addresses():
    assert rows(dict(LOCAL, model={}))["Model"].kind == leaves.AUTO
    lan = rows(dict(LOCAL, model={"provider": "custom", "base_url": "http://192.168.1.20:1234/v1"}))["Model"]
    assert lan.kind == leaves.NETWORK and lan.hint == ""
    remote = rows(dict(LOCAL, model={"provider": "custom", "base_url": "https://api.example.com/v1"}))["Model"]
    assert remote.kind == leaves.OUTSIDE and "api.example.com" in remote.where and remote.hint
    config = dict(LOCAL, fallback_providers=[{"provider": "ollama"}, "not a dict"],
                  auxiliary={"vision": {"provider": "auto", "base_url": "http://nas:1234/v1"}})
    kinds = {row.what: row.kind for row in leaves.report(config, "http://serveur.local/searxng", "ask")}
    assert kinds["Fallback model"] == leaves.HERE
    assert kinds["Side model (vision)"] == leaves.NETWORK


def test_search_backends():
    public = rows(LOCAL, url="https://searx.example.org")["Search"]
    assert public.kind == leaves.OUTSIDE
    assert rows(dict(LOCAL, web={"search_backend": "brave"}))["Search"].kind == leaves.OUTSIDE
    assert rows(dict(LOCAL, web={"backend": "searxng"}))["Search"].kind == leaves.NETWORK  # web.backend
    assert rows(dict(LOCAL, web={}))["Search"].kind == leaves.AUTO


def test_where_ipv6():
    assert leaves.where("http://[::1]:1234/v1")[0] == leaves.HERE
    assert leaves.where("http://[fe80::1]/")[0] == leaves.NETWORK
    assert leaves.where("http://[fd00::5]/")[0] == leaves.NETWORK
    assert leaves.where("http://[2606:4700::1111]/")[0] == leaves.OUTSIDE


def test_status_reads_searxng_url_from_the_environment_and_prints_hints(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_config", lambda: {"web": {"search_backend": "searxng"}})
    monkeypatch.setattr(cli, "_secret", lambda name: "")
    monkeypatch.setenv("SEARXNG_URL", "http://192.168.1.5:8080")
    cli.print_leaves()
    out = capsys.readouterr().out
    assert "your SearXNG at 192.168.1.5" in out
    assert "to keep it here" in out
