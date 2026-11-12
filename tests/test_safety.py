"""Network and child-process safety: SSRF-safe client, redirect targets, browser views, child env."""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

import importlib  # noqa: E402

load()
ex = importlib.import_module("webgraft.extract")
compat = importlib.import_module("webgraft.compat")

HTML = "<html><head><title>Specs</title></head><body><article><p>" + "Boost clock 2617 MHz. " * 40 + "</p></article></body></html>"


class Response:
    def __init__(self, url, status=200, body=HTML, ctype="text/html"):
        self.url, self.status_code = url, status
        self.headers = {"content-type": ctype}
        self.encoding = "utf-8"
        self.body = body.encode("utf-8")


def fake_url_safety(monkeypatch, safe=lambda url: True):
    """Stand-in for Hermes's tools.url_safety: records the client's arguments."""
    made = {}
    mod = types.ModuleType("tools.url_safety")

    def create_ssrf_safe_async_client(**kwargs):
        made.update(kwargs)
        return "guarded client"
    mod.create_ssrf_safe_async_client = create_ssrf_safe_async_client
    mod.is_safe_url = safe
    pkg = types.ModuleType("tools")
    pkg.url_safety = mod
    monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.setitem(sys.modules, "tools.url_safety", mod)
    return made


def setup_function():
    ex.setting = lambda key, default: default


# ---------------------------------------------------------------- 1. SSRF-safe client, final URL


def test_pages_are_fetched_with_hermes_ssrf_safe_client(monkeypatch):
    made = fake_url_safety(monkeypatch)
    assert ex.http_client(follow_redirects=True, timeout=5) == "guarded client"
    assert made == {"follow_redirects": True, "timeout": 5}


def test_without_hermes_pages_are_not_read(monkeypatch):
    monkeypatch.setitem(sys.modules, "tools", None)
    try:
        ex.http_client()
    except RuntimeError as exc:
        assert "url_safety" in str(exc)
    else:
        raise AssertionError("an unguarded client was made")
    assert asyncio.run(ex.url_is_safe("https://example.com")) is False


def _no_browser(calls):
    async def view(url, slots=None):
        calls.append(url)
        return {"success": False}
    return view


def test_a_redirected_page_reports_where_it_ended(monkeypatch):
    calls = []

    async def download(client, url):
        return Response("https://final.example/specs"), HTML.encode("utf-8"), False
    monkeypatch.setattr(ex, "_download", download)
    monkeypatch.setattr(ex, "browser_view", _no_browser(calls))
    page = asyncio.run(ex._read_page_itself(None, "https://short.example/x"))
    assert (page.url, page.requested, page.error) == ("https://final.example/specs", "https://short.example/x", "")
    result = page.as_result()
    assert result["url"] == "https://final.example/specs"
    assert result["metadata"]["sourceURL"] == "https://short.example/x"
    assert calls == ["https://final.example/specs"]  # the browser opens the checked address


def test_a_page_without_redirect_keeps_its_url():
    result = ex.Page("https://a.example", "A", "text").as_result()
    assert result["url"] == result["metadata"]["sourceURL"] == "https://a.example"


def test_a_redirect_to_a_private_address_is_blocked_and_never_reaches_the_browser(monkeypatch):
    calls = []

    class SSRFConnectionBlocked(ValueError):
        pass

    async def download(client, url):
        await ex._note_status(types.SimpleNamespace(status_code=302))  # the hook, on the redirect
        try:
            raise SSRFConnectionBlocked("169.254.169.254 is a cloud metadata address")
        except SSRFConnectionBlocked as inner:
            raise OSError("connect failed") from inner  # as a wrapping layer would
    monkeypatch.setattr(ex, "_download", download)
    monkeypatch.setattr(ex, "browser_view", _no_browser(calls))
    page = asyncio.run(ex._read_page_itself(None, "https://redirector.example/"))
    # Hermes's origin wording: the site sent us there, so no keyless rescue through an outside service
    assert page.error == "HTTP 302 from origin: redirects to a private or internal network address; webgraft did not follow it"
    assert calls == []


def test_a_blocked_fetch_without_a_redirect_uses_hermes_blocked_wording():
    assert ex._blocked_redirect(0) == ex.BLOCKED_ADDRESS
    assert ex._blocked_redirect(307).startswith("HTTP 307 from origin: ")


class StreamResponse:
    def __init__(self, url, body, ctype="text/plain", headers=None, chunks=1):
        self.url, self.status_code, self.encoding = url, 200, "utf-8"
        self.headers = {"content-type": ctype, **(headers or {})}
        self.body, self.chunks, self.sent = body.encode("utf-8"), chunks, 0

    async def aiter_bytes(self):
        for _ in range(self.chunks):
            self.sent += len(self.body)
            yield self.body


class StreamClient:
    """Answers stream() from a dict of URL -> StreamResponse."""
    def __init__(self, routes):
        self.routes = routes

    def stream(self, method, url, **_):
        response = self.routes[url]

        class Ctx:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *exc):
                return False
        return Ctx()


