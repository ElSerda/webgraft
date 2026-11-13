"""Builds the footer appended to an answer: the pages it was built from, as clickable links."""

from __future__ import annotations

import unicodedata
from urllib.parse import quote

_TEXT = {
    "en": {
        "sources": "Sources (opened pages):",
        "snippets": "No page opened: this answer relies on search snippets only.",
        "no_page": "No page could be read.",
        "unread": "Refused or unreadable:",
        "attached": "Attached by you (@url), read before the answer:",
    },
    "fr": {
        "sources": "Sources (pages ouvertes) :",
        "snippets": "Aucune page ouverte : réponse basée sur les extraits de recherche.",
        "no_page": "Aucune page n'a pu être lue.",
        "unread": "Refusées ou illisibles :",
        "attached": "Jointes par toi (@url), lues avant la réponse :",
    },
}

def _hermes_language() -> str | None:
    """Hermes's interface language, in Hermes's order: HERMES_LANGUAGE (per profile) > display.language
    > en. Read through Hermes's public plugin API (agent.secret_scope, hermes_cli.config): its own
    resolver, agent.i18n, is not part of that API. None outside Hermes."""
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


def language() -> str:
    """The footer's language: Hermes's interface language when webgraft has its wording (en, fr),
    English otherwise and outside Hermes."""
    lang = _hermes_language() or "en"
    return lang if lang in _TEXT else "en"


def text(key: str) -> str:
    return _TEXT[language()][key]


# The second list heading that versions up to 0.2.0 wrote (removed in 0.3.0): still recognised
# in a conversation's history, so the model is told the lists were not its own.
_OLDER_HEADINGS = ("Also used, opened earlier in this conversation:",
                   "Aussi utilisées, ouvertes plus tôt dans la conversation :")


def headings() -> set[str]:
    """Every footer heading webgraft may have written, in any of its languages."""
    return {t["sources"] for t in _TEXT.values()} | set(_OLDER_HEADINGS)


_MARKDOWN_CHARS = str.maketrans({ch: " " for ch in "[]()<>!`\\"})
# Characters a URL keeps as they are (RFC 3986 reserved and unreserved, and "%" for what is already
# encoded): everything else, Markdown's brackets and parentheses and any space included, is encoded.
_URL_SAFE = ":/?#@!$&'*+,;=%-._~"


def _source_line(url: str, titles: dict[str, str]) -> str:
    """"- Page title: https://..." with the link bare, so terminals, Desktop and chat apps make it clickable."""
    # The title comes from the page: no Markdown that could render as a link or an image, and no
    # URL of its own, so the line can only point where webgraft says.
    raw = (titles.get(url, "") or "")[:300]  # shown up to 60 characters
    title = "".join(ch for ch in raw if unicodedata.category(ch) != "Cf")  # bidi, zero-width
    title = title.translate(_MARKDOWN_CHARS)  # no link or image syntax
    # Words that a terminal or chat app would turn into a link: URLs, e-mail addresses, www. names.
    words = [w for w in title.split() if "://" not in w and "@" not in w and not w.lower().startswith("www.")]
    title = " ".join(words).strip(" :-")
    # The line is kept in the history as the assistant's words, outside Hermes's untrusted-data
    # wrapper: the page's title is kept short.
    if len(title) > 60:
        title = title[:57].rstrip() + "..."
    # The URL comes from the page's redirects: characters that Markdown reads as link or image
    # syntax are percent-encoded, so the line stays one plain link to that address.
    url = quote(url, safe=_URL_SAFE)
    return f"- {title}: {url}" if title else f"- {url}"


MAX_UNREAD = 20  # refused or unreadable pages listed under one answer


def _unread_line(url: str, why: str) -> str:
    """"- https://... (HTTP 403 from origin)": the reason comes from the tool result, cleaned like a title."""
    why = "".join(ch if ch.isprintable() else " " for ch in why[:300]).translate(_MARKDOWN_CHARS)
    why = " ".join(why.split())
    if len(why) > 80:
        why = why[:77].rstrip() + "..."
    return f"- {quote(url, safe=_URL_SAFE)} ({why})" if why else f"- {quote(url, safe=_URL_SAFE)}"


def build(opened: list[str], searched: bool, titles: dict[str, str] | None = None,
          unread: list[tuple[str, str]] | None = None, attached: list[str] | None = None) -> str:
    """opened: the pages web_extract returned in this turn; unread: (url, why) for the pages it could
    not read; searched: a web_search ran; attached: the pages the user attached with @url:. All from
    the tool results and the user's message: nothing is guessed from the answer. The wording follows
    Hermes's language."""
    unread, attached = unread or [], attached or []
    if opened:
        lines = [text("sources")] + [_source_line(url, titles or {}) for url in opened]
    elif unread:
        lines = [text("no_page")]
    elif searched:
        lines = [text("snippets")]
    elif attached:
        lines = []
    else:
        return text("no_page")
    if attached:
        lines += [text("attached")] + [f"- {quote(url, safe=_URL_SAFE)}" for url in attached[:MAX_UNREAD]]
    if unread:
        lines += [text("unread")] + [_unread_line(url, why) for url, why in unread[:MAX_UNREAD]]
    return "\n".join(lines)
