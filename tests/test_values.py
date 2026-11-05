"""Value spotting, used to tell which pages an answer relies on and which browser lines to keep."""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

load()
values = importlib.import_module("webgraft.values")

OFFICIAL = {
    "url": "https://portal.example/cloud",
    "content": (
        "| Plan | Price | Cloud agent |\n"
        "| Plus | $20/month | 4 GB RAM, 25 GB disk |\n"
        "| Super | $100/month | 4 GB RAM, 100 GB disk |\n"
        "| Ultra | $200/month | 8 GB RAM, 100 GB disk |\n"
        "| Seats | 1,000 |\n"
        "Logs kept: 1,024 MB per day. Storage encrypted with 256-bit keys.\n"
        "Median speed\n65.3 tok/s¹\n"
        "Uptime 99.5%, median latency 250 ms.\n"
    ),
}
OTHER = {"url": "https://other.example/x", "content": "Nothing relevant here, 2024."}


def used(answer):
    return values.pages_used(answer, [OFFICIAL, OTHER])


def test_spellings_and_units_written_differently_still_match():
    for answer in ("1 024 Mo par jour", "65,3 tok/s", "200 $", "99,5 %", "clés 256‑bit"):
        assert used(answer) == [OFFICIAL["url"]], answer


def test_unit_must_match():
    assert used("Elle a 250 Go de mémoire.") == []  # 250 is on the page, but as milliseconds


def test_versions_including_at_sentence_end():
    pages = [{"url": "https://repo.example/releases", "content": "Hermes Agent v0.22.7 is out. Build b12061."}]
    assert values.pages_used("La dernière version stable est 0.22.7.", pages) == [pages[0]["url"]]
    assert values.pages_used("La dernière build est b12061.", pages) == [pages[0]["url"]]
    assert values.pages_used("La dernière version est v0.17.0.", pages) == []


def test_model_sizes_and_context_lengths():
    pages = [{"url": "https://models.example", "content": "Hermes 4 comes in 14B, 70B and 405B, with a 128K context."}]
    assert values.pages_used("A 70B model with 128K tokens.", pages) == [pages[0]["url"]]
    assert values.pages_used("A 7B model.", pages) == []


def test_bare_numbers_do_not_mark_a_page_as_used():
    assert used("Il y a 1 000 sièges.") == []


def test_small_numbers_codes_and_model_sources_are_skipped():
    assert values.extract_facts("Agents de 2e génération, 3 profils, 2 à 3 fois moins.") == []
    assert [f.text for f in values.extract_facts("Référence NP-400-A1, alimentation 24V-3×8.")] == []
    answer = "Disque : 100 Go.\n\nSources : [Portail (403 - Accès refusé)](https://x.example/2024/11)"
    assert [f.text for f in values.extract_facts(answer)] == ["100 Go"]
