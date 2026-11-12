"""`hermes webgraft status` and `hermes webgraft setup`.

setup walks through what webgraft needs and writes the Hermes config:
  1. a search backend: SearXNG (your instance) or DuckDuckGo (built into Hermes, no key);
  2. web_extract on webgraft;
  3. optional Crawl4AI, in its own environment, driving the Chromium Hermes installs;
  4. optional local OCR for scanned PDFs (RapidOCR, no language model);
  5. auto-loading the bundled research skill;
  6. a live test: one real page, one 404.
Every step is shown before it runs. Nothing is installed without a yes, or the matching flag.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import stat
import subprocess
import sys

from . import browser, compat, extract

CRAWL4AI_REQUIREMENT = "crawl4ai>=0.9.1,<0.10"  # 0.9.0 and older need lxml 5: no Python 3.14 wheels
TEST_PAGE = "https://www.iana.org/help/example-domains"
SEARXNG_GUIDE = "https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search"
TEST_MISSING = "https://www.iana.org/webgraft-test-page-that-does-not-exist"


# ---------------------------------------------------------------- output, in Hermes's own style


def _hermes_out(name: str):
    """A helper from hermes_cli.cli_output (the documented wizard output module), or None."""
    try:
        import hermes_cli.cli_output as out
        return getattr(out, name)
    except (ImportError, AttributeError):
        return None


def _styled(text: str, code: str) -> str:
    """ANSI style when writing to a colour terminal, like Hermes (NO_COLOR turns it off)."""
    import os
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def header(text: str) -> None:
    # The cyan "◆" section header of `hermes setup`, drawn here: its helper is not a public API.
    try:
        "◆".encode(sys.stdout.encoding or "ascii")
        mark = "◆"
    except (UnicodeEncodeError, LookupError):
        mark = "=="
    print()
    print(_styled(f"{mark} {text}", "1;36"))


def info(text: str) -> None:
    fn = _hermes_out("print_info")
    fn(text) if fn else print(f"  {text}")


def ok(text: str) -> None:
    fn = _hermes_out("print_success")
    fn(text) if fn else print(f"OK {text}")


def warn(text: str) -> None:
    fn = _hermes_out("print_warning")
    fn(text) if fn else print(f"!  {text}")


def error(text: str) -> None:
    fn = _hermes_out("print_error")
    fn(text) if fn else print(f"X  {text}")


# ---------------------------------------------------------------- reading the current state


def _config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly
        return load_config_readonly() or {}
    except Exception:  # noqa: BLE001
        return {}


def _secret(name: str) -> str:
    try:
        from agent.secret_scope import get_secret
        return (get_secret(name) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def state() -> dict:
    web = (_config().get("web") or {})
    c4ai = extract.crawl4ai_python()
    return {
        "search_backend": web.get("search_backend") or web.get("backend") or "(auto)",
        "extract_backend": web.get("extract_backend") or web.get("backend") or "(auto)",
        "searxng_url": _secret("SEARXNG_URL"),
        "trafilatura": _has("trafilatura"),
        "anydoc": _has("anydoc"),
        "ddgs": _has("ddgs"),
        "crawl4ai_python": c4ai,
        "chromium": browser.recorded(),
        "ocr_python": extract.ocr_python(),
    }


def print_state(s: dict, title: str = "webgraft status") -> None:
    header(title)
    search = s["search_backend"]
    if search == "(auto)":
        warn("Search             (auto): Hermes picks a backend from your environment variables")
    else:
        ok(f"Search             {search}")
    if search == "searxng" or s["searxng_url"]:
        (ok if s["searxng_url"] else error)(f"SEARXNG_URL        {s['searxng_url'] or '(not set)'}")
    if s["extract_backend"] == "webgraft":
        ok("Page reading       webgraft")
    else:
        error(f"Page reading       {s['extract_backend']} (webgraft is not selected)")
    (ok if s["trafilatura"] else error)("trafilatura        " + ("installed" if s["trafilatura"] else "missing: run `hermes plugins enable webgraft`"))
    (ok if s["anydoc"] else warn)("Documents          " + ("anydoc (Hermes's converter) installed" if s["anydoc"]
                                                       else "Hermes's converter not installed yet: run setup"))
    if search == "ddgs":
        (ok if s["ddgs"] else error)("DuckDuckGo (ddgs)  " + ("installed" if s["ddgs"] else "missing"))
    if s["crawl4ai_python"]:
        ok("Crawl4AI           installed")
        if s["chromium"]:
            ok(f"Chromium           {s['chromium']}")
        else:
            error("Chromium           none recorded: run `hermes webgraft setup`")
    else:
        warn("Crawl4AI           not installed (pages are read with trafilatura only)")
    if s["ocr_python"]:
        ok("OCR                RapidOCR installed (scanned PDFs are read locally)")
    else:
        warn("OCR                not installed (scanned PDFs come back as errors)")
    others = compat.other_reply_rewriters("webgraft", getattr(_ctx, "has_plugin", None))
    if others:
        verb = "also rewrites" if len(others) == 1 else "also rewrite"
        warn(f"Reply rewriters    {', '.join(others)} {verb} replies, and Hermes keeps only one rewrite.")
        info("webgraft leaves the reply to them (show_sources: auto). Set it to `always` to show its")
        info("footer anyway, or turn the other plugin off: `hermes plugins disable <name>`.")


# ---------------------------------------------------------------- asking


def _ask(question: str, default: str, interactive: bool) -> str:
    """A typed answer through Hermes's documented wizard prompt (hermes_cli.cli_output.prompt)."""
    if not interactive:
        return default
    prompt = _hermes_out("prompt")
    if prompt is None:
        return input(f"{question} [{default}] ").strip() or default
    return prompt(question.strip(), default) or default


