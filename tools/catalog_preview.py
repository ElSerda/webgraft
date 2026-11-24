"""Step 3 of tools/catalog_ci.sh: what Hermes and the catalog page will show for the entry.

    python catalog_preview.py OUTPUT_DIR

OUTPUT_DIR holds what Hermes's own website/scripts/extract-plugins.py wrote for the entry alone:
plugin-catalog.json (the file installed Hermes clients download: the Desktop and CLI catalog) and
plugins.json (the /docs/plugins page). Nous's CI does not limit the description. Since the 0.3.0
review (6541a26), the maintainers add the rule 13 disclosure to it after "Disclosure —". Line breaks
are kept: an intro paragraph, then lines starting with "- ", as in the api-indonesia entry, which the
maintainers' review (73e6dde) kept. webgraft's own check: the intro paragraph, the part a card shows,
stays short; the rest is measured, not limited (the disclosure's wording is the reviewers').
Card and detail widths are estimates measured on Hermes Desktop screenshots, not Hermes's layout.
"""

import json
import re
import sys
import textwrap
from pathlib import Path

MAX_INTRO = 400  # characters of the intro paragraph; the 0.2.0 entry had about 4,000, a wall of text in Desktop
DISCLOSURE = re.compile(r"\s+Disclosure — ")  # the maintainers' marker (6541a26)
CARD_WIDTH, CARD_LINES = 45, 4
DETAIL_WIDTH = 115


def wrap(text: str, width: int) -> list[str]:
    """Wrap each line on its own: the catalog keeps the description's line breaks."""
    return [part for line in text.strip().splitlines() for part in (textwrap.wrap(line, width) or [""])]


def main(out: Path) -> int:
    client = json.loads((out / "plugin-catalog.json").read_text(encoding="utf-8"))["entries"]
    page = json.loads((out / "plugins.json").read_text(encoding="utf-8"))
    if len(client) != 1 or len(page) != 1:
        print(f"!! expected one entry, got {len(client)} for clients and {len(page)} for the page")
        return 4
    entry, card = client[0], page[0]
    for key in ("name", "title", "version", "tier", "category", "platforms", "requires_hermes"):
        print(f"   {key:16} {entry.get(key)!r}")
    print(f"   {'readme on page':16} {card.get('readmeUrl') or card.get('readme')!r}")
    text = str(entry.get("description") or "")
    summary, *rest = DISCLOSURE.split(text.strip(), maxsplit=1)
    intro = summary.strip().split("\n\n")[0]
    print(f"   {'description':16} {len(text)} characters: intro {len(intro)}, summary {len(summary)}"
          + (f", disclosure {len(rest[0])} (after the maintainers' marker)" if rest else ""))
    print("   Card in Desktop (estimate):")
    lines = wrap(text, CARD_WIDTH)
    for line in lines[:CARD_LINES]:
        print(f"     | {line}")
    if len(lines) > CARD_LINES:
        print("     | ...")
    print("   Detail window in Desktop (estimate):")
    for line in wrap(text, DETAIL_WIDTH):
        print(f"     | {line}")
    problems = []
    if str(card.get("description") or "").strip() != text.strip():
        problems.append("the page and the clients get different descriptions")
    if len(intro) > MAX_INTRO:
        problems.append(f"the intro paragraph has {len(intro)} characters (webgraft's limit: {MAX_INTRO}); "
                        "it is what a card shows: details go in the lines below it, in the PR "
                        "description and in the README")
    for problem in problems:
        print(f"!! {problem}")
    return 4 if problems else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
