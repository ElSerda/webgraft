"""OCR of scanned PDF pages, on this machine, without a language model. Runs in webgraft's OCR environment.

    python ocr_runner.py PAGES MAX_CHARS < document.pdf   ->  {"pages": {"1": "text", ...}} or {"error": ...}

PAGES is a comma-separated list of 1-based page numbers (from anydoc's NeedsOcrError). Pages are
rendered with pypdfium2 and read by RapidOCR (PaddleOCR models in ONNX format, run by onnxruntime).
RapidOCR downloads its models once, from its official repository, and checks their SHA-256.
"""

import json
import sys

RENDER_SCALE = 2.0  # 144 dpi: small print stays readable, memory stays modest
MAX_PIXELS = 20_000_000  # about 80 MB as RGBA; an A4 page at RENDER_SCALE is about 2 million


def ocr(data: bytes, pages: list[int], max_chars: int) -> dict:
    import pypdfium2
    from rapidocr import RapidOCR

    engine = RapidOCR()
    pdf = pypdfium2.PdfDocument(data)
    out, size = {}, 0
    for number in pages:
        if not 1 <= number <= len(pdf):
            continue
        page = pdf[number - 1]
        width, height = page.get_size()  # points
        # A page can declare any size: cap the rendered image (MAX_PIXELS) so it cannot take GBs.
        scale = min(RENDER_SCALE, (MAX_PIXELS / max(1.0, width * height)) ** 0.5)
        image = page.render(scale=scale).to_pil()
        result = engine(image)
        text = "\n".join(result.txts or ()) if result is not None else ""
        out[str(number)] = text
        size += len(text)
        if size > max_chars:
            break
    return {"pages": out}


if __name__ == "__main__":
    try:
        wanted = [int(p) for p in sys.argv[1].split(",") if p.strip()]
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 30_000
        result = ocr(sys.stdin.buffer.read(), wanted, limit)
    except Exception as exc:  # noqa: BLE001 - reported to the caller as the document's error
        result = {"error": f"{exc.__class__.__name__}: {exc}"[:300]}
    sys.stdout.write("\n" + json.dumps(result) + "\n")
    sys.stdout.flush()
