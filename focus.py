"""Long pages: put the passages about the question first, so the window the model sees keeps them.

Hermes shows the model at most `web.extract_char_limit` characters of each page (15,000 by default):
the first 75% and the last 25%, the middle goes to a file. In a long document the passage that
answers the question is often in that middle (the keyless_rescue setting, 215,000 characters into
Hermes's 255,000-character configuration docs). webgraft knows the question (the user's message and the model's
search queries), so it moves the passages that mention its terms to the top, with a note, and
keeps the rest after them in the original order. Nothing is removed; short pages are unchanged.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict

DEFAULT_WINDOW = 15_000  # Hermes's DEFAULT_EXTRACT_CHAR_LIMIT
HEAD_SHARE = 0.75        # Hermes keeps this share of the window from the top
KEEP_OPENING = 600       # the page's opening (title, product name) always stays first
MAX_REMEMBERED = 200
PASSAGE_CHARS = 600      # target size of a passage cut out of a long block
MAX_BLOCK_CHARS = 1_500  # a paragraph longer than this is cut into passages
MAX_TABLE_CHARS = 4_000  # a table is kept whole up to this size

# Words that say nothing about what a passage is about (English and French, the plugin's languages).
STOPWORDS = set("""
a an and are as at be by can do does for from has have how i in is it its me my of on or our please
show tell that the their them there these this to us was what when where which who why will with
you your about find search web page give list get
au aux avec ce ces cette dans de des du elle en est et il ils je la le les leur ma mes mon ne nous
ou par pas pour qu que qui sa se ses son sur ta te tes ton tu un une vos votre vous moi trouve
trouver cherche chercher donne donner quel quelle quels quelles comment est-ce fait faire peux
peut sont
""".split())

_TOKEN = re.compile(r"[^\W_][\w.+-]*", re.UNICODE)
_OPERATOR = re.compile(r"\b\w+:\S+")
_lock = threading.Lock()
_pending: "OrderedDict[str, tuple[list[str], int]]" = OrderedDict()  # url -> (terms, window)


def _telling(word: str) -> bool:
    """A number or an identifier: 2.1b, 0.22.7, keyless_rescue, extract_char_limit."""
    return any(ch.isdigit() for ch in word) or "_" in word


def terms_of(*texts: str) -> list[str]:
    """Search terms from free text, the most telling first: numbers and identifiers (0.22.7,
    keyless_rescue), then the other words of 3+ letters that are not stop words. A dotted name
    such as web.keyless_rescue also gives its last part, the way a YAML file writes it nested."""
    seen: dict[str, int] = {}

    def add(word: str) -> None:
        if not word or word in STOPWORDS or word in seen:
            return
        if _telling(word) or len(word) >= 3:
            seen[word] = 3 if _telling(word) else 1

    for text in texts:
        text = _OPERATOR.sub(" ", text or "")  # site:example.com, filetype:pdf...
        for raw in _TOKEN.findall(text.lower()):
            word = raw.strip(".+-")
            add(word)
            parts = word.split(".")
            if len(parts) > 1 and not all(part.isdigit() for part in parts):
                add(parts[-1])
    return sorted(seen, key=lambda w: -seen[w])[:12]


def _hits(block: str, term: str) -> int:
    return len(re.findall(rf"(?<![\w]){re.escape(term)}(?![\w])", block))


def _weights(blocks: list[str], terms: list[str]) -> dict[str, float]:
    """How telling each term is in this page: rare terms count, terms found everywhere do not
    (inverse document frequency over the page's passages); a number or an identifier counts double."""
    import math
    n = len(blocks)
    out = {}
    for term in terms:
        df = sum(1 for b in blocks if _hits(b, term))
        if df:
            out[term] = math.log((n + 1) / df) * (2 if _telling(term) else 1)
    return out


_LEADERS = re.compile(r"\.{5,}")


def _score(block: str, weights: dict[str, float]) -> float:
    """Relevance of a passage. A table of contents (dot leaders) scores nothing, and a long passage
    must hold the terms densely: a 7,000-character block that names the product once is not about it."""
    if _LEADERS.search(block):
        return 0.0
    score = 0.0
    for term, weight in weights.items():
        hits = _hits(block, term)
        if hits:
            score += weight * (1 + min(hits, 3) / 3)
    return score / max(1.0, (len(block) / 800) ** 0.5)


def _continuation(blocks: list[str], i: int) -> list[int]:
    """Blocks that carry on passage i: the rest of a table split by page headers, up to 6 blocks."""
    out = []
    for j in range(i + 1, min(i + 7, len(blocks))):
        b = blocks[j].lstrip()
        if b.startswith("|"):
            out.append(j)
        elif len(b) < 80 and blocks[i].lstrip().startswith("|"):
            out.append(j)  # a running page header inside a table
        else:
            break
    while out and not blocks[out[-1]].lstrip().startswith("|"):
        out.pop()
    return out


def passages(text: str) -> list[str]:
    """The page cut into passages. Paragraphs (blank-line separated) first; a block longer than
    MAX_BLOCK_CHARS, such as trafilatura's text of an HTML page, which has no blank lines, is cut
    at line ends into passages of about PASSAGE_CHARS, never inside a table of reasonable size."""
    out: list[str] = []
    for block in (b for b in re.split(r"\n\s*\n", text) if b.strip()):
        if len(block) <= MAX_BLOCK_CHARS:
            out.append(block)
            continue
        chunk: list[str] = []
        size, in_table = 0, False
        for line in block.split("\n"):
            is_table = line.lstrip().startswith("|")
            inside_table = is_table and in_table and size < MAX_TABLE_CHARS
            if chunk and size >= PASSAGE_CHARS and not inside_table:
                out.append("\n".join(chunk))
                chunk, size = [], 0
            chunk.append(line)
            size += len(line) + 1
            in_table = is_table
        if chunk:
            out.append("\n".join(chunk))
    return out


def arrange(text: str, terms: list[str], window: int = DEFAULT_WINDOW) -> str:
    """The page with the passages about `terms` first when it is longer than `window`."""
    if len(text) <= window or not terms:
        return text
    blocks = passages(text)
    if len(blocks) < 3:
        return text
    opening, rest = [], blocks
    size = 0
    while rest and size + len(rest[0]) <= KEEP_OPENING and not _LEADERS.search(rest[0]):
        size += len(rest[0]) + 2
        opening.append(rest[0])
        rest = rest[1:]
    lowered = [b.lower() for b in rest]
    weights = _weights(lowered, terms)
    if not weights:
        return text
    scored = [(i, _score(b, weights)) for i, b in enumerate(lowered)]
    best = max(s for _, s in scored)
    # Passages close to the best match only: a page full of the product's name moves nothing.
    hits = [(i, s) for i, s in scored if s > 0 and s >= best * 0.5]
    if not hits or best < 1.0:
        return text
    budget = int(window * HEAD_SHARE * 0.9) - size
    chosen: set[int] = set()
    groups: list[list[int]] = []
    for i, _ in sorted(hits, key=lambda h: (-h[1], h[0])):
        group = [j for j in [i, *_continuation(rest, i)] if j not in chosen]
        size_needed = sum(len(rest[j]) + 2 for j in group)
        if group and size_needed <= budget:
            chosen.update(group)
            groups.append(group)
            budget -= size_needed
    if not chosen or min(chosen) == 0 and chosen == set(range(len(chosen))):
        return text  # the passages were already at the top
    # The note does not name the terms: Hermes caches the page for every session for a while, and
    # the words of one user's question must not show up in another user's result.
    note = ("[webgraft: this page is long. The passages about the question come first; "
            "the rest of the page follows in its original order. Nothing was removed.]")
    # Best match first; passages next to each other in the page stay together and in order.
    order: list[int] = []
    for group in groups:
        lead = group[0]
        before = [j for j in range(lead - 2, lead) if j in chosen and j not in order]
        order.extend(before + [j for j in group if j not in order])
    first = [rest[i] for i in order]
    after = [b for i, b in enumerate(rest) if i not in chosen]
    parts = opening + [note] + first + ["[webgraft: rest of the page, in its original order]"] + after
    return "\n\n".join(parts)


def _key(url: str) -> str:
    """The URL as Hermes passes it to the provider (normalize_url_for_request: IDNA host,
    percent-encoded path), then without fragment, trailing slash or case in scheme and host."""
    try:
        from tools.url_safety import normalize_url_for_request
        url = normalize_url_for_request(url or "")
    except Exception:  # noqa: BLE001 - outside Hermes
        pass
    url = (url or "").split("#")[0].strip()
    scheme, sep, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}{sep}{host.lower()}{slash}{path}".rstrip("/")


def hermes_window() -> int:
    """Hermes's per-page budget: web.extract_char_limit, else its default."""
    try:
        from hermes_cli.config import load_config_readonly
        value = ((load_config_readonly() or {}).get("web") or {}).get("extract_char_limit")
        return max(2_000, min(int(value), 500_000)) if value is not None else DEFAULT_WINDOW
    except Exception:  # noqa: BLE001 - outside Hermes, or an odd value: Hermes falls back the same way
        return DEFAULT_WINDOW


def expect(url: str, terms: list[str], window: int) -> None:
    """Remember what the next read of `url` is for (set by the pre_tool_call hook)."""
    if not url or not terms:
        return
    with _lock:
        _pending[_key(url)] = (terms, window)
        _pending.move_to_end(_key(url))
        while len(_pending) > MAX_REMEMBERED:
            _pending.popitem(last=False)


def take(url: str) -> tuple[list[str], int] | None:
    """What a read of `url` is for, or None: the page is then only cut, never arranged for the
    question of another call (another session may be reading at the same time)."""
    with _lock:
        return _pending.pop(_key(url), None)
