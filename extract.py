"""Reads web pages and documents (PDF, Office, EPUB) on this machine, for web_extract.

Order for one URL:
  1. download it (HTTP errors such as 404 stop here, unless a browser can still open it);
  2. documents -> anydoc, Hermes's own converter (Markdown, tables kept); HTML -> trafilatura;
  3. Crawl4AI, when its isolated environment is installed, renders the same page in a
     browser. trafilatura drops what it takes for page furniture, spec tables included, so
     the lines of Crawl4AI's output that hold values missing from trafilatura's text are
     appended. When trafilatura gets nothing (script-built pages), Crawl4AI's text is used;
  4. error and block pages that answer HTTP 200 ("Page not found", "Just a moment...") are
     reported as errors, never handed to the model as content.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import browser, site_rules, values


def setting(key: str, default):
    """The plugin's settings; register() replaces this with Hermes's config lookup."""
    return default

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
FETCH_TIMEOUT = 25
BROWSER_TIMEOUT = 60
MAX_HTML_BYTES = 15 * 1024 * 1024
MAX_DOC_BYTES = 50 * 1024 * 1024  # Hermes's own anydoc limit (tools/read_extract.py)
MAX_CONTENT_CHARS = 30_000  # what one page may hand to the model
MAX_MARKDOWN_BYTES = 4 * 1024 * 1024  # a page's .md version listed in llms.txt (larger: the HTML is read)
# What a page is read up to, before focus.arrange() picks what comes first and the cut above.
READ_CHARS = 400_000
MAX_COMPLEMENT_CHARS = 8_000
# Links of the page whose text is in the content, listed under it so the model can open the
# next page (a product in a listing, an article in an index) without a search. Kept short enough
# to stay in the tail of Hermes's per-page window.
MAX_LINKS = 40
MAX_LINKS_CHARS = 3_000
BROWSERS_AT_ONCE = 3  # each Crawl4AI read starts its own headless Chromium; a limit for the whole process
HEAD_CHARS = 100_000  # of the browser's HTML, for the page's AI opt-out tags
SHORT_PAGE_CHARS = 1_500
CRAWL4AI_ENV = "webgraft-crawl4ai"
RUNNER = Path(__file__).with_name("crawl4ai_runner.py")
OCR_ENV = "webgraft-ocr"
OCR_REQUIREMENTS = ["rapidocr>=3.10,<4", "onnxruntime>=1.20,<2", "pypdfium2>=5,<6"]
OCR_RUNNER = Path(__file__).with_name("ocr_runner.py")
OCR_MAX_PAGES = 30
# Below Hermes's own cap on one extract call (web.extract_timeout, 120 s by default), which cancels
# the call: the child is then stopped too. 30 pages on a CPU take well under this.
OCR_TIMEOUT = 100

# Bot walls and challenge pages. Phrases only a challenge page uses count anywhere near the top,
# whatever the page length; words an article about the topic also uses ("captcha", "access
# denied") count only on a short page (title included): a long page about the topic is content.
_WALL_RE = re.compile(
    r"\b(?:just a moment\.\.\.|attention required!?\s*\|\s*cloudflare|checking (?:if the site connection is secure|your browser)|"
    r"verify(?:ing)? you are (?:a )?human|are you a (?:robot|human)\?|bot check in progress|"
    r"automated (?:bot check|access to this page)|drag the handle|ddos protection by|"
    r"please enable (?:javascript|cookies) (?:and|to continue)|enable javascript and cookies to continue)",
    re.IGNORECASE,
)
_WALL_WORDS_RE = re.compile(
    r"\b(?:just a moment|attention required|captcha|bot check|automated (?:bot|access|traffic)|"
    r"ddos protection|security check|unusual traffic|access denied|accès refusé|request blocked|"
    r"please enable (?:javascript|cookies))\b",
    re.IGNORECASE,
)
# What the HTML head says about a page: an article (og:type article, a publication date, an
# <article> element) is content, never a bot wall, whatever its title or text.
_ARTICLE_RE = re.compile(
    r"<meta[^>]{1,500}?(?:property|name)=[\"'](?:og:type[\"'][^>]{1,500}?content=[\"']article|article:published_time)|<article[\s>]",
    re.IGNORECASE,
)
@dataclass
class Page:
    url: str  # where the content came from (after redirects); the requested URL on an error
    title: str = ""
    content: str = ""
    error: str = ""
    engine: str = ""
    links: str = ""  # "- text: url" lines, see page_links()
    requested: str = ""  # the URL web_extract asked for, when `url` differs from it

    def as_result(self, terms: list[str] | None = None, window: int = 0) -> dict:
        """The provider's dict for this page, cut to MAX_CONTENT_CHARS; with `terms`, a long page
        has its passages about them first (focus.arrange). The page's links come last.

        `url` is the page actually read, so Hermes re-checks a redirect target (SSRF and website
        policy) as it does for other backends; the requested URL is in metadata.sourceURL."""
        text = self.content
        if terms and not self.error:
            from .focus import arrange
            text = arrange(text, terms, window or 15_000)
        tail = f"\n\n## Links on this page\n{self.links}" if self.links and not self.error else ""
        text = text[:MAX_CONTENT_CHARS - len(tail)] + tail
        metadata = {"sourceURL": self.requested or self.url}
        if self.engine:
            metadata["engine"] = self.engine
        out = {"url": self.url, "title": self.title, "content": text, "raw_content": text, "metadata": metadata}
        if self.error:
            out["error"] = self.error
            out["content"] = out["raw_content"] = ""
        return out


# ---------------------------------------------------------------- network and child safety


# What a child process (anydoc, OCR, Crawl4AI and its Chromium) gets from Hermes's environment:
# what a program needs to start, find its home and temp folders, speak the locale and use a proxy.
# Never the rest: Hermes's environment holds every provider's API key.
CHILD_ENV_KEYS = (
    "PATH", "PATHEXT", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "TMP", "TEMP", "TMPDIR",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "LANG", "LANGUAGE", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
)


# Children start in the plugin's own folder, not in the folder Hermes runs from: on Windows a
# program name without a path is looked up in the current folder first.
CHILD_CWD = str(Path(__file__).resolve().parent)


def taskkill_exe() -> str:
    """taskkill by its full path, for the same reason."""
    root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or "C:\\Windows"
    return os.path.join(root, "System32", "taskkill.exe")


async def _kill(proc) -> None:
    """Stop a child process and wait for it (used on timeout and when Hermes cancels the call)."""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except Exception:  # noqa: BLE001
        pass


async def _communicate(proc, data: bytes | None, timeout: float, what: str) -> tuple[bytes, bytes]:
    """proc.communicate() with a time limit. On the limit, or when Hermes cancels the call (its
    web.extract_timeout, an interrupted turn), the child is stopped: it never outlives the call."""
    try:
        return await asyncio.wait_for(proc.communicate(data), timeout)
    except asyncio.TimeoutError:
        await _kill(proc)
        raise RuntimeError(f"{what} stopped after {timeout:g}s") from None
    except asyncio.CancelledError:
        await asyncio.shield(_kill(proc))
        raise


def child_env(**extra: str) -> dict[str, str]:
    """A minimal environment for a child process: CHILD_ENV_KEYS and the locale (LC_*) from ours,
    PYTHONIOENCODING, and the variables this child needs (`extra`)."""
    env = {k: v for k, v in os.environ.items() if k in CHILD_ENV_KEYS or k.startswith("LC_")}
    if os.name == "nt":  # Windows keys are case-insensitive: Path, SystemRoot...
        wanted = {k.upper() for k in CHILD_ENV_KEYS}
        env.update({k: v for k, v in os.environ.items() if k.upper() in wanted})
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def http_client(**kwargs):
    """Hermes's SSRF-safe httpx client: every connection, redirects included, is checked and
    refused when it would reach a private, loopback, link-local or cloud metadata address.
    Outside Hermes there is no such client, and pages are not read (fails closed)."""
    try:
        from tools.url_safety import create_ssrf_safe_async_client
    except ImportError as exc:
        raise RuntimeError("Hermes's tools.url_safety is required to read pages") from exc
    return create_ssrf_safe_async_client(**kwargs)


async def url_is_safe(url: str) -> bool:
    """Hermes's SSRF check for a URL (DNS resolved, every answer checked). False outside Hermes."""
    try:
        from tools.url_safety import is_safe_url
    except ImportError:
        return False
    try:
        return await asyncio.to_thread(is_safe_url, url)
    except Exception:  # noqa: BLE001 - fail closed
        return False


