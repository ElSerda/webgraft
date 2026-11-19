#!/usr/bin/env python3
"""Core-contract drift detector for webgraft.

Hermes's catalog CI validates a plugin against the core AT PIN TIME, and the
core's own suite protects its hook seams generically. Nothing checks that the
specific core behaviors webgraft relies on still hold AFTER a `hermes update`.
This script does: every contract from the part-by-part audit
(F:/GPU/webgraft-qa/core-interface-audit.md) is a source marker in the installed
core; a missing marker means the seam moved — audit that contract before
trusting the plugin on this core.

Run after every `hermes update` (and before a re-pin):

    python tools/core_contracts.py
    python tools/core_contracts.py --core <checkout>   # e.g. the upstream main
                                                     # clone kept by catalog_ci.sh

Stdlib only. Source markers, not behavior: the suite (tools/check.py) and the
live probes cover behavior; this covers "did the core change under us".
Exit 0 = every contract still present, 1 = at least one DRIFT.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# (contract, why webgraft relies on it, core file, patterns that must ALL match)
CONTRACTS = [
    ("hook-crash-semantics",
     "pre_tool_call fails CLOSED (block on raise/timeout), every other hook fails OPEN "
     "(skip) — a crash in post_tool_call would leave the conversation unmarked, guard off",
     "hermes_cli/plugins_dispatch.py",
     [r"_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS", r'"pre_tool_call"']),
    ("safe-mode-loads-no-plugins",
     "under --safe-mode no plugin is discovered at all (guard, footer AND extract provider "
     "absent) — the README says so",
     "hermes_cli/plugins.py",
     [r"Safe workers never discover"]),
    ("post-tool-payload",
     "post_tool_call receives tool_name/args/result/session_id/conversation_id — the mark "
     "and the card record read them",
     "model_tools.py",
     [r"post_tool_call", r"conversation_id"]),
    ("transform-seams",
     "transform_tool_result (first string wins) annotates pages; transform_llm_output "
     "(once per turn, before persistence) appends the footer",
     "hermes_cli/plugins_dispatch.py",
     [r"transform_tool_result", r"transform_llm_output"]),
    ("footer-survives-finalizer",
     "transform_llm_output runs in the turn finalizer before the response is persisted",
     "agent/turn_finalizer.py",
     [r"transform_llm_output"]),
    ("pre-llm-user-injection",
     "pre_llm_call's returned string is injected into the USER message (never the system "
     "prompt) — the reminder rides that",
     "agent/turn_context.py",
     [r"pre_llm_call"]),
    ("web-trim-keeps-policy",
     "the extract trim keeps url/title/content/error/blocked_by_policy and drops "
     "cached/metadata — webgraft infers 'rescued' from its own refusal notes instead",
     "tools/web_tools_truncate.py",
     [r"blocked_by_policy"]),
    ("rescue-policy-refusals",
     "policy refusals never go to the keyless rescue ring; the provider's url/result "
     "order parity is what keeps the batch from being replaced wholesale",
     "tools/web_tools_rescue.py",
     [r"_policy_blocked_result"]),
    ("kanban-finite-turn",
     "a card's turn is admitted finite in-process — nobody can answer its approval "
     "prompts, the reason tool_guard_kanban exists",
     "gateway/session_kanban.py",
     [r"Finite like the"]),
    ("kanban-instant-approval",
     "finite turns resolve approvals from approvals.single_query_mode (deny by default)",
     "tools/approval.py",
     [r"single_query_mode"]),
    ("kanban-prompt-format",
     "the card prompt sections webgraft parses (parent results, recent work, metadata, "
     "em dash) — drift means 'unreadable' and the worker stays guarded (fail-safe)",
     "hermes_cli/kanban_db_context.py",
     [r"## Parent task results", r"## Recent work by @", "\u2014", r"_metadata_"]),
    ("cli-command-signature",
     "register_cli_command(name, help, setup_fn, handler_fn=None, description='') — "
     "the webgraft subcommand wires into the local CLI parser with it",
     "hermes_cli/plugins_context.py",
     [r"def register_cli_command", r"handler_fn", r"description"]),
    ("provider-registration",
     "register_web_search_provider exists — ONE registration serves both backends: "
     "WebGraftProvider implements the unified interface, web.extract_backend resolves "
     "against the same registry",
     "hermes_cli/plugins_context.py",
     [r"def register_web_search_provider"]),
    ("provider-extract-interface",
     "WebSearchProvider carries supports_search/supports_extract/extract — the unified "
     "interface WebGraftProvider subclasses (there is no register_web_extract_provider)",
     "agent/web_search_provider.py",
     [r"class WebSearchProvider", r"def supports_extract", r"def extract"]),
]

# Informational: not a drift, a decision point.
RESCUE_HOOK_FILES = ["model_tools.py", "tools/web_tools_rescue.py",
                     "hermes_cli/plugins_dispatch.py"]


def core_dir() -> Path | None:
    """The installed core source, from `hermes --version`'s Install directory."""
    try:
        out = subprocess.run(["hermes", "--version"], capture_output=True, timeout=60).stdout
        text = out.decode("utf-8", errors="replace")
        m = re.search(r"Install directory:\s*(.+)", text)
        if m:
            p = Path(m.group(1).strip())
            if (p / "hermes_cli").is_dir():
                return p
    except (OSError, subprocess.TimeoutExpired):
        pass
    for cand in (Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "hermes-agent",
                 Path.home() / ".hermes" / "hermes-agent"):
        if (cand / "hermes_cli").is_dir():
            return cand
    return None


