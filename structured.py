"""What a page states in its markup and trafilatura's main-text extraction leaves out.

trafilatura keeps the body of an article and drops the rest as boilerplate: spec tables in a tab, a
product's details panel, a purchase box. The page still labels those facts in standard markup, which
is all this module reads:

- HTML: table rows whose first cell is a label ("Boost Clock (GHz) | 2.62"), definition lists (dt/dd);
- schema.org, as microdata (itemprop) or JSON-LD: every simple property of the page's items, whatever
  their type (a product's price and currency, an event's date, a recipe's time...), except the types
  that describe the site around the page (navigation, the publisher) and technical fields (links,
  images, rating scales).

No site convention (class names), no vocabulary list and no notion of what a "value" is: the page's
markup says what is labelled. Only pairs the main text does not already give are kept, bounded.
"""

from __future__ import annotations

import json

MAX_HTML_CHARS = 3_000_000  # parsed at most; the plugin downloads up to 15 MB of HTML
MAX_JSON_LD_CHARS = 500_000  # per script
MAX_JSON_LD_SCRIPTS = 10
MAX_JSON_DEPTH = 8
MAX_PAIRS = 300
MAX_LABEL = 80
MAX_VALUE = 200
# schema.org types that describe the site around the page, not the page (schema.org's own vocabulary).
SITE_TYPES = frozenset({"BreadcrumbList", "ListItem", "WebSite", "Organization", "SearchAction",
                        "SiteNavigationElement", "WPHeader", "WPFooter", "WPSideBar", "Brand", "ImageObject"})
# schema.org properties that are links, identifiers of the data itself or the scale of a rating.
TECHNICAL_PROPS = frozenset({"url", "image", "logo", "sameAs", "thumbnailUrl", "contentUrl", "embedUrl",
                             "mainEntityOfPage", "potentialAction", "target", "query-input", "urlTemplate",
                             "inLanguage", "bestRating", "worstRating"})


def _text(el) -> str:
    return " ".join(el.text_content().split())


def _is_label(text: str) -> bool:
    """A label names something: more letters than digits ("Boost Clock (GHz)", not "2x1R DDR5-5600")."""
    letters = sum(ch.isalpha() for ch in text)
    return 0 < len(text) <= MAX_LABEL and letters > sum(ch.isdigit() for ch in text)


def _type_names(value) -> set[str]:
    """schema.org type names from an @type or itemtype value ("https://schema.org/Product" -> Product)."""
    names = value if isinstance(value, list) else str(value or "").split()
    return {str(name).rstrip("/").rsplit("/", 1)[-1] for name in names if name}


def _pairs(root) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for tr in root.iter("tr"):
        cells = [(c.tag, _text(c)) for c in tr if c.tag in ("th", "td")]
        cells = [(tag, t) for tag, t in cells if t]
        if len(cells) >= 2 and (cells[0][0] == "th" or _is_label(cells[0][1])):
            out.append((cells[0][1], " | ".join(t for _, t in cells[1:])))
    for dt in root.iter("dt"):
        dd = dt.getnext()
        while dd is not None and dd.tag != "dd":
            dd = dd.getnext()
        if dd is not None:
            out.append((_text(dt), _text(dd)))
    return out


def _microdata(root) -> list[tuple[str, str]]:
    out = []
    for el in root.iter():
        if not isinstance(el.tag, str) or not el.get("itemprop") or el.get("itemscope") is not None:
            continue  # an item holds properties; only its simple properties are values
        scope = el.xpath("ancestor::*[@itemscope][1]/@itemtype")
        if scope and _type_names(scope[0]) & SITE_TYPES:
            continue
        value = el.get("content") or el.get("datetime") or _text(el)
        out.extend((prop, value) for prop in el.get("itemprop").split() if prop not in TECHNICAL_PROPS)
    return out


def _json_ld(root) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def walk(node, depth):
        if depth > MAX_JSON_DEPTH or len(out) >= MAX_PAIRS:
            return
        if isinstance(node, dict):
            if _type_names(node.get("@type")) & SITE_TYPES:
                return
            for key, value in node.items():
                if key.startswith("@") or key in TECHNICAL_PROPS:
                    continue
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    out.append((key, str(value)))
                elif isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:100]:
                walk(item, depth + 1)

    for script in root.xpath('//script[@type="application/ld+json"]')[:MAX_JSON_LD_SCRIPTS]:
        try:
            walk(json.loads((script.text or "")[:MAX_JSON_LD_CHARS]), 0)
        except (ValueError, RecursionError):  # broken, or nested deeper than the parser allows
            continue
    return out


def _norm(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text.casefold()).split())


def values(html: str, main_text: str) -> list[str]:
    """"Label: value" lines the page's markup states and main_text lacks, in page order."""
    if not html or "<" not in html:
        return []
    from lxml import html as lhtml

    try:
        root = lhtml.fromstring(html[:MAX_HTML_CHARS])
    except Exception:  # noqa: BLE001 - unparsable markup: nothing to add
        return []
    found = _json_ld(root)  # read before scripts are dropped
    for bad in root.xpath("//script|//style|//noscript|//template"):
        bad.drop_tree()
    found += _microdata(root) + _pairs(root)
    known = f" {_norm(main_text)} "
    lines: list[str] = []
    seen: set[str] = set()
    for label, value in found:
        label, value = label.strip().rstrip(":").strip(), value.strip()
        if not _is_label(label) or not value or len(value) > MAX_VALUE:
            continue
        if value.startswith(("http://", "https://")) or f" {_norm(value)} " in known:
            continue
        line = f"{label}: {value}"
        if (key := _norm(line)) in seen:
            continue
        seen.add(key)
        lines.append(line)
        if len(lines) >= MAX_PAIRS:
            break
    return lines