def ssrf_blocked(exc: BaseException) -> bool:
    """True when a download was refused by the SSRF guard because a hop reached a private
    address, whether the guard's exception arrives as is or wrapped by httpx. The guard also
    raises when DNS fails; that is an ordinary download failure, not a private address."""
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(exc, RedirectBlocked):
            return True
        if any(cls.__name__ == "SSRFConnectionBlocked" for cls in type(exc).__mro__):
            return "DNS" not in str(exc)
        exc, seen = exc.__cause__ or exc.__context__, seen + 1
    return False


# ---------------------------------------------------------------- pure helpers


def looks_blocked(title: str, text: str, headers: dict | None = None, html: str = "") -> str:
    """A reason when an HTTP 200 page is really a bot wall, else an empty string.

    Errors come from the HTTP status only (404, 410, 403, 5xx): a page's text is never searched
    for "404" or "not found", which a table, a price or a blog post can hold. A bot wall can
    answer 200, though, so for walls: the response headers first (Cloudflare marks its
    challenges), then the text, where a challenge page's own phrases count at any length and
    the words an article could also use ("captcha", "access denied") count only on a short page
    that does not declare itself an article in its HTML head. Without `html` (a result from
    another backend), the text alone is judged, with the same limits.
    """
    headers = {k.lower(): str(v).lower() for k, v in (headers or {}).items()}
    if headers.get("cf-mitigated") == "challenge":
        return "bot wall (Cloudflare challenge, cf-mitigated header)"
    short = len(text) < SHORT_PAGE_CHARS
    m = _WALL_RE.search(f"{title}\n{text[:600]}")
    article = bool(html) and bool(_ARTICLE_RE.search(html[:200_000]))
    if not m and short and not article:
        m = _WALL_WORDS_RE.search(f"{title}\n{text[:600]}")
    if not m:
        return ""
    return f"bot wall ({m.group(0)!r} in {'title' if m.group(0).lower() in title.lower() else 'text'})"


