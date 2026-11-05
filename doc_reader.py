"""Reads one document with anydoc, Hermes's own document converter (the one `read_file` uses).

    python doc_reader.py [max_chars] [format] < document  ->  {"text": "..."} or {"error": "..."} (one ASCII JSON line)

PDF, Word, PowerPoint, Excel, OpenDocument, EPUB, RTF and CSV become Markdown, tables included.
format (pdf, docx, csv...) is passed when the URL or content type gives it; anydoc detects the rest.
Run as a child process by extract.document_text_isolated: anydoc is native code, and a crafted
file must not hang or crash Hermes. Standalone on purpose: no imports from the plugin.
"""

import json
import sys


def document_text(data: bytes, max_chars: int = 30_000, fmt: str | None = None) -> dict:
    import anydoc

    try:
        text = anydoc.to_markdown_bytes(data, fmt or None)
    except Exception as exc:  # noqa: BLE001 - every anydoc failure is a page error for the caller
        kind = type(exc).__name__
        if kind == "NeedsOcrError":
            return {"error": "scanned document with no text layer (needs OCR)",
                    "needs_ocr": [int(p) for p in (getattr(exc, "pages", None) or [])]}
        if kind == "EncryptedError":
            return {"error": "encrypted document"}
        return {"error": f"{kind}: {exc}"[:300]}
    return {"text": (text or "").strip()[:max_chars]}


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 30_000
    fmt = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    sys.stdout.write(json.dumps(document_text(sys.stdin.buffer.read(), limit, fmt)) + "\n")
