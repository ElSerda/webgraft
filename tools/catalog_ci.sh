#!/usr/bin/env bash
# Local run of Hermes's "Plugin Catalog CI" (.github/workflows/plugin-catalog-ci.yml), no cloud CI.
# Windows, Git Bash. Run it before opening a catalog PR (a new entry or a re-pin).
#
#   bash tools/catalog_ci.sh                 # the webgraft entry published on Hermes's main
#   bash tools/catalog_ci.sh my-entry.yaml   # an entry you are about to submit (new sha)
#   KEEP=1 bash tools/catalog_ci.sh          # keep Hermes's cloned tree afterwards, to inspect it
#
# Nothing of Hermes's is copied here: every run clones NousResearch/hermes-agent (main, sparse,
# shallow) and runs its own scripts, unmodified:
#   1. structural             scripts/validate_plugin_catalog.py plugin-catalog/
#   2. pinned-source-validate  scripts/ci/catalog_validate_pinned_entries.py
#      (clones the entry's repo at its pinned sha, symlink / payload / self-updater checks,
#       hermes plugins validate --install-deps)
# The Python version and the ruamel.yaml pin are read from their workflow, in a throwaway uv venv.
# If Nous changes that workflow, the run stops and says so: read the change, then update this file.
# tools/catalog_ci_windows.py adapts their Linux script to Windows without changing it.
set -euo pipefail

# Git blob id of .github/workflows/plugin-catalog-ci.yml that this script reproduces.
KNOWN_WORKFLOW=1cebc291ffe6cc28e3415330f198636a3e805e2f
UPSTREAM=https://github.com/NousResearch/hermes-agent

HERE="$(cd "$(dirname "$0")" && pwd)"
for tool in git uv hermes cygpath timeout; do
  command -v "$tool" >/dev/null || { echo "$tool is not on PATH (this script needs Git Bash on Windows, uv and Hermes)"; exit 2; }
done

WORK="$(mktemp -d)"
TREE="$WORK/hermes-agent"
if [ "${KEEP:-0}" = 1 ]; then trap 'echo "Hermes tree kept: $TREE"' EXIT; else trap 'rm -rf "$WORK"' EXIT; fi

echo "== Cloning $UPSTREAM (main, sparse, shallow)"
git clone -q --depth 1 --filter=blob:none --sparse "$UPSTREAM" "$TREE"
git -C "$TREE" sparse-checkout set scripts plugin-catalog .github/workflows
HEAD_SHA="$(git -C "$TREE" rev-parse HEAD)"
echo "   main at $HEAD_SHA ($(git -C "$TREE" log -1 --format=%cd))"
echo "   browse it: $UPSTREAM/tree/$HEAD_SHA"

WORKFLOW="$TREE/.github/workflows/plugin-catalog-ci.yml"
NOW_WORKFLOW="$(git -C "$TREE" rev-parse "HEAD:.github/workflows/plugin-catalog-ci.yml")"
if [ "$NOW_WORKFLOW" != "$KNOWN_WORKFLOW" ]; then
  echo "!! Nous changed plugin-catalog-ci.yml (blob $NOW_WORKFLOW, this script knows $KNOWN_WORKFLOW)."
  echo "!! Read the change and update tools/catalog_ci.sh before trusting a local pass:"
  echo "!!   $UPSTREAM/commits/main/.github/workflows/plugin-catalog-ci.yml"
  exit 3
fi

if [ $# -ge 1 ]; then
  NAME="$(basename "$1")"
  cp "$1" "$TREE/plugin-catalog/$NAME"
else
  NAME=webgraft.yaml
fi
[ -f "$TREE/plugin-catalog/$NAME" ] || { echo "No plugin-catalog/$NAME on Hermes's main: pass your entry file"; exit 2; }
echo "== Files changed in Hermes's tree (only your entry may appear; nothing listed = identical to main):"
git -C "$TREE" status --short | sed 's/^/   /'

# The workflow's Python version and ruamel.yaml pin, in a throwaway venv (Hermes's own env untouched).
PYVER="$(grep -oE 'python-version: *"[0-9.]+"' "$WORKFLOW" | head -1 | grep -oE '[0-9.]+')"
RUAMEL="$(grep -oE 'ruamel\.yaml==[0-9.]+' "$WORKFLOW" | head -1)"
uv venv -q --python "$PYVER" "$WORK/venv"
uv pip install -q --python "$WORK/venv/Scripts/python.exe" "$RUAMEL"
PY="$WORK/venv/Scripts/python.exe"
echo "   python $("$PY" -c 'import platform; print(platform.python_version())'), $RUAMEL (from the workflow)"

export GIT_TIMEOUT_EXE="$(cygpath -w "$(command -v timeout)")"
export PYTHONIOENCODING=utf-8

echo "== 1/2 structural"
(cd "$TREE" && "$PY" scripts/validate_plugin_catalog.py plugin-catalog/)

echo "== 2/2 pinned-source-validate ($NAME)"
CHANGED_FILES="plugin-catalog/$NAME" "$PY" -I "$(cygpath -w "$HERE/catalog_ci_windows.py")" "$(cygpath -w "$TREE")"

echo "== Local Plugin Catalog CI passed for $NAME"
