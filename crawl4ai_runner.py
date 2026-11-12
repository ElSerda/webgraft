"""Opens one URL with Crawl4AI and prints one JSON line. Runs in webgraft's isolated environment.

    python crawl4ai_runner.py URL CHROMIUM_EXECUTABLE WORK_FOLDER

WORK_FOLDER (made and removed by the plugin) holds the browser profile and Crawl4AI's log and cache.

Crawl4AI drives the Chromium Hermes already installs (`hermes pm install chromium`), started here
headless with a DevTools port and connected over CDP, so the plugin downloads no browser of its own.
Kept out of Hermes's environment because Crawl4AI pulls about 35 packages.
Output: {"success", "status", "title", "final_url", "markdown", "head", "error"}; markdown is Crawl4AI's pruned
("fit") markdown, falling back to the full markdown when pruning leaves almost nothing.
"""

import asyncio
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore")
# The whole read, Chromium's start included, ends within BUDGET seconds, below the plugin's
# 60 s cap on this process: the crawl always ends here and this script stops Chromium itself.
BUDGET = 50
DEVTOOLS_WAIT = 20
MAX_MARKDOWN = 400_000  # characters handed back; the plugin reads no more of a page
MAX_HEAD = 100_000  # of the HTML, for the page's AI opt-out tags (robots / tdm-reservation)


def start_chromium(executable: str, profile: str, deadline: float):
    """Start Chromium headless on a free DevTools port. Returns (process, ws_url, profile_dir).

    Chromium's stderr is read by a thread for the whole run: the DevTools line comes from it, and
    a pipe nobody reads would fill and stall Chromium."""
    os.makedirs(profile, exist_ok=True)
    proc = subprocess.Popen(
        [executable, "--headless=new", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", "--disable-gpu",
         "--disable-extensions", "--mute-audio", "about:blank"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        errors="replace",
    )
    lines: "queue.Queue[str | None]" = queue.Queue()

    def drain():
        for line in proc.stderr:
            lines.put(line)
        lines.put(None)
    threading.Thread(target=drain, daemon=True).start()
    until = min(deadline, time.monotonic() + DEVTOOLS_WAIT)
    while (left := until - time.monotonic()) > 0:
        try:
            line = lines.get(timeout=left)
        except queue.Empty:
            break
        if line is None:
            break
        m = re.search(r"DevTools listening on (ws://\S+)", line)
        if m:
            return proc, m.group(1), profile
    stop_chromium(proc, profile)
    raise RuntimeError("Chromium did not open its DevTools port")


def kill_tree(proc) -> None:
    """Stop a process and everything it started (Chromium runs a process per tab, GPU and network).
    On Windows, children outlive their parent unless killed with it; elsewhere Chromium's helpers
    exit with the browser, and Chromium stays in this script's process group, which the plugin
    kills as a whole when it has to stop this script."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or "C:\\Windows"
            taskkill = os.path.join(root, "System32", "taskkill.exe")  # full path: never one from the cwd
            subprocess.run([taskkill, "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        except Exception:  # noqa: BLE001 - fall back to the process itself
            pass
    if proc.poll() is None:
        proc.kill()


def stop_chromium(proc, profile: str) -> None:
    kill_tree(proc)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    shutil.rmtree(profile, ignore_errors=True)


async def crawl(url: str, ws_url: str) -> dict:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

    try:
        from crawl4ai.content_filter_strategy import PruningContentFilterLXML as Pruning
    except ImportError:  # older Crawl4AI
        from crawl4ai.content_filter_strategy import PruningContentFilter as Pruning

    run = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=35_000,
        # Pages built by scripts (Vue, React...) fill in after the DOM is ready: wait for the page's
        # scripts to load, then give them a second to fetch and render their data.
        wait_until="load",
        delay_before_return_html=1.0,
        markdown_generator=DefaultMarkdownGenerator(content_filter=Pruning()),
        verbose=False,
    )
    browser = BrowserConfig(cdp_url=ws_url, browser_mode="cdp", headless=True, verbose=False)
    async with AsyncWebCrawler(config=browser) as crawler:
        r = await crawler.arun(url=url, config=run)
    if not r.success:
        return {"success": False, "status": r.status_code, "error": str(r.error_message or "crawl failed")[:300]}
    md = r.markdown
    fit = (md.fit_markdown or "") if md else ""
    full = (md.raw_markdown or "") if md else ""
    title = ((r.metadata or {}).get("title") or "") if isinstance(r.metadata, dict) else ""
    # Where the browser ended, after redirects and script navigation: the plugin checks it before
    # using the text (Chromium does not go through Hermes's SSRF-safe client).
    final = getattr(r, "redirected_url", None) or getattr(r, "url", None) or url
    html = r.html or ""
    end = html.lower().find("</head>")
    head = html[:end if 0 <= end < MAX_HEAD else MAX_HEAD]
    markdown = fit if len(fit) > 200 else full
    return {"success": True, "status": r.status_code, "title": title, "final_url": str(final),
            "markdown": markdown[:MAX_MARKDOWN], "head": head}


def main(url: str, executable: str, work: str) -> dict:
    # Crawl4AI keeps a log of every URL it visits, and its cache database, under
    # CRAWL4_AI_BASE_DIRECTORY (default: the home folder): here, the plugin's temporary folder.
    base = os.path.join(work, "crawl4ai")
    os.makedirs(base, exist_ok=True)
    os.environ["CRAWL4_AI_BASE_DIRECTORY"] = base
    deadline = time.monotonic() + BUDGET
    proc, ws_url, profile = start_chromium(executable, os.path.join(work, "profile"), deadline)
    try:
        return asyncio.run(asyncio.wait_for(crawl(url, ws_url), max(1.0, deadline - time.monotonic())))
    finally:
        stop_chromium(proc, profile)


if __name__ == "__main__":
    try:
        result = main(sys.argv[1], sys.argv[2], sys.argv[3])
    except Exception as exc:  # noqa: BLE001 - reported to the caller as the page's error
        result = {"success": False, "error": f"{exc.__class__.__name__}: {exc}"[:300]}
    sys.stdout.write("\n" + json.dumps(result) + "\n")  # ASCII-only: the console code page varies
    sys.stdout.flush()
