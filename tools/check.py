"""Local checks for webgraft, run before a release or a catalog re-pin. No cloud CI needed.

    python tools/check.py                    # tests on Python 3.11 and 3.14, then Hermes doctor + validate
    python tools/check.py --python 3.12      # other interpreter versions (repeatable)
    python tools/check.py --skip-hermes      # tests only

For each Python version, `uv` builds a throwaway virtual environment and installs the plugin's
dependencies from pyproject.toml plus pytest, keeping only releases at least 3 days old
(`--exclude-newer`): the release-age quarantine the Hermes plugin guide asks authors to run.
Then the Hermes on this machine runs `plugins doctor --ci` and `plugins validate --install-deps`,
the checks catalog CI runs. Needs `uv` on PATH; the Hermes checks need `hermes` on PATH.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUARANTINE_DAYS = 3


def run(cmd: list[str], **kwargs) -> bool:
    print("  $", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, **kwargs).returncode == 0


def tests(python: str, cutoff: str) -> bool:
    with tempfile.TemporaryDirectory(prefix=f"gw-check-{python}-") as tmp:
        venv = Path(tmp) / "venv"
        if not run(["uv", "venv", "--quiet", "--python", python, str(venv)]):
            return False
        if not run(["uv", "pip", "install", "--quiet", "--python", str(venv), "--exclude-newer", cutoff,
                    "-r", "pyproject.toml", "pytest", "firecrawl-anydoc", "pillow"]):
            return False
        exe = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        return run([str(exe), "-m", "pytest", "tests", "-q"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--python", action="append", help="Python version to test (default: 3.11 and 3.14)")
    parser.add_argument("--skip-hermes", action="store_true", help="do not run Hermes doctor and validate")
    args = parser.parse_args()
    if shutil.which("uv") is None:
        print("uv is needed: https://docs.astral.sh/uv/")
        return 2
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=QUARANTINE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    results: list[tuple[str, bool]] = []
    for python in args.python or ["3.11", "3.14"]:
        print(f"\n== tests, Python {python}, dependencies released before {cutoff}")
        results.append((f"tests, Python {python}", tests(python, cutoff)))
    if not args.skip_hermes:
        if shutil.which("hermes") is None:
            print("\n== Hermes checks skipped: `hermes` is not on PATH")
            results.append(("hermes on PATH", False))
        else:
            print("\n== hermes plugins doctor --ci")
            results.append(("hermes plugins doctor --ci", run(["hermes", "plugins", "doctor", ".", "--ci"])))
            print("\n== hermes plugins validate --install-deps")
            results.append(("hermes plugins validate --install-deps",
                            run(["hermes", "plugins", "validate", ".", "--install-deps"])))
    print("\n== summary")
    for name, passed in results:
        print(f"  {'ok  ' if passed else 'FAIL'}  {name}")
    return 0 if all(passed for _, passed in results) else 1


if __name__ == "__main__":
    sys.exit(main())
