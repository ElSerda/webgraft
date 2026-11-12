"""webgraft: reads web pages on this machine and lists the pages a web answer used.

Hooks only, no extra model call:
  pre_llm_call          starts a fresh record for the turn and adds a short reminder
  pre_tool_call         keeps only the first URL of a web_extract call that asks for several (one
                        URL per call keeps each page within a small model's context); once web
                        content is in the session, sends tools with effects to Hermes's approval gate
                        (setting tool_guard)
  transform_tool_result adds to that result the URLs that were left out, so the model can open them,
                        and tells the model to open the next result when no page could be read
  post_tool_call        keeps the text of every page web_extract returned, for the whole session
  transform_llm_output  appends the opened pages to the answer (append-only,
                        so a streamed answer is not reprinted)
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from . import compat, footer, values

# Page text is kept in memory per session, so an answer can rely on a page opened in an
# earlier turn. Bounded: oldest pages, then oldest sessions, are dropped first. (Hermes's
# on_session_end fires after every turn, so it cannot be used to forget a session.)
MAX_PAGES_PER_SESSION = 30
MAX_SESSIONS = 20

_lock = threading.Lock()
_turns: "OrderedDict[str, dict[str, Any]]" = OrderedDict()  # interrupted turns never reach the footer
_sessions: "OrderedDict[str, OrderedDict[str, tuple[str, str]]]" = OrderedDict()  # url -> (title, text)
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
    "If a web_extract result lists pages it did not open, open the ones you still need."
)
# A message that holds a URL asks for that page, not for a search.
URL_REMINDER = (
    "[webgraft note to the assistant, not part of the user's message] "
    "Open the URL in the user's message with web_extract, one URL per call, and answer from the page. "
    "If it cannot be read, say so; if you then search, search snippets are not sources: open the most "
    "relevant results with web_extract before answering."
)
# Said only when webgraft will add its footer: with show_sources off, or stepping aside for another
# plugin that rewrites replies, the model has to cite its pages itself. A URL the user asks for is
# still written.
FOOTER_SENTENCE = ("webgraft itself adds the list of the pages you opened under your answer; "
                   "do not write a sources list (a URL the user asks for is fine).")
NO_FOOTER_SENTENCE = "End your answer with the URLs of the pages you used."
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


def _text_of(message: Any) -> str:
    """The text of a user message. On a multimodal turn Hermes passes a list of parts (text, image):
    only the text parts count, so an image URL is not taken for a page to open."""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        texts = []
        for part in message:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and part.get("type") in (None, "text", "input_text"):
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return "\n".join(texts)
    return ""


def _start_turn(session_id: str = "", user_message: Any = "", conversation_history: Any = None,
                **_: Any) -> Optional[str]:
    # A resumed conversation runs in a new process: its earlier pages come back from the history.
    if session_id not in _sessions and conversation_history:
        for page in _pages_from_history(conversation_history):
            with _lock:
                _remember(session_id, page["url"], page["title"], page["content"])
    # Web content read earlier in the conversation is still in the context (also after a restart).
    if _web_in_history(conversation_history):
        _mark_web(session_id)
    # An interrupted turn never reaches the transform hook, so its record is dropped here.
    with _lock:
        _turns.pop(session_id, None)
    parts = []
    if _setting("remind", True):
        footer = FOOTER_SENTENCE if _footer_on() else NO_FOOTER_SENTENCE
        parts.append((URL_REMINDER if _URL.search(_text_of(user_message)) else REMINDER) + " " + footer)
    heading = _footer_in_history(conversation_history)
    if heading and compat.sources_mode(_setting("show_sources", "always")) != "never":
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


def _url_of(item: Any) -> Optional[str]:
    """The URL of a web_extract item, as Hermes reads it: a string, or an object with a string
    "url" or "href" (models sometimes pass a whole search result). None when unusable."""
    if isinstance(item, dict):
        item = item.get("url") or item.get("href")
    return (item.strip() or None) if isinstance(item, str) else None


def _items(args: Any) -> list:
    """The web_extract items that hold a usable URL, as the model sent them."""
    urls = (args or {}).get("urls") if isinstance(args, dict) else None
    if isinstance(urls, (str, dict)):
        urls = [urls]
    return [u for u in urls or [] if _url_of(u)] if isinstance(urls, list) else []


def _urls(args: Any) -> list[str]:
    return [_url_of(u) for u in _items(args)]


_held: "OrderedDict[str, list[str]]" = OrderedDict()  # tool call -> URLs left out of it

# tool_guard. A web page can carry instructions meant for the assistant (prompt injection). Hermes
# marks web results as data, but a model may still obey them: in testing, a cloud model sent the
# user's question to a URL the page named and ran the shell command it asked for. Once web content is
# in a session it stays in the context, so from then on every tool that is not read-only goes to
# Hermes's own approval gate ("approve"; nobody to answer, as in -Q or cron, means refused), or is
# refused ("block", which also holds under --yolo, where Hermes approves everything itself).
# Tools that bring web content into the session (Hermes wraps their results as untrusted data):
WEB_TOOLS = frozenset({"web_search", "web_extract", "x_search"})
WEB_TOOL_PREFIX = "browser_"
# A command or a script can fetch a page too (curl, git clone, urllib): a URL in it counts as reading
# the web. Judged on the call, not on its output, like the tools above.
COMMAND_TOOLS = frozenset({"terminal", "process_manage", "execute_code"})
# Read-only tools, which stay free: names from Hermes's toolsets reference. Any other tool, including
# one added later, counts as having effects.
READ_ONLY_TOOLS = frozenset({
    "web_search", "web_extract", "x_search",
    "skill_view", "skills_list",
    "session_search", "todo_list", "clarify",
    "vision_analyze", "video_analyze",
})
# Reading local files is the first half of "read a secret, then put it in a URL": after web content,
# read_file and search_files stay free only where Hermes keeps what it fetched (documented:
# HERMES_HOME/cache/web for web_extract text, cache/spillover for oversized tool results, which
# read_file pages through). search_files is not free in cache/spillover, which holds other tools' and
# other sessions' output too.
PATH_READ_TOOLS = {"read_file": (("cache", "web"), ("cache", "spillover")), "search_files": (("cache", "web"),)}
MAX_WEB_SESSIONS = 1000  # session ids only; each turn also rechecks Hermes's history
_web_sessions: "OrderedDict[str, None]" = OrderedDict()
# Tools that execute_code runs for a script reach the hooks with task_id only (no session_id);
# the model's own calls carry both, so the session is found from the task.
_task_sessions: "OrderedDict[str, str]" = OrderedDict()
# A subagent reads the web in its own session; its result then enters the parent's context. Web
# reads are counted, and a delegate_task during which any web tool ran marks the parent.
_web_reads = 0
_delegations: "OrderedDict[str, int]" = OrderedDict()  # delegate_task call -> web reads at its start
GUARD_ASK = ("webgraft: this conversation has read web pages or search results, and a page can hold "
             "instructions aimed at the assistant. Approve {tool} only if it does what you asked for. "
             "(webgraft setting tool_guard)")
GUARD_BLOCK = ("Blocked by webgraft: this conversation has read web content, and its tool_guard setting "
               "refuses tools with effects after that, because a web page can hold instructions aimed at you. "
               "Do not try another tool for the same action: tell the user what you wanted to do, so they "
               "can do it themselves or change tool_guard.")


def _brings_web(tool_name: str, args: Any = None) -> bool:
    """Whether a call brings web content into the conversation: a web tool, or a command or script
    that holds an http(s) URL."""
    if tool_name in WEB_TOOLS or tool_name.startswith(WEB_TOOL_PREFIX):
        return True
    if tool_name not in COMMAND_TOOLS or args is None:
        return False
    try:
        text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(args)
    return bool(_URL.search(text))


def _session_of(session_id: Any, task_id: Any) -> str:
    """The conversation a tool call belongs to. A call with no session id (a tool that execute_code
    runs for a script) is matched through its task id; failing that, the task id itself is the key."""
    session_id, task_id = str(session_id or ""), str(task_id or "")
    with _lock:
        if session_id:
            if task_id:
                _task_sessions[task_id] = session_id
                _task_sessions.move_to_end(task_id)
                while len(_task_sessions) > MAX_WEB_SESSIONS:
                    _task_sessions.popitem(last=False)
            return session_id
        if task_id:
            return _task_sessions.get(task_id) or f"task:{task_id}"
    return ""


def _mark_web(session_id: str) -> None:
    with _lock:
        _web_sessions[session_id] = None
        _web_sessions.move_to_end(session_id)
        while len(_web_sessions) > MAX_WEB_SESSIONS:
            _web_sessions.popitem(last=False)


def _web_in_history(history: Any) -> bool:
    """Whether Hermes's message list holds a result of a tool that brings web content."""
    names: dict[str, str] = {}
    for message in history if isinstance(history, list) else []:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                fn = call.get("function") or {}
                name = str(fn.get("name") or call.get("name") or "")
                names[str(call.get("id"))] = name
                if name in COMMAND_TOOLS and _brings_web(name, fn.get("arguments")):
                    return True
        if message.get("role") == "tool":
            name = str(message.get("name") or names.get(str(message.get("tool_call_id")), ""))
            if _brings_web(name):
                return True
    return False


