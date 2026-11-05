"""Document reading (anydoc, in a child process): real text comes back, a stuck reader is stopped."""

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import load  # noqa: E402

load()
ex = importlib.import_module("webgraft.extract")
pytest.importorskip("anydoc")


def tiny_pdf(text: str) -> bytes:
    """A one-page PDF with one line of text, built by hand (no PDF library needed)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_pdf_text_comes_back_from_the_child_process():
    text = asyncio.run(ex.document_text_isolated(tiny_pdf("Total power 360 W")))
    assert "360 W" in text


def test_a_stuck_reader_is_stopped(tmp_path, monkeypatch):
    stuck = tmp_path / "stuck.py"
    stuck.write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(ex, "DOC_READER", stuck)
    monkeypatch.setattr(ex, "DOC_TIMEOUT", 1)
    with pytest.raises(RuntimeError, match="stopped after 1s"):
        asyncio.run(ex.document_text_isolated(b"%PDF-1.4"))


def test_a_broken_pdf_is_an_error_not_a_crash():
    with pytest.raises(RuntimeError):
        asyncio.run(ex.document_text_isolated(b"%PDF-1.4 not really a pdf"))


def test_child_finds_packages_from_the_parent_path(monkeypatch, tmp_path):
    # Hermes runs a base interpreter and adds its package environment to sys.path at startup:
    # the child must import anydoc through the parent's path, not its interpreter's own packages.
    import venv
    venv.create(tmp_path / "empty", with_pip=False)  # an interpreter that has no anydoc of its own
    bare = tmp_path / "empty" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    monkeypatch.setattr(ex.sys, "executable", str(bare))
    assert "360 W" in asyncio.run(ex.document_text_isolated(tiny_pdf("Total power 360 W")))


def test_documents_are_recognized_by_signature_type_or_extension():
    assert ex.is_document("https://a.example/x", "", b"%PDF-1.7 ...")
    assert ex.is_document("https://a.example/x", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK")
    assert ex.is_document("https://a.example/report.epub?dl=1", "application/octet-stream", b"PK")
    assert not ex.is_document("https://a.example/page.html", "text/html; charset=utf-8", b"<html>")


def test_format_is_passed_when_known():
    assert ex.document_format("https://a.example/data.csv", "text/csv; charset=utf-8", b"a,b") == (True, "csv")
    assert ex.document_format("https://a.example/x", "application/pdf", b"%PDF-") == (True, "pdf")
    assert ex.document_format("https://a.example/old.xls", "", b"\xd0\xcf") == (True, None)


def test_csv_is_read_as_a_table():
    text = asyncio.run(ex.document_text_isolated(b"plan,ram_gb,disk_gb\nplus,4,25\nultra,8,100\n", "csv"))
    assert "ultra" in text and "100" in text


def scanned_pdf(tmp_path) -> bytes:
    """A two-page PDF made only of images: what a scanner produces."""
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    pages = []
    for line in ("Total power 360 W", "Memory 16 GB"):
        img = Image.new("RGB", (600, 200), "white")
        ImageDraw.Draw(img).text((20, 80), line, fill="black")
        pages.append(img)
    path = tmp_path / "scan.pdf"
    pages[0].save(path, save_all=True, append_images=pages[1:])
    return path.read_bytes()


def test_scanned_pdf_reports_its_pages(tmp_path):
    with pytest.raises(ex.NeedsOcr) as caught:
        asyncio.run(ex.document_text_isolated(scanned_pdf(tmp_path), "pdf"))
    assert caught.value.pages == [1, 2]


def test_ocr_text_comes_back_with_a_note(tmp_path, monkeypatch):
    fake = tmp_path / "fake_ocr.py"
    fake.write_text('import json, sys\nsys.stdin.buffer.read()\n'
                    'print(json.dumps({"pages": {"1": "Total power 360 W", "2": ""}}))\n')
    monkeypatch.setattr(ex, "ocr_python", lambda: Path(sys.executable))
    monkeypatch.setattr(ex, "OCR_RUNNER", fake)
    text = asyncio.run(ex.ocr_isolated(b"%PDF-1.4", [1, 2]))
    assert text.startswith("[Text read by OCR from scanned pages 1;")
    assert "[page 1]\nTotal power 360 W" in text


def test_without_ocr_the_error_says_how_to_turn_it_on(monkeypatch):
    monkeypatch.setattr(ex, "ocr_python", lambda: None)
    with pytest.raises(RuntimeError, match="hermes webgraft setup"):
        asyncio.run(ex.ocr_isolated(b"%PDF-1.4", [1]))


def test_a_memory_crash_reads_as_one_clear_line():
    rust_panic = ('pyo3_runtime.PanicException: assertion `left == right` failed\n  left: MemError\n right: Ok\n'
                  'note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace\n')
    assert "GB of address space" in ex._child_failure(rust_panic)
    assert ex._child_failure("Traceback...\nValueError: bad xref\nnote: hint\n") == "ValueError: bad xref"


def test_the_reader_runs_with_few_threads_and_arenas(tmp_path, monkeypatch):
    probe = tmp_path / "probe.py"
    probe.write_text('import json, os, sys\nsys.stdin.buffer.read()\n'
                     'print(json.dumps({"text": os.environ["MALLOC_ARENA_MAX"] + "/" + os.environ["RAYON_NUM_THREADS"]}))\n')
    monkeypatch.setattr(ex, "DOC_READER", probe)
    assert asyncio.run(ex.document_text_isolated(b"%PDF-1.4")) == "2/4"
