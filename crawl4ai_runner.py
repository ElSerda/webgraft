"""Opens one URL with Crawl4AI and prints one JSON line. Runs in webgraft's isolated environment.

    python crawl4ai_runner.py URL CHROMIUM_EXECUTABLE

Crawl4AI drives the Chromium Hermes already installs (`hermes pm install chromium`), started here
headless with a DevTools port and connected over CDP, so the plugin downloads no browser of its own.
Kept out of Hermes's environment because Crawl4AI pulls about 35 packages.
Output: {"success", "status", "title", "markdown", "error"}; markdown is Crawl4AI's pruned
("fit") markdown, falling back to the full markdown when pruning leaves almost nothing.
"""

import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")
DEVTOOLS_WAIT = 20
# Below the plugin's 60 s cap on this process, so the crawl always ends here and Chromium is
# stopped by this script: killing the script from outside would leave Chromium running on Windows.
CRAWL_TIMEOUT = 45


def start_chromium(executable: str):
    """Start Chromium headless on a free DevTools port. Returns (process, ws_url, profile_dir)."""
    profile = tempfile.mkdtemp(prefix="webgraft-chromium-")
    proc = subprocess.Popen(
        [executable, "--headless=new", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", "--disable-gpu",
         "--disable-extensions", "--mute-audio", "about:blank"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + DEVTOOLS_WAIT
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line and proc.poll() is not None:
            break
        m = re.search(r"DevTools listening on (ws://\S+)", line)
        if m:
            return proc, m.group(1), profile
    stop_chromium(proc, profile)
    raise RuntimeError("Chromium did not open its DevTools port")


def stop_chromium(proc, profile: str) -> None:
    proc.kill()
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
    return {"success": True, "status": r.status_code, "title": title, "markdown": fit if len(fit) > 200 else full}


def main(url: str, executable: str) -> dict:
    proc, ws_url, profile = start_chromium(executable)
    try:
        return asyncio.run(asyncio.wait_for(crawl(url, ws_url), CRAWL_TIMEOUT))
    finally:
        stop_chromium(proc, profile)


if __name__ == "__main__":
    try:
        result = main(sys.argv[1], sys.argv[2])
    except Exception as exc:  # noqa: BLE001 - reported to the caller as the page's error
        result = {"success": False, "error": f"{exc.__class__.__name__}: {exc}"[:300]}
    sys.stdout.write("\n" + json.dumps(result) + "\n")  # ASCII-only: the console code page varies
    sys.stdout.flush()
