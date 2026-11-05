"""The bundled skill meets Hermes's skill authoring standards (skills/AGENTS.md, tests/skills/test_authoring_standards.py)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = sorted((ROOT / "skills").glob("*/SKILL.md"))
DESC_LIMIT = 60  # agent.skill_utils.SKILL_PROMPT_DESC_LIMIT
# Bundled Hermes skills this one may point to (checked against Hermes 0.22.6).
KNOWN_HERMES_SKILLS = {"grounded-citations", "pdf"}
SECTIONS = ["## When to Use", "## Prerequisites", "## Quick Reference", "## Procedure", "## Pitfalls", "## Verification"]


def frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    end = text.index("\n---\n", 4)
    fields, current = {}, None
    for line in text[4:end].splitlines():  # flat reader: enough for these keys, no yaml dependency
        if m := re.match(r"^(\w+):\s*(.*)$", line):
            current = m.group(1)
            fields[current] = m.group(2).strip().strip('"')
        elif m := re.match(r"^\s+(\w+):\s*(.*)$", line):
            fields[m.group(1)] = m.group(2).strip()
    return fields, text[end + 5:]


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_frontmatter_fields(path):
    fm, _ = frontmatter(path)
    for field in ("name", "description", "version", "author", "license", "platforms", "tags"):
        assert fm.get(field), f"missing {field}"
    assert fm["name"] == path.parent.name


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_description_hardline(path):
    desc = frontmatter(path)[0]["description"]
    assert len(desc) <= DESC_LIMIT, f"{len(desc)} chars"
    assert desc.endswith(".")
    assert not re.search(r"\b(powerful|comprehensive|seamless|revolutionary|cutting-edge|state-of-the-art)\b", desc, re.I)


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_related_skills_exist_in_hermes(path):
    related = frontmatter(path)[0].get("related_skills", "[]").strip("[]")
    assert {r.strip() for r in related.split(",") if r.strip()} <= KNOWN_HERMES_SKILLS


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_section_order_and_no_local_paths(path):
    _, body = frontmatter(path)
    assert re.search(r"^# .+ Skill$", body, re.M)
    positions = [body.find(s) for s in SECTIONS]
    assert all(p >= 0 for p in positions), [s for s, p in zip(SECTIONS, positions) if p < 0]
    assert positions == sorted(positions)
    assert not re.search(r"/home/[a-z0-9_-]+/|[A-Z]:\\+Users\\+", body)