def strip_links(markdown: str) -> str:
    # Bounded repeats throughout: on page text, an unbounded [^\]]* or [^>]+ re-reads the rest of the
    # page at every "[" or "<" that is never closed, and a crafted page then takes minutes.
    text = re.sub(r"!\[[^\]]{0,500}\]\([^)]{0,2000}\)", "", markdown)  # images
    text = re.sub(r"\[([^\]]{0,500})\]\([^)]{0,2000}\)", r"\1", text)  # links -> their text
    text = re.sub(r"<https?://[^>\s]{1,2000}>", "", text)
    text = "\n".join(line.rstrip(" \t") for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Bounded repeats, as in strip_links(): an <a> or "[" that is never closed must not make every
# later match attempt re-read the rest of the page.
_HTML_LINK_RE = re.compile(r"<a\s[^>]{0,1000}?href\s*=\s*[\"']([^\"'#]{1,2000})[^>]{0,1000}>(.{0,1000}?)</a>",
                           re.IGNORECASE | re.DOTALL)
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]{1,500})\]\(([^)\s]{1,2000})\)")
LINKS_SCAN_CHARS = 400_000  # of the HTML and of the browser view, searched for links
_TAG_RE = re.compile(r"<[^>]+>")


def page_links(html: str, browser_md: str, url: str, content: str) -> str:
    """The page's links whose text appears in `content`, one "- text: url" line each.

    trafilatura and strip_links() keep a link's text and drop its target, so a model reading a
    listing (products, search results, an index) sees names it cannot open. The links of the
    HTML, then of the browser view, are listed in page order when their text is in the content:
    navigation and footers, which the extraction already dropped, stay out. MAX_LINKS lines,
    MAX_LINKS_CHARS at most.
    """
    from html import unescape
    from urllib.parse import urljoin, urldefrag

    if not content.strip():
        return ""
    haystack = re.sub(r"\s+", " ", content).casefold()
    page = urldefrag(url)[0].rstrip("/")
    seen_urls: set[str] = {page}
    seen_texts: set[str] = set()
    out: list[str] = []
    size = 0
    found = [(h, t) for h, t in _HTML_LINK_RE.findall((html or "")[:LINKS_SCAN_CHARS])] + \
            [(h, t) for t, h in _MD_LINK_RE.findall((browser_md or "")[:LINKS_SCAN_CHARS])]
    for href, raw in found:
        text = re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", raw))).strip()
        if len(text) < 3 or not re.search(r"[^\W\d_]", text):
            continue
        target = urldefrag(urljoin(url, unescape(href.strip())))[0].rstrip("/")
        if not target.startswith(("http://", "https://")) or target in seen_urls:
            continue
        key = text.casefold()
        if key in seen_texts or key not in haystack:
            continue
        line = f"- {text[:120]}: {target}"
        if len(out) >= MAX_LINKS or size + len(line) + 1 > MAX_LINKS_CHARS:
            break
        out.append(line)
        size += len(line) + 1
        seen_urls.add(target)
        seen_texts.add(key)
    return "\n".join(out)


def _value_keys(text: str) -> set[tuple[str, str]]:
    """Values with a unit, and bare numbers of 100 or more that are not years (copyright lines)."""
    keys = set()
    for occ in values.index_page("", text):
        numeric = [float(v) for v in occ.values if re.fullmatch(r"\d+(?:\.\d+)?", v)]  # versions excluded
        if occ.unit or any(n >= 100 and not (1900 <= n < 2100 and n.is_integer()) for n in numeric):
            keys.update((v, occ.unit) for v in occ.values)
    return keys


