"""What a site says about language models: llms.txt, and opt-outs from AI use.

llms.txt (https://llmstxt.org): a Markdown file at the site root, written for language models. It
opens with an H1 and lists the site's pages as Markdown links, often to clean `.md` versions of
them. When the page asked for has a `.md` version listed there, webgraft reads that instead of
the HTML: better text, and what the site asked for. The file is fetched once per site and checked:
plain text that opens with "# ", never HTML (many sites answer any path with their home page).

Opt-outs: a page that asks not to be used by AI is returned as an error, not as content:
`noai` in the X-Robots-Tag header or in a robots meta tag, and the TDM Reservation Protocol
(W3C community group: `tdm-reservation: 1` as a header or a meta tag).
"""

from __future__ import annotations

import codecs
import re
import threading
import time
from collections import OrderedDict
from urllib.parse import urljoin, urlsplit

LLMS_TTL = 3600          # seconds a site's llms.txt (or its absence) is remembered
LLMS_MAX_BYTES = 2 * 1024 * 1024
LLMS_TIMEOUT = 8
MAX_SITES = 200

BOM = chr(0xFEFF)  # byte order mark that some editors put at the start of a text file
# Bounded repeats: an llms.txt (up to 2 MB) or a page head with an unclosed "[" or "<meta" must not
# make every later match attempt re-read the rest of the file.
_LINK = re.compile(r"\[[^\]]{0,500}\]\(\s{0,20}<?([^)\s>]{1,2000})>?(?:\s{1,20}\"[^\"]{0,500}\")?\s{0,20}\)")
_lock = threading.Lock()
_sites: "OrderedDict[str, tuple[float, frozenset[str] | None]]" = OrderedDict()


# ---------------------------------------------------------------- opt-outs


_META = re.compile(r"<meta\b[^>]{0,2000}>", re.IGNORECASE)
_ATTR = re.compile(r"""(\w[\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")


def _metas(html: str) -> list[dict[str, str]]:
    head = html[: html.lower().find("</head>")] if "</head>" in html.lower() else html[:200_000]
    out = []
    for tag in _META.findall(head):
        attrs = {m.group(1).lower(): (m.group(2) or m.group(3) or m.group(4) or "") for m in _ATTR.finditer(tag)}
        out.append(attrs)
    return out


def ai_optout(headers: dict | None, html: str = "") -> str:
    """Why the page asks not to be used by AI, or an empty string."""
    h = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}
    if re.search(r"\bnoai\b", h.get("x-robots-tag", "")):
        return "X-Robots-Tag: noai"
    if h.get("tdm-reservation", "").strip() == "1":
        return "tdm-reservation: 1 (TDM Reservation Protocol)"
    for meta in _metas(html) if html else []:
        name = meta.get("name", "").lower()
        content = meta.get("content", "").lower()
        if name in ("robots", "ai", "ai-robots") and re.search(r"\bnoai\b", content):
            return f'<meta name="{name}" content="noai">'
        if name == "tdm-reservation" and content.strip() == "1":
            return '<meta name="tdm-reservation" content="1"> (TDM Reservation Protocol)'
    return ""


# ---------------------------------------------------------------- llms.txt


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme in ("http", "https") and parts.netloc else ""


def parse_llms_txt(text: str, base: str) -> frozenset[str] | None:
    """The URLs an llms.txt links to, or None when the text is not an llms.txt."""
    body = text.lstrip(BOM + " \t\r\n")
    if not body.startswith("# ") or body[:200].lower().lstrip().startswith(("<!doctype", "<html")):
        return None
    return frozenset(urljoin(base + "/", m.group(1)) for m in _LINK.finditer(body))


async def get_capped(client, url: str, limit: int, timeout: float | None = None):
    """(response, body) for a GET, read as a stream and stopped past `limit` bytes; body is None
    when the file is larger. httpx's timeouts apply per read, so a server that keeps sending
    would otherwise fill the memory before the size is known."""
    kwargs = {"timeout": timeout} if timeout else {}
    async with client.stream("GET", url, **kwargs) as response:
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                return response, None
            chunks.append(chunk)
        return response, b"".join(chunks)


# Codecs a server may name for a page. Python also accepts names such as punycode, idna, hex or
# zlib, which are not text encodings and can be slow to decode: those fall back to UTF-8.
_TEXT_CODECS = frozenset(
    codecs.lookup(name).name for name in (
        "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be",
        "ascii", "latin-1", "iso8859-2", "iso8859-3", "iso8859-4", "iso8859-5", "iso8859-6", "iso8859-7",
        "iso8859-8", "iso8859-9", "iso8859-10", "iso8859-11", "iso8859-13", "iso8859-14", "iso8859-15",
        "iso8859-16", "cp1250", "cp1251", "cp1252", "cp1253", "cp1254", "cp1255", "cp1256", "cp1257",
        "cp1258", "cp874", "cp437", "cp850", "cp866", "koi8-r", "koi8-u", "mac-roman", "mac-cyrillic",
        "shift_jis", "cp932", "euc-jp", "iso2022-jp", "euc-kr", "cp949", "gb2312", "gbk", "gb18030",
        "big5", "big5hkscs", "cp950", "tis-620",
    )
)


def text_codec(name: str | None) -> str:
    """The codec to decode a page with: the one the server named when it is a text encoding."""
    try:
        codec = codecs.lookup(name or "utf-8").name
    except LookupError:
        return "utf-8"
    return codec if codec in _TEXT_CODECS else "utf-8"


def decoded(response, body: bytes) -> str:
    return body.decode(text_codec(getattr(response, "encoding", None)), "replace")


async def llms_index(client, url: str) -> tuple[str, frozenset[str]] | None:
    """(llms.txt URL, the URLs it lists) for the page's site, or None. Fetched once per hour per site."""
    origin = _origin(url)
    if not origin:
        return None
    now = time.monotonic()
    with _lock:
        cached = _sites.get(origin)
    if cached and now - cached[0] < LLMS_TTL:
        return (f"{origin}/llms.txt", cached[1]) if cached[1] is not None else None
    links = None
    try:
        response, body = await get_capped(client, f"{origin}/llms.txt", LLMS_MAX_BYTES, LLMS_TIMEOUT)
        ctype = response.headers.get("content-type", "").lower()
        if response.status_code == 200 and "html" not in ctype and body is not None:
            links = parse_llms_txt(decoded(response, body), origin)
    except Exception:  # noqa: BLE001 - no index is the normal case
        links = None
    with _lock:
        _sites[origin] = (now, links)
        _sites.move_to_end(origin)
        while len(_sites) > MAX_SITES:
            _sites.popitem(last=False)
    return (f"{origin}/llms.txt", links) if links is not None else None


def markdown_version(url: str, links: frozenset[str]) -> str:
    """The `.md` version of `url` that the site's llms.txt lists, or an empty string."""
    page = url.split("#")[0]
    bare = page.split("?")[0].rstrip("/")
    candidates = {bare + ".md", bare + "/index.md", bare + "/index.html.md"}
    if bare.endswith((".html", ".htm")):
        candidates.add(bare + ".md")
        candidates.add(re.sub(r"\.html?$", ".md", bare))
    for link in links:
        if link.split("#")[0] in candidates:
            return link.split("#")[0]
    return ""


def title_of(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:200]
    return ""
