"""Finding Hermes's Chromium in a Playwright-style browsers folder, on each OS layout."""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

load()
browser = importlib.import_module("webgraft.browser")


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


@pytest.mark.parametrize("layout", [
    "chrome-win64/chrome.exe",
    "chrome-linux64/chrome",
    "chrome-linux-arm64/chrome",
    "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
])
def test_finds_chromium_in_each_layout(tmp_path, layout):
    exe = touch(tmp_path / "chromium-1208" / layout)
    assert browser.in_browsers_path(tmp_path) == exe


def test_newest_revision_wins(tmp_path):
    touch(tmp_path / "chromium-1100" / "chrome-win64" / "chrome.exe")
    newest = touch(tmp_path / "chromium-1208" / "chrome-win64" / "chrome.exe")
    assert browser.in_browsers_path(tmp_path) == newest


def test_other_browsers_are_ignored(tmp_path):
    touch(tmp_path / "firefox-1500" / "firefox" / "firefox.exe")
    touch(tmp_path / "chromium_headless_shell-1248" / "chrome-win64" / "headless_shell.exe")
    assert browser.in_browsers_path(tmp_path) is None