def main() -> int:
    core = None
    if "--core" in sys.argv:
        core = Path(sys.argv[sys.argv.index("--core") + 1])
        if not (core / "hermes_cli").is_dir():
            print(f"--core {core}: no hermes_cli/ there")
            return 1
    if core is None:
        core = core_dir()
    if core is None:
        print("core source not found (hermes --version gave no Install directory)")
        return 1
    head = ""
    try:
        head = subprocess.run(["git", "-C", str(core), "log", "-1", "--format=%h %cs"],
                              capture_output=True, timeout=30).stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    print(f"core: {core}" + (f"  ({head})" if head else ""))
    print(f"contracts: {len(CONTRACTS)} (source markers from the audit; behavior = tools/check.py + live)\n")

    drift = 0
    for name, why, rel, patterns in CONTRACTS:
        path = core / rel
        if not path.is_file():
            print(f"DRIFT  {name}: {rel} is gone")
            print(f"       relies on: {why}")
            drift += 1
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        missing, lines = [], []
        for pat in patterns:
            m = re.search(pat, text, re.S)
            if m:
                lines.append(text.count("\n", 0, m.start()) + 1)
            else:
                missing.append(pat)
        if missing:
            print(f"DRIFT  {name}: {rel} no longer matches {missing}")
            print(f"       relies on: {why}")
            drift += 1
        else:
            loc = ", ".join(str(n) for n in lines)
            print(f"ok     {name}  ({rel}:{loc})")

    # The rescue hook: merged or not decides webgraft's next move.
    merged = any((core / rel).is_file()
                 and "pre_keyless_rescue" in (core / rel).read_text(encoding="utf-8", errors="replace")
                 for rel in RESCUE_HOOK_FILES)
    print()
    if merged:
        print("info   pre_keyless_rescue IS in the core — PR #155917 merged: register the hook "
              "in rescue.py, drop the hold() workaround, re-verify _rescue_eligible (#155901).")
    else:
        print("info   pre_keyless_rescue not in the core — PR #155917 unmerged; "
              "the hold() workaround stays, the callback shim reads both payload shapes.")

    print()
    if drift:
        print(f"{drift} contract(s) DRIFTED — audit them against the new core "
              "(F:/GPU/webgraft-qa/core-interface-audit.md) before trusting the plugin here.")
        return 1
    print("every audited contract still holds on this core.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
