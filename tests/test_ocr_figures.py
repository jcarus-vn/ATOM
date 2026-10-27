"""OCR and embedded figures: figure blocks, placeholders, the vision hook,
and clean skips when the OCR tools are absent from the machine."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from book_to_skill import ocr as ocr_module


class TestFigureBlocks:
    def test_block_carries_page_index_size_and_ocr_text(self):
        block = ocr_module.figure_block(
            3, 2, {"width": 800, "height": 600, "format": "png"},
            "Revenue grows with price.", None)
        assert "[FIGURE page 3, figure 2 | 800x600 png" in block
        assert "Revenue grows with price." in block

    def test_empty_ocr_keeps_placeholder(self):
        block = ocr_module.figure_block(
            1, 1, {"width": 100, "height": 100, "format": "png"}, None, None)
        assert ocr_module.FIGURE_PLACEHOLDER in block

    def test_vision_text_is_labelled_unverified(self):
        block = ocr_module.figure_block(
            1, 1, {"width": 100, "height": 100, "format": "png"},
            None, "A bar chart rising left to right.")
        assert ocr_module.VISION_LABEL in block
        assert "A bar chart rising left to right." in block

    def test_scanned_page_detection(self):
        assert ocr_module.is_scanned_text("") is True
        assert ocr_module.is_scanned_text("12\n") is True
        assert ocr_module.is_scanned_text("A real paragraph. " * 20) is False


class TestVisionHook:
    def test_vision_command_runs_with_image_path(self, tmp_path, monkeypatch):
        # A tiny stand-in vision command: print a description, ignore argv.
        script = tmp_path / "vision.py"
        script.write_text("import sys; print('a diagram with two peaks')\n")
        monkeypatch.setenv(
            ocr_module.VISION_ENV_VAR, f"{sys.executable} {script}")
        assert ocr_module.describe_with_vision(b"fake-png-bytes") == (
            "a diagram with two peaks")

    def test_no_vision_command_means_no_vision(self, monkeypatch):
        monkeypatch.delenv(ocr_module.VISION_ENV_VAR, raising=False)
        assert ocr_module.describe_with_vision(b"fake-png-bytes") is None


class TestTesseractPaths:
    def test_ocr_image_bytes_uses_tesseract_when_mocked(self, monkeypatch):
        class FakeResult:
            returncode = 0
            stdout = b"hello from the figure\n"

        monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/tesseract")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeResult())
        assert ocr_module.ocr_image_bytes(b"fake") == "hello from the figure"

    def test_real_tesseract_path_skips_cleanly_when_absent(self):
        if shutil.which("tesseract"):
            pytest.skip("tesseract is installed; the mocked test covers this path")
        assert ocr_module.ocr_image_bytes(b"fake") is None
        assert ocr_module.ocr_status()["tesseract"] is False

    def test_real_ocr_on_generated_image_skips_without_tesseract(self, tmp_path):
        """End-to-end OCR on a PIL-rendered image; skips cleanly when the
        tool is absent, so the suite stays green on machines without it."""
        if not shutil.which("tesseract"):
            pytest.skip("tesseract not installed on this machine")
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (400, 100), "white")
        ImageDraw.Draw(img).text((10, 10), "HELLO FIGURE", fill="black")
        path = tmp_path / "fig.png"
        img.save(path)
        text = ocr_module.ocr_image_file(str(path))
        assert text and "HELLO" in text.upper()

    def test_check_reports_ocr_tools(self):
        status = ocr_module.ocr_status()
        assert set(status) == {"ocrmypdf", "tesseract", "fitz", "pdftoppm"}