def _has_label(line: str) -> bool:
    """True when a line names what its values are, unlike benchmark rows such as "117.5 1%"."""
    words = re.findall(r"[^\W\d_]{3,}", line.lower())
    return any(w not in values.UNIT_SPELLINGS for w in words)


def complement(main_text: str, browser_md: str) -> str:
    """Lines of the browser text that hold values the main text lacks, with their headings."""
    known = _value_keys(main_text)
    out: list[str] = []
    heading = ""
    size = 0
    for line in strip_links(browser_md).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            heading = stripped
            continue
        new = _value_keys(stripped) - known
        if not new or not _has_label(stripped):
            continue
        if heading:
            out.append(heading)
            size += len(heading) + 1
            heading = ""
        out.append(stripped)
        size += len(stripped) + 1
        known |= new
        if size > MAX_COMPLEMENT_CHARS:
            break
    return "\n".join(out)


def merge(main_text: str, browser_md: str) -> tuple[str, str]:
    """(content, engine) from trafilatura's text and Crawl4AI's markdown."""
    browser_text = strip_links(browser_md) if browser_md else ""
    if len(main_text.strip()) < 300 and browser_text:
        return browser_text[:READ_CHARS], "crawl4ai"
    extra = complement(main_text, browser_md) if browser_md else ""
    if extra:
        return f"{main_text}\n\n## More values from this page (browser view)\n{extra}"[:READ_CHARS], "trafilatura+crawl4ai"
    return main_text[:READ_CHARS], "trafilatura"


DOC_TIMEOUT = 60
# POSIX only (Windows has no per-process limit to set). This caps address space, not memory used:
# anydoc uses about 85 MB on a 5 MB PDF but reserves far more, and the reservation grows with the
# number of threads and malloc arenas. With DOC_CHILD_ENV below it peaks near 420 MB on 16 cores.
DOC_MEMORY_LIMIT = 2 * 1024 * 1024 * 1024
# Few threads and malloc arenas: a document needs no more, and it keeps the child small and
# polite on shared machines.
DOC_CHILD_ENV = {"MALLOC_ARENA_MAX": "2", "RAYON_NUM_THREADS": "4"}
DOC_READER = Path(__file__).with_name("doc_reader.py")
# What anydoc converts, by content type and by URL extension (Hermes's ANYDOC_EXTENSIONS).
# What anydoc converts (Hermes's ANYDOC_EXTENSIONS plus CSV), and the format name anydoc takes.
DOC_EXTENSIONS = {
    ".pdf": "pdf", ".doc": "doc", ".docx": "docx", ".docm": "docx", ".odt": "odt", ".rtf": "rtf",
    ".ppt": "ppt", ".pps": "ppt", ".pptx": "pptx", ".pptm": "pptx", ".ppsx": "pptx", ".odp": "odp",
    ".xls": None, ".xlsx": "xlsx", ".xlsm": "xlsx", ".xlsb": None, ".ods": "ods",
    ".epub": "epub", ".csv": "csv",
}
DOC_TYPES = {
    "application/pdf": "pdf", "application/msword": "doc", "application/rtf": "rtf", "text/rtf": "rtf",
    "application/epub+zip": "epub", "text/csv": "csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml": "xlsx",
    "application/vnd.oasis.opendocument.text": "odt", "application/vnd.oasis.opendocument.presentation": "odp",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.ms-powerpoint": "ppt", "application/vnd.ms-excel": None,
}


def document_format(url: str, ctype: str, body: bytes) -> tuple[bool, str | None]:
    """(is a document, anydoc format name or None to let anydoc detect it)."""
    path = url.lower().split("?")[0].split("#")[0]
    if body[:5] == b"%PDF-":
        return True, "pdf"
    for prefix, fmt in DOC_TYPES.items():
        if ctype.startswith(prefix):
            return True, fmt
    for ext, fmt in DOC_EXTENSIONS.items():
        if path.endswith(ext):
            return True, fmt
    return False, None


def is_document(url: str, ctype: str, body: bytes) -> bool:
    return document_format(url, ctype, body)[0]


def _limit_memory() -> None:  # runs in the child, before anydoc starts
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (DOC_MEMORY_LIMIT, DOC_MEMORY_LIMIT))


def _child_failure(stderr: str) -> str:
    """One readable line from a crashed reader: the exception, not Rust's trailing backtrace hint."""
    if "MemError" in stderr or "MemoryError" in stderr:
        return f"the document needs more than {DOC_MEMORY_LIMIT // 2**30} GB of address space"
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    for line in reversed(lines):
        if "Error" in line or "Exception" in line:
            return line[:200]
    return (lines or ["no output"])[-1][:200]