def _choose(title: str, options: list[tuple[str, str]], default: str, interactive: bool, description: str = "") -> str:
    """One option key, from a numbered radio list in the style of `hermes setup`.

    Enter keeps the default; an answer that is not a listed number keeps it too and says so.
    """
    keys = [k for k, _ in options]
    selected = keys.index(default) if default in keys else 0
    if not interactive:
        return keys[selected]
    print()
    print(_styled(f"  {title}", "33"))
    for line in description.splitlines() if description else []:
        print(_styled(f"  {line}", "2"))
    print(_styled("  Select by number, Enter to confirm.", "2"))
    print()
    try:
        "●○◆".encode(sys.stdout.encoding or "ascii")
        on, off = "(●)", "(○)"
    except (UnicodeEncodeError, LookupError):  # a console code page without these glyphs
        on, off = "(*)", "( )"
    for i, (_, label) in enumerate(options):
        mark = _styled(on, "32") if i == selected else off
        print(f"  {mark} {i + 1:>2}. {label}")
    print()
    answer = _ask("Choice", str(selected + 1), interactive).strip()
    if answer.isdigit() and 1 <= int(answer) <= len(keys):
        return keys[int(answer) - 1]
    if answer != str(selected + 1):
        warn(f"{answer!r} is not one of the numbers above; keeping {selected + 1}.")
    return keys[selected]


def _confirm(question: str, default: bool, interactive: bool, description: str = "") -> bool:
    options = [("yes", "Yes"), ("no", "No")]
    return _choose(question.strip(), options, "yes" if default else "no", interactive, description) == "yes"


# ---------------------------------------------------------------- what setup replaced, for uninstall

PLUGIN_NAME = "webgraft"
_ctx = None
RESTORED_KEYS = ("web.search_backend", "web.extract_backend")


def _data_dir():
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(PLUGIN_NAME)
    except Exception:  # noqa: BLE001
        return None


def _previous() -> dict:
    d = _data_dir()
    try:
        return json.loads((d / "previous.json").read_text(encoding="utf-8")) if d else {}
    except (OSError, ValueError):
        return {}


def _config_value(key: str):
    node = _config()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set(key: str, value: str) -> None:
    """Write a Hermes setting; the first time setup changes a web backend, keep the old value."""
    from hermes_cli.config import set_config_value
    if key in RESTORED_KEYS:
        previous = _previous()
        if key not in previous and (d := _data_dir()) is not None:
            previous[key] = _config_value(key)
            (d / "previous.json").write_text(json.dumps(previous, indent=1), encoding="utf-8")
    set_config_value(key, value)


