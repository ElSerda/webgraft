"""Fixes from an external review of d926de3, and the browser-view gaps seen on Steam and NVIDIA pages."""

import asyncio
import importlib
import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

gw = load()
ex = importlib.import_module("webgraft.extract")
runner = importlib.import_module("webgraft.crawl4ai_runner")
footer = importlib.import_module("webgraft.footer")


# ---------------------------------------------------------------- the browser's host is pinned (DNS rebinding)


def fake_dns(monkeypatch, answers, safe=lambda url: True):
    async def getaddrinfo(host, port, **kw):
        if host not in answers:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
                for a in answers[host]]

    class Loop:
        pass
    loop = Loop()
    loop.getaddrinfo = getaddrinfo
    monkeypatch.setattr(ex.asyncio, "get_running_loop", lambda: loop)

    async def url_is_safe(url):
        return safe(url)
    monkeypatch.setattr(ex, "url_is_safe", url_is_safe)


def test_the_page_host_is_pinned_to_the_address_checked_here(monkeypatch):
    fake_dns(monkeypatch, {"site.example": ["93.184.215.14", "93.184.215.15"]})
    assert asyncio.run(ex.pinned_host("https://site.example/specs")) == ("site.example", "93.184.215.14")


def test_the_runner_writes_an_ipv6_pin_in_brackets(monkeypatch):
    fake_dns(monkeypatch, {"site.example": ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"]})
    host, address = asyncio.run(ex.pinned_host("https://site.example/"))
    assert runner.host_rule_flags(host, address) == [
        "--host-resolver-rules=MAP site.example [2606:2800:21f:cb07:6820:80da:af6b:8b2c]"]


def test_an_ipv4_answer_is_preferred_for_the_pin(monkeypatch):
    fake_dns(monkeypatch, {"site.example": ["2606:2800:21f:cb07:6820:80da:af6b:8b2c", "93.184.215.14"]})
    assert asyncio.run(ex.pinned_host("https://site.example/")) == ("site.example", "93.184.215.14")


def test_any_private_answer_means_no_browser(monkeypatch):
    # a rebinding name that answers a public address and 127.0.0.1
    fake_dns(monkeypatch, {"rebind.example": ["93.184.215.14", "127.0.0.1"]}, safe=lambda url: "127.0.0.1" not in url)
    assert asyncio.run(ex.pinned_host("http://rebind.example:1234/v1/models")) is None


@pytest.mark.parametrize("url", ["https://unknown.example/", "https://bad_host!.example/", "https:///nohost"])
def test_unresolvable_or_odd_hosts_mean_no_browser(monkeypatch, url):
    fake_dns(monkeypatch, {})
    assert asyncio.run(ex.pinned_host(url)) is None


def test_an_ip_literal_needs_no_pin_but_is_still_checked(monkeypatch):
    fake_dns(monkeypatch, {}, safe=lambda url: "127.0.0.1" not in url)
    assert asyncio.run(ex.pinned_host("https://93.184.215.14/")) == ("", "")
    assert asyncio.run(ex.pinned_host("http://127.0.0.1:8080/")) is None


def test_no_browser_starts_when_the_host_is_not_public(monkeypatch):
    async def pin(url):
        return None
    monkeypatch.setattr(ex, "pinned_host", pin)
    monkeypatch.setattr(ex, "crawl4ai_python", lambda: Path(sys.executable))
    monkeypatch.setattr(ex.browser, "recorded", lambda: Path("chromium"))

    async def no_process(*a, **kw):
        raise AssertionError("the browser was started")
    monkeypatch.setattr(ex.asyncio, "create_subprocess_exec", no_process)
    token = ex._user_agent.set("Mozilla/5.0 (compatible; HermesAgent/1.0)")
    try:
        view = asyncio.run(ex.browser_view("https://rebind.example/"))
    finally:
        ex._user_agent.reset(token)
    assert view == {"success": False, "error": "the page's address is not public now: no browser"}


def test_the_runner_builds_one_pin_and_refuses_anything_else():
    assert runner.host_rule_flags("", "") == []
    assert runner.host_rule_flags("site.example", "93.184.215.14") == [
        "--host-resolver-rules=MAP site.example 93.184.215.14"]
    for host, address in (("*", "127.0.0.1"), ("a.example", "1.2.3.4, MAP b.example 127.0.0.1"),
                          ("site.example", "1.2.3.4 --remote-allow-origins=*"), ("site.example", ""),
                          ("", "1.2.3.4"), ("-site.example", "1.2.3.4"), ("site.example,b.example", "1.2.3.4")):
        with pytest.raises(ValueError):
            runner.host_rule_flags(host, address)


def test_the_runner_reads_the_devtools_port_from_chromium_s_own_file(tmp_path):
    class Running:
        def poll(self):
            return None
    (tmp_path / "DevToolsActivePort").write_text("51234\n/devtools/browser/abc-123\n", encoding="utf-8")
    import time
    assert runner.devtools_url(str(tmp_path), Running(), time.monotonic() + 5) == "ws://127.0.0.1:51234/devtools/browser/abc-123"


def test_the_runner_gives_up_when_chromium_exits_without_a_port(tmp_path):
    class Exited:
        def poll(self):
            return 1
    import time
    assert runner.devtools_url(str(tmp_path), Exited(), time.monotonic() + 5) is None


# ---------------------------------------------------------------- tool_guard: calls with no ids, connectors


def setup(settings=None):
    gw._turns.clear()
    gw._web_sessions.clear()
    gw._task_sessions.clear()
    gw._delegations.clear()

    class Ctx:
        hooks = {}

        def register_hook(self, name, fn, **_):
            self.hooks[name] = fn

        def get_config(self, key, default=None):
            return (settings or {}).get(key, default)
    ctx = Ctx()
    ctx.hooks = {}
    gw.register(ctx)
    return ctx


PAGE = json.dumps({"results": [{"url": "https://a.example/", "title": "A", "content": "text"}]})


def test_a_call_with_no_ids_is_guarded_once_any_conversation_read_the_web():
    ctx = setup()
    pre = ctx.hooks["pre_tool_call"]
    assert pre(tool_name="terminal", args={}, session_id="", task_id="") is None
    ctx.hooks["post_tool_call"](tool_name="web_extract", args={}, result=PAGE, session_id="s1")
    assert pre(tool_name="terminal", args={}, session_id="", task_id="")["action"] == "approve"
    assert pre(tool_name="terminal", args={}, session_id="s2") is None


def test_a_web_read_with_no_ids_guards_calls_with_no_ids():
    ctx = setup()
    ctx.hooks["post_tool_call"](tool_name="web_extract", args={}, result=PAGE, session_id="", task_id="")
    assert ctx.hooks["pre_tool_call"](tool_name="terminal", args={}, session_id="", task_id="")["action"] == "approve"


@pytest.mark.parametrize("tool", ["connectors__execute", "connectors__gmail__read_message"])
def test_hosted_connector_results_count_as_outside_content(tool):
    ctx = setup()
    ctx.hooks["post_tool_call"](tool_name=tool, args={}, result="mail text", session_id="s1")
    assert ctx.hooks["pre_tool_call"](tool_name="terminal", args={}, session_id="s1")["action"] == "approve"


# ---------------------------------------------------------------- footer


def test_a_page_title_in_the_footer_is_at_most_60_characters():
    line = footer._source_line("https://a.example/", {"https://a.example/": "Word " * 40})
    title = line[2:line.rindex(": https://")]
    assert len(title) <= 60 and title.endswith("...")


# ---------------------------------------------------------------- what the page's markup states (structured.py)

structured = importlib.import_module("webgraft.structured")

# Shapes taken from real pages (Steam, NVIDIA, AMD, LDLC; 2026-11-13), trimmed.
STEAM_HTML = """<html><body>
<div class="game_description_snippet">The first-ever sequel from Supergiant Games.</div>
<div itemprop="offers" itemscope itemtype="http://schema.org/Offer" style="display: none;">
  <meta itemprop="price" content="28,99"><meta itemprop="priceCurrency" content="EUR"></div>
<div itemprop="aggregateRating" itemscope itemtype="http://schema.org/AggregateRating">
  <meta itemprop="ratingValue" content="10"><meta itemprop="bestRating" content="10">
  <meta itemprop="worstRating" content="1"><meta itemprop="reviewCount" content="66371"></div>
<div class="grid_label grid_date">Released</div>
<div class="grid_content grid_date"><div class="release_date">25 Sep, 2025</div></div>
</body></html>"""

NVIDIA_HTML = """<html><body><table>
<tr><td>GPU Engine Specs:</td><td>NVIDIA CUDA® Cores</td><td>10752</td></tr>
<tr><td>Boost Clock (GHz)</td><td>2.62</td></tr>
<tr><td>Ray Tracing</td><td>Yes</td></tr>
<tr><td>Memory Interface Width</td><td>256-bit</td></tr>
</table></body></html>"""

LDLC_HTML = """<html><head><script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Product", "name": "QNAP QSW-L2110-10T", "sku": "AR202606180054",
 "gtin13": "4711103091647", "image": "https://media.ldlc.com/x.jpg", "url": "https://www.ldlc.com/fiche/x.html",
 "offers": {"@type": "Offer", "price": 249.95, "priceCurrency": "EUR", "url": "https://www.ldlc.com/fiche/x.html"}}
</script><script type="application/ld+json">
{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
 {"@type": "ListItem", "position": 1, "name": "LDLC"}, {"@type": "ListItem", "position": 2, "name": "Informatique"}]}
</script></head><body>
<ol itemscope itemtype="https://schema.org/BreadcrumbList"><li itemprop="itemListElement" itemscope
 itemtype="https://schema.org/ListItem"><span itemprop="name">Home</span><meta itemprop="position" content="1"></li></ol>
<dl><dt>Manageable</dt><dd>Oui</dd><dt>Rackable</dt><dd>Non</dd></dl>
</body></html>"""

AMD_MEMORY_TABLE = """<table><tr><td>2x1R DDR5-5600</td><td>2x2R DDR5-5600</td></tr>
<tr><td>4x1R DDR5-3600</td><td>4x2R DDR5-3600</td></tr></table>"""


def test_schema_org_properties_are_read_whatever_their_meaning():
    lines = structured.values(STEAM_HTML, "Hades II is a rogue-like from Supergiant Games.")
    assert "price: 28,99" in lines and "priceCurrency: EUR" in lines  # the currency comes with the price
    assert "ratingValue: 10" in lines and "reviewCount: 66371" in lines
    assert not any(line.startswith(("bestRating", "worstRating")) for line in lines)  # a rating's scale


def test_site_conventions_such_as_class_names_are_not_read():
    lines = structured.values(STEAM_HTML, "Hades II.")
    assert not any(line.startswith("Released") for line in lines)


def test_table_rows_are_read_with_their_label_cell_text_values_included():
    lines = structured.values(NVIDIA_HTML, "The RTX 5080 has 16 GB of GDDR7.")
    assert "Boost Clock (GHz): 2.62" in lines and "Ray Tracing: Yes" in lines
    assert "GPU Engine Specs: NVIDIA CUDA® Cores | 10752" in lines


def test_json_ld_items_and_dt_dd_are_read_and_the_site_around_the_page_is_not():
    lines = structured.values(LDLC_HTML, "Switch QNAP 8 ports 2.5 GbE.")
    assert {"price: 249.95", "priceCurrency: EUR", "gtin13: 4711103091647", "name: QNAP QSW-L2110-10T"} <= set(lines)
    assert "Manageable: Oui" in lines and "Rackable: Non" in lines
    assert not any(line in ("name: LDLC", "name: Informatique", "name: Home") for line in lines)  # breadcrumbs
    assert not any("http" in line for line in lines)  # links and images


def test_a_value_the_main_text_gives_is_not_repeated():
    lines = structured.values(NVIDIA_HTML, "Boost clock 2.62 GHz, ray tracing yes.")
    assert not any(line.startswith(("Boost Clock", "Ray Tracing")) for line in lines)
    assert "Memory Interface Width: 256-bit" in lines


def test_a_number_inside_a_longer_number_does_not_count_as_given():
    assert "Boost Clock (GHz): 2.62" in structured.values(NVIDIA_HTML, "Model 12.623 rev B.")


def test_a_first_cell_that_is_itself_a_value_is_not_a_label():
    assert structured.values(AMD_MEMORY_TABLE, "Ryzen 9 9950X3D.") == []


@pytest.mark.parametrize("html", ["", "plain text, no markup", "<<<>>>", "<table><tr><td>",
                                  '<script type="application/ld+json">{not json</script>'])
def test_odd_or_broken_markup_adds_nothing(html):
    assert structured.values(html, "text") == []


def test_deep_or_many_json_ld_scripts_are_bounded():
    deep = "[" * 5000 + "]" * 5000
    many = '<script type="application/ld+json">{"price": 1}</script>' * 50
    assert structured.values(f'<script type="application/ld+json">{deep}</script><p>x</p>', "t") == []
    assert structured.values(many + "<p>x</p>", "t") == ["price: 1"]


def test_hidden_and_script_text_is_not_read_as_pairs():
    html = ("<script>var t = '<table><tr><th>Price</th><td>$1</td></tr></table>'</script>"
            "<template><dl><dt>Price</dt><dd>$2</dd></dl></template><p>Page.</p>")
    assert structured.values(html, "Page.") == []


def test_merge_lists_them_under_one_heading_and_bounded():
    rows = "".join(f"<tr><th>Row {i}</th><td>{i} GB</td></tr>" for i in range(2000))
    content, engine = ex.merge("Main text of the page. " * 20, "", f"<table>{rows}</table>")
    extra = content.split(ex.STRUCTURE_HEADING, 1)[1]
    assert engine == "trafilatura" and len(extra) <= ex.MAX_COMPLEMENT_CHARS + 50
    assert "- Row 0: 0 GB" in extra
