"""A tilted page is straightened before OCR, as a recovery pass on a
low-confidence page. Needs Pillow for the estimate and tesseract for the
end-to-end reads; both are skipped when absent. Fixtures are synthetic.
"""

import io
import shutil
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "tools"))

from atom_brain import ocr  # noqa: E402

needs_pil = pytest.mark.skipif(__import__("importlib").util.find_spec("PIL") is None,
                               reason="needs Pillow")
needs_both = pytest.mark.skipif(
    not shutil.which("tesseract") or __import__("importlib").util.find_spec("PIL") is None,
    reason="needs tesseract and Pillow")


def _png(angle):
    import eval_ocr
    from PIL import Image
    png, truth = eval_ocr.build("one-column")
    image = Image.open(io.BytesIO(png)).convert("L")
    if angle:
        image = image.rotate(angle, expand=True, fillcolor=255)
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue(), truth


def test_deskew_angle_is_zero_without_pillow_or_on_junk():
    assert ocr._deskew_angle(b"not an image") == 0.0
    assert ocr._deskewed_png(b"not an image", 5.0) is None


@needs_pil
@pytest.mark.parametrize("tilt", [-10, -6, 4, 9])
def test_deskew_estimates_the_tilt(tilt):
    png, _ = _png(tilt)
    # The scan is tilted by +tilt, so straightening needs about -tilt.
    assert abs(ocr._deskew_angle(png) - (-tilt)) <= 1.5


@needs_pil
def test_upright_page_is_not_flagged_as_tilted():
    png, _ = _png(0)
    assert abs(ocr._deskew_angle(png)) < ocr.DESKEW_TRIGGER


@needs_both
@pytest.mark.parametrize("tilt", [7, 10, -9])
def test_a_tilted_page_recovers(tilt):
    png, truth = _png(tilt)
    assert eval_ocr_cer(png, truth) < 0.03


def eval_ocr_cer(png, truth):
    import eval_ocr
    return eval_ocr.cer(ocr.ocr_page_bytes(png, lang="eng"), truth)


def test_deskew_is_a_recovery_candidate(monkeypatch):
    # First pass low-confidence; the straightened read is the strongest.
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ocr, "_split_into_false_columns", lambda tsv: False)
    monkeypatch.setattr(ocr, "is_blank_image", lambda data: False)
    monkeypatch.setattr(ocr, "_deskew_angle", lambda data: 8.0)
    monkeypatch.setattr(ocr, "_deskewed_png", lambda data, angle: b"straightened")

    def layout(data, lang, timeout, psm):
        if data == b"straightened":
            return ("STRAIGHT TEXT", _conf_tsv(92.0))
        return ("tilted", _conf_tsv(20.0))        # first pass and the OSD retry

    monkeypatch.setattr(ocr, "_ocr_with_layout", layout)
    assert ocr.ocr_page_bytes(b"png", lang="eng") == "STRAIGHT TEXT"


def test_cache_text_version_bumped():
    assert ocr.PAGE_TEXT_VERSION == "4"


def _conf_tsv(conf):
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    return header + f"\n5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t{conf}\tword"