# ---------------------------------------------------------------- steps


def _check_searxng(url: str) -> str:
    """An empty string when the instance answers JSON searches, else what is wrong."""
    import httpx
    try:
        r = httpx.get(f"{url.rstrip('/')}/search", params={"q": "webgraft test", "format": "json"}, timeout=20)
    except httpx.HTTPError as exc:
        return f"cannot reach {url}: {exc}"
    if r.status_code == 403:
        return "SearXNG refuses JSON: add `json` to `search.formats` in its settings.yml, then restart it"
    if r.status_code >= 400:
        return f"SearXNG answered HTTP {r.status_code}"
    try:
        data = r.json()
        n = len(data.get("results", []))
    except ValueError:
        return "SearXNG did not answer JSON (is this the instance URL?)"
    if n:
        return ""
    down = ", ".join(f"{e[0]} ({e[1]})" for e in data.get("unresponsive_engines") or [] if isinstance(e, (list, tuple)) and e)
    return "SearXNG answered, but with 0 results " + (f"(engines not answering: {down})" if down else "(are its engines enabled?)")


def step_search(args, s: dict, interactive: bool) -> None:
    header("1. Search backend")
    options = [
        ("ddgs", "DuckDuckGo: built into Hermes, no key, no server"),
        ("searxng", "SearXNG: your own instance (self-hosted, many engines)"),
        ("keep", f"Keep the current one ({s['search_backend']})"),
    ]
    if args.search:
        choice = args.search
    else:
        default = "searxng" if s["searxng_url"] else "ddgs"
        choice = _choose("Search backend for web_search", options, default, interactive,
                         "webgraft reads pages; search comes from one of these.")
    info(dict(options)[choice])
    if choice == "keep":
        return
    if choice == "searxng":
        url = args.searxng_url or _ask("   SearXNG URL?", s["searxng_url"] or "http://localhost:8080", interactive)
        while (problem := _check_searxng(url)):
            error(problem)
            info(f"No SearXNG yet? Hermes's guide runs one with Docker: {SEARXNG_GUIDE}")
            # Never leave search unset without saying so: DuckDuckGo is the safe default.
            fallback = "save" if args.searxng_url and not interactive else "ddgs"
            what = _choose("SearXNG is not answering. What now?", [
                ("ddgs", "Use DuckDuckGo instead (works now, no server)"),
                ("retry", "Enter another SearXNG URL"),
                ("save", "Save this URL anyway (start SearXNG later)"),
                ("keep", f"Keep the current search backend ({s['search_backend']})"),
            ], fallback, interactive, problem)
            if what == "retry":
                url = _ask("   SearXNG URL?", url, interactive)
                continue
            if what == "keep":
                warn(f"Search backend unchanged: {s['search_backend']}")
                return
            if what == "ddgs":
                warn("Using DuckDuckGo instead. Run setup again once SearXNG is up.")
                choice = "ddgs"
            break
        else:
            ok("SearXNG answers JSON searches")
        if choice == "searxng":
            from hermes_cli.config import save_env_value
            save_env_value("SEARXNG_URL", url.rstrip("/"))
            _set("web.search_backend", "searxng")
            return
    if choice == "ddgs":
        if not s["ddgs"]:
            info("DuckDuckGo needs Hermes's `ddgs` extra.")
            if not _confirm("Install Hermes's ddgs extra now?", True, interactive,
                            "DuckDuckGo search runs through the ddgs package, which Hermes installs on request."):
                return
            from pm import ensure_import
            if _install(lambda: ensure_import("ddgs"), "Hermes's ddgs extra") is None:
                return
        if s["search_backend"] == "ddgs":
            ok("Search backend already ddgs")
        else:
            _set("web.search_backend", "ddgs")
    else:
        warn(f"Unknown choice {choice!r}, search backend unchanged.")


