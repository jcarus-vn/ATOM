"""Scanned pages: two-column prose and tables both come out in reading order.

PSM 6 reads a two-column page across both columns; PSM 3 reads a full-page
table column by column. ``ocr_page_bytes`` uses PSM 3 and falls back to
PSM 6 for a page that PSM 3 returns as a table cut into columns.
"""

import shutil
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "tools"))

from atom_brain import ocr  # noqa: E402

PROSE = "\n".join(f"a line of ordinary prose that runs to about eight words {i}"
                  for i in range(20))
TABLE_BY_COLUMN = "\n".join([f"Item {i:02d}" for i in range(20)]
                            + [f"{i}.5" for i in range(20)])
TABLE_BY_ROW = "\n".join(f"Item {i:02d} {i}.5 3 units ok" for i in range(20))
POEM = "\n".join(["Short line here", "and another one", "the wind is cold"] * 4)


def _fake(monkeypatch, by_psm):
    calls = []

    def fake(data, lang=None, timeout=120, psm=6):
        calls.append(psm)
        return by_psm[psm]

    monkeypatch.setattr(ocr, "ocr_image_bytes", fake)
    return calls


def test_two_column_prose_uses_psm3_once(monkeypatch):
    calls = _fake(monkeypatch, {3: PROSE, 6: "joined across columns"})
    assert ocr.ocr_page_bytes(b"png") == PROSE
    assert calls == [3]


def test_table_split_into_columns_is_read_again_by_rows(monkeypatch):
    calls = _fake(monkeypatch, {3: TABLE_BY_COLUMN, 6: TABLE_BY_ROW})
    assert ocr.ocr_page_bytes(b"png") == TABLE_BY_ROW
    assert calls == [3, 6]


def test_short_line_page_keeps_psm3_when_psm6_is_no_better(monkeypatch):
    calls = _fake(monkeypatch, {3: POEM, 6: POEM})
    assert ocr.ocr_page_bytes(b"png") == POEM
    assert calls == [3, 6]


def test_explicit_psm_is_used_as_is(monkeypatch):
    calls = _fake(monkeypatch, {6: "x", 3: "y"})
    assert ocr.ocr_page_bytes(b"png", psm=6) == "x"
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
                                  "one-column"])
def test_real_pages_read_in_order(page):
    pytest.importorskip("PIL")
    import eval_ocr
    png, truth = eval_ocr.build(page)
    text = ocr.ocr_page_bytes(png, lang="eng")
    assert eval_ocr.cer(text, truth) < 0.02, text[:300]
