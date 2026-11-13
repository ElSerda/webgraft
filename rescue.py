"""Hermes's keyless rescue, held for the user's approval (setting keyless_rescue: ask, block or off).

When every page of a web_extract call fails with no HTTP answer from the site (a certificate webgraft
refused, a connection that failed, a timeout), Hermes re-fetches the URLs through outside services
(its keyless ring: Exa, Parallel, Firecrawl, Keenable), with web.keyless_rescue on by default. It
does the same for a search when the search backend fails. A user who chose a local setup is not
asked.

Pages: Hermes never rescues a page refused by policy (blocked_by_policy, tools/web_tools_rescue.py),
so webgraft returns its failed pages that way, with its own reason. With "ask", the model is told
it can call web_extract on the page again: that call goes to Hermes's approval prompt first, and
only an approved call lets the failure through to the rescue.

Searches: webgraft is not the search backend and cannot know before a search that the backend
will fail, so the first rescued search after an outage still goes out (Hermes says so in its
result). From then on, until a search works again through the backend or SEARCH_HOLD_SECONDS pass,
each web_search goes to the approval prompt ("ask") or is refused ("block").
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable
from urllib.parse import urldefrag, urlsplit

setting: Callable[[str, Any], Any] = lambda key, default: default  # noqa: E731 - set by register()

HOLD_SECONDS = 1800  # a page kept from the rescue can be asked for again during this time (the footer says how)
APPROVAL_SECONDS = 600  # an approval prompt the user is still answering
SEARCH_HOLD_SECONDS = 600
MAX_PAGES = 500
RULE_KEY = "webgraft:keyless_rescue"
SOURCE = "webgraft"
VENDORS = "Exa, Parallel, Firecrawl or Keenable"  # Hermes's keyless ring (plugins/web/keyless_mcp.py)

_lock = threading.Lock()
_held: "OrderedDict[str, tuple[float, str]]" = OrderedDict()  # page -> (when, why webgraft could not read it)
_approved: "OrderedDict[str, float]" = OrderedDict()  # page -> when its approval prompt was shown
_search_down: tuple[float, str, str] | None = None  # (when, your backend, the vendor that served it)


def mode() -> str:
    """ask (default), block or off. A boolean is read as ask (true) or off (false)."""
    value = setting("keyless_rescue", "ask")
    if isinstance(value, bool):
        return "ask" if value else "off"
    value = str(value).strip().lower()
    return value if value in ("ask", "block", "off") else "ask"


def hermes_rescues() -> bool:
    """Hermes's web.keyless_rescue (default on). Unreadable config: assume on, and hold."""
    try:
        from hermes_cli.config import load_config_readonly
        web = (load_config_readonly() or {}).get("web") or {}
        return bool(web.get("keyless_rescue", True)) if isinstance(web, dict) else True
    except Exception:  # noqa: BLE001 - outside Hermes, or a config problem
        return True


def extract_deadline() -> float:
    """Seconds webgraft gives itself for one web_extract call: under Hermes's web.extract_timeout
    (120 s by default), because Hermes rescues a call that times out, whatever it returns. 0: none."""
    try:
        from hermes_cli.config import load_config_readonly
        web = (load_config_readonly() or {}).get("web") or {}
        cap = float(web.get("extract_timeout", 120)) if isinstance(web, dict) else 120.0
    except Exception:  # noqa: BLE001
        cap = 120.0
    return max(cap - 5, 1.0) if cap > 0 else 0.0


def _key(url: Any) -> str:
    return urldefrag(str(url or ""))[0].rstrip("/")


def _origin_error(error: Any) -> bool:
    """"HTTP <code> from origin...": the site's own answer, which Hermes never rescues. webgraft writes
    it that way (extract._origin); Hermes's test for it is not part of its public plugin API."""
    if not isinstance(error, str) or not error.startswith("HTTP "):
        return False
    code, _, rest = error[5:].partition(" ")
    return len(code) == 3 and code.isdigit() and rest.startswith("from origin")


def _rescued_by_hermes(results: list[dict]) -> bool:
    """Hermes's condition for a whole-batch rescue (tools/web_tools_extract.py)."""
    errors = [r.get("error") for r in results]
    return bool(errors) and all(errors) and not any(_origin_error(e) for e in errors)


def take_approved(urls: list[str]) -> dict[str, str]:
    """The URLs of this call the user approved for the rescue, with why webgraft could not read them.
    Each approval serves one call."""
    now, out = time.monotonic(), {}
    with _lock:
        for url in urls:
            key = _key(url)
            asked, held = _approved.get(key), _held.get(key)
            if asked is not None and now - asked < APPROVAL_SECONDS and held:
                out[url] = held[1]
                _approved.pop(key, None)
    return out


def hold(results: list[dict], approved: set[str] | None = None) -> list[dict]:
    """The provider's results, with the pages Hermes would rescue marked as refused by webgraft's
    policy, unless the user approved them. Unchanged when nothing would be rescued."""
    current = mode()
    approved = approved or set()
    if current == "off" or not _rescued_by_hermes(results) or not hermes_rescues():
        return results
    now, out = time.monotonic(), []
    for r in results:
        url = str(r.get("url") or "")
        requested = str((r.get("metadata") or {}).get("sourceURL") or url) if isinstance(r.get("metadata"), dict) else url
        if requested in approved or url in approved:
            out.append(r)
            continue
        why = str(r.get("error") or "")
        with _lock:
            for u in {url, requested}:
                if u:
                    _held[_key(u)] = (now, why)
                    _held.move_to_end(_key(u))
            while len(_held) > MAX_PAGES:
                _held.popitem(last=False)
        try:
            host = (urlsplit(requested).hostname or "").lower()
        except ValueError:
            host = ""
        out.append({**r, "error": f"{why} [kept on this machine: not sent to Hermes's keyless rescue]"[:400],
                    "blocked_by_policy": {"host": host, "rule": f"keyless_rescue: {current}", "source": SOURCE}})
    return out