def ensure_doc_reader() -> None:
    """Make anydoc importable: Hermes's `doc-extract` extra, the way its read_file tool does.

    Raises RuntimeError when it cannot be installed (lazy installs off, offline...)."""
    try:
        import anydoc  # noqa: F401
        return
    except ImportError:
        pass
    try:
        from pm import ensure_import
        ensure_import("doc-extract")
    except Exception as exc:  # noqa: BLE001 - pm.InstallError and friends
        raise RuntimeError(f"Hermes's document converter is not installed: {exc}") from exc


async def document_text_isolated(data: bytes, fmt: str | None = None) -> str:
    """Markdown of a document, converted by doc_reader.py (anydoc) in a child process stopped after
    DOC_TIMEOUT seconds (and, on POSIX, capped in memory). anydoc is native code and documents come
    from anywhere: a crafted one must not hang, bloat or crash Hermes.

    Raises RuntimeError with the reason when the document cannot be read.
    """
    await asyncio.to_thread(ensure_doc_reader)
    kwargs = {} if os.name == "nt" else {"preexec_fn": _limit_memory}
    # Hermes runs a base interpreter and adds its package environment to sys.path at startup, so
    # the child gets the parent's import path explicitly (and no user site-packages: -s).
    env = child_env(PYTHONPATH=os.pathsep.join(p for p in sys.path if p and os.path.isdir(p)), **DOC_CHILD_ENV)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-s", str(DOC_READER), str(READ_CHARS), fmt or "",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env, cwd=CHILD_CWD, **kwargs,
    )
    out, err = await _communicate(proc, data, DOC_TIMEOUT, "document reading")
    if proc.returncode != 0:
        raise RuntimeError(_child_failure(err.decode("utf-8", "replace")))
    result = json.loads(out.decode("utf-8"))
    if result.get("needs_ocr") is not None:
        raise NeedsOcr(result["needs_ocr"])
    if result.get("error"):
        raise RuntimeError(result["error"])
    if not result.get("text"):
        raise RuntimeError("no text in the document")
    return result["text"]


class NeedsOcr(RuntimeError):
    """A scanned document: anydoc found no text layer on these 1-based pages."""

    def __init__(self, pages: list[int]):
        self.pages = list(pages)
        super().__init__(f"scanned pages {_page_list(self.pages)} have no text layer")


def _page_list(pages: list[int]) -> str:
    return ", ".join(str(p) for p in pages) if pages else "(all)"


def ocr_python() -> Path | None:
    """The OCR environment's interpreter, without installing anything."""
    try:
        from pm import environment_python
        return environment_python(OCR_ENV)
    except Exception:  # noqa: BLE001
        return None


async def ocr_isolated(data: bytes, pages: list[int]) -> str:
    """Text of scanned PDF pages, read by RapidOCR in the OCR environment (no language model)."""
    python = ocr_python()
    if python is None:
        raise RuntimeError("local OCR is not installed (run `hermes webgraft setup`)")
    # No page list means anydoc found no text layer at all: read from the first page.
    wanted = pages[:OCR_MAX_PAGES] or list(range(1, OCR_MAX_PAGES + 1))
    env = child_env()
    proc = await asyncio.create_subprocess_exec(
        str(python), "-I", str(OCR_RUNNER), ",".join(str(p) for p in wanted), str(READ_CHARS),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        cwd=CHILD_CWD,
    )
    out, _ = await _communicate(proc, data, OCR_TIMEOUT, "OCR")
    lines = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.startswith("{")]
    result = json.loads(lines[-1]) if lines else {"error": "OCR gave no output"}
    if result.get("error"):
        raise RuntimeError(result["error"])
    read = {int(k): v for k, v in (result.get("pages") or {}).items() if v.strip()}
    if not read:
        raise RuntimeError("OCR found no text on the scanned pages")
    note = f"[Text read by OCR from scanned pages {_page_list(sorted(read))}; OCR can misread characters.]"
    skipped = pages[OCR_MAX_PAGES:]
    if skipped:
        note += f" [Pages {_page_list(skipped)} were not read: OCR stops at {OCR_MAX_PAGES} pages.]"
    body = "\n\n".join(f"[page {n}]\n{text}" for n, text in sorted(read.items()))
    return f"{note}\n\n{body}"[:READ_CHARS]


