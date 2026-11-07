"""webgraft: reads web pages on this machine and lists the pages a web answer used.

Hooks only, no extra model call:
  pre_llm_call          starts a fresh record for the turn and adds a short reminder
  pre_tool_call         keeps only the first URL of a web_extract call that asks for several (one
                        URL per call keeps each page within a small model's context), and notes
                        the question so a long page has its relevant passages first (focus.py)
  transform_tool_result adds to that result the URLs that were left out, so the model can open them
  post_tool_call        keeps the text of every page web_extract returned, for the whole session
  transform_llm_output  appends the opened pages to the answer (append-only,
                        so a streamed answer is not reprinted)
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from . import compat, focus, footer, values

# Page text is kept in memory per session, so an answer can rely on a page opened in an
# earlier turn. Bounded: oldest pages, then oldest sessions, are dropped first. (Hermes's
# on_session_end fires after every turn, so it cannot be used to forget a session.)
MAX_PAGES_PER_SESSION = 30
MAX_SESSIONS = 20

_lock = threading.Lock()
_turns: dict[str, dict[str, Any]] = {}
_sessions: "OrderedDict[str, OrderedDict[str, tuple[str, str]]]" = OrderedDict()  # url -> (title, text)
_questions: "OrderedDict[str, list[str]]" = OrderedDict()  # session -> user message, then search queries
_ctx = None


def _setting(key: str, default):
    try:
        return _ctx.get_config(key, default=default) if _ctx is not None else default
    except Exception:  # noqa: BLE001 - a settings problem must never break a turn
        return default


def _payload(result: Any) -> dict:
    """The JSON object inside a tool result; Hermes may wrap it in a text envelope."""
    text = str(result or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _pages(result: Any) -> list[dict]:
    """Pages with real content from a web_extract result.

    A bot wall that another backend returned as content (Hermes's keyless rescue can do that)
    is not a source, so it is dropped here too.
    """
    from .extract import looks_blocked

    return [
        {"url": p["url"], "title": str(p.get("title") or ""), "content": str(p["content"])}
        for p in _payload(result).get("results") or []
        if isinstance(p, dict) and p.get("url") and p.get("content") and not p.get("error")
        and not looks_blocked(str(p.get("title") or ""), str(p["content"]))
    ]


def _snippets(result: Any) -> str:
    """The titles and descriptions of a web_search result, as one text."""
    data = _payload(result).get("data")
    hits = (data or {}).get("web") if isinstance(data, dict) else None
    return "\n".join(f"{h.get('title') or ''}\n{h.get('description') or ''}" for h in hits or [] if isinstance(h, dict))


# Added to the user's message, a trusted position: inside a tool result Hermes marks text as data
# the model must not follow. Small models otherwise answer from snippets that happen to hold numbers.
# Said to be a note, not the request: a model given a bare URL and this text once took the text
# for the question and searched the web for it.
REMINDER = (
    "[webgraft note to the assistant, not part of the user's message] "
    "If you search the web for this message: search snippets are not sources. "
    "Open the most relevant results with web_extract, one URL per call, before answering. "
    "If a web_extract result lists pages it did not open, open the ones you still need. "
    "webgraft itself adds the list of the pages you opened under your answer; "
    "do not write a sources list or any URL."
)
# A message that holds a URL asks for that page, not for a search.
URL_REMINDER = (
    "[webgraft note to the assistant, not part of the user's message] "
    "Open the URL in the user's message with web_extract, one URL per call, and answer from the page. "
    "If it cannot be read, say so; if you then search, search snippets are not sources: open the most "
    "relevant results with web_extract before answering. "
    "webgraft itself adds the list of the pages you opened under your answer; "
    "do not write a sources list or any URL."
)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
# Hermes keeps the footer in the conversation, so a model sees it under its own earlier answers and
# may copy it into an answer that opened nothing (seen in testing with a large cloud model). Said only then.
FOOTER_NOTE = (
    "[webgraft] The source lists under your earlier answers (\"{heading}\") were added by "
    "webgraft, not written by you. Do not write one: webgraft adds it when pages were used."
)


def _footer_in_history(history: Any) -> str:
    """The footer heading found under an earlier answer, if any."""
    headings = [text[key] for text in footer._TEXT.values() for key in ("sources", "earlier")]
    for message in history if isinstance(history, list) else []:
        if isinstance(message, dict) and message.get("role") == "assistant":
            content = str(message.get("content") or "")
            for heading in headings:
                if heading in content:
                    return heading
    return ""


def _pages_from_history(history: Any) -> list[dict]:
    """Pages that web_extract returned earlier in the conversation, from Hermes's message list."""
    names: dict[str, str] = {}
    pages: list[dict] = []
    for message in history if isinstance(history, list) else []:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                fn = call.get("function") or {}
                names[str(call.get("id"))] = str(fn.get("name") or call.get("name") or "")
        if message.get("role") == "tool":
            name = message.get("name") or names.get(str(message.get("tool_call_id")), "")
            if name == "web_extract":
                pages.extend(_pages(message.get("content")))
    return pages


def _start_turn(session_id: str = "", user_message: Any = "", conversation_history: Any = None,
                **_: Any) -> Optional[str]:
    # A resumed conversation runs in a new process: its earlier pages come back from the history.
    if session_id not in _sessions and conversation_history:
        for page in _pages_from_history(conversation_history):
            with _lock:
                _remember(session_id, page["url"], page["title"], page["content"])
    # An interrupted turn never reaches the transform hook, so its record is dropped here.
    with _lock:
        _turns.pop(session_id, None)
        _questions[session_id] = [str(user_message or "")[:2000]]
        _questions.move_to_end(session_id)
        while len(_questions) > MAX_SESSIONS:
            _questions.popitem(last=False)
    parts = []
    if _setting("remind", True):
        parts.append(URL_REMINDER if _URL.search(str(user_message or "")) else REMINDER)
    heading = _footer_in_history(conversation_history)
    if heading and compat.sources_mode(_setting("show_sources", "auto")) != "never":
        parts.append(FOOTER_NOTE.format(heading=heading))
    return " ".join(parts) or None


def _remember(session_id: str, url: str, title: str, content: str) -> None:
    pages = _sessions.setdefault(session_id, OrderedDict())
    _sessions.move_to_end(session_id)
    pages[url] = (title, content)
    pages.move_to_end(url)
    while len(pages) > MAX_PAGES_PER_SESSION:
        pages.popitem(last=False)
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)