LLMS = "# Docs\n- [Specs](/specs.md)"


def _llms_client(md):
    importlib.import_module("webgraft.site_rules")._sites.clear()
    return StreamClient({"https://docs.example/llms.txt": StreamResponse("https://docs.example/llms.txt", LLMS),
                         "https://docs.example/specs.md": md})


def test_the_llms_txt_markdown_page_reports_its_own_url():
    client = _llms_client(StreamResponse("https://docs.example/specs.md", "# Specs\n\n16 GB", "text/markdown"))
    page, _ = asyncio.run(ex._from_llms_txt(client, "https://docs.example/specs"))
    assert (page.url, page.requested) == ("https://docs.example/specs.md", "https://docs.example/specs")


def test_an_endless_markdown_file_is_cut_off_and_the_html_is_read_instead(monkeypatch):
    monkeypatch.setattr(ex, "MAX_MARKDOWN_BYTES", 1000)
    endless = StreamResponse("https://docs.example/specs.md", "x" * 100, "text/markdown", chunks=10_000)
    page, llms_url = asyncio.run(ex._from_llms_txt(_llms_client(endless), "https://docs.example/specs"))
    assert page is None and llms_url == "https://docs.example/llms.txt"
    assert endless.sent <= 1100  # stopped right after the cap, not read to the end


def test_a_markdown_version_that_opts_out_of_ai_is_an_error():
    md = StreamResponse("https://docs.example/specs.md", "# Specs", "text/markdown", {"X-Robots-Tag": "noai"})
    page, _ = asyncio.run(ex._from_llms_txt(_llms_client(md), "https://docs.example/specs"))
    assert "asks not to be used by AI" in page.error


def test_a_huge_llms_txt_is_ignored(monkeypatch):
    sr = importlib.import_module("webgraft.site_rules")
    sr._sites.clear()
    monkeypatch.setattr(sr, "LLMS_MAX_BYTES", 1000)
    big = StreamResponse("https://docs.example/llms.txt", LLMS + " " * 200, chunks=50)
    assert asyncio.run(sr.llms_index(StreamClient({"https://docs.example/llms.txt": big}), "https://docs.example/x")) is None


# ---------------------------------------------------------------- 2. browser view


def test_a_browser_view_that_ended_on_a_private_address_is_dropped(monkeypatch):
    fake_url_safety(monkeypatch, safe=lambda url: "127.0.0.1" not in url)
    view = {"success": True, "status": 200, "markdown": "secret", "final_url": "http://127.0.0.1:8080/admin"}
    assert asyncio.run(ex.vet_view(view))["success"] is False
    ok = {"success": True, "status": 200, "markdown": "page", "final_url": "https://public.example/"}
    assert asyncio.run(ex.vet_view(ok)) == ok


def test_a_browser_view_that_does_not_say_where_it_ended_is_dropped(monkeypatch):
    fake_url_safety(monkeypatch)
    assert asyncio.run(ex.vet_view({"success": True, "markdown": "page"}))["success"] is False


def test_a_page_read_only_by_the_browser_is_named_by_its_final_url(monkeypatch):
    async def download(client, url):
        return Response(url, status=403, body="Forbidden"), b"Forbidden", False

    async def view(url, slots=None):
        return {"success": True, "status": 200, "title": "Specs", "markdown": "Boost clock 2617 MHz. " * 30,
                "final_url": "https://www.public.example/specs"}
    monkeypatch.setattr(ex, "_download", download)
    monkeypatch.setattr(ex, "browser_view", view)
    page = asyncio.run(ex._read_page_itself(None, "https://public.example/specs"))
    assert (page.engine, page.url, page.requested) == ("crawl4ai", "https://www.public.example/specs",
                                                        "https://public.example/specs")


# ---------------------------------------------------------------- 3. child environment


def test_children_get_a_minimal_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("NOUS_API_KEY", "sk-test")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    env = ex.child_env(CRAWL4AI_NO_CLOUD_NOTICE="1")
    assert "OPENROUTER_API_KEY" not in env and "NOUS_API_KEY" not in env
    assert env["HTTPS_PROXY"] == "http://proxy.example:3128" and env["LC_ALL"] == "C.UTF-8"
    assert env["CRAWL4AI_NO_CLOUD_NOTICE"] == "1" and env["PYTHONIOENCODING"] == "utf-8"
    assert any(k.upper() == "PATH" for k in env)


def test_the_document_reader_does_not_see_provider_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    probe = tmp_path / "probe.py"
    probe.write_text("import json, os, sys\nsys.stdin.buffer.read()\n"
                     'print(json.dumps({"text": str("OPENROUTER_API_KEY" in os.environ)}))\n')
    monkeypatch.setattr(ex, "DOC_READER", probe)
    monkeypatch.setattr(ex, "ensure_doc_reader", lambda: None)
    assert asyncio.run(ex.document_text_isolated(b"%PDF-1.4")) == "False"


# ---------------------------------------------------------------- 4. manifests without PyYAML


