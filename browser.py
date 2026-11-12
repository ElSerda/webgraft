"""Finds the Chromium that Hermes uses, so Crawl4AI drives it instead of downloading its own.

Same order as Hermes's browser tool (user guide, "Executable resolution"):
  1. `browser.executable_path` in config.yaml;
  2. the Chromium installed by Hermes's package manager (`hermes pm install chromium`), found
     through `hermes pm env chromium` (its PLAYWRIGHT_BROWSERS_PATH);
  3. /etc/hermes/chromium-executable-path (official Docker image);
  4. a system `chromium` / `chromium-browser` on PATH.

`setup` resolves it once and records the path in the plugin's data directory; page reads use the
recorded path, so they never start a `hermes` process.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

DATA_FILE = "browser.json"
_LAYOUTS = (
    "chromium-*/chrome-win64/chrome.exe",
    "chromium-*/chrome-win/chrome.exe",
    "chromium-*/chrome-linux64/chrome",
    "chromium-*/chrome-linux/chrome",
    "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
)


def _data_file() -> Path | None:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir("webgraft") / DATA_FILE
    except Exception:  # noqa: BLE001 - outside Hermes
        return None


def _revision(path: Path) -> int:
    m = re.search(r"chromium-(\d+)", str(path))
    return int(m.group(1)) if m else 0


def in_browsers_path(root: Path) -> Path | None:
    """The newest Chromium in a Playwright-style browsers folder."""
    found = [p for pattern in _LAYOUTS for p in root.glob(pattern) if p.is_file()]
    return max(found, key=_revision) if found else None


def _from_config() -> Path | None:
    try:
        from hermes_cli.config import load_config_readonly
        value = str(((load_config_readonly() or {}).get("browser") or {}).get("executable_path") or "").strip()
    except Exception:  # noqa: BLE001
        return None
    return Path(value) if value and Path(value).is_file() else None


def which(name: str) -> str | None:
    """`name` found in the absolute folders of PATH only. shutil.which() also looks in the current
    folder on Windows, where a file planted in the folder Hermes runs from would win."""
    exts = [""] + ([e.lower() for e in (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if e]
                   if os.name == "nt" else [])
    for folder in (os.environ.get("PATH") or "").split(os.pathsep):
        if not folder or not os.path.isabs(folder):
            continue
        for ext in exts:
            candidate = os.path.join(folder, name + ext)
            if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _from_pm() -> Path | None:
    hermes = which("hermes")
    if hermes is None:
        return None
    try:
        out = subprocess.run([hermes, "pm", "env", "chromium"], capture_output=True, text=True, timeout=120)
        start = out.stdout.find("{")
        env = json.loads(out.stdout[start:]) if start >= 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    root = env.get("PLAYWRIGHT_BROWSERS_PATH")
    return in_browsers_path(Path(root)) if root else None


def _from_docker() -> Path | None:
    marker = Path("/etc/hermes/chromium-executable-path")
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(value) if value and Path(value).is_file() else None


def _from_path() -> Path | None:
    for name in ("chromium", "chromium-browser"):
        if found := which(name):
            return Path(found)
    return None


def resolve() -> tuple[Path | None, str]:
    """(executable, where it came from), searched in Hermes's order. May start `hermes pm env`."""
    for source, finder in (("browser.executable_path", _from_config), ("hermes pm", _from_pm),
                           ("/etc/hermes", _from_docker), ("PATH", _from_path)):
        if (path := finder()) is not None:
            return path, source
    return None, ""


def record(path: Path) -> None:
    if (f := _data_file()) is not None:
        f.write_text(json.dumps({"executable": str(path)}), encoding="utf-8")


def recorded() -> Path | None:
    """The executable `setup` recorded, if it still exists. Never starts a process."""
    f = _data_file()
    try:
        value = json.loads(f.read_text(encoding="utf-8")).get("executable", "") if f else ""
    except (OSError, ValueError):
        return None
    return Path(value) if value and Path(value).is_file() else None
