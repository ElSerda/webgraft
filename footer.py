"""Builds the footer appended to an answer: the pages it was built from, as clickable links."""

from __future__ import annotations

import re

_TEXT = {
    "en": {
        "sources": "Sources (opened pages):",
        "earlier": "Also used, opened earlier in this conversation:",
        "snippets": "No page opened: this answer relies on search snippets only.",
        "no_page": "No page could be read.",
    },
    "fr": {
        "sources": "Sources (pages ouvertes) :",
        "earlier": "Aussi utilisées, ouvertes plus tôt dans la conversation :",
        "snippets": "Aucune page ouverte : réponse basée sur les extraits de recherche.",
        "no_page": "Aucune page n'a pu être lue.",
    },
}

_FRENCH_WORDS = re.compile(r"\b(?:le|la|les|des|est|une|avec|pour|dans|sur|pas|qui|que|du|et)\b", re.IGNORECASE)
_ENGLISH_WORDS = re.compile(r"\b(?:the|is|are|with|for|in|on|not|which|that|of|and)\b", re.IGNORECASE)


def _answer_language(text: str) -> str:
    return "fr" if len(_FRENCH_WORDS.findall(text)) > len(_ENGLISH_WORDS.findall(text)) else "en"


def _hermes_language() -> str | None:
    """Hermes's UI language, resolved as Hermes does: HERMES_LANGUAGE > display.language > en.
    None outside Hermes (tests, tools)."""
    try:
        from agent.secret_scope import get_secret
        from hermes_cli.config import load_config_readonly
    except ImportError:
        return None
    try:
        lang = (get_secret("HERMES_LANGUAGE") or "").strip()
        if not lang:
            display = (load_config_readonly() or {}).get("display") or {}
            lang = str(display.get("language") or "en")
    except Exception:  # noqa: BLE001
        return None
    return lang.lower().replace("_", "-").split("-")[0]


def language_of(text: str, setting: str = "auto") -> str:
    """auto: Hermes's display.language, like Hermes's own trailers (other languages get English).
    answer: the language the answer is written in. en / fr: fixed."""
    if setting in _TEXT:
        return setting
    if setting == "auto":
        lang = _hermes_language()
        if lang is not None:
            return lang if lang in _TEXT else "en"
    return _answer_language(text)


def _source_line(url: str, titles: dict[str, str]) -> str:
    """"- Page title: https://..." with the link bare, so terminals, Desktop and chat apps make it clickable."""
    title = re.sub(r"\s+", " ", titles.get(url, "")).strip()
    if len(title) > 80:
        title = title[:77].rstrip() + "..."
    return f"- {title}: {url}" if title else f"- {url}"


def build(opened: list[str], searched: bool, lang: str, earlier: list[str] | None = None,
          titles: dict[str, str] | None = None) -> str:
    """opened: pages read in this turn. earlier: pages from previous turns the answer relies on."""
    t = _TEXT[lang]
    titles = titles or {}
    lines: list[str] = []
    if opened:
        lines.append(t["sources"])
        lines.extend(_source_line(url, titles) for url in opened)
    if earlier:
        lines.append(t["earlier"])
        lines.extend(_source_line(url, titles) for url in earlier)
    if not lines:
        lines.append(t["snippets"] if searched else t["no_page"])
    return "\n".join(lines)
