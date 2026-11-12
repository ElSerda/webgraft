"""Run Hermes's own scripts/ci/catalog_validate_pinned_entries.py on Windows, unchanged.

Called by tools/catalog_ci.sh. Hermes's script is written for its Linux runner. Two names resolve
differently on Windows, so this wrapper maps them before each child process starts:
  - "python3"  -> this interpreter (the workflow's Python version, with its ruamel.yaml pin);
  - "timeout"  -> Git's coreutils timeout (Windows finds System32\\timeout.exe first, a pause command).
It also lets the final cleanup delete git's read-only files, which Windows refuses by default.
Every check in Hermes's script runs as written.

Usage: python -I catalog_ci_windows.py <hermes-agent tree>
  env CHANGED_FILES    entry paths, one per line (plugin-catalog/<name>.yaml)
  env GIT_TIMEOUT_EXE  Windows path of Git's timeout.exe
"""
import importlib.util
import os
import shutil
import stat
import sys

tree = os.path.abspath(sys.argv[1])
os.chdir(tree)  # the workflow runs the script from the PR tree
os.environ["GITHUB_WORKSPACE"] = tree
os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")  # as in the workflow
TIMEOUT = os.environ["GIT_TIMEOUT_EXE"]

spec = importlib.util.spec_from_file_location(
    "catalog_validate_pinned_entries", os.path.join(tree, "scripts", "ci", "catalog_validate_pinned_entries.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

_original_run = m._run


def _run(argv, timeout=None, **kwargs):
    argv = list(argv)
    if argv and argv[0] == "python3":
        argv[0] = sys.executable
    if timeout is not None:
        argv = [TIMEOUT, str(timeout), *argv]
    return _original_run(argv, None, **kwargs)


m._run = _run

_rmtree = shutil.rmtree


def _rmtree_writable(path, *args, **kwargs):
    def retry(func, failed, _exc):
        os.chmod(failed, stat.S_IWRITE)
        func(failed)
    if sys.version_info >= (3, 12):
        return _rmtree(path, onexc=retry)
    return _rmtree(path, onerror=retry)  # the workflow's Python 3.11 has no onexc


m.shutil.rmtree = _rmtree_writable
sys.stdout.reconfigure(encoding="utf-8")  # -I ignores PYTHONIOENCODING; Hermes's script prints emoji
sys.stderr.reconfigure(encoding="utf-8")
sys.exit(m.main([]))
