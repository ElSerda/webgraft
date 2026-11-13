"""Proof for the hook webgraft asks Hermes to add: pre_keyless_rescue.

Runs Hermes's own rescue code (tools/web_tools.py, web_tools_extract.py, web_tools_rescue.py), with
the proposed hook added in this process only, and webgraft's callback (rescue.pre_keyless_rescue_webgraft).
The keyless ring is replaced by a recorder: nothing leaves this machine.

    <Hermes's python> tests/test_hermes_rescue_hook_proof.py

Hermes's python is the one its gateway runs (HERMES_HOME/tools/python-*/python.exe on Windows). The
Hermes source folder comes from HERMES_AGENT_DIR, or from `hermes --version` ("Install directory").
Under pytest the tests run only when HERMES_AGENT_DIR is set (webgraft's own test venv has no Hermes).

Shows, for a search whose backend is down and a page refused for its certificate:
  today                        the query and the URL go to the ring, nobody asked
  proposed hook, nobody there  Hermes's own approval gate fails closed: nothing leaves
  proposed hook, user denies   nothing leaves
  proposed hook, user approves the rescue runs, once
  keyless_rescue: block / off  nothing leaves, no prompt / as today
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
QUERY = "symptoms of a rare disease my doctor mentioned"  # what a private search looks like
URL = "https://self-signed.badssl.com/"
SEARXNG_DOWN = "Could not reach SearXNG at http://127.0.0.1:9: [Errno 10061] connection refused"
CERT = "could not download: ConnectError: certificate verify failed: self-signed certificate"


# ---------------------------------------------------------------- the code proposed for Hermes
# For tools/web_tools_rescue.py. _rescue_search and _rescue_extract call it first (see apply_proposal).

def proposed_rescue_veto(tool_name: str, backend: str, error: str, *, urls: Optional[list] = None,
                         query: Optional[str] = None, hooks: Optional[Callable[..., list]] = None) -> Optional[str]:
    """``pre_keyless_rescue`` plugin hook, called before anything leaves for the keyless ring.

    Same directive shape and precedence as ``pre_tool_call`` (block > approve > none) and the same
    human gate (request_tool_approval: once / session / always / deny; fail closed when nobody can
    answer). Returns None to let the rescue run, else the reason the failed call keeps.
    """
    from hermes_cli.plugins import _PreToolCallDirective, _resolve_block_from_details
    if hooks is None:
        from hermes_cli.lifecycle import invoke_hook as hooks
    try:
        results = hooks("pre_keyless_rescue", tool_name=tool_name, backend=backend, error=error,
                        urls=list(urls or []), query=query)
    except Exception as exc:  # a broken hook keeps the data home: fail closed, like the approval gate
        return f"keyless rescue hook failed: {exc}"
    directives = [r for r in results or [] if isinstance(r, dict)]
    block = next((r for r in directives if r.get("action") == "block" and r.get("message")), None)
    approve = next((r for r in directives if r.get("action") == "approve"), None)
    if block:
        details = _PreToolCallDirective(action="block", message=block["message"])
    elif approve:
        details = _PreToolCallDirective(action="approve", message=approve.get("message"),
                                        rule_key=approve.get("rule_key"))
    else:
        return None
    return _resolve_block_from_details(details, tool_name)


# ---------------------------------------------------------------- running it inside Hermes's code


def hermes_dir() -> Path:
    if os.environ.get("HERMES_AGENT_DIR"):
        return Path(os.environ["HERMES_AGENT_DIR"])
    out = subprocess.run(["hermes", "--version"], capture_output=True, text=True, timeout=60).stdout
    found = re.search(r"Install directory:\s*(.+)", out)
    if not found:
        raise RuntimeError("Hermes's source folder not found: set HERMES_AGENT_DIR")
    return Path(found.group(1).strip())


class Proof:
    """Hermes's rescue modules, a recording ring, and webgraft's callback as the only plugin hook."""

    def __init__(self) -> None:
        sys.path.insert(0, str(hermes_dir()))
        sys.path.insert(0, str(HERE))
        import plugins.web.keyless_mcp as ring
        import tools.approval as approval
        import tools.web_tools as web
        import tools.web_tools_extract as extract
        import tools.web_tools_rescue as rescue_code
        from _pkg import load

        load()
        from webgraft import rescue

        self.ring, self.approval, self.web, self.extract, self.code, self.webgraft = (
            ring, approval, web, extract, rescue_code, rescue)
        self.sent: list[tuple[str, Any]] = []
        self.prompts: list[str] = []
        self.saved = {(m, n): getattr(m, n) for m, n in [
            (ring, "search_with_failover"), (ring, "extract_with_failover"), (rescue_code, "_keyless_rescue_enabled"),
            (web, "_rescue_search"), (extract, "_rescue_extract"), (approval, "request_tool_approval")]}
        ring.search_with_failover = self._ring_search
        ring.extract_with_failover = self._ring_extract
        rescue_code._keyless_rescue_enabled = lambda: True  # web.keyless_rescue at its default

    def _ring_search(self, provider_name: str, query: str, limit: int) -> dict:
        self.sent.append(("search", query))
        return {"success": True, "data": {"web": [{"title": "t", "url": "https://x.example/"}], "served_by": "exa"}}

    def _ring_extract(self, provider_name: str, urls: list) -> list:
        self.sent += [("page", u) for u in urls]
        return [{"url": u, "title": u, "content": "page text, fetched by the vendor"} for u in urls]

    def close(self) -> None:
        for (module, name), value in self.saved.items():
            setattr(module, name, value)

    # -- the proposal, applied in this process only
    def apply_proposal(self, setting: str, answer: Optional[bool]) -> None:
        """answer None: Hermes's real approval gate, with nobody to answer. True / False: the user's
        answer at Hermes's prompt (the prompt itself is Hermes's UI, not run here)."""
        self.webgraft.setting = lambda key, default: setting if key == "keyless_rescue" else default
        hooks = lambda name, **kw: [self.webgraft.pre_keyless_rescue_webgraft(**kw)]  # noqa: E731
        search, extract = self.saved[(self.web, "_rescue_search")], self.saved[(self.extract, "_rescue_extract")]
        code = self.code

        def rescue_search(provider_name, original_error, query, limit):
            veto = proposed_rescue_veto("web_search", provider_name, original_error, query=query, hooks=hooks)
            if veto:
                return {"success": False, "error": f"{original_error or 'search failed'} (keyless rescue not used: {veto})"}
            return search(provider_name, original_error, query, limit)

        def rescue_extract(provider_name, urls, results):
            failing = [r for r in results if not code._policy_blocked_result(r)]
            if failing:
                veto = proposed_rescue_veto("web_extract", provider_name, str(failing[0].get("error") or ""),
                                            urls=[r.get("url") for r in failing], hooks=hooks)
                if veto:
                    return [r if code._policy_blocked_result(r) else
                            {**r, "error": f"{r.get('error')} (keyless rescue not used: {veto})"} for r in results]
            return extract(provider_name, urls, results)

        self.web._rescue_search, self.extract._rescue_extract = rescue_search, rescue_extract
        if answer is not None:

            def answered(tool_name, reason, **_):
                self.prompts.append(reason)
                return {"approved": answer, "message": "BLOCKED: Action denied by user."}

            self.approval.request_tool_approval = answered

    # -- one failed search and one failed page, through Hermes's own paths
    def run(self) -> dict:
        self.sent.clear()

        class SearxngDown:
            name = "searxng"

            def search(self, query, limit):
                return {"success": False, "error": SEARXNG_DOWN}

        class CertRefused:
            name = "webgraft"

            async def extract(self, urls, format=None):
                return [{"url": u, "title": "", "content": "", "error": CERT} for u in urls]

        search = self.web._memoized_search(SearxngDown(), QUERY, 5)
        pages, _ = asyncio.run(self.extract._fetch_with_rescue(CertRefused(), [URL], None))
        return {"sent": list(self.sent), "search": search, "page": pages[0]}


SCENARIOS = [
    ("today (no hook)", None, "unused"),
    ("hook, keyless_rescue: ask, nobody to answer", "ask", None),
    ("hook, keyless_rescue: ask, user denies", "ask", False),
    ("hook, keyless_rescue: ask, user approves", "ask", True),
    ("hook, keyless_rescue: block", "block", None),
    ("hook, keyless_rescue: off", "off", None),
]


def run_scenario(setting: Optional[str], answer: Any) -> tuple[dict, list[str]]:
    proof = Proof()
    try:
        if setting is not None:
            proof.apply_proposal(setting, answer if answer != "unused" else None)
        return proof.run(), list(proof.prompts)
    finally:
        proof.close()


def main() -> int:
    print(f"Hermes source: {hermes_dir()}")
    print("The keyless ring is a recorder here: nothing leaves this machine.\n")
    for label, setting, answer in SCENARIOS:
        out, prompts = run_scenario(setting, answer)
        sent = ", ".join(f"{kind}: {what[:50]}" for kind, what in out["sent"]) or "nothing"
        print(f"== {label}\n   sent to the keyless ring: {sent}")
        if prompts:
            print(f"   approval prompt (first): {prompts[0][:160]}...")
        if not out["search"].get("success"):
            print(f"   web_search result: {str(out['search'].get('error'))[:160]}")
        print(f"   web_extract result: {str(out['page'].get('error') or 'page served by the vendor')[:160]}\n")
    return 0


# ---------------------------------------------------------------- the same, as tests

try:
    import pytest
except ImportError:  # Hermes's interpreter: run as a script
    pytest = None

if pytest is not None:
    requires_hermes = pytest.mark.skipif(not os.environ.get("HERMES_AGENT_DIR"),
                                         reason="set HERMES_AGENT_DIR and run with Hermes's interpreter")

    @requires_hermes
    def test_today_the_query_and_the_url_leave_without_asking():
        out, prompts = run_scenario(None, "unused")
        assert ("search", out["sent"][0][1]) == out["sent"][0] and QUERY in out["sent"][0][1]
        assert ("page", URL) in out["sent"] and not prompts

    @requires_hermes
    @pytest.mark.parametrize("setting,answer", [("ask", None), ("ask", False), ("block", None)])
    def test_with_the_hook_nothing_leaves_unless_the_user_approves(setting, answer):
        out, _ = run_scenario(setting, answer)
        assert out["sent"] == []
        assert "keyless rescue not used" in out["search"]["error"] and "keyless rescue not used" in out["page"]["error"]

    @requires_hermes
    def test_with_the_hook_an_approved_rescue_runs_once():
        out, prompts = run_scenario("ask", True)
        assert [kind for kind, _ in out["sent"]] == ["search", "page"] and len(prompts) == 2
        assert "searxng" in prompts[0] and URL in prompts[1]


if __name__ == "__main__":
    sys.exit(main())
