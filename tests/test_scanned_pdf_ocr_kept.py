"""A fully scanned PDF keeps the text its OCR produced.

The text-mode pdftotext chain used to run after the scanned-PDF OCR step
whenever the mode was "text" (the default), replacing the OCR text with
pdftotext's empty result, so the file failed with "Could not extract text".
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from atom_brain import ocr, utils  # noqa: E402


def _scanned(monkeypatch, tmp_path, ocrmypdf_text, page_pairs):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    monkeypatch.setattr(utils, "prepare_dependencies", lambda *_: None)
    monkeypatch.setattr(utils, "looks_image_only", lambda *_a, **_k: True)
    monkeypatch.setattr(utils, "count_pages", lambda *_: len(page_pairs) or 1)
    monkeypatch.setattr(ocr, "ocr_pdf_with_ocrmypdf", lambda *_a, **_k: ocrmypdf_text)
    monkeypatch.setattr(ocr, "ocr_pdf_pages", lambda *_a, **_k: list(page_pairs))
    monkeypatch.setattr(ocr, "process_pdf_figures", lambda *_a, **_k: ([], {
        "figures_extracted": 0, "figures_with_ocr": 0, "vision_used": False}))

    def no_text_layer(*_a, **_k):
        raise AssertionError("pdftotext chain must not run after OCR produced text")

    monkeypatch.setattr(utils, "extract_with_pdftotext_detail", no_text_layer)
    return pdf


@pytest.mark.parametrize("mode", ["text", "technical"])
def test_page_ocr_text_is_kept(monkeypatch, tmp_path, mode):
    pdf = _scanned(monkeypatch, tmp_path, None,
                   [(1, "Chapter 1\nScanned words on page one."), (2, "Page two.")])
    monkeypatch.setattr(utils, "extract_with_docling", lambda *_: None)
    result = utils.extract_single_file(pdf, mode, "no")
    assert result["extraction_method"] == "tesseract-page-ocr"
    assert "Scanned words on page one." in result["text"]


def test_ocrmypdf_text_is_kept(monkeypatch, tmp_path):
    pdf = _scanned(monkeypatch, tmp_path, "Chapter 1\nText from ocrmypdf.", [])
    result = utils.extract_single_file(pdf, "text", "no")
    assert result["extraction_method"] == "ocrmypdf"
    assert "Text from ocrmypdf." in result["text"]


@pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftotext")),
                    reason="needs tesseract and poppler")
def test_scanned_pdf_end_to_end(tmp_path):
    pil = pytest.importorskip("PIL")  # noqa: F841
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1654, 2339), "white")
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(img)
    for i in range(12):
        draw.text((150, 200 + 60 * i), f"Scanned line number {i} of this test page.",
                  font=font, fill="black")
    pdf = tmp_path / "scan.pdf"
    img.save(pdf, resolution=200)
    work = tmp_path / "work"
    env = {**os.environ, "BOOK_SKILL_WORKDIR": str(work),
           "ATOM_OCR_CACHE_DIR": "off"}
    proc = subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "extract.py"),
                           str(pdf), "--install-missing", "no"],
                          capture_output=True, text=True, env=env, timeout=300)
    assert proc.returncode == 0, proc.stdout[-600:] + proc.stderr[-600:]
    assert "Scanned line number 11" in (work / "full_text.txt").read_text()