def step_extract(s: dict) -> None:
    header("2. Page reading")
    info("web_extract reads pages and documents (PDF, Office, EPUB) on this machine with webgraft.")
    if s["extract_backend"] == "webgraft":
        ok("Page reading already on webgraft")
    else:
        _set("web.extract_backend", "webgraft")
    if s["anydoc"]:
        ok("Documents: Hermes's converter (anydoc) installed")
        return
    info("Documents use anydoc, the converter Hermes's read_file uses (its `doc-extract` extra).")
    try:
        extract.ensure_doc_reader()
        ok("Documents: Hermes's converter (anydoc) installed")
    except RuntimeError as exc:
        warn(f"{exc}. Hermes installs it on the first document read when lazy installs are on.")


def _find_chromium(interactive: bool):
    """Hermes's Chromium, installing it with `hermes pm install chromium` if needed and wanted."""
    path, source = browser.resolve()
    if path is not None:
        ok(f"Chromium from {source}: {path}")
        return path
    warn("No Chromium found (browser.executable_path, `hermes pm`, /etc/hermes, PATH).")
    if not _confirm("Install Hermes's Chromium with `hermes pm install chromium`?", True, interactive,
                    "Hermes's own pinned Chromium, the one its browser tool uses. Crawl4AI drives it."):
        return None
    hermes = browser.which("hermes")
    if hermes is None or subprocess.run([hermes, "pm", "install", "chromium"]).returncode != 0:
        error("`hermes pm install chromium` failed.")
        return None
    path, source = browser.resolve()
    if path is not None:
        ok(f"Chromium from {source}: {path}")
    return path


def step_crawl4ai(args, s: dict, interactive: bool) -> None:
    header("3. Crawl4AI (optional)")
    info("Opens each page in a headless browser as well. It recovers values that the main")
    info("text reader drops (spec tables) and pages built by scripts. It lives in its own")
    info("environment, apart from Hermes's packages (about 35 packages), and drives the")
    info("Chromium Hermes already installs: webgraft downloads no browser.")
    if s["crawl4ai_python"] and s["chromium"]:
        ok("Crawl4AI already installed")
        return
    wanted = args.crawl4ai if args.crawl4ai is not None else _confirm(
        "Install Crawl4AI?", True, interactive,
        "Crawl4AI: about 35 packages in their own environment; it uses Hermes's Chromium.\n"
        "It recovers spec tables and script-built pages that the main text reader misses.")
    if not wanted:
        warn("Skipped. Pages are read with trafilatura alone; run setup again to add it.")
        return
    if not s["crawl4ai_python"]:
        from pm import ensure_environment
        info("Preparing the environment (a few minutes the first time)...")
        if _install(lambda: ensure_environment(extract.CRAWL4AI_ENV, [CRAWL4AI_REQUIREMENT], explicit=True),
                    "the Crawl4AI environment") is None:
            return
    chromium = _find_chromium(interactive)
    if chromium is None:
        error("Crawl4AI stays off until a Chromium is available.")
        return
    browser.record(chromium)
    ok("Crawl4AI installed")


SKILL = "webgraft:webgraft"
OLD_SKILLS = ("webgraft:web-research",)  # names of earlier versions, dropped from skills.auto_load


OCR_WARMUP = "from rapidocr import RapidOCR; RapidOCR(); print('models ready')"


def _install(action, what: str):
    """Runs a Hermes PM install and returns its result, or None after reporting why it failed.
    pm.InstallError carries the cause and the remedy (a restart Hermes needs, `hermes pm doctor`)."""
    try:
        return action() or True
    except Exception as exc:  # noqa: BLE001 - pm.InstallError and friends: reported, setup goes on
        error(f"Could not install {what}: {exc}")
        return None


