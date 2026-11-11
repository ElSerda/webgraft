"""Finds other enabled plugins that rewrite the final reply.

Hermes runs every `transform_llm_output` callback but keeps only the first non-empty rewrite,
so two plugins that both append to the reply silently hide one another. webgraft steps
aside when another one is active (setting `show_sources: auto`), whatever that plugin is.

Only documented surfaces: each installed plugin's manifest (`provides_hooks`, which the catalog
requires to match what the plugin registers) and `ctx.has_plugin()` for whether it is enabled.
Plugins installed with pip, which have no manifest on disk, are not seen.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

HOOK = "transform_llm_output"
_KEYS = ("name", "provides_hooks", "hooks")
_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")


def _scalar(text: str) -> str:
    text = re.sub(r"\s+#.*$", "", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text.strip()


def read_manifest(text: str) -> dict:
    """The few top-level keys webgraft needs from a plugin.yaml (name, provides_hooks, hooks), read
    without a YAML library (Hermes does not ship PyYAML). Lists may be flow ([a, b]) or block
    (- a); anything else in the file is ignored."""
    data: dict = {}
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            current = None
            m = _TOP_KEY.match(line)
            if not m or m.group(1) not in _KEYS:
                continue
            key, value = m.group(1), _scalar(m.group(2))
            if key == "name":
                data[key] = value
            elif value.startswith("[") and value.endswith("]"):
                data[key] = [_scalar(v) for v in value[1:-1].split(",") if _scalar(v)]
            elif not value:
                data[key], current = [], key
        elif current and line.lstrip().startswith("- "):
            data[current].append(_scalar(line.lstrip()[2:]))
    return data


def _manifests(root: Path):
    for path in list(root.glob("*/plugin.yaml")) + list(root.glob("*/*/plugin.yaml")):
        try:
            yield read_manifest(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable manifest is that plugin's problem
            continue


def other_reply_rewriters(own_name: str, has_plugin: Callable[[str], bool] | None) -> list[str]:
    """Names of the other enabled plugins that declare transform_llm_output; [] when unknown."""
    if has_plugin is None:
        return []
    try:
        from hermes_constants import get_hermes_home
        root = get_hermes_home() / "plugins"
    except Exception:  # noqa: BLE001 - outside Hermes
        return []
    names = set()
    for manifest in _manifests(root):
        name = str(manifest.get("name") or "")
        hooks = manifest.get("provides_hooks") or manifest.get("hooks") or []
        if name and name != own_name and HOOK in hooks:
            try:
                if has_plugin(name):
                    names.add(name)
            except Exception:  # noqa: BLE001
                continue
    return sorted(names)


def sources_mode(value) -> str:
    """show_sources as auto / always / never. true/false (YAML turns on/off into those) map to auto / never."""
    if value is True:
        return "auto"
    if value is False:
        return "never"
    value = str(value or "auto").lower()
    return {"on": "always", "off": "never"}.get(value, value if value in ("auto", "always", "never") else "auto")
