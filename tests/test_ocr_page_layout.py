"""Scanned pages: two-column prose, tables and lists all come out in order.

PSM 6 reads a two-column page across both columns. PSM 3 finds the columns
but also cuts some one-column pages into side-by-side blocks (a table into
its columns, lines after a repeated first word). ``ocr_page_bytes`` uses
PSM 3 and reads again with PSM 6 when its side-by-side blocks are narrow
or a word-space apart rather than real columns.
"""

import shutil
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "tools"))

from atom_brain import ocr  # noqa: E402

HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def _tsv(*blocks):
    """TSV with one block per (left, top, width, height), one word each
    spanning the block's width, word height 30."""
    rows = [HEADER, "1\t1\t0\t0\t0\t0\t0\t0\t1654\t2339\t-1\t"]
    for n, (left, top, width, height) in enumerate(blocks, 1):
        rows.append(f"2\t1\t{n}\t0\t0\t0\t{left}\t{top}\t{width}\t{height}\t-1\t")
        rows.append(f"5\t1\t{n}\t1\t1\t1\t{left}\t{top}\t{width}\t30\t95\tword")
    return "\n".join(rows)


TWO_COLUMNS = _tsv((150, 150, 640, 1800), (860, 150, 640, 1800))
ONE_BLOCK = _tsv((150, 150, 1350, 1800))
TABLE_SPLIT = _tsv((150, 150, 140, 1500), (550, 150, 90, 1500),
                   (900, 150, 120, 1500), (1250, 150, 80, 1500))
WORD_SPACE_SPLIT = _tsv((152, 206, 215, 626), (383, 206, 479, 693))
COLUMNS_WITH_TABLE = _tsv((150, 150, 640, 1800), (860, 150, 640, 1800),
                          (860, 900, 200, 300), (1100, 900, 200, 300))


@pytest.mark.parametrize("tsv, false_columns", [
    (TWO_COLUMNS, False),
    (ONE_BLOCK, False),
    (TABLE_SPLIT, True),
    (WORD_SPACE_SPLIT, True),
    (COLUMNS_WITH_TABLE, False),
])
def test_false_column_detection(tsv, false_columns):
    assert ocr._split_into_false_columns(tsv) is false_columns


def _fake(monkeypatch, tsv, psm3_text="psm3 text", psm6_text="psm6 text"):
    calls = []
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/usr/bin/" + name)

    def layout(data, lang, timeout, psm):
        calls.append(psm)
        return psm3_text, tsv

    def plain(data, lang=None, timeout=120, psm=6):
        calls.append(psm)
        return psm6_text

    monkeypatch.setattr(ocr, "_ocr_with_layout", layout)
    monkeypatch.setattr(ocr, "ocr_image_bytes", plain)
    return calls


def test_two_column_page_uses_psm3_once(monkeypatch):
    calls = _fake(monkeypatch, TWO_COLUMNS)
    assert ocr.ocr_page_bytes(b"png", lang="eng") == "psm3 text"
    assert calls == [3]


@pytest.mark.parametrize("tsv", [TABLE_SPLIT, WORD_SPACE_SPLIT])
def test_false_columns_are_read_again_with_psm6(monkeypatch, tsv):
    calls = _fake(monkeypatch, tsv)
    assert ocr.ocr_page_bytes(b"png", lang="eng") == "psm6 text"
    assert calls == [3, 6]


def test_psm3_text_kept_when_psm6_reads_nothing(monkeypatch):
    calls = _fake(monkeypatch, TABLE_SPLIT, psm6_text=None)
    assert ocr.ocr_page_bytes(b"png", lang="eng") == "psm3 text"
    assert calls == [3, 6]


def test_explicit_psm_is_used_as_is(monkeypatch):
    calls = _fake(monkeypatch, TWO_COLUMNS)
    assert ocr.ocr_page_bytes(b"png", psm=6) == "psm6 text"
    assert calls == [6]


def test_cache_keys_separate_auto_from_fixed_psm():
    assert ocr._cache_key(b"p", "eng", None) != ocr._cache_key(b"p", "eng", 6)


def test_page_ocr_goes_through_ocr_page_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, "_render_cached", lambda *a, **k: b"png")
    monkeypatch.setattr(ocr, "is_blank_image", lambda data: False)
    seen = []
    monkeypatch.setattr(ocr, "ocr_page_bytes",
                        lambda data, lang=None, timeout=120, psm=None: seen.append(psm) or "text")
    pairs = ocr.ocr_pdf_pages("x.pdf", 2, cache_dir="", jobs=1, lang="eng")
    assert pairs == [(1, "text"), (2, "text")]
    assert seen == [None, None]


needs_tesseract = pytest.mark.skipif(not shutil.which("tesseract"),
                                     reason="needs tesseract")


@needs_tesseract
@pytest.mark.parametrize("page", ["two-column", "two-column-offset", "table-page",
                                  "one-column", "repeated-start", "toc-page"])
def test_real_pages_read_in_order(page):
    pytest.importorskip("PIL")
    import eval_ocr
    png, truth = eval_ocr.build(page)
    text = ocr.ocr_page_bytes(png, lang="eng")
    assert eval_ocr.cer(text, truth) < 0.02, text[:300]