def _urls(args: Any) -> list[str]:
    urls = (args or {}).get("urls") if isinstance(args, dict) else None
    if isinstance(urls, str):
        return [urls]
    return [str(u) for u in urls or [] if u]


_held: "OrderedDict[str, list[str]]" = OrderedDict()  # tool call -> URLs left out of it


def _call_key(session_id: str, tool_call_id: Any) -> str:
    return f"{session_id}\x00{tool_call_id or ''}"


def _guard_tool(tool_name: str = "", args: Any = None, session_id: str = "", tool_call_id: Any = None,
                **_: Any) -> Optional[dict]:
    if tool_name != "web_extract":
        return None
    urls = _urls(args)
    directive = None
    if len(urls) > 1 and _setting("one_url_per_call", True):
        # Read the model's first choice now; the result then says which URLs were left out.
        # (Sending the whole call back cost a round trip, and a small model then often picked
        # another URL than its first choice.)
        with _lock:
            _held[_call_key(session_id, tool_call_id)] = urls[1:]
            while len(_held) > MAX_SESSIONS * 4:
                _held.popitem(last=False)
        directive = {"action": "modify", "args": {"urls": urls[:1]}}
    if urls and _setting("focus", True):
        with _lock:
            texts = list(_questions.get(session_id, []))
        limit = (args or {}).get("char_limit") if isinstance(args, dict) else None
        try:
            window = max(2_000, min(int(limit), 500_000)) if limit is not None else focus.hermes_window()
        except (TypeError, ValueError):
            window = focus.hermes_window()
        focus.expect(urls[0], focus.terms_of(*texts), window)
    return directive


EMPTY_SEARCH_NOTE = ("This search returned no result. A self-hosted search backend also answers nothing when "
                     "the engines behind it rate-limit it, which several searches sent at once trigger. "
                     "An empty search does not show that something does not exist: search again with one "
                     "query at a time and other words, or open a page that lists it.")


