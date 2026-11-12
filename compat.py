"""The values of the `show_sources` setting.

Hermes keeps only the first non-empty rewrite of a reply when several plugins register
`transform_llm_output` (hooks guide), and gives a plugin no documented way to know which other
plugins do. webgraft therefore does not guess: the user chooses with `show_sources`.
"""

from __future__ import annotations


def sources_mode(value) -> str:
    """show_sources as always / never. Older values: auto, on and true mean always; off and false never."""
    if value is True:
        return "always"
    if value is False:
        return "never"
    value = str(value or "always").lower()
    return "never" if value in ("never", "off") else "always"