def html_text(html: str, url: str) -> tuple[str, str]:
    import trafilatura

    text = trafilatura.extract(html, url=url, include_tables=True, include_comments=False) or ""
    meta = trafilatura.extract_metadata(html, default_url=url)
    title = (meta.title if meta and meta.title else "") or ""
    if not title:
        m = re.search(r"<title[^>]{0,500}>(.{0,2000}?)</title>", html[:200_000], re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    return title, text


# ---------------------------------------------------------------- Crawl4AI side environment


def crawl4ai_python() -> Path | None:
    """The isolated environment's interpreter, without installing anything."""
    try:
        from pm import environment_python
        return environment_python(CRAWL4AI_ENV)
    except Exception:  # noqa: BLE001 - Hermes without pm, or a broken selection: run without it
        return None


_browsers_lock = threading.Lock()
_browsers_running = 0


@contextlib.asynccontextmanager
async def _browser_slot():
    """At most BROWSERS_AT_ONCE browser reads in this Hermes process, across calls and event loops
    (Hermes may call the provider from different loops, so no asyncio primitive is shared)."""
    global _browsers_running
    while True:
        with _browsers_lock:
            if _browsers_running < BROWSERS_AT_ONCE:
                _browsers_running += 1
                break
        await asyncio.sleep(0.2)
    try:
        yield
    finally:
        with _browsers_lock:
            _browsers_running -= 1


async def browser_view(url: str) -> dict:
    """Crawl4AI result for one URL: {success, status, final_url, title, markdown, head, error}."""
    async with _browser_slot():
        return await _browser_view(url)


# Temporary folders webgraft makes for the browser (and the names older versions used). Hermes can
# be stopped in the middle of a read, so those left over from an earlier run are removed, once per
# process, when they are more than STALE_SECONDS old: a read in progress is never older than 60 s.
TEMP_PREFIXES = ("webgraft-browser-", "webgraft-chromium-", "webgraft-crawl4ai-")
STALE_SECONDS = 3600
_pruned = False


def prune_stale_folders(now: float | None = None) -> int:
    """Remove webgraft's own temporary folders left by an earlier run. Returns how many."""
    root, now, removed = tempfile.gettempdir(), now or time.time(), 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(root, name)
        if not name.startswith(TEMP_PREFIXES) or os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            if now - os.path.getmtime(path) < STALE_SECONDS:
                continue
        except OSError:
            continue
        _remove_folder(path)
        removed += not os.path.exists(path)
    return removed


def _remove_folder(path: str) -> None:
    """Remove a temporary folder; on Windows a just-killed Chromium can hold files for a moment."""
    for _ in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.3)


async def _browser_view(url: str) -> dict:
    python = crawl4ai_python()
    if python is None:
        return {"success": False, "error": "crawl4ai not installed"}
    chromium = browser.recorded()
    if chromium is None:
        return {"success": False, "error": "no Chromium recorded: run `hermes webgraft setup`"}
    global _pruned
    if not _pruned:
        _pruned = True
        await asyncio.to_thread(prune_stale_folders)
    # The browser profile and Crawl4AI's log and cache live in this folder, made and removed here:
    # when the runner has to be killed, its own clean-up never runs.
    work = tempfile.mkdtemp(prefix="webgraft-browser-")
    try:
        proc = await asyncio.create_subprocess_exec(
            str(python), "-I", str(RUNNER), url, str(chromium), work,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=child_env(CRAWL4AI_NO_CLOUD_NOTICE="1"), cwd=CHILD_CWD,
            **({} if os.name == "nt" else {"start_new_session": True}),  # its own group, Chromium included
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), BROWSER_TIMEOUT)
        except asyncio.TimeoutError:
            await _stop_tree(proc)
            return {"success": False, "error": f"browser timed out after {BROWSER_TIMEOUT}s"}
        except asyncio.CancelledError:  # Hermes's extract timeout, or the turn was interrupted
            await asyncio.shield(_stop_tree(proc))
            raise
    finally:
        await asyncio.shield(asyncio.to_thread(_remove_folder, work))
    lines = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.startswith("{")]
    if not lines:
        return {"success": False, "error": (err.decode("utf-8", "replace").strip().splitlines() or ["no output"])[-1][:300]}
    view = json.loads(lines[-1])
    view["markdown"] = str(view.get("markdown") or "")[:READ_CHARS]
    view["head"] = str(view.get("head") or "")[:HEAD_CHARS]
    return await vet_view(view)


async def _stop_tree(proc) -> None:
    """Stop the runner and the Chromium it started: killing the runner alone leaves Chromium
    running (on Windows, children outlive their parent)."""
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                taskkill_exe(), "/T", "/F", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                env=child_env(), cwd=CHILD_CWD)
            await asyncio.wait_for(killer.wait(), 15)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001 - fall back to the runner itself
        pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except Exception:  # noqa: BLE001
        pass


async def vet_view(view: dict) -> dict:
    """The browser view, or a failure when the page it ended on is not a public address.

    Chromium follows redirects and script navigation on its own, outside the SSRF-safe client,
    so the URL it ended on (crawl4ai_runner reports it) is checked here; a view that does not
    say where it ended is dropped too."""
    if not view.get("success"):
        return view
    final = str(view.get("final_url") or "")
    if not final or not await url_is_safe(final):
        logger.info("webgraft: dropped a browser view that ended on an unsafe URL")
        return {"success": False, "error": "the browser ended on a private or internal address"}
    return view


