"""Step 3 of tools/catalog_ci.sh: what Hermes and the catalog page will show for the entry.

    python catalog_preview.py OUTPUT_DIR

OUTPUT_DIR holds what Hermes's own website/scripts/extract-plugins.py wrote for the entry alone:
plugin-catalog.json (the file installed Hermes clients download: the Desktop and CLI catalog) and
plugins.json (the /docs/plugins page). Nous's CI does not limit the description. The entry schema asks
for a "One-line description."; since the 0.3.0 review (6541a26), the maintainers add the rule 13
disclosure lines to it after "Disclosure —". webgraft's own check: the summary before that marker
stays one short line; the disclosure part is measured, not limited (its wording is the reviewers').
Card and detail widths are estimates measured on Hermes Desktop screenshots, not Hermes's layout.
"""

import json
import sys
import textwrap
from pathlib import Path

MAX_DESCRIPTION = 400  # characters of the summary; the 0.2.0 entry had about 4,000, a wall of text in Desktop
DISCLOSURE = " Disclosure — "  # the maintainers' marker (6541a26)
CARD_WIDTH, CARD_LINES = 45, 4
DETAIL_WIDTH = 115


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
    summary, marker, disclosure = text.partition(DISCLOSURE)
    print(f"   {'description':16} {len(text)} characters: summary {len(summary)}"
          + (f", disclosure {len(disclosure)} (after the maintainers' marker)" if marker else ""))
    print("   Card in Desktop (estimate):")
    lines = textwrap.wrap(text, CARD_WIDTH)
    for line in lines[:CARD_LINES]:
        print(f"     | {line}")
    if len(lines) > CARD_LINES:
        print("     | ...")
    print("   Detail window in Desktop (estimate):")
    for line in textwrap.wrap(text, DETAIL_WIDTH):
        print(f"     | {line}")
    problems = []
    if str(card.get("description") or "").strip() != text.strip():
        problems.append("the page and the clients get different descriptions")
    if "\n" in text.strip():
        problems.append("the description spans several lines")
    if len(summary) > MAX_DESCRIPTION:
        problems.append(f"the summary has {len(summary)} characters (webgraft's limit: {MAX_DESCRIPTION}); "
                        "the schema asks for a one-line description: details go after the disclosure "
                        "marker, in the PR description and in the README")
    for problem in problems:
        print(f"!! {problem}")
    return 4 if problems else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