def held_reason(url: str) -> str:
    with _lock:
        seen = _held.get(_key(url))
    return seen[1] if seen and time.monotonic() - seen[0] < HOLD_SECONDS else ""


def ask_for(urls: list[str]) -> None:
    """The approval prompt for these pages is shown now: if the call then runs, it was approved."""
    now = time.monotonic()
    with _lock:
        for url in urls:
            _approved[_key(url)] = now
            _approved.move_to_end(_key(url))
        while len(_approved) > MAX_PAGES:
            _approved.popitem(last=False)


def kept(result_page: Any) -> str:
    """"ask" or "block" when this web_extract result entry is a page webgraft kept from the rescue."""
    policy = result_page.get("blocked_by_policy") if isinstance(result_page, dict) else None
    if isinstance(policy, dict) and policy.get("source") == SOURCE:
        rule = str(policy.get("rule") or "")
        return "block" if rule.endswith("block") else "ask"
    return ""


def note_search(rescue: tuple[str, str] | None, success: bool) -> None:
    """After a web_search: a rescued one starts the hold, one served by your backend ends it."""
    global _search_down
    with _lock:
        if rescue:
            _search_down = (time.monotonic(), rescue[1], rescue[0])
        elif success:
            _search_down = None


def search_down() -> tuple[int, str, str] | None:
    """(seconds since, your backend, vendor) while searches are held, else None."""
    with _lock:
        down = _search_down
    if down is None:
        return None
    age = time.monotonic() - down[0]
    return (int(age), down[1], down[2]) if age < SEARCH_HOLD_SECONDS else None


def clean(text: Any, limit: int = 200) -> str:
    """Model- or page-supplied text for an approval prompt: printable, one line, bounded."""
    out = " ".join("".join(ch if ch.isprintable() else " " for ch in str(text or "")[: limit * 2]).split())
    return out[:limit]


# ---------------------------------------------------------------- the hook Hermes is asked to add
#
# What webgraft does above is a workaround: it has to fake a policy refusal, it needs a second
# web_extract to ask, and it cannot hold the first search of an outage, since the rescue runs inside
# the tool, where no plugin hook is called. Hermes already asks for approval inside a tool (terminal
# does, for a dangerous command), so the simple fix is one hook, called right before anything leaves
# for the keyless ring, with pre_tool_call's directive shape and the same human gate:
#
#   pre_keyless_rescue(tool_name, backend, error, urls=[...] | query="...", **kwargs)
#     -> None                                        the rescue runs, as today
#     -> {"action": "block", "message"}              it does not; the call keeps its own failure
#     -> {"action": "approve", "message", "rule_key"} Hermes's approval prompt decides (fail closed)
#
# pre_keyless_rescue_webgraft is webgraft's callback for it, ready for the day Hermes calls it. Until
# then, Hermes's validator rejects a hook it does not know, so it is not registered: the proof runs it
# against Hermes's own rescue code (tests/test_hermes_rescue_hook_proof.py, Hermes's interpreter).

PROPOSED_HOOK = "pre_keyless_rescue"
HOOK_ASK_PAGES = ("webgraft could not read {pages} on this machine ({why}). Let Hermes's keyless rescue send "
                  "{it} to an outside service ({vendors})? That service fetches {that} from its own network, "
                  "without webgraft's certificate check.")
HOOK_ASK_SEARCH = ("webgraft: your search backend ({backend}) did not answer ({why}). Let Hermes's keyless rescue "
                   "send this search to an outside service ({vendors})? \"{query}\"")
HOOK_BLOCK = ("webgraft kept this on this machine (keyless_rescue: block): Hermes's keyless rescue would have "
              "sent it to an outside service.")


def pre_keyless_rescue_webgraft(tool_name: str = "", backend: str = "", error: str = "", urls: Any = None,
                                query: Any = None, **_: Any) -> dict | None:
    """webgraft's answer when Hermes is about to rescue a failed web_extract or web_search, by the
    keyless_rescue setting: off lets it run, block refuses it, ask sends it to the approval prompt."""
    current = mode()
    if current == "off" or tool_name not in ("web_extract", "web_search"):
        return None
    if current == "block":
        return {"action": "block", "message": HOOK_BLOCK}
    why = clean(error, 160) or "no answer"
    if tool_name == "web_search":
        message = HOOK_ASK_SEARCH.format(backend=clean(backend, 40) or "?", why=why, vendors=VENDORS,
                                         query=clean(query, 200))
    else:
        pages = [clean(u, 300) for u in (urls if isinstance(urls, (list, tuple)) else [urls]) if u][:5]
        one = len(pages) == 1
        message = HOOK_ASK_PAGES.format(pages=", ".join(pages) or "these pages", why=why, vendors=VENDORS,
                                        it="this URL" if one else "these URLs", that="the page" if one else "them")
    return {"action": "approve", "message": message, "rule_key": f"{RULE_KEY}:{tool_name}"}


def reset() -> None:
    global _search_down
    with _lock:
        _held.clear()
        _approved.clear()
        _search_down = None
