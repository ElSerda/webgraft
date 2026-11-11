"""llms.txt and AI opt-outs."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

load()
import webgraft.extract as ex  # noqa: E402
import webgraft.site_rules as sr  # noqa: E402

LLMS = """# Example Docs

> Docs for Example.

## Docs

- [Install](https://docs.example/install.md): how to install
- [Specs](/specs.md "Specs page")
"""


class Response:
    def __init__(self, status=200, text="", ctype="text/plain", headers=None):
        self.status_code, self.text = status, text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": ctype, **(headers or {})}
        self.encoding = "utf-8"

    async def aiter_bytes(self):
        yield self.content


class _Stream:
    def __init__(self, coro):
        self.coro = coro

    async def __aenter__(self):
        return await self.coro

    async def __aexit__(self, *exc):
        return False


class FakeClient:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def get(self, url, **_):
        self.calls.append(url)
        return self.routes.get(url, Response(404, "<html>not found</html>", "text/html"))

    def stream(self, method, url, **_):
        return _Stream(self.get(url))


def setup_function():
    sr._sites.clear()
    ex.setting = lambda key, default: default


# ---------------------------------------------------------------- opt-outs


def test_optout_signals():
    assert "noai" in sr.ai_optout({"X-Robots-Tag": "noai, noimageai"})
    assert "TDM" in sr.ai_optout({"TDM-Reservation": "1"})
    assert "noai" in sr.ai_optout({}, '<head><meta name="robots" content="noai, noimageai"></head>')
    assert "TDM" in sr.ai_optout({}, "<head><meta content='1' name='tdm-reservation'></head>")


def test_no_optout_without_a_signal():
    assert sr.ai_optout({"X-Robots-Tag": "noindex"}) == ""
    assert sr.ai_optout({}, '<head><meta name="robots" content="index, follow"></head>') == ""
    assert sr.ai_optout({}, "<body><p>We use noai tags on our art.</p></body>") == ""  # text, not a tag
    assert sr.ai_optout({"tdm-reservation": "0"}) == ""


def test_an_opted_out_page_is_an_error(monkeypatch):
    async def fake_download(client, url):
        return Response(200, "", "text/html", {"X-Robots-Tag": "noai"}), b"<html><body>Art</body></html>", False

    async def no_browser(url, slots=None):
        return {"success": False}

    monkeypatch.setattr(ex, "_download", fake_download)
    monkeypatch.setattr(ex, "browser_view", no_browser)
    page = asyncio.run(ex._read_page_itself(None, "https://art.example/piece"))
    assert page.error.startswith("HTTP 200 from origin: the site asks not to be used by AI")
    ex.setting = lambda key, default: False if key == "respect_ai_optout" else default
    assert asyncio.run(ex._read_page_itself(None, "https://art.example/piece")).error != page.error


# ---------------------------------------------------------------- llms.txt


def test_llms_txt_is_parsed_and_html_is_not_taken_for_one():
    links = sr.parse_llms_txt(LLMS, "https://docs.example")
    assert links == {"https://docs.example/install.md", "https://docs.example/specs.md"}
    spa_home = '<!DOCTYPE html><html lang="fr"><head><title>App</title></head><body><div id="app"></div>'
    assert sr.parse_llms_txt(spa_home, "https://app.example") is None
    assert sr.parse_llms_txt("Just some text", "https://x.example") is None


def test_markdown_version_of_a_page():
    links = sr.parse_llms_txt(LLMS, "https://docs.example")
    assert sr.markdown_version("https://docs.example/specs", links) == "https://docs.example/specs.md"
    assert sr.markdown_version("https://docs.example/specs/#memory", links) == "https://docs.example/specs.md"
    assert sr.markdown_version("https://docs.example/other", links) == ""


def test_the_markdown_version_is_read_instead_of_the_html():
    client = FakeClient({
        "https://docs.example/llms.txt": Response(200, LLMS),
        "https://docs.example/specs.md": Response(200, "# Specs\n\nMemory: 16 GB GDDR7.", "text/markdown"),
    })
    page = asyncio.run(ex._read_page(client, "https://docs.example/specs"))
    assert (page.engine, page.title, page.error) == ("llms.txt", "Specs", "")
    assert "16 GB GDDR7" in page.content


def test_llms_txt_is_fetched_once_per_site_and_a_fake_one_is_ignored():
    client = FakeClient({"https://app.example/llms.txt": Response(200, "<!DOCTYPE html><div id='app'>", "text/html")})
    assert asyncio.run(sr.llms_index(client, "https://app.example/")) is None
    assert asyncio.run(sr.llms_index(client, "https://app.example/about")) is None
    assert client.calls == ["https://app.example/llms.txt"]


def test_llms_txt_can_be_turned_off():
    ex.setting = lambda key, default: False if key == "llms_txt" else default
    client = FakeClient({"https://docs.example/llms.txt": Response(200, LLMS)})
    assert asyncio.run(ex._from_llms_txt(client, "https://docs.example/specs")) == (None, "")
    assert client.calls == []


def test_llms_txt_saved_with_a_byte_order_mark_is_read():
    assert sr.parse_llms_txt(chr(0xFEFF) + LLMS, "https://docs.example")