def test_manifests_are_read_without_a_yaml_library():
    block = 'name: "other"   # a comment\nversion: 1.0\nprovides_hooks:\n  - pre_llm_call\n  - transform_llm_output\nconfig_schema:\n  name:\n    type: str\n'
    assert compat.read_manifest(block) == {"name": "other", "provides_hooks": ["pre_llm_call", "transform_llm_output"]}
    flow = "name: 'flowy'\nhooks: [post_tool_call, transform_llm_output]\n"
    assert compat.read_manifest(flow) == {"name": "flowy", "hooks": ["post_tool_call", "transform_llm_output"]}


def test_our_own_manifest_is_read():
    data = compat.read_manifest((Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8"))
    assert data["name"] == "webgraft" and compat.HOOK in data["provides_hooks"]


def test_another_enabled_rewriter_is_found(tmp_path, monkeypatch):
    for name, hooks in (("webgraft", "[transform_llm_output]"), ("other", "\n  - transform_llm_output"),
                        ("quiet", "[pre_llm_call]")):
        folder = tmp_path / "plugins" / name
        folder.mkdir(parents=True)
        (folder / "plugin.yaml").write_text(f"name: {name}\nprovides_hooks: {hooks}\n")
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    assert compat.other_reply_rewriters("webgraft", lambda name: True) == ["other"]
    assert compat.other_reply_rewriters("webgraft", lambda name: name != "other") == []


# ---------------------------------------------------------------- crafted pages stay fast


def _seconds(fn, *args):
    import time
    start = time.perf_counter()
    fn(*args)
    return time.perf_counter() - start


def test_crafted_number_runs_are_indexed_in_linear_time():
    values = importlib.import_module("webgraft.values")
    for text in ("123 " * 20_000, "$1 " * 20_000, "12 GB " * 15_000):  # 60-90 KB, one line
        assert _seconds(values.index_page, "", text) < 2, text[:6]


def test_unclosed_tags_and_brackets_stay_fast():
    for html, md in (('<a href="x">' * 8_000, ""), ("", "[abc" * 20_000), ("<meta " * 15_000, "")):
        assert _seconds(ex.page_links, html, md, "https://x.example", "abc x") < 2
        assert _seconds(ex.looks_blocked, "t", "short", {}, html) < 2
    assert _seconds(ex.strip_links, "[a](b" * 20_000 + " " * 100_000) < 2
    sr = importlib.import_module("webgraft.site_rules")
    assert _seconds(sr.parse_llms_txt, "# x\n" + "[a" * 50_000, "https://x.example") < 2
    assert _seconds(sr.ai_optout, {}, "<meta " * 15_000) < 2


def test_a_long_page_note_does_not_name_the_question_words():
    fo = importlib.import_module("webgraft.focus")
    blocks = [f"Paragraph {i} about general things and more general things." * 3 for i in range(400)]
    blocks[300] = "The zyxquant plan costs 200 dollars a month and includes zyxquant support."
    text = "\n\n".join(blocks)
    arranged = fo.arrange(text, ["zyxquant"], 15_000)
    assert arranged != text and "passages about the question come first" in arranged
    assert "zyxquant" not in arranged.split("come first")[0]


def test_a_page_read_without_a_matching_call_is_not_arranged_for_another_question():
    fo = importlib.import_module("webgraft.focus")
    fo.expect("https://one.example/a", ["secret"], 9000)
    assert fo.take("https://other.example/b") is None
    assert fo.take("https://one.example/a") == (["secret"], 9000)


def test_a_dns_failure_is_not_called_a_private_address():
    class SSRFConnectionBlocked(ValueError):
        pass
    assert ex.ssrf_blocked(SSRFConnectionBlocked("Blocked request to private/internal address during connect: x"))
    assert not ex.ssrf_blocked(SSRFConnectionBlocked("Blocked request - DNS resolution failed for: x"))


# ---------------------------------------------------------------- browser child is stopped whole


def _alive(pid: int) -> bool:
    import os
    import subprocess as sp
    if os.name == "nt":
        out = sp.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_a_browser_read_that_times_out_is_stopped_with_its_children(tmp_path, monkeypatch):
    import time
    pid_file = tmp_path / "child.pid"
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(os.path.join(sys.argv[3], 'profile.lock'), 'w').write('x')\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid) + ' ' + sys.argv[3])\n"
        "time.sleep(60)\n")
    monkeypatch.setattr(ex, "RUNNER", runner)
    monkeypatch.setattr(ex, "crawl4ai_python", lambda: Path(sys.executable))
    monkeypatch.setattr(ex.browser, "recorded", lambda: Path("chromium"))
    monkeypatch.setattr(ex, "BROWSER_TIMEOUT", 3)
    view = asyncio.run(ex.browser_view("https://x.example"))
    assert "timed out" in view["error"]
    pid, work = pid_file.read_text().split(" ", 1)
    child = int(pid)
    for _ in range(20):
        if not _alive(child):
            break
        time.sleep(0.25)
    assert not _alive(child)
    assert not Path(work).exists()  # the plugin removes the work folder even when the runner is killed
