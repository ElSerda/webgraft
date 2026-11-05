"""Spots values (numbers with a unit, prices, versions, large numbers) in text. Internal only.

Two uses, where a miss or an extra match costs little:

* extract: which lines of Crawl4AI's browser view hold values the main text lacks;
* the footer: which pages opened in earlier turns an answer relies on.

Nothing here is shown to the user as a verdict on the answer.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Canonical unit -> spellings, lowercase. Longer spellings first inside the regex.
_UNITS: dict[str, tuple[str, ...]] = {
    "tb/s": ("tb/s", "to/s", "tb/sec", "to/sec"),
    "gb/s": ("gb/s", "go/s", "gb/sec", "go/sec", "gbytes/s"),
    "mb/s": ("mb/s", "mo/s", "mb/sec", "mo/sec"),
    "gbps": ("gbps", "gbit/s", "gb/s"),
    "mbps": ("mbps", "mbit/s"),
    # Bare "to" (French terabytes) and "a" (amps) are left out: they are common words.
    "tb": ("tb", "terabytes", "téraoctets"),
    "gb": ("gb", "go", "gib", "gigabytes", "gigaoctets"),
    "mb": ("mb", "mo", "mib", "megabytes", "mégaoctets"),
    "kb": ("kb", "ko", "kib"),
    "ghz": ("ghz",),
    "mhz": ("mhz",),
    "khz": ("khz",),
    "hz": ("hz",),
    "pflops": ("pflops", "petaflops"),
    "tflops": ("tflops", "teraflops"),
    "tops": ("tops",),
    "kw": ("kw",),
    "w": ("w", "watts", "watt"),
    "v": ("v", "volts", "volt"),
    "a": ("amps", "ampères"),
    "bit": ("bit", "bits", "-bit"),
    "°c": ("°c", "℃", "degrees c", "degrés"),
    "nm": ("nm",),
    "mm": ("mm",),
    "cm": ("cm",),
    "ms": ("ms",),
    "µs": ("µs",),
    "fps": ("fps", "ips"),
    "tok/s": ("tok/s", "tokens/s"),
    # Model sizes and context windows, as AI answers write them: 70B, 1.5B, 350M parameters, 128K tokens.
    "b-params": ("b",),
    "m-params": ("m",),
    "k-tokens": ("k",),
    "%": ("%",),
}
UNIT_SPELLINGS = {s: u for u, spellings in _UNITS.items() for s in spellings}
# gb/s is ambiguous (gigabytes or gigabits per second): keep it as bytes, which is far more common in specs.
UNIT_SPELLINGS["gb/s"] = "gb/s"
_UNIT_ALT = "|".join(sorted((re.escape(s) for s in UNIT_SPELLINGS), key=len, reverse=True))

_CURRENCIES = {"$": "usd", "usd": "usd", "€": "eur", "eur": "eur", "£": "gbp", "gbp": "gbp"}

# A number: 1 344 / 1,344 / 1.344 / 2.62 / 2,62 / 14080. Spaces include no-break and thin spaces.
_NUM = r"\d{1,3}(?:[    ,.]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_QUANTITY_RE = re.compile(
    rf"(?<![\w.,])({_NUM})\s?({_UNIT_ALT})(?![\w/])",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(
    rf"(?:([$€£])\s?({_NUM}))|(?:(?<![\w.,])({_NUM})\s?([$€£]|usd|eur|gbp)(?![\w]))",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"(?<![\w.])(v?\d+\.\d+(?:\.\d+)+|b\d{4,6})(?!\w|\.\w)", re.IGNORECASE)
_BARE_RE = re.compile(rf"(?<![\w.,/])({_NUM})(?![\w,/%]|\.\d|\s?(?:{_UNIT_ALT})(?![\w/]))", re.IGNORECASE)

_MODEL_SOURCES_RE = re.compile(r"(?im)^[ \t*_#>-]*(?:sources?|références|references)[ \t*_]*:")
_FOOTER_MARKERS = ("Sources (pages ouvertes)", "Sources (opened pages)", "Verification:", "Vérification :")


@dataclass(frozen=True)
class Fact:
    """One value found in an answer."""

    text: str  # as written in the answer
    kind: str  # quantity | price | version | number
    values: frozenset[str]  # candidate normalized numbers (ambiguous separators give two)
    unit: str = ""


def _clean(text: str) -> str:
    text = re.sub(r"[¹²³⁰-₟]", "", text)  # footnote marks, before NFKC turns them into digits
    text = re.sub(r"[‐-―−]", "-", text)  # non-breaking hyphen, dashes, minus sign
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # markdown links -> their text
    return text.replace("**", "").replace("__", "")


def _numbers(raw: str) -> frozenset[str]:
    """Every reading of a written number, as plain decimal strings without trailing zeros."""
    s = re.sub(r"[    ]", "", raw)
    readings = set()
    if re.fullmatch(r"\d+", s):
        readings.add(s)
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", s):  # 1,344 or 1,344.5
        readings.add(s.replace(",", ""))
        if s.count(",") == 1 and "." not in s:
            readings.add(s.replace(",", "."))  # French decimal 1,344
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", s):  # 1.344 or 1.344,5
        readings.add(s.replace(".", "").replace(",", "."))
        if s.count(".") == 1 and "," not in s:
            readings.add(s)  # English decimal 1.344
    else:
        readings.add(s.replace(",", "."))
    out = set()
    for r in readings:
        try:
            out.add(format(float(r), "f").rstrip("0").rstrip(".") if "." in r else str(int(r)))
        except ValueError:
            continue
    return frozenset(out)


def _unit(spelling: str) -> str:
    return UNIT_SPELLINGS.get(spelling.lower(), spelling.lower())


def extract_facts(answer: str) -> list[Fact]:
    """Values in an answer. The sources footer, URLs and a sources list the model wrote are skipped."""
    for marker in _FOOTER_MARKERS:
        idx = answer.find(marker)
        if idx >= 0:
            answer = answer[:idx]
    # A sources list the model wrote itself: its link labels hold status codes and dates, not claims.
    answer = _MODEL_SOURCES_RE.split(answer, maxsplit=1)[0]
    text = re.sub(r"https?://\S+", " ", _clean(answer))
    facts: list[Fact] = []
    taken: list[tuple[int, int]] = []

    def add(span, fact):
        if any(a < span[1] and span[0] < b for a, b in taken):
            return
        # Part of a product code such as NP-400-A1 or 24V-3x8: a name, not a value.
        if re.match(r"-\w", text[span[1]:span[1] + 2]) or re.search(r"\w-$", text[max(0, span[0] - 2):span[0]]):
            taken.append(span)
            return
        taken.append(span)
        if fact not in facts:
            facts.append(fact)

    for m in _VERSION_RE.finditer(text):
        add(m.span(), Fact(m.group(0), "version", frozenset({m.group(1).lower().lstrip("v")})))
    for m in _PRICE_RE.finditer(text):
        num = m.group(2) or m.group(3)
        cur = _CURRENCIES[(m.group(1) or m.group(4)).lower()]
        add(m.span(), Fact(m.group(0).strip(), "price", _numbers(num), cur))
    for m in _QUANTITY_RE.finditer(text):
        add(m.span(), Fact(m.group(0).strip(), "quantity", _numbers(m.group(1)), _unit(m.group(2))))
    for m in _BARE_RE.finditer(text):
        values = _numbers(m.group(1))
        # Small bare numbers are list counts, ordinals and generations: too common to mean anything.
        if all(float(v) < 100 for v in values):
            continue
        add(m.span(), Fact(m.group(1).strip(), "number", values))
    return facts


@dataclass
class _Occurrence:
    url: str
    values: frozenset[str]
    unit: str
    line: str = ""  # lowercased line or table row holding the value, for a unit written elsewhere in it


def index_page(url: str, text: str) -> list[_Occurrence]:
    text = _clean(text)
    occ = []
    for m in _QUANTITY_RE.finditer(text):
        occ.append(_Occurrence(url, _numbers(m.group(1)), _unit(m.group(2))))
    for m in _PRICE_RE.finditer(text):
        num = m.group(2) or m.group(3)
        cur = _CURRENCIES[(m.group(1) or m.group(4)).lower()]
        occ.append(_Occurrence(url, _numbers(num), cur))
    for m in _VERSION_RE.finditer(text):
        occ.append(_Occurrence(url, frozenset({m.group(1).lower().lstrip("v")}), "version"))
    for m in re.finditer(rf"(?<![\w.,])({_NUM})(?![\w])", text):
        occ.append(_Occurrence(url, _numbers(m.group(1)), "", _line(text, m.start())))
    return occ


def _line(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start:end if end >= 0 else len(text)].lower()


def _matches(fact: Fact, occ: _Occurrence) -> bool:
    if not (fact.values & occ.values):
        return False
    if fact.kind == "version":
        return occ.unit == "version"
    if fact.kind in ("quantity", "price"):
        if occ.unit == fact.unit:
            return True
        # A table row such as "| AI TOPS | 2064 |" puts the unit before the number.
        spellings = _UNITS.get(fact.unit, (fact.unit,))
        return occ.unit == "" and any(re.search(rf"(?<!\w){re.escape(sp)}(?!\w)", occ.line) for sp in spellings)
    return True


def pages_used(answer: str, pages: list[dict]) -> list[str]:
    """URLs of the pages holding a value of the answer that has a unit, a currency or a version.

    pages: [{"url": ..., "content": ...}]. Bare numbers are left out: they match too many pages.
    """
    facts = [f for f in extract_facts(answer) if f.kind != "number"]
    index = [o for p in pages if p.get("content") for o in index_page(p.get("url", ""), p["content"])]
    used: list[str] = []
    for fact in facts:
        used.extend(o.url for o in index if _matches(fact, o) and o.url not in used)
    return used