def guard_mode() -> str:
    """tool_guard: ask (default), block or off. A boolean is read as ask (true) or off (false)."""
    value = _setting("tool_guard", "ask")
    if isinstance(value, bool):
        return "ask" if value else "off"
    value = str(value).strip().lower()
    return value if value in ("ask", "block", "off") else "ask"


def _hermes_home() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - outside Hermes: no folder is free
        return None


def _fetched_folder(tool_name: str, args: Any) -> bool:
    """Whether a read_file / search_files call targets a folder where Hermes keeps fetched content.
    A relative path, or one that leaves the folder (.., a link), does not count."""
    path = (args or {}).get("path") if isinstance(args, dict) else None
    home = _hermes_home()
    if not isinstance(path, str) or not path.strip() or home is None:
        return False
    target = Path(path.strip()).expanduser()
    if not target.is_absolute():
        return False
    real = os.path.normcase(os.path.realpath(target))
    for parts in PATH_READ_TOOLS.get(tool_name, ()):
        folder = os.path.normcase(os.path.realpath(home.joinpath(*parts)))
        if real == folder or real.startswith(folder.rstrip(os.sep) + os.sep):
            return True
    return False


def _tool_guard(tool_name: str, session_id: str, args: Any = None) -> Optional[dict]:
    if tool_name in READ_ONLY_TOOLS:
        return None
    mode = guard_mode()
    if mode == "off":
        return None
    with _lock:
        if session_id not in _web_sessions:
            return None
    if tool_name in PATH_READ_TOOLS and _fetched_folder(tool_name, args):
        return None
    if mode == "block":
        return {"action": "block", "message": GUARD_BLOCK}
    what = tool_name or "this tool"
    if tool_name in PATH_READ_TOOLS:
        # The path comes from the model, maybe from a page: no control characters in the prompt.
        path = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str((args or {}).get("path") or "the current folder"))[:300]
        what = f"{tool_name} on {path}"
    return {"action": "approve", "message": GUARD_ASK.format(tool=what),
            "rule_key": f"webgraft:tool_guard:{tool_name}"}


