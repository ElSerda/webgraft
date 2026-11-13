"""Opens one URL with Crawl4AI and prints one JSON line. Runs in webgraft's isolated environment.

    python crawl4ai_runner.py URL CHROMIUM_EXECUTABLE WORK_FOLDER

WORK_FOLDER (made and removed by the plugin) holds the browser profile and Crawl4AI's log and cache.

Crawl4AI drives the Chromium Hermes already installs (`hermes pm install chromium`), started here
headless with a DevTools port and connected over CDP, so the plugin downloads no browser of its own.
Kept out of Hermes's environment because Crawl4AI pulls about 35 packages.
Output: {"success", "status", "title", "final_url", "markdown", "head", "html", "error"}; markdown is Crawl4AI's whole
page as Markdown, without link targets or images. The plugin uses it only for pages trafilatura gets nothing
from (built by scripts), where pruning would drop content (seen: a Vue page's heading, description and status
counters); the values of other pages come from the rendered HTML.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import warnings

warnings.filterwarnings("ignore")
# The whole read, Chromium's start included, ends within BUDGET seconds, below the plugin's
# 60 s cap on this process: the crawl always ends here and this script stops Chromium itself.
BUDGET = 50
DEVTOOLS_WAIT = 20
MAX_MARKDOWN = 400_000  # characters handed back; the plugin reads no more of a page
MAX_HEAD = 100_000  # of the HTML, for the page's AI opt-out tags (robots / tdm-reservation)
MAX_HTML = 3_000_000  # of the rendered HTML, for the values its markup states (structured.py)
# Crawl4AI's BrowserConfig ignores TLS certificate errors by default (ignore_https_errors=True). Over
# CDP, Chromium still refused a bad certificate in testing (net::ERR_CERT_AUTHORITY_INVALID on
# badssl.com), but that rests on how Crawl4AI builds its context: certificates are checked on purpose,
# like Hermes's own HTTP client, so a man-in-the-middle without a trusted certificate is refused.
TLS_CHECKED = {"ignore_https_errors": False}


# The page's host pinned to the address the plugin checked (DNS rebinding), sent as two variables.
def valid_hostname(host: str) -> bool:
    """A DNS name: ASCII letters, digits, dots and hyphens, starting and ending with a letter or digit."""
    return (0 < len(host) <= 253 and host.isascii() and host[0].isalnum() and host[-1].isalnum()
            and all(ch.isalnum() or ch in ".-" for ch in host))


def host_rule_flags(host: str, address: str) -> list[str]:
    """Chromium's --host-resolver-rules for WEBGRAFT_PIN_HOST / WEBGRAFT_PIN_ADDRESS: nothing when the
    plugin sent none (an IP literal), and an error for anything that is not one host and one address,
    so the browser never runs unpinned by mistake."""
    import ipaddress

    if not host and not address:
        return []
    if not valid_hostname(host):
        raise ValueError("malformed host pin")
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise ValueError("malformed host pin") from None
    return [f"--host-resolver-rules=MAP {host} {f'[{ip}]' if ip.version == 6 else ip}"]


def devtools_url(profile: str, proc, deadline: float) -> str | None:
    """The browser's DevTools WebSocket URL, from the DevToolsActivePort file Chromium writes in its
    profile when started with --remote-debugging-port=0 (port on the first line, path on the second),
    the way browser automation tools find it. None when Chromium exits or the time runs out."""
    port_file = os.path.join(profile, "DevToolsActivePort")
    until = min(deadline, time.monotonic() + DEVTOOLS_WAIT)
    while time.monotonic() < until and proc.poll() is None:
        try:
            with open(port_file, encoding="utf-8") as f:
                port, path = (f.read().splitlines() + ["", ""])[:2]
        except OSError:
            port = path = ""
        if port.strip().isdigit() and path.startswith("/devtools/browser/"):
            return f"ws://127.0.0.1:{port.strip()}{path.strip()}"
        time.sleep(0.1)
    return None


def start_chromium(executable: str, profile: str, deadline: float):
    """Start Chromium headless on a free DevTools port. Returns (process, ws_url, profile_dir)."""
    os.makedirs(profile, exist_ok=True)
    flags = host_rule_flags(os.environ.get("WEBGRAFT_PIN_HOST", ""), os.environ.get("WEBGRAFT_PIN_ADDRESS", ""))
    proc = subprocess.Popen(
        [executable, "--headless=new", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", "--disable-gpu",
         "--disable-extensions", "--mute-audio", *flags, "about:blank"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ws_url = devtools_url(profile, proc, deadline)
    if ws_url:
        return proc, ws_url, profile
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

    run = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=35_000,
        # Pages built by scripts (Vue, React...) fill in after the DOM is ready: wait for the page's
        # scripts to load, then give them a second to fetch and render their data.
        wait_until="load",
        delay_before_return_html=1.0,
        # Text without link targets or images: the plugin lists a page's links from its HTML (lxml).
        markdown_generator=DefaultMarkdownGenerator(options={"ignore_links": True, "ignore_images": True}),
        verbose=False,
    )
    # Hermes's own User-Agent, passed by the plugin: Crawl4AI would otherwise present itself as a
    # desktop Chrome on macOS.
    browser = BrowserConfig(cdp_url=ws_url, browser_mode="cdp", headless=True, verbose=False,
                            user_agent=os.environ["WEBGRAFT_USER_AGENT"], user_agent_mode="", **TLS_CHECKED)
    async with AsyncWebCrawler(config=browser) as crawler:
        r = await crawler.arun(url=url, config=run)
    if not r.success:
        return {"success": False, "status": r.status_code, "error": str(r.error_message or "crawl failed")[:300]}
    md = r.markdown
    markdown = (md.raw_markdown or "") if md else ""
    title = ((r.metadata or {}).get("title") or "") if isinstance(r.metadata, dict) else ""
    # Where the browser ended, after redirects and script navigation: the plugin checks it before
    # using the text (Chromium does not go through Hermes's SSRF-safe client).
    final = getattr(r, "redirected_url", None) or getattr(r, "url", None) or url
    html = r.html or ""
    end = html.lower().find("</head>")
    head = html[:end if 0 <= end < MAX_HEAD else MAX_HEAD]
    return {"success": True, "status": r.status_code, "title": title, "final_url": str(final),
            "markdown": markdown[:MAX_MARKDOWN], "head": head, "html": html[:MAX_HTML]}


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
