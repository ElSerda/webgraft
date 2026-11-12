"""The `webgraft` web_extract backend: pages and PDFs read on this machine.

Search stays with any search backend (SearXNG, DDGS, Brave...): set
`web.search_backend` to it and `web.extract_backend: webgraft`.
"""

from __future__ import annotations

import importlib.util
from typing import Any

from agent.web_search_provider import WebSearchProvider

from . import extract


class WebGraftProvider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "webgraft"

    @property
    def display_name(self) -> str:
        return "webgraft (local extraction)"

    def is_available(self) -> bool:
        # No network here: runs on every `hermes tools` paint.
        return all(importlib.util.find_spec(m) is not None for m in ("trafilatura", "httpx"))

    def get_setup_schema(self) -> dict[str, Any]:
        """Row in the `hermes tools` picker (ProviderBase contract). No key, no service to configure."""
        return {
            "name": "webgraft (local extraction)",
            "badge": "free · local · extract only",
            "tag": "Pages and documents (PDF, Office) read on this machine; 404s and bot walls reported as errors. "
                   "Pair with any search backend. Run `hermes webgraft setup` for Crawl4AI.",
            "env_vars": [],
        }

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        # Contract from agent.web_search_provider: one dict per URL, "error" only on failure.
        # (The developer guide's {"success", "data"} envelope is outdated for extract.)
        try:
            pages = await extract.read_pages(list(urls))
        except Exception as exc:  # noqa: BLE001 - whole-call failure, reported on every URL
            error = f"webgraft could not read the pages: {exc}"[:300]
            return [{"url": u, "title": "", "content": "", "error": error} for u in urls]
        return [p.as_result() for p in pages]