# ---------------------------------------------------------------- one URL


# Status of the last response this task received, redirects included (see _note_status): when the
# SSRF guard refuses a redirect hop, it says which answer of the site led there.
_last_status: contextvars.ContextVar[int] = contextvars.ContextVar("webgraft_last_status", default=0)


class RedirectBlocked(ValueError):
    """A redirect target that is not a public address, refused by webgraft's own check."""


def proxy_configured() -> bool:
    return any(os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                           "http_proxy", "https_proxy", "all_proxy"))


async def _note_status(response) -> None:
    """httpx response hook, run in the reading task for every response, redirects included.

    Notes the status (see _blocked_redirect). With a proxy configured, Hermes's SSRF-safe client
    leaves each hop to the proxy (only direct connections are checked), so webgraft checks the
    target of each redirect itself before it is followed."""
    _last_status.set(response.status_code)
    if getattr(response, "is_redirect", False) and proxy_configured():
        from urllib.parse import urljoin
        target = urljoin(str(response.url), response.headers.get("location", ""))
        if not await url_is_safe(target):
            raise RedirectBlocked("redirect to a private or internal network address")


async def _download(client, url: str):
    _last_status.set(0)
    async with client.stream("GET", url) as response:
        chunks, size = [], 0
        limit = MAX_DOC_BYTES
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                break
        return response, b"".join(chunks), size > limit


def _origin(status: int, reason: str) -> str:
    """A page-level failure in Hermes's origin wording ("HTTP <code> from origin: ...").

    The page answered, but with nothing usable. Hermes does not hand such a page to its keyless
    rescue, which would fetch it again through an outside service and can return the same wall
    as content.
    """
    return f"HTTP {status or 200} from origin: {reason}"[:300]


async def read_page(client, url: str) -> Page:
    try:
        return await _read_page(client, url)
    except Exception as exc:  # noqa: BLE001 - one bad page never fails the whole call
        logger.warning("webgraft: reading %s failed", url, exc_info=True)
        return Page(url, error=f"could not read the page: {exc.__class__.__name__}: {exc}"[:300])


async def _from_llms_txt(client, url: str) -> tuple[Page | None, str]:
    """(the page's Markdown version listed in its site's llms.txt, or None; the llms.txt URL or "")."""
    if not setting("llms_txt", True) or is_document(url, "", b""):
        return None, ""
    index = await site_rules.llms_index(client, url)
    if not index:
        return None, ""
    llms_url, links = index
    md_url = site_rules.markdown_version(url, links)
    if not md_url:
        return None, llms_url
    try:
        response, body = await site_rules.get_capped(client, md_url, MAX_MARKDOWN_BYTES)
    except Exception:  # noqa: BLE001 - fall back to the HTML page
        return None, llms_url
    ctype = response.headers.get("content-type", "").lower()
    ok = response.status_code == 200 and "html" not in ctype and body is not None
    text = site_rules.decoded(response, body).strip() if ok else ""
    if not text:
        return None, llms_url
    if opted_out := _optout(response.status_code, dict(response.headers)):
        return Page(url, error=opted_out), llms_url
    title = site_rules.title_of(text) or url.rstrip("/").rsplit("/", 1)[-1]
    final = str(getattr(response, "url", "") or md_url)
    return Page(final, title, text[:READ_CHARS], engine="llms.txt", requested=url), llms_url


def _optout(status: int, headers: dict, html: str = "") -> str:
    if not setting("respect_ai_optout", True):
        return ""
    reason = site_rules.ai_optout(headers, html)
    return _origin(status, f"the site asks not to be used by AI ({reason})") if reason else ""


async def _read_page(client, url: str) -> Page:
    from_index, llms_url = await _from_llms_txt(client, url)
    if from_index:
        return from_index
    page = await _read_page_itself(client, url)
    if llms_url and not page.error:
        page.content += f"\n\n[webgraft: this site publishes an index for language models: {llms_url}]"
    return page


BLOCKED_ADDRESS = "Blocked: URL targets a private or internal network address"  # Hermes's wording


def _blocked_redirect(status: int) -> str:
    """The error for a page whose redirect leads to a private address. In Hermes's origin wording
    when the redirect's status is known: the site itself sent the agent there, so Hermes must not
    hand the URL to its keyless rescue, an outside service that would fetch it again."""
    if 300 <= status < 400:
        return _origin(status, "redirects to a private or internal network address; webgraft did not follow it")
    return BLOCKED_ADDRESS


