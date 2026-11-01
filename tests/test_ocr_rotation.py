"""A low-confidence page is retried with orientation detection, so an
upside-down scan comes out upright; a normal page pays nothing.

The pure-Python parts (confidence parsing, the recovery trigger, the
ocrmypdf option list) always run. The end-to-end rotation checks need
tesseract and are skipped without it. Fixtures are synthetic.
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

needs_tesseract = pytest.mark.skipif(not shutil.which("tesseract"),
                                     reason="needs tesseract")

TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def _tsv(words):
    rows = [TSV_HEADER]
    for i, (text, conf) in enumerate(words, 1):
        rows.append(f"5\t1\t1\t1\t1\t{i}\t0\t0\t10\t10\t{conf}\t{text}")
    return "\n".join(rows)


def test_confidence_mean_and_count():
    assert ocr._page_confidence(_tsv([("a", 90.0), ("b", 80.0)])) == (85.0, 2)
    assert ocr._page_confidence(_tsv([])) == (-1.0, 0)
    assert ocr._page_confidence(_tsv([("x", "notanumber")])) == (-1.0, 0)


def test_ocrmypdf_args_default_and_override(monkeypatch):
    monkeypatch.delenv(ocr.OCRMYPDF_ARGS_ENV, raising=False)
    args = ocr._ocrmypdf_args()
    assert "--rotate-pages" in args and "--deskew" in args and "--force-ocr" in args
    assert "--clean" not in args                       # needs unpaper, not a dependency
    monkeypatch.setenv(ocr.OCRMYPDF_ARGS_ENV, "--redo-ocr --deskew")
    assert ocr._ocrmypdf_args() == ["--redo-ocr", "--deskew"]


def test_ocrmypdf_passes_straightening_flags(monkeypatch, tmp_path):
    monkeypatch.delenv(ocr.OCRMYPDF_ARGS_ENV, raising=False)
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/usr/bin/" + name)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"%PDF-1.4")

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    monkeypatch.setattr("atom_brain.parsers.pdf.extract_with_pdftotext",
                        lambda path: "text")
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert ocr.ocr_pdf_with_ocrmypdf(str(pdf)) == "text"
    assert seen["cmd"][0] == "ocrmypdf"
    assert "--rotate-pages" in seen["cmd"] and "--deskew" in seen["cmd"]


def test_recovery_runs_only_on_a_low_confidence_page_with_ink(monkeypatch):
    calls = []
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ocr, "_split_into_false_columns", lambda tsv: False)
    monkeypatch.setattr(ocr, "is_blank_image", lambda data: False)
    monkeypatch.setattr(ocr, "_recover_low_confidence",
                        lambda *a, **k: calls.append(1) or [("UPRIGHT TEXT", 95.0)])

    def first_pass(conf):
        def layout(data, lang, timeout, psm):
            return ("original text", _tsv([("w", conf)]))
        return layout

    monkeypatch.setattr(ocr, "_ocr_with_layout", first_pass(96.0))
    assert ocr.ocr_page_bytes(b"png", lang="eng") == "original text"
    assert calls == []                                 # confident: no recovery

    monkeypatch.setattr(ocr, "_ocr_with_layout", first_pass(30.0))
    assert ocr.ocr_page_bytes(b"png", lang="eng") == "UPRIGHT TEXT"
    assert calls == [1]                                # low confidence: recovered


def test_blank_page_does_not_trigger_recovery(monkeypatch):
    calls = []
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ocr, "_split_into_false_columns", lambda tsv: False)
    monkeypatch.setattr(ocr, "is_blank_image", lambda data: True)
    monkeypatch.setattr(ocr, "_recover_low_confidence",
                        lambda *a, **k: calls.append(1) or [])
    monkeypatch.setattr(ocr, "_ocr_with_layout",
                        lambda *a, **k: ("", _tsv([])))
    assert ocr.ocr_page_bytes(b"png", lang="eng") is None
    assert calls == []


def test_recovery_keeps_the_first_read_when_it_is_better(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ocr, "_split_into_false_columns", lambda tsv: False)
    monkeypatch.setattr(ocr, "is_blank_image", lambda data: False)
    monkeypatch.setattr(ocr, "_ocr_with_layout",
                        lambda *a, **k: ("low but real", _tsv([("w", 40.0)])))
    monkeypatch.setattr(ocr, "_recover_low_confidence",
                        lambda *a, **k: [("garbage", 10.0)])
    assert ocr.ocr_page_bytes(b"png", lang="eng") == "low but real"


def test_cache_key_version_changed():
    assert ocr.PAGE_TEXT_VERSION == "3"


@needs_tesseract
@pytest.mark.parametrize("page", ["one-column-upside-down", "two-column-upside-down"])
def test_upside_down_page_reads_upright(page):
    pytest.importorskip("PIL")
    import eval_ocr
    png, truth = eval_ocr.build(page)
    assert eval_ocr.cer(ocr.ocr_page_bytes(png, lang="eng"), truth) < 0.02


@needs_tesseract
def test_a_sideways_rotated_page_is_recovered():
    pytest.importorskip("PIL")
    import eval_ocr
    from PIL import Image
    png, truth = eval_ocr.build("one-column")
    turned = Image.open(io.BytesIO(png)).rotate(-90, expand=True, fillcolor=255)
    buf = io.BytesIO()
    turned.save(buf, "PNG")
    assert eval_ocr.cer(ocr.ocr_page_bytes(buf.getvalue(), lang="eng"), truth) < 0.02


@needs_tesseract
def test_normal_pages_do_not_trigger_a_recovery_pass(monkeypatch):
    pytest.importorskip("PIL")
    import eval_ocr
    calls = []
    original = ocr._recover_low_confidence
    monkeypatch.setattr(ocr, "_recover_low_confidence",
                        lambda *a, **k: calls.append(1) or original(*a, **k))
    for page in ("one-column", "two-column", "table-page", "one-column-noisy"):
        png, _truth = eval_ocr.build(page)
        ocr.ocr_page_bytes(png, lang="eng")
    assert calls == []
