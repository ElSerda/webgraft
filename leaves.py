"""What can leave this machine: `hermes webgraft status`, read from Hermes's config only.

A local model and webgraft keep a conversation and the pages it reads on this machine, but some of
Hermes's defaults send data out anyway, and its setup does not say so: the keyless rescue (failed
pages and searches go to outside services), the edge voice (spoken replies go to Microsoft), a
speech-to-text provider left on auto. This lists, for each, where it goes, and how to keep it here.

Read-only: webgraft changes none of these settings. Only Hermes's own config is read, and only what
it says: a setting Hermes resolves at run time (an auto choice) is named as such, not guessed.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

HERE, NETWORK, HELD, OUTSIDE, AUTO, OFF = "here", "network", "held", "outside", "auto", "off"

# Providers whose server runs on this machine unless base_url says otherwise (Hermes's defaults).
LOCAL_SERVERS = {"lmstudio": "http://localhost:1234/v1", "ollama": "http://localhost:11434/v1"}
LOCAL_VOICES = {"piper", "kittentts", "neutts"}
# Hermes's built-in voices (tools/tts_command_provider.py): a command under one of these names is ignored.
BUILTIN_VOICES = {"edge", "elevenlabs", "openai", "minimax", "xai", "mistral", "gemini", "neutts", "kittentts",
                  "piper", "deepinfra"}
TTS_SERVICES = {"edge": "Microsoft's Edge speech service", "nous": "Nous's Tool Gateway"}
STT_LOCAL = {"local", "local_command"}
VENDORS = "Exa, Parallel, Firecrawl or Keenable"


@dataclass(frozen=True)
class Row:
    what: str
    kind: str  # here / network / outside / auto / off
    where: str
    hint: str = ""


def where(url: Any) -> tuple[str, str]:
    """(kind, host) for a server address: this machine, your network, or outside."""
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        host = ""
    if not host:
        return OUTSIDE, "?"
    if host == "localhost":
        return HERE, host
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        local_name = "." not in host or host.endswith((".local", ".lan", ".home", ".internal", ".home.arpa"))
        return (NETWORK if local_name else OUTSIDE), host
    if ip.is_loopback:
        return HERE, host
    if ip.is_private or ip.is_link_local:
        return NETWORK, host
    return OUTSIDE, host


def _model_row(what: str, provider: str, base_url: str, model: str = "", hint: str = "") -> Row:
    if not provider and not base_url:
        return Row(what, AUTO, "not set (Hermes asks at the first run)")
    url = base_url or LOCAL_SERVERS.get(provider, "")
    label = f"{provider or 'custom'}" + (f" {model}" if model else "")
    if url:
        kind, host = where(url)
        return Row(what, kind, f"{label} at {host}", hint if kind == OUTSIDE else "")
    return Row(what, OUTSIDE, f"{label} (the provider's servers)", hint)


def _section(config: dict, name: str) -> dict:
    value = config.get(name)
    return value if isinstance(value, dict) else {}


def _name(value: Any) -> str:
    """A provider or backend name as Hermes compares it: stripped, lower case."""
    return str(value or "").strip().lower()


def _truthy(value: Any, default: bool) -> bool:
    """Hermes's is_truthy_value (utils.py): a string counts only as 1, true, yes or on."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _command_voice(tts: dict, voice: str) -> bool:
    """Hermes's rule for a command voice: not a built-in name, tts.providers.<name> (else tts.<name>)
    with type command or none, and a command."""
    if not voice or voice in BUILTIN_VOICES:
        return False
    block = _section(tts, "providers").get(voice)
    if not isinstance(block, dict):
        block = _section(tts, voice)
    command = block.get("command")
    return _name(block.get("type")) in ("", "command") and isinstance(command, str) and bool(command.strip())