async def _read_page_itself(client, url: str) -> Page:
    try:
        response, body, too_big = await _download(client, url)
        status, ctype = response.status_code, response.headers.get("content-type", "").lower()
    except Exception as exc:  # noqa: BLE001 - network errors become the page's error
        if ssrf_blocked(exc):  # a redirect led to a private address: no browser either
            return Page(url, error=_blocked_redirect(_last_status.get()))
        status, ctype, body, too_big, response = 0, "", b"", False, None
        fetch_error = f"could not download: {exc.__class__.__name__}: {exc}"[:300]
    else:
        fetch_error = f"HTTP {status} from origin" if status >= 400 else ""  # Hermes wording: no keyless rescue
    # Where the download ended, after redirects the SSRF-safe client checked hop by hop.
    final = str(getattr(response, "url", "") or url) if response is not None else url

    if not fetch_error and (opted_out := _optout(status, dict(response.headers))):
        return Page(url, error=opted_out)

    if not fetch_error and is_document(final, ctype, body):
        if too_big:
            return Page(url, error=_origin(status, f"document larger than {MAX_DOC_BYTES // 2**20} MB"))
        name = final.rstrip("/").rsplit("/", 1)[-1]
        try:
            text = await document_text_isolated(body, document_format(final, ctype, body)[1])
        except NeedsOcr as scanned:
            try:
                return Page(final, name, await ocr_isolated(body, scanned.pages), engine="anydoc+ocr", requested=url)
            except Exception as exc:  # noqa: BLE001
                return Page(url, error=_origin(status, f"{scanned}; {exc}"))
        except Exception as exc:  # noqa: BLE001
            return Page(url, error=_origin(status, f"unreadable document: {exc}"))
        return Page(final, name, text, engine="anydoc", requested=url)

    title, main, html, headers = "", "", "", {}
    if not fetch_error:
        html = await asyncio.to_thread(site_rules.decoded, response, body[:MAX_HTML_BYTES])
        headers = dict(response.headers)
        if opted_out := _optout(status, headers, html):
            return Page(url, error=opted_out)
    # The browser starts once the download is done, on the address it reached: a URL whose
    # download was refused never reaches Chromium.
    browser = None if is_document(final, ctype, b"") else asyncio.create_task(browser_view(final))
    if not fetch_error:
        title, main = await asyncio.to_thread(html_text, html, final)

    view = await browser if browser else {"success": False}
    view_ok = view.get("success") and int(view.get("status") or 200) < 400
    if view_ok and not fetch_error and not same_site(final, str(view.get("final_url") or "")):
        # The browser ended on another site than the page downloaded: its text is not this page's,
        # and Hermes's website policy would only ever see the downloaded URL.
        view_ok = False
    if view_ok and (opted_out := _optout(int(view.get("status") or 200), {}, str(view.get("head") or ""))):
        return Page(url, error=opted_out)  # the browser's copy of the page asks not to be used by AI
    if fetch_error and not view_ok:
        return Page(url, error=fetch_error)  # 404, 410, 5xx...: the browser saw no better
    browser_md = view.get("markdown", "") if view_ok else ""
    view_title = (view.get("title") or "") if view_ok else ""
    # Judge each view on its own: a bot wall in one must not pass by riding on the other's text.
    blocked = ""
    if main and (reason := looks_blocked(title, main, headers, html)):
        main, blocked = "", reason
    if browser_md and (reason := looks_blocked(view_title, strip_links(browser_md), headers, html)):
        browser_md, blocked = "", reason
    # Off the event loop: on a large page these take a moment, and Hermes's other work must go on.
    content, engine = await asyncio.to_thread(merge, main, browser_md)
    title = title or view_title
    if not content.strip():
        return Page(url, title, error=_origin(status, blocked or "no readable text on the page"))
    if engine == "crawl4ai" and view.get("final_url"):
        final = str(view["final_url"])  # all the text is the browser's: name the page it read
    links = await asyncio.to_thread(page_links, html, browser_md, final, content)
    return Page(final, title, content, engine=engine, links=links, requested=url)


def same_site(a: str, b: str) -> bool:
    """Same host, ignoring case and a leading "www."."""
    from urllib.parse import urlsplit

    def host(u: str) -> str:
        try:
            h = (urlsplit(u).hostname or "").lower()
        except ValueError:
            return ""
        return h[4:] if h.startswith("www.") else h
    return bool(host(a)) and host(a) == host(b)


async def read_pages(urls: list[str]) -> list[Page]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8,fr;q=0.6"}
    async with http_client(follow_redirects=True, timeout=FETCH_TIMEOUT, headers=headers,
                           event_hooks={"response": [_note_status]}) as client:
        return list(await asyncio.gather(*(read_page(client, u) for u in urls)))


if __name__ == "__main__" and len(sys.argv) > 1:  # manual check: python -m ... URL
    for p in asyncio.run(read_pages(sys.argv[1:])):
        print(json.dumps(p.as_result(), ensure_ascii=False)[:2000])
