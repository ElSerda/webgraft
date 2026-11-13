"""Second audit: what webgraft fixed itself, and the harness limits it now handles or documents."""

import asyncio
import os
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

import importlib  # noqa: E402

gw = load()
ex = importlib.import_module("webgraft.extract")
sr = importlib.import_module("webgraft.site_rules")
footer = importlib.import_module("webgraft.footer")
browser = importlib.import_module("webgraft.browser")
cli = importlib.import_module("webgraft.cli")

HTML = "<html><head><title>Specs</title></head><body><article><p>" + "Boost clock 2617 MHz. " * 40 + "</p></article></body></html>"


def setup_function():
    ex.setting = lambda key, default: default


def fake_url_safety(monkeypatch, safe=lambda url: True):
    mod = types.ModuleType("tools.url_safety")
    mod.is_safe_url = safe
    mod.create_ssrf_safe_async_client = lambda **kw: None
    pkg = types.ModuleType("tools")
    pkg.url_safety = mod
    monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.setitem(sys.modules, "tools.url_safety", mod)


# ---------------------------------------------------------------- crafted pages


def test_only_text_codecs_are_used_to_decode_a_page():
    assert sr.text_codec("punycode") == "utf-8"
    assert sr.text_codec("zlib") == "utf-8"
    assert sr.text_codec("hex") == "utf-8"
    assert sr.text_codec("no-such-codec") == "utf-8"
    assert sr.text_codec("ISO-8859-1") == "iso8859-1"
    assert sr.text_codec("Shift_JIS") == "shift_jis"
    response = types.SimpleNamespace(encoding="punycode")
    assert sr.decoded(response, "café".encode("utf-8")) == "café"


# ---------------------------------------------------------------- programs and children


def test_programs_are_never_taken_from_the_current_folder(tmp_path, monkeypatch):
    name = "hermes"
    planted = tmp_path / "cwd"
    real = tmp_path / "bin"
    planted.mkdir()
    real.mkdir()
    suffix = ".exe" if os.name == "nt" else ""
    for folder in (planted, real):
        f = folder / (name + suffix)
        f.write_text("")
        f.chmod(0o755)
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(real)]))
    assert browser.which(name) == str(real / (name + suffix))
    monkeypatch.setenv("PATH", ".")
    assert browser.which(name) is None


def test_taskkill_is_run_by_its_full_path():
    path = ex.taskkill_exe()
    assert os.path.isabs(path) and path.lower().endswith(os.path.join("system32", "taskkill.exe"))
    assert os.path.isabs(ex.CHILD_CWD)


def test_a_child_is_stopped_when_hermes_cancels_the_call():
    async def run():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        task = asyncio.create_task(ex._communicate(proc, b"", 60, "test"))
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Hermes's loop keeps running and closes the dead child's pipes on their EOF; asyncio.run
        # would close this loop first and leave them to the garbage collector (a stray warning).
        await asyncio.sleep(0.2)
        return proc.returncode
    assert asyncio.run(run()) is not None


def test_the_ocr_cap_is_below_hermes_extract_timeout():
    assert ex.OCR_TIMEOUT < 120 and ex.DOC_TIMEOUT < 120 and ex.BROWSER_TIMEOUT < 120


def test_at_most_three_browsers_run_at_once_across_calls():
    running, peak = [0], [0]

    async def one():
        async with ex._browser_slot():
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            await asyncio.sleep(0.3)
            running[0] -= 1

    async def calls():  # separate read_pages calls, as one_url_per_call makes them
        await asyncio.gather(*(one() for _ in range(7)))
    asyncio.run(calls())
    assert peak[0] == ex.BROWSERS_AT_ONCE and ex._browsers_running == 0


# ---------------------------------------------------------------- browser view


class Response:
    def __init__(self, url, status=200, body=HTML):
        self.url, self.status_code = url, status
        self.headers = {"content-type": "text/html"}
        self.encoding = "utf-8"
        self.body = body.encode("utf-8")


def _page_with_view(monkeypatch, view, status=200):
    async def download(client, url):
        return Response("https://site.example/specs", status), HTML.encode("utf-8") if status < 400 else b"", False

    async def fake_view(url):
        return view
    monkeypatch.setattr(ex, "_download", download)
    monkeypatch.setattr(ex, "browser_view", fake_view)
    return asyncio.run(ex._read_page_itself(None, "https://site.example/specs"))


def test_a_browser_view_from_another_site_is_not_merged(monkeypatch):
    view = {"success": True, "status": 200, "markdown": "| Price | $999 |\n| Memory | 32 GB |",
            "final_url": "https://other.example/landing"}
    page = _page_with_view(monkeypatch, view)
    assert page.engine == "trafilatura" and "$999" not in page.content


