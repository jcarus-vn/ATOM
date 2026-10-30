"""Real-OCR pipeline tests: language auto-detection, page triage,
page-hash caching, and order-preserving parallel OCR.

These tests use real tesseract when it is installed (and skip cleanly
without it). The synthetic fixtures are original text rendered with
DejaVu; no copyrighted material is involved.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from atom_brain import ocr as ocr_module

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _needs_ocr():
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed on this machine")
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        pytest.skip("Pillow not installed in this environment")
    if not Path(FONT).exists():
        pytest.skip("DejaVu font not available")


def _needs_pil():
    try:
        import PIL  # noqa: F401
    except ImportError:
        pytest.skip("Pillow not installed in this environment")


def _lev(a, b):
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[len(b)]


def _cer(truth, got):
    t = " ".join((truth or "").split())
    g = " ".join((got or "").split())
    if not t:
        return 0.0 if not g else 1.0
    return _lev(t, g) / len(t)


def _render_text_image(text, path, size=(1200, 400), font_size=28):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, font_size)
    y = 20
    for line in text.split("\n"):
        draw.text((20, y), line, fill="black", font=font)
        y += font_size + 12
    img.save(path)
    return path


def _image_only_pdf(pages, path):
    """pages: list of PIL images -> single image-only PDF (no text layer)."""
    pages[0].save(path, save_all=True, append_images=pages[1:])


class TestLanguageDetection:
    def test_prefers_vie_eng_when_both_installed(self, monkeypatch):
        class Res:
            returncode = 0
            stdout = b'List of available languages in "/usr/share/tesseract-ocr/5/tessdata/":\neng\nosd\nvie\n'
            stderr = b""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Res())
        assert ocr_module.detect_ocr_languages() == "vie+eng"

    def test_falls_back_to_eng_when_vie_missing(self, monkeypatch):
        class Res:
            returncode = 0
            stdout = b"eng\nosd\n"
            stderr = b""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Res())
        assert ocr_module.detect_ocr_languages() == "eng"

    def test_probe_failure_falls_back_to_eng(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("no tesseract")

        monkeypatch.setattr(subprocess, "run", boom)
        assert ocr_module.detect_ocr_languages() == "eng"

    def test_real_detection_matches_installed_data(self):
        _needs_ocr()
        langs = ocr_module.detect_ocr_languages()
        assert langs in ("vie+eng", "eng")


class TestRealOcr:
    def test_english_recovers_cleanly(self, tmp_path):
        _needs_ocr()
        text = "The quick brown fox jumps over the lazy dog"
        path = _render_text_image(text, tmp_path / "en.png")
        got = ocr_module.ocr_image_file(str(path), lang="eng")
        assert got and _cer(text, got) < 0.05

    def test_vietnamese_recovers_with_auto_lang(self, tmp_path):
        _needs_ocr()
        if ocr_module.detect_ocr_languages() != "vie+eng":
            pytest.skip("Vietnamese tesseract data not installed")
        text = "Ti\u1ebfng Vi\u1ec7t c\u00f3 d\u1ea5u c\u1ea7n d\u1eef li\u1ec7u ti\u1ebfng Vi\u1ec7t"
        path = _render_text_image(text, tmp_path / "vi.png")
        got = ocr_module.ocr_image_file(str(path))
        assert got and _cer(text, got) < 0.08


class TestTriage:
    def test_blank_and_pagenum_pages_are_blank(self, tmp_path):
        _needs_pil()
        from PIL import Image

        white = tmp_path / "white.png"
        Image.new("RGB", (800, 600), "white").save(white)
        assert ocr_module.is_blank_image(white.read_bytes()) is True
        num = tmp_path / "num.png"
        _render_text_image("42", num, size=(1654, 2339))
        assert ocr_module.is_blank_image(num.read_bytes()) is True

    def test_text_page_is_not_blank(self, tmp_path):
        _needs_pil()
        path = _render_text_image(
            "A real paragraph with several words in it.", tmp_path / "text.png")
        assert ocr_module.is_blank_image(path.read_bytes()) is False

    def test_undecodable_bytes_are_not_blank(self):
        assert ocr_module.is_blank_image(b"not a png at all") is False
        assert ocr_module.is_blank_image(b"\x89PNG\r\n\x1a\n\x00\x00") is False

    def test_stdlib_png_decoder_agrees_with_pil(self, tmp_path):
        _needs_pil()
        from PIL import Image

        white = tmp_path / "w.png"
        Image.new("RGB", (400, 300), "white").save(white)
        ratio = ocr_module._png_ink_ratio(white.read_bytes())
        assert ratio == 0.0
        text = _render_text_image("Hello world, this is text.", tmp_path / "t.png")
        ratio = ocr_module._png_ink_ratio(text.read_bytes())
        assert ratio is not None and ratio > 0.001


class TestCacheAndParallel:
    def test_cache_roundtrip(self, tmp_path):
        key_png = b"fake-png-bytes"
        assert ocr_module._cache_get(str(tmp_path), key_png, "eng", 6) is None
        ocr_module._cache_put(str(tmp_path), key_png, "eng", 6, "hello")
        assert ocr_module._cache_get(str(tmp_path), key_png, "eng", 6) == "hello"
        assert ocr_module._cache_get("", key_png, "eng", 6) is None
        assert ocr_module._cache_get(str(tmp_path), key_png, "vie+eng", 6) is None

    def test_triage_skips_blank_and_cache_serves_second_run(self, tmp_path, monkeypatch):
        _needs_ocr()
        from PIL import Image

        _render_text_image("Hello cached world", tmp_path / "p1.png", size=(900, 300))
        text_img = Image.open(tmp_path / "p1.png")
        blank = Image.new("RGB", (900, 300), "white")
        pdf = tmp_path / "sample.pdf"
        _image_only_pdf([text_img, blank], pdf)
        cache = tmp_path / "cache"
        calls = []

        real_ocr = ocr_module.ocr_page_bytes

        def counting(data, lang=None, timeout=120, psm=None):
            calls.append(1)
            return real_ocr(data, lang=lang, timeout=timeout, psm=psm)

        monkeypatch.setattr(ocr_module, "ocr_page_bytes", counting)
        first = ocr_module.ocr_pdf_pages(str(pdf), 2, cache_dir=str(cache), jobs=1)
        assert [p for p, _ in first] == [1]
        assert len(calls) == 1
        calls.clear()
        second = ocr_module.ocr_pdf_pages(str(pdf), 2, cache_dir=str(cache), jobs=1)
        assert second == first
        assert calls == []

    def test_parallel_order_matches_sequential(self, monkeypatch):
        def fake_ocr(pdf_path, page, dpi, lang, psm, cache_dir, timeout, triage, pdf_hash=None):
            return page, f"text-{page}"

        monkeypatch.setattr(ocr_module, "_ocr_one_page", fake_ocr)
        seq = ocr_module.ocr_pdf_pages("x.pdf", 5, jobs=1, cache_dir="")
        par = ocr_module.ocr_pdf_pages("x.pdf", 5, jobs=4, cache_dir="")
        assert seq == par == [(i, f"text-{i}") for i in range(1, 6)]