def _call_key(session_id: str, tool_call_id: Any) -> str:
    return f"{session_id}\x00{tool_call_id or ''}"


def _guard_tool(tool_name: str = "", args: Any = None, session_id: str = "", tool_call_id: Any = None,
                task_id: Any = None, **_: Any) -> Optional[dict]:
    if tool_name != "web_extract":
        conversation = _session_of(session_id, task_id)
        if tool_name == "delegate_task":
            with _lock:
                _delegations[_call_key(conversation, tool_call_id)] = _web_reads
                while len(_delegations) > MAX_SESSIONS * 4:
                    _delegations.popitem(last=False)
        return _tool_guard(tool_name, conversation, args)
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
        # The first item as the model sent it (string or object): Hermes reads it the same way.
        directive = {"action": "modify", "args": {"urls": _items(args)[:1]}}
    return directive


EMPTY_SEARCH_NOTE = ("This search returned no result. A self-hosted search backend also answers nothing when "
                     "the engines behind it rate-limit it, which several searches sent at once trigger. "
                     "An empty search does not show that something does not exist: search again with one "
                     "query at a time and other words, or open a page that lists it.")
# Said in the failed result itself: a small model given only the error tends to answer from the
# search snippets it already holds, although the skill tells it to open the next result.
UNREAD_NOTE = ("This page could not be read (see its error). Search snippets are not sources: open the next "
               "relevant search result with web_extract, one URL per call, before answering. One failure "
               "never ends the research. If the user asked for this page itself, say that it could not be read.")