def test_a_browser_view_of_the_same_site_is_merged(monkeypatch):
    view = {"success": True, "status": 200, "markdown": "## Specs\n| Memory size | 32 GB |",
            "html": "<table><tr><th>Memory size</th><td>32 GB</td></tr></table>",
            "final_url": "https://www.site.example/specs"}
    page = _page_with_view(monkeypatch, view)
    assert page.engine == "trafilatura+crawl4ai" and "- Memory size: 32 GB" in page.content


def test_the_browser_copy_of_a_page_respects_its_ai_opt_out(monkeypatch):
    view = {"success": True, "status": 200, "markdown": "## Specs\n| Memory size | 32 GB |",
            "final_url": "https://site.example/specs",
            "head": '<head><meta name="robots" content="noai"></head>'}
    page = _page_with_view(monkeypatch, view)
    assert "asks not to be used by AI" in page.error


# ---------------------------------------------------------------- proxies (harness limit, handled)


def test_with_a_proxy_webgraft_checks_each_redirect_target(monkeypatch):
    fake_url_safety(monkeypatch, safe=lambda url: "127.0.0.1" not in url)
    redirect = types.SimpleNamespace(status_code=302, is_redirect=True, url="https://public.example/go",
                                     headers={"location": "http://127.0.0.1:8080/admin"})
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    try:
        asyncio.run(ex._note_status(redirect))
    except ex.RedirectBlocked as exc:
        assert ex.ssrf_blocked(exc)
    else:
        raise AssertionError("the redirect to 127.0.0.1 was not refused")
    fine = types.SimpleNamespace(status_code=302, is_redirect=True, url="https://public.example/go",
                                 headers={"location": "/next"})
    asyncio.run(ex._note_status(fine))


def test_without_a_proxy_hermes_guard_does_the_checking(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    fake_url_safety(monkeypatch, safe=lambda url: False)
    redirect = types.SimpleNamespace(status_code=302, is_redirect=True, url="https://public.example/go",
                                     headers={"location": "http://127.0.0.1/"})
    asyncio.run(ex._note_status(redirect))  # no second DNS check: the guard refuses the hop at connect


# ---------------------------------------------------------------- footer


def test_a_redirect_url_cannot_add_a_link_to_the_footer():
    line = footer._source_line("https://a.example/x[Login](https://phish.example)", {})
    assert "](" not in line and "[" not in line
    assert line.startswith("- https://a.example/x%5BLogin%5D%28https://phish.example%29")


def test_titles_lose_invisible_and_autolinked_parts():
    url = "https://real.example/a"
    line = footer._source_line(url, {url: "Docs ‮gpj.exe​ www.evil.example contact admin@evil.example"})
    assert "‮" not in line and "​" not in line
    assert "evil" not in line and line.endswith(url)


# ---------------------------------------------------------------- multimodal turns


def test_a_multimodal_message_counts_its_text_parts_only():
    from test_hooks import setup
    ctx = setup()
    message = [{"type": "text", "text": "what is in this picture?"},
               {"type": "image_url", "image_url": {"url": "https://img.example/cat.png"}}]
    out = ctx.hooks["pre_llm_call"](session_id="m1", user_message=message)
    assert "Open the URL in the user's message" not in out
    assert gw._text_of(message) == "what is in this picture?"


# ---------------------------------------------------------------- setup


def test_an_install_error_is_reported_not_raised(capsys):
    class InstallError(RuntimeError):
        pass

    def fail():
        raise InstallError("ddgs: restart required — restart Hermes")
    assert cli._install(fail, "Hermes's ddgs extra") is None
    printed = capsys.readouterr()
    assert "restart required" in printed.out + printed.err


def test_stale_browser_folders_from_an_earlier_run_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(ex.tempfile, "gettempdir", lambda: str(tmp_path))
    old = time.time() - 2 * ex.STALE_SECONDS
    for name in ("webgraft-chromium-old1", "webgraft-crawl4ai-old2", "webgraft-browser-old3",
                 "webgraft-browser-fresh", "someone-elses-folder"):
        (tmp_path / name / "Default").mkdir(parents=True)
        (tmp_path / name / "Default" / "Cache").write_text("page data")
    for name in ("webgraft-chromium-old1", "webgraft-crawl4ai-old2", "webgraft-browser-old3", "someone-elses-folder"):
        os.utime(tmp_path / name, (old, old))
    (tmp_path / "webgraft-chromium-file").write_text("not a folder")
    os.utime(tmp_path / "webgraft-chromium-file", (old, old))
    assert ex.prune_stale_folders() == 3
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["someone-elses-folder", "webgraft-browser-fresh", "webgraft-chromium-file"]