def _annotate(tool_name: str = "", result: Any = None, session_id: str = "", tool_call_id: Any = None,
              **_: Any) -> Optional[str]:
    """Adds a webgraft_note to two tool results: a web_extract call whose other URLs _guard_tool
    left out (they are listed), and a web_search that found nothing (an empty result is not an absence)."""
    if tool_name == "web_extract":
        with _lock:
            held = _held.pop(_call_key(session_id, tool_call_id), None)
        if not held:
            return None
        note = ("webgraft reads one URL per web_extract call, so each page fits in the context. "
                "This call read only the first URL. Not opened: " + ", ".join(held))
    elif tool_name == "web_search":
        data = _json(result)
        if not isinstance(data, dict) or not data.get("success"):
            return None
        hits = (data.get("data") or {}).get("web") if isinstance(data.get("data"), dict) else None
        if hits != []:
            return None
        note = EMPTY_SEARCH_NOTE
    else:
        return None
    if not isinstance(result, str):
        return None
    data = _json(result)
    if not isinstance(data, dict):
        return f"{result}\n\n[{note}]"
    data["webgraft_note"] = note
    return json.dumps(data, ensure_ascii=False)


def _json(result: Any) -> Any:
    if not isinstance(result, str):
        return None
    try:
        return json.loads(result)
    except ValueError:
        return None


def _record_tool(tool_name: str = "", result: Any = None, session_id: str = "", args: Any = None,
                 **_: Any) -> None:
    if tool_name not in ("web_search", "web_extract"):
        return
    if tool_name == "web_search" and isinstance(args, dict) and args.get("query"):
        with _lock:
            texts = _questions.setdefault(session_id, [""])
            texts.append(str(args["query"])[:300])
            del texts[1:-3]  # the user's message and the last three queries
    pages = _pages(result) if tool_name == "web_extract" else []
    with _lock:
        turn = _turns.setdefault(session_id, {"opened": [], "searched": False, "snippets": ""})
        if tool_name == "web_search":
            turn["searched"] = True
            turn["snippets"] = (turn["snippets"] + "\n" + _snippets(result))[-20_000:]
        for page in pages:
            _remember(session_id, page["url"], page["title"], page["content"])
            if page["url"] not in turn["opened"]:
                turn["opened"].append(page["url"])


def _append_footer(response_text: str = "", session_id: str = "", **_: Any) -> Optional[str]:
    with _lock:
        turn = _turns.pop(session_id, None)
        known = dict(_sessions.get(session_id, {}))
    if not response_text or (turn is None and not known):
        return None  # no web tool this turn and no page read before: nothing to report
    mode = compat.sources_mode(_setting("show_sources", "auto"))
    # Hermes keeps only the first plugin rewrite of a reply: leave it to another rewriter rather
    # than hide its output, unless the user asked for this footer anyway (show_sources: always).
    if mode == "never":
        return None
    if mode == "auto" and compat.other_reply_rewriters("webgraft", getattr(_ctx, "has_plugin", None)):
        return None
    titles = {u: t for u, (t, _) in known.items()}
    opened = turn["opened"] if turn else []
    # Pages from earlier turns are listed only when the answer uses one of their values.
    earlier_pages = [{"url": u, "content": c} for u, (_, c) in known.items() if u not in opened]
    earlier = values.pages_used(response_text, earlier_pages) if earlier_pages else []
    if turn is None and not earlier:
        return None  # a turn without web tools that does not draw on an earlier page
    # Values the answer took from search snippets rather than from a page anyone read.
    from_snippets = values.only_in(response_text, (turn or {}).get("snippets", ""),
                                   [c for _, c in known.values()]) if turn and turn["searched"] else []
    lang = footer.language_of(response_text, _setting("language", "auto"))
    text = footer.build(opened, bool(turn and turn["searched"]), lang, earlier, titles, from_snippets)
    # Keep the answer byte for byte: Hermes then prints only the added footer after a streamed reply.
    return f"{response_text}\n\n{text}" if text else None


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    from . import extract
    extract.setting = _setting  # llms_txt and respect_ai_optout reach the reader
    ctx.register_hook("pre_llm_call", _start_turn)
    ctx.register_hook("pre_tool_call", _guard_tool)
    ctx.register_hook("post_tool_call", _record_tool)
    ctx.register_hook("transform_tool_result", _annotate)
    ctx.register_hook("transform_llm_output", _append_footer)
    skills = Path(__file__).parent / "skills"
    if hasattr(ctx, "register_skill") and skills.is_dir():
        for child in sorted(skills.iterdir()):
            if (child / "SKILL.md").is_file():
                ctx.register_skill(child.name, child / "SKILL.md")  # loads as webgraft:<name>
    if hasattr(ctx, "register_cli_command"):
        from . import cli
        cli.register(ctx)
    # The extraction backend needs Hermes's provider base class; hooks work without it.
    try:
        from .provider import WebGraftProvider
    except ImportError:
        return
    ctx.register_web_search_provider(WebGraftProvider())