def _annotate(tool_name: str = "", result: Any = None, session_id: str = "", tool_call_id: Any = None,
              **_: Any) -> Optional[str]:
    """Adds a webgraft_note to three tool results: a web_extract call whose other URLs _guard_tool
    left out (they are listed), a web_extract call that read no page (open the next result), and a
    web_search that found nothing (an empty result is not an absence)."""
    if tool_name == "web_extract":
        with _lock:
            held = _held.pop(_call_key(session_id, tool_call_id), None)
        notes = []
        if held:
            notes.append("webgraft reads one URL per web_extract call, so each page fits in the context. "
                         "This call read only the first URL. Not opened: " + ", ".join(held))
        if _nothing_read(result):
            notes.append(UNREAD_NOTE)
        if not notes:
            return None
        note = " ".join(notes)
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


def _nothing_read(result: Any) -> bool:
    """True when every page of a web_extract result came back with an error and no text."""
    data = _json(result)
    pages = data.get("results") if isinstance(data, dict) else None
    if not isinstance(pages, list) or not pages:
        return False
    return all(isinstance(p, dict) and p.get("error") and not str(p.get("content") or "").strip() for p in pages)


def _json(result: Any) -> Any:
    if not isinstance(result, str):
        return None
    try:
        return json.loads(result)
    except ValueError:
        return None


def _record_tool(tool_name: str = "", result: Any = None, session_id: str = "", args: Any = None,
                 task_id: Any = None, tool_call_id: Any = None, **_: Any) -> None:
    global _web_reads
    conversation = _session_of(session_id, task_id)
    if _brings_web(tool_name, args):
        with _lock:
            _web_reads += 1
        _mark_web(conversation)
    elif tool_name == "delegate_task":
        with _lock:
            start = _delegations.pop(_call_key(conversation, tool_call_id), None)
            subagent_read_web = start is None or _web_reads > start
        if subagent_read_web:  # unknown start (another process, a lost record) counts as read
            _mark_web(conversation)
    if tool_name not in ("web_search", "web_extract"):
        return
    pages = _pages(result) if tool_name == "web_extract" else []
    with _lock:
        turn = _turns.setdefault(session_id, {"opened": [], "searched": False, "snippets": ""})
        _turns.move_to_end(session_id)
        while len(_turns) > MAX_SESSIONS:
            _turns.popitem(last=False)
        if tool_name == "web_search":
            turn["searched"] = True
            turn["snippets"] = (turn["snippets"] + "\n" + _snippets(result))[-20_000:]
        for page in pages:
            _remember(session_id, page["url"], page["title"], page["content"])
            if page["url"] not in turn["opened"]:
                turn["opened"].append(page["url"])


def _footer_on() -> bool:
    """Whether webgraft adds its footer to replies (show_sources). Hermes keeps the first non-empty
    rewrite of a reply when several plugins register transform_llm_output, and gives a plugin no
    documented way to know which others do: a user who runs another one picks with this setting."""
    return compat.sources_mode(_setting("show_sources", "always")) != "never"


def _append_footer(response_text: str = "", session_id: str = "", **_: Any) -> Optional[str]:
    with _lock:
        turn = _turns.pop(session_id, None)
        known = dict(_sessions.get(session_id, {}))
    if not response_text or (turn is None and not known):
        return None  # no web tool this turn and no page read before: nothing to report
    if not _footer_on():
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


def _frontmatter(path: Path) -> dict:
    """Top-level scalar keys of a SKILL.md front matter (``key: value`` lines between the ``---`` markers)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    m = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
    meta: dict = {}
    for line in (m.group(1).splitlines() if m else []):
        kv = re.match(r"([A-Za-z_][\w-]*):\s*(.*)$", line)
        if kv and not line.startswith((" ", "\t")):
            value = kv.group(2).strip()
            if value.startswith("[") and value.endswith("]"):  # flow list: platforms: [linux, macos, windows]
                items = [_unquote(item) for item in value[1:-1].split(",")]
                meta[kv.group(1)] = [item for item in items if item]
            elif _unquote(value) != "":
                meta[kv.group(1)] = _unquote(value)
    return meta


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


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
                meta = _frontmatter(child / "SKILL.md")
                # Resolvable as webgraft:<name> only; the description shows in skills_list and the prompt.
                ctx.register_skill(child.name, child / "SKILL.md", description=meta.get("description", ""), frontmatter=meta)
    if hasattr(ctx, "register_cli_command"):
        from . import cli
        cli.register(ctx)
    # The extraction backend needs Hermes's provider base class; hooks work without it.
    try:
        from .provider import WebGraftProvider
    except ImportError:
        return
    ctx.register_web_search_provider(WebGraftProvider())