def step_ocr(args, s: dict, interactive: bool) -> None:
    header("4. OCR for scanned PDFs (optional)")
    info("A scanned PDF has no text, only page images. RapidOCR reads them on this machine, without a")
    info("language model (about 1 s per page). It lives in its own environment (about 250 MB, with")
    info("onnxruntime and pypdfium2) and downloads its models once (about 32 MB) from its official")
    info("repository on modelscope.cn, checking their SHA-256.")
    if s["ocr_python"]:
        ok("OCR already installed")
        return
    wanted = args.ocr if args.ocr is not None else _confirm(
        "Install local OCR?", True, interactive,
        "RapidOCR: about 250 MB in its own environment plus 32 MB of models, checked by SHA-256.\n"
        "Without it, scanned PDFs come back as errors that say which pages are scanned.")
    if not wanted:
        warn("Skipped. Run setup again to add it.")
        return
    from pm import ensure_environment
    info("Preparing the environment (a few minutes the first time)...")
    python = _install(lambda: ensure_environment(extract.OCR_ENV, extract.OCR_REQUIREMENTS, explicit=True),
                      "the OCR environment")
    if python is None:
        return
    info("Downloading the OCR models...")
    done = subprocess.run([str(python), "-I", "-c", OCR_WARMUP], capture_output=True, text=True,
                          env=extract.child_env(), cwd=extract.CHILD_CWD)
    if done.returncode == 0:
        ok("OCR installed")
    else:
        error("The OCR models did not download; OCR stays off until they do. Run setup again.")


def step_skill(args, interactive: bool) -> None:
    header("5. Research skill")
    info(f"{SKILL} tells the model to open pages instead of answering from search snippets,")
    info("one URL per web_extract call. Auto-loading puts it in every new session (about 600 tokens).")
    current = ((_config().get("skills") or {}).get("auto_load")) or []
    if not isinstance(current, list):
        current = []
    current = [name for name in current if name not in OLD_SKILLS]
    if SKILL in current:
        ok("Research skill already auto-loaded")
        return
    wanted = args.skill if args.skill is not None else _confirm(
        f"Auto-load {SKILL}?", True, interactive,
        "The skill makes the model open pages instead of answering from search snippets.\n"
        "It is added to every new session (about 600 tokens).")
    if not wanted:
        warn(f"Skipped. Load it by hand with /skill {SKILL}.")
        return
    import json
    _set("skills.auto_load", json.dumps([*current, SKILL]))


def step_test() -> bool:
    header("6. Test")
    page, missing = asyncio.run(extract.read_pages([TEST_PAGE, TEST_MISSING]))
    good = bool(page.content) and not page.error
    if good:
        ok(f"Read {TEST_PAGE}: {len(page.content)} characters via {page.engine}")
    else:
        error(f"Could not read {TEST_PAGE}: {page.error}")
    caught = bool(missing.error)
    if caught:
        ok(f"A missing page is reported as an error ({missing.error})")
    else:
        error("A missing page was NOT reported as an error")
    return good and caught


# ---------------------------------------------------------------- uninstall


def _rmtree(path) -> None:
    def clear_readonly(func, target, _exc):
        import os
        os.chmod(target, stat.S_IWRITE)
        func(target)
    shutil.rmtree(path, onexc=clear_readonly) if sys.version_info >= (3, 12) else shutil.rmtree(path, onerror=clear_readonly)


def uninstall(interactive: bool) -> int:
    """Undo what setup did, so `hermes plugins remove webgraft` leaves nothing behind."""
    from hermes_cli.config import set_config_value, unset_config_value

    header("Remove webgraft's settings and downloads")
    info("Run this before `hermes plugins remove webgraft`. It restores the web backends that")
    info("setup replaced, removes the research skill from auto-load, and deletes Crawl4AI's")
    info("and OCR environments. Hermes's Chromium stays: it belongs to Hermes.")
    if not _confirm("Remove webgraft's settings and downloads?", True, interactive,
                    "Restores web backends, drops the skill from auto-load, deletes the Crawl4AI environment."):
        warn("Nothing changed.")
        return 1
    previous = _previous()
    for key in RESTORED_KEYS:
        current = _config_value(key)
        if key in previous:
            old = previous[key]
            if old in (None, ""):
                unset_config_value(key)
            elif old != current:
                set_config_value(key, str(old))
            ok(f"{key} restored to {old or '(not set)'}")
        elif current == PLUGIN_NAME:
            unset_config_value(key)  # set by hand: webgraft will be gone, so do not leave it pointing there
            ok(f"{key} cleared (it pointed at webgraft)")
    auto = _config_value("skills.auto_load")
    if isinstance(auto, list) and any(name in auto for name in (SKILL, *OLD_SKILLS)):
        set_config_value("skills.auto_load", json.dumps([name for name in auto if name != SKILL and name not in OLD_SKILLS]))
    try:
        from hermes_constants import get_hermes_home
        env_dir = get_hermes_home() / "environments" / extract.CRAWL4AI_ENV
    except Exception:  # noqa: BLE001
        env_dir = None
    ocr_dir = env_dir.parent / extract.OCR_ENV if env_dir is not None else None
    if ocr_dir is not None and ocr_dir.exists():
        _rmtree(ocr_dir)
        ok(f"OCR environment deleted ({ocr_dir})")
    if env_dir is not None and env_dir.exists():
        _rmtree(env_dir)
        ok(f"Crawl4AI environment deleted ({env_dir})")
    d = _data_dir()
    if d is not None and d.exists():
        _rmtree(d)
    if _secret("SEARXNG_URL"):
        info("SEARXNG_URL stays in Hermes's .env; Hermes's own SearXNG backend uses it too.")
    print()
    info("Now run `hermes plugins remove webgraft`, then restart Hermes.")
    return 0


