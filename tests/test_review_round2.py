"""Regression tests for the second round of review fixes.

Two-column PDF pages, scanned pages inside a digital PDF, orthographic
joiners, the OCR cache cap, and taxonomy placement.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from atom import ocr  # noqa: E402
from atom.master import build_master, load_master  # noqa: E402
from atom.parsers import pdf as pdf_parser  # noqa: E402
from atom.sanitize import sanitize_extracted_text  # noqa: E402


def _two_column_page(rows=12):
    left = [f"Left column sentence number {i:02d} goes on" for i in range(rows)]
    right = [f"Right column sentence number {i:02d} goes on" for i in range(rows)]
    lines = ["                 A Title Across The Page"]
    lines += [f"{lt:<48}      {rt}" for lt, rt in zip(left, right)]
    return "\n".join(lines)


# --- two columns -------------------------------------------------------------

def test_two_column_page_is_rewritten_left_then_right():
    out = pdf_parser.split_columns(_two_column_page())
    assert out is not None
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    assert lines[0] == "A Title Across The Page"
    assert lines[1].startswith("Left column sentence number 00")
    assert lines[12].startswith("Left column sentence number 11")
    assert lines[13].startswith("Right column sentence number 00")
    assert all("Right" not in ln for ln in lines[1:13])


def test_one_column_page_with_a_small_table_is_left_alone():
    prose = [f"An ordinary paragraph line of prose that runs wide, number {i}."
             for i in range(14)]
    table = ["Name        Value", "alpha       1", "beta        2"]
    assert pdf_parser.split_columns("\n".join(prose + table)) is None


def test_detail_rewrites_columns_and_ocrs_blank_pages_in_place(monkeypatch):
    pages = [_two_column_page(), "   \n  12  \n", "A normal page with plenty of text " * 3]
    monkeypatch.setattr(pdf_parser, "pdftotext_pages", lambda *_a, **_k: list(pages))
    text, info = pdf_parser.extract_with_pdftotext_detail(
        "x.pdf", lambda numbers: {n: f"OCR TEXT OF PAGE {n}" for n in numbers})
    assert info == {"column_pages": [1], "ocr_pages": [2], "empty_pages": []}
    assert text.index("Right column sentence number 11") < text.index("OCR TEXT OF PAGE 2")
    assert text.index("OCR TEXT OF PAGE 2") < text.index("A normal page")


def test_detail_reports_blank_pages_when_ocr_is_off(monkeypatch):
    pages = ["Real text on the first page, long enough to count.", " ", "More real text here, also long enough."]
    monkeypatch.setattr(pdf_parser, "pdftotext_pages", lambda *_a, **_k: list(pages))
    _text, info = pdf_parser.extract_with_pdftotext_detail("x.pdf", None)
    assert info["empty_pages"] == [2] and info["ocr_pages"] == []


@pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftotext")),
                    reason="needs tesseract and poppler")
def test_scanned_page_inside_digital_pdf_lands_in_place(tmp_path):
    pytest.importorskip("PIL")
    reportlab = pytest.importorskip("reportlab")  # noqa: F841
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    import io

    def scan(words):
        im = Image.new("L", (1240, 1754), 255)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
        except OSError:
            font = ImageFont.load_default()
        ImageDraw.Draw(im).text((120, 200), words, fill=0, font=font)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return ImageReader(io.BytesIO(buf.getvalue()))

    path = tmp_path / "mixed.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    for page in (1, 2, 3):
        if page == 2:
            c.drawImage(scan("SCANNED MIDDLE PAGE"), 0, 0, *A4)
        else:
            c.setFont("Helvetica", 12)
            for i in range(20):
                c.drawString(72, 760 - 16 * i, f"Digital page {page} line {i} with text.")
        c.showPage()
    c.save()
    work = tmp_path / "work"
    env = dict(os.environ, BOOK_SKILL_WORKDIR=str(work),
               ATOM_OCR_CACHE_DIR=str(tmp_path / "cache"))
    proc = subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "extract.py"),
                           str(path), "--install-missing", "no"],
                          capture_output=True, text=True, env=env, timeout=300)
    assert proc.returncode == 0, proc.stderr[-800:]
    text = (work / "full_text.txt").read_text()
    middle = text.find("SCANNED MIDDLE PAGE")
    assert text.find("Digital page 1") < middle < text.find("Digital page 3")
    assert text.count("SCANNED MIDDLE PAGE") == 1  # not read again as a figure


# --- orthographic joiners ---------------------------------------------------

@pytest.mark.parametrize("word", ["می‌خواهم", "අධ්‍යාය", "क्‍ष"])
def test_single_joiner_inside_a_joining_script_word_is_kept(word):
    assert sanitize_extracted_text(word) == (word, 0)


@pytest.mark.parametrize("text, expected, removed", [
    ("abc‍def", "abcdef", 1),                 # Latin: no orthographic use
    ("می‌‌خواهم", "میخواهم", 2),           # a run is padding
    ("a‌‍IGNORE", "aIGNORE", 2),
])
def test_other_joiners_are_still_stripped(text, expected, removed):
    assert sanitize_extracted_text(text) == (expected, removed)


# --- OCR cache --------------------------------------------------------------

def test_cache_is_pruned_oldest_first(tmp_path):
    for i in range(5):
        f = tmp_path / f"f{i}.txt"
        f.write_bytes(b"x" * 400_000)
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
    removed = ocr.prune_cache(str(tmp_path), max_mb=1.0)
    assert removed == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f3.txt", "f4.txt"]


def test_cache_can_be_disabled_and_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv(ocr.CACHE_ENV_VAR, "off")
    assert ocr.default_cache_dir() == ""
    (tmp_path / "a.txt").write_text("secret page text")
    assert ocr.clear_cache(str(tmp_path)) == (1, 16)
    assert list(tmp_path.iterdir()) == []


# --- taxonomy placement -----------------------------------------------------

def _src(name, chapter, *terms):
    return {"name": name, "kind": "book", "chapters": [{"title": chapter}],
            "glossary": [{"term": t, "definition": f"def {t}", "chapter": chapter,
                          "topic": "Unrelated topic"} for t in terms]}


def test_concepts_are_filed_under_their_source_chapter(tmp_path):
    build_master("Robots", [_src("Book", "Preface", "Autonomy"),
                            _src("Book 2", "Localization", "Kalman filter", "SLAM")], tmp_path)
    tax = {n["id"]: n for n in load_master(tmp_path / "robots")["taxonomy"]}
    assert tax["kalman-filter"]["parents"] == ["area-localization"]
    assert tax["autonomy"]["parents"] == ["area-preface"]


def test_concept_with_no_matching_area_is_unplaced_not_dumped(tmp_path):
    src = {"name": "Book", "kind": "book", "chapters": [{"title": "Preface"}],
           "glossary": [{"term": "Orphan", "definition": "d", "chapter": "Elsewhere"}]}
    build_master("Robots", [src], tmp_path)
    tax = {n["id"]: n for n in load_master(tmp_path / "robots")["taxonomy"]}
    assert tax["orphan"]["parents"] == []
    assert "## Unplaced concepts (1)" in (tmp_path / "robots" / "taxonomy.md").read_text()


def test_wrong_old_placement_is_corrected_but_manual_placement_kept(tmp_path):
    srcs = [_src("A", "Preface", "Autonomy"), _src("B", "Localization", "SLAM", "Kalman filter")]
    build_master("Robots", srcs, tmp_path)
    existing = load_master(tmp_path / "robots")
    for node in existing["taxonomy"]:
        if node["id"] == "slam":
            node["parents"] = ["area-preface"]           # an older build's mistake
        if node["id"] == "kalman-filter":
            node["parents"], node["manual"] = ["area-preface"], True   # a person's choice
    build_master("Robots", srcs, tmp_path, existing=existing)
    tax = {n["id"]: n for n in load_master(tmp_path / "robots")["taxonomy"]}
    assert tax["slam"]["parents"] == ["area-localization"]
    assert tax["kalman-filter"]["parents"] == ["area-preface"]


def test_taxonomy_md_links_notes_and_carries_no_citations(tmp_path):
    build_master("Robots", [_src("Long Book Title With A Citation", "Localization", "SLAM")], tmp_path)
    md = (tmp_path / "robots" / "taxonomy.md").read_text()
    assert "(notes/slam.md)" in md and "Long Book Title" not in md
