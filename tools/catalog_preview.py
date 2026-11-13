"""Step 3 of tools/catalog_ci.sh: what Hermes and the catalog page will show for the entry.

    python catalog_preview.py OUTPUT_DIR

OUTPUT_DIR holds what Hermes's own website/scripts/extract-plugins.py wrote for the entry alone:
plugin-catalog.json (the file installed Hermes clients download: the Desktop and CLI catalog) and
plugins.json (the /docs/plugins page). Nous's CI does not limit the description; the length check
here is webgraft's own rule: the entry schema asks for a "One-line description." and rule 13 puts the
disclosure in the PR description and the README, which the plugin page renders at the pinned sha.
Card and detail widths are estimates measured on Hermes Desktop screenshots, not Hermes's layout.
"""

import json
import sys
import textwrap
from pathlib import Path

MAX_DESCRIPTION = 400  # characters; the 0.2.0 entry had about 4,000, a wall of text in Desktop
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
    print(f"   {'description':16} {len(text)} characters")
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
    if len(text) > MAX_DESCRIPTION:
        problems.append(f"the description has {len(text)} characters (webgraft's limit: {MAX_DESCRIPTION}); "
                        "the schema asks for a one-line description, and rule 13 puts the disclosure "
                        "in the PR description and the README")
    for problem in problems:
        print(f"!! {problem}")
    return 4 if problems else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