def report(config: dict, searxng_url: str = "", rescue_mode: str = "ask") -> list[Row]:
    """One row per place Hermes may send a conversation's data, from its config."""
    rows: list[Row] = []
    model = _section(config, "model")
    rows.append(_model_row("Model", str(model.get("provider") or ""), str(model.get("base_url") or ""),
                           str(model.get("default") or ""), "a local model: LM Studio or Ollama, then `hermes model`"))

    for fallback in config.get("fallback_providers") or []:
        if isinstance(fallback, dict):
            rows.append(_model_row("Fallback model", str(fallback.get("provider") or ""),
                                   str(fallback.get("base_url") or ""), str(fallback.get("model") or ""),
                                   "a local fallback, or none (see `hermes fallback`)"))

    for task, block in sorted(_section(config, "auxiliary").items()):
        if isinstance(block, dict) and (str(block.get("provider") or "auto") not in ("auto", "")
                                        or block.get("base_url")):
            rows.append(_model_row(f"Side model ({task})", str(block.get("provider") or ""),
                                   str(block.get("base_url") or ""), str(block.get("model") or ""),
                                   f"auxiliary.{task}.provider: auto (your main model)"))

    web = _section(config, "web")
    search = _name(web.get("search_backend") or web.get("backend"))
    if search == "searxng":
        if searxng_url:
            kind, host = where(searxng_url)
            rows.append(Row("Search", kind, f"your SearXNG at {host} (it passes the query on to search engines)"))
        else:
            rows.append(Row("Search", AUTO, "SearXNG, but SEARXNG_URL is not set", "`hermes webgraft setup`"))
    elif search == "ddgs":
        rows.append(Row("Search", OUTSIDE, "DuckDuckGo (a search goes out by nature)",
                        "a SearXNG instance of your own (the engines then see it, not you)"))
    elif search:
        rows.append(Row("Search", OUTSIDE, f"{search} (an outside search service)"))
    else:
        rows.append(Row("Search", AUTO, "Hermes picks a backend from your keys", "`hermes webgraft setup` to choose"))

    reading = _name(web.get("extract_backend") or web.get("backend"))
    if reading == "webgraft":
        rows.append(Row("Page reading", HERE, "each page is fetched from its own site and read here (webgraft)"))
    else:
        rows.append(Row("Page reading", OUTSIDE if reading else AUTO,
                        f"{reading} (the page's address goes to that service)" if reading else "Hermes picks a backend",
                        "`hermes webgraft setup` sets webgraft"))

    if not web.get("keyless_rescue", True):  # Hermes's own tests, in tools/web_tools_rescue.py
        rows.append(Row("Keyless rescue", OFF, "off in Hermes (web.keyless_rescue: false)"))
    elif not web.get("keyless_fallback", True):
        rows.append(Row("Keyless rescue", OFF, "off in Hermes (web.keyless_fallback: false)"))
    elif rescue_mode == "block":
        rows.append(Row("Keyless rescue", HELD, "refused (keyless_rescue: block), "
                                                "but the first search of an outage still goes out",
                        "web.keyless_rescue: false in Hermes's config also holds the first search"))
    elif rescue_mode == "ask":
        pages = "pages" if reading == "webgraft" else "searches only (webgraft does not read your pages)"
        rows.append(Row("Keyless rescue", HELD, f"asks you first for {pages}, "
                                                "the first search of an outage still goes out",
                        "web.keyless_rescue: false in Hermes's config also holds the first search"))
    else:
        rows.append(Row("Keyless rescue", OUTSIDE, f"failed pages and searches go to {VENDORS}, nobody asked",
                        "webgraft's keyless_rescue: ask, or web.keyless_rescue: false"))

    tts = _section(config, "tts")
    voice = _name(tts.get("provider")) or "edge"
    piper = "a local voice: Piper (`hermes tools`, Voice & TTS), then tts.provider: piper"
    if voice in LOCAL_VOICES:
        rows.append(Row("Spoken replies", HERE, voice))
    elif _command_voice(tts, voice):
        rows.append(Row("Spoken replies", AUTO, f"{voice}: your own command (it decides)"))
    elif voice not in BUILTIN_VOICES and voice not in TTS_SERVICES:
        rows.append(Row("Spoken replies", AUTO, f"{voice}: a voice plugin of that name if one is installed, "
                                                "else Microsoft's Edge speech service", piper))
    else:
        rows.append(Row("Spoken replies", OUTSIDE, TTS_SERVICES.get(voice, f"{voice} (an outside voice service)"),
                        piper))

    stt = _section(config, "stt")
    heard = _name(stt.get("provider"))
    if not _truthy(stt.get("enabled"), True):
        rows.append(Row("Voice input", OFF, "off (stt.enabled: false)"))
    elif heard in STT_LOCAL:
        rows.append(Row("Voice input", HERE, heard))
    elif heard:
        rows.append(Row("Voice input", OUTSIDE, f"{heard} (an outside transcription service)",
                        "stt.provider: local transcribes here"))
    else:
        rows.append(Row("Voice input", AUTO, "auto: a local engine first, else a cloud service whose key is set",
                        "stt.provider: local keeps it here"))
    return rows