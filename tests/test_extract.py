"""Extraction helpers that need no network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

import importlib  # noqa: E402

load()
ex = importlib.import_module("webgraft.extract")

MAIN = "Hermes Cloud\nAn always-on Hermes Agent, hosted for you, included in every paid plan. " * 10
BROWSER = """
# Hermes Cloud
[Start now](https://portal.example/start)
## Plans
| Plan | Price | Cloud agent |
| --- | --- | --- |
| Plus | $20 / month | 4 GB RAM, 25 GB disk |
| Super | $100 / month | 4 GB RAM, 100 GB disk |
| Ultra | $200 / month | 8 GB RAM, 100 GB disk |
## Footer
[Privacy](https://portal.example/privacy) © 2026
"""


PLANS_HTML = """<html><body><article><p>Hermes Cloud, an always-on agent.</p></article>
<aside><table><tr><th>Plan</th><th>Price</th><th>Cloud agent</th></tr>
<tr><td>Plus</td><td>$20 / month</td><td>4 GB RAM, 25 GB disk</td></tr>
<tr><td>Ultra</td><td>$200 / month</td><td>8 GB RAM, 100 GB disk</td></tr></table>
<footer><a href="/privacy">Privacy</a> 2026</footer></aside></body></html>"""


def test_table_rows_the_main_text_lacks_are_added():
    content, engine = ex.merge(MAIN, "", PLANS_HTML)
    assert engine == "trafilatura" and content.startswith(MAIN)
    assert ex.STRUCTURE_HEADING in content
    assert "- Plus: $20 / month | 4 GB RAM, 25 GB disk" in content and "- Ultra: $200 / month" in content
    assert "Privacy" not in content


def test_rows_already_in_the_main_text_are_not_repeated():
    content, _ = ex.merge(MAIN + " Plus costs $20 / month | 4 GB RAM, 25 GB disk.", "", PLANS_HTML)
    assert "- Plus:" not in content and "- Ultra:" in content


def test_merge_uses_browser_text_when_main_text_is_empty():
    # Crawl4AI hands over its markdown without link targets or images (ignore_links, ignore_images)
    content, engine = ex.merge("", "# Hermes Cloud\n| Ultra | $200 / month | 8 GB RAM |")
    assert engine == "crawl4ai" and "8 GB RAM" in content


def test_values_from_the_rendered_page_come_after_the_downloaded_ones():
    rendered = PLANS_HTML.replace("</table>", "<tr><td>Max</td><td>$400 / month</td><td>16 GB RAM</td></tr></table>")
    content, engine = ex.merge(MAIN, "", PLANS_HTML, rendered)
    assert engine == "trafilatura+crawl4ai"
    assert content.index("- Ultra:") < content.index("- Max: $400 / month")
    assert content.count("- Plus:") == 1


def test_merge_without_browser():
    assert ex.merge(MAIN, "") == (MAIN, "trafilatura")


def test_bot_walls():
    assert ex.looks_blocked("Just a moment...", "Checking your browser before accessing.")
    assert ex.looks_blocked("Access Denied", "You don't have permission to access this server.")


def test_error_words_in_a_200_page_are_content():
    # Errors come from the HTTP status only: a page that answers 200 is never an error by its text.
    assert ex.looks_blocked("Page introuvable", "Désolé, cette page n'existe plus.") == ""
    assert ex.looks_blocked("Oops", "404 - the page you requested was not found") == ""


def test_a_short_table_with_the_number_404_is_content():
    # Found in testing: an air travel CSV (404 passengers in September 1958) lost its footer line.
    csv = ("| Month | 1958 | 1959 |\n| --- | --- | --- |\n| AUG | 505 | 559 |\n"
           "| SEP | 404 | 463 |\n| OCT | 410 | 407 |")
    assert ex.looks_blocked("airtravel.csv", csv) == ""
    assert ex.looks_blocked("Price list", "Model A: 404 EUR. Model B: 410 EUR.") == ""


def test_long_pages_are_never_called_blocked():
    assert ex.looks_blocked("HTTP 404 explained", "The 404 status code means not found. " * 60) == ""


def test_normal_short_page_is_kept():
    assert ex.looks_blocked("Hermes Agent v0.22.7", "Release notes: faster web tools.") == ""


def test_bot_wall_is_caught_even_on_a_long_page():
    wall = "Automated bot check in progress\nThis should only take a few seconds.\nDrag the handle to the target\n"
    assert ex.looks_blocked("Specs database", wall + "filler text " * 300)


def test_rows_without_a_label_cell_are_not_added():
    html = ("<table><tr><td>117.5</td><td>1%</td></tr><tr><td>240 fps</td><td>37%</td></tr>"
            "<tr><th>Memory Speed</th><td>30000 effective</td></tr></table>")
    content, _ = ex.merge("Some text about the plans. " * 20, "", html)
    assert "117.5" not in content and "240 fps" not in content
    assert "- Memory Speed: 30000 effective" in content



def test_origin_wording_only_when_the_site_answered_an_error():
    # Hermes's "HTTP <code> from origin" form means the site answered 400 or more; nothing else
    # borrows it (it would steer Hermes's keyless rescue).
    assert ex._origin(403, "bot wall") == "HTTP 403 from origin: bot wall"
    assert ex._origin(200, "error or block page") == "error or block page"
    assert ex._origin(0, "x") == "x"


ARTICLE_HEAD = '<html><head><meta property="og:type" content="article"><title>Error 404</title></head><body><article>'


def test_a_troll_post_titled_error_404_is_content():
    text = "Error 404: my motivation was not found this morning. Coffee fixed it."
    assert ex.looks_blocked("Error 404", text, {}, ARTICLE_HEAD + text + "</article>") == ""


def test_an_article_about_captchas_or_access_denied_is_content():
    text = ("Access denied, captcha, security check: why websites test you. " + "A long explanation. " * 120)
    assert ex.looks_blocked("Why websites test you", text) == ""  # long page, words not in the title
    head = '<html><head><meta property="article:published_time" content="2026-01-01"></head>'
    assert ex.looks_blocked("Access denied: a short story", "A tale.", {}, head) == ""  # an article says so


def test_a_cloudflare_challenge_header_is_a_wall_whatever_the_text():
    assert ex.looks_blocked("Docs", "Real looking text. " * 200, {"CF-Mitigated": "challenge"})


def test_challenge_phrases_still_count_on_long_pages_but_plain_words_do_not():
    assert ex.looks_blocked("Example", "Checking your browser before accessing example.com. " + "x " * 2000)
    assert ex.looks_blocked("Example", "We added a captcha to the sign-up form. " + "More text. " * 300) == ""


def test_a_long_page_about_captchas_keeps_its_title():
    assert ex.looks_blocked("CAPTCHA - Encyclopedia", "A CAPTCHA is a type of challenge-response test. " * 60) == ""
    assert ex.looks_blocked("Access Denied", "You don't have permission to access this server.")  # short: a wall


LISTING_HTML = """
<nav><a href="/">Home</a> <a href="/account">My account</a></nav>
<h1>Cloud plans</h1>
<ul>
<li><a href="/plans/plus"><b>Plus</b> plan</a> $20 / month</li>
<li><a href="https://portal.example/plans/super">Super plan</a> $100 / month</li>
<li><a href="/plans/super#faq">Super plan</a> (same page, fragment)</li>
<li><a href="/plans/ultra">Ultra plan</a> $200 / month</li>
<li><a href="/plans/x">x</a></li>
</ul>
<footer><a href="/privacy">Privacy</a></footer>
"""
LISTING_TEXT = "Cloud plans\nPlus plan $20 / month\nSuper plan $100 / month\nUltra plan $200 / month"


def test_links_whose_text_is_in_the_content_are_listed_in_page_order():
    links = ex.page_links(LISTING_HTML, "", "https://portal.example/plans", LISTING_TEXT)
    assert links.splitlines() == [
        "- Plus plan: https://portal.example/plans/plus",
        "- Super plan: https://portal.example/plans/super",
        "- Ultra plan: https://portal.example/plans/ultra",
    ]  # navigation and footer links are not in the text; "x" is too short; one URL, one line


def test_links_come_from_the_browser_view_too_and_the_page_itself_is_skipped():
    view_html = '<a href="https://portal.example/plans">Cloud plans</a> <a href="/plans/ultra">Ultra plan</a>'
    assert ex.page_links("", view_html, "https://portal.example/plans/", "Cloud plans\nUltra plan $200") == \
        "- Ultra plan: https://portal.example/plans/ultra"
    assert ex.page_links(LISTING_HTML, "", "https://portal.example/plans", "") == ""


def test_the_whole_page_goes_to_hermes_with_the_links_last():
    html = "".join(f'<a href="/p/{i}">Product number {i}</a>' for i in range(100))
    text = " ".join(f"Product number {i}" for i in range(100))
    links = ex.page_links(html, "", "https://shop.example/", text)
    assert len(links.splitlines()) <= ex.MAX_LINKS and len(links) <= ex.MAX_LINKS_CHARS
    page = ex.Page("https://shop.example/", "Shop", "word " * 50_000, engine="trafilatura", links=links)
    out = page.as_result()["content"]
    # No cut of webgraft's own: Hermes windows the page (web.extract_char_limit) and stores the rest
    assert out.startswith("word " * 50_000) and out.endswith(links) and "## Links on this page" in out
    assert not hasattr(ex, "MAX_CONTENT_CHARS")
    assert "Links on this page" not in ex.Page("https://shop.example/", error="HTTP 404 from origin", links=links).as_result()["raw_content"]