# ---------------------------------------------------------------- entry points


def _setup_parser(parser) -> None:
    subs = parser.add_subparsers(dest="webgraft_command")
    subs.add_parser("status", help="Show what webgraft uses")
    setup = subs.add_parser("setup", help="Configure search, page reading and Crawl4AI")
    setup.add_argument("--search", choices=["searxng", "ddgs", "keep"], help="Search backend to select")
    setup.add_argument("--searxng-url", help="SearXNG instance URL (with --search searxng)")
    c4 = setup.add_mutually_exclusive_group()
    c4.add_argument("--crawl4ai", dest="crawl4ai", action="store_true", default=None, help="Install Crawl4AI")
    c4.add_argument("--no-crawl4ai", dest="crawl4ai", action="store_false", help="Do not install Crawl4AI")
    oc = setup.add_mutually_exclusive_group()
    oc.add_argument("--ocr", dest="ocr", action="store_true", default=None, help="Install local OCR for scanned PDFs")
    oc.add_argument("--no-ocr", dest="ocr", action="store_false", help="Do not install local OCR")
    sk = setup.add_mutually_exclusive_group()
    sk.add_argument("--skill", dest="skill", action="store_true", default=None, help="Auto-load the research skill")
    sk.add_argument("--no-skill", dest="skill", action="store_false", help="Do not auto-load the research skill")
    setup.add_argument("--yes", action="store_true", help="Accept the defaults without asking")
    rm = subs.add_parser("uninstall", help="Undo setup before `hermes plugins remove webgraft`")
    rm.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    parser.set_defaults(func=handle)


def handle(args) -> int:
    command = getattr(args, "webgraft_command", None)
    if command == "status":
        print_state(state())
        return 0
    if command == "uninstall":
        if not args.yes and not sys.stdin.isatty():
            error("No terminal to confirm in: run `hermes webgraft uninstall --yes`.")
            return 1
        return uninstall(not args.yes)
    if command != "setup":
        print("Usage: hermes webgraft <status|setup|uninstall>")
        return 1
    interactive = not args.yes
    if interactive and not sys.stdin.isatty():
        error("No terminal to ask questions in: run with --yes, plus --search / --crawl4ai / --skill to choose.")
        return 1
    s = state()
    print_state(s)
    step_search(args, s, interactive)
    step_extract(s)
    step_crawl4ai(args, s, interactive)
    step_ocr(args, s, interactive)
    step_skill(args, interactive)
    passed = step_test()
    print_state(state(), "Saved settings")  # what was really saved, so a skipped step shows here
    print()
    info("Restart Hermes (or `hermes gateway restart`) so running sessions pick up the changes.")
    return 0 if passed else 1


def register(ctx) -> None:
    global _ctx
    _ctx = ctx  # for ctx.has_plugin in status
    ctx.register_cli_command(
        name="webgraft",
        help="Set up and check webgraft (search, page reading, Crawl4AI)",
        setup_fn=_setup_parser,
        handler_fn=handle,
    )
