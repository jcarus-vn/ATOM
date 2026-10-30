"""Regression tests for the defects found in the 2026-10-30 review.

Each test reproduces the original failure scenario; the B-numbers match
the review's defect table.
"""

import json
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import atom_brain.master as master  # noqa: E402
from atom_brain import ocr, web_input  # noqa: E402
from atom_brain.chapter_detection import detect_chapters  # noqa: E402
from atom_brain.exceptions import ExtractionError  # noqa: E402
from atom_brain.master import (  # noqa: E402
    build_master,
    load_master,
    merge_sources,
    normalize_title,
    parse_skill_dir,
    rank_notes,
    slugify,
    validate_manifest,
)
from atom_brain.parsers.docx import validate_docx_xml_safety  # noqa: E402
from atom_brain.utils import detect_structure  # noqa: E402

BODY = ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 12).strip()


def _book(name, *terms):
    return {"name": name, "kind": "book",
            "glossary": [{"term": t, "definition": d, "chapter": "1"} for t, d in terms]}


# --- B1: contested definitions keep their own sources -----------------------

def test_b1_contested_definition_is_shown_with_its_own_source():
    merged = merge_sources([
        _book("Book A", ("Anchoring", "DEF-X")),
        _book("Book B", ("Anchoring", "DEF-X")),
        _book("Book C", ("Anchoring", "DEF-Z")),
    ])
    note = merged["notes"][0]
    assert note["status"] == "contested"
    lines = note["body"].splitlines()
    assert lines == ["- DEF-X (source: Book A, ch. 1; Book B, ch. 1)",
                     "- DEF-Z (source: Book C, ch. 1)"]
    sides = {d["definition"]: d["source"] for d in merged["conflicts"][0]["definitions"]}
    assert sides["DEF-Z"] == "Book C, ch. 1"


# --- B2: fold-in applies new and corrected definitions -----------------------

def test_b2_fold_in_turns_single_source_into_contested_with_both_sides(tmp_path):
    build_master("Decision", [_book("Book A", ("Anchoring", "First number dominates."))], tmp_path)
    second = build_master("Decision", [_book("Book B", ("Anchoring", "Initial values bias estimates."))],
                          tmp_path, existing=load_master(tmp_path / "decision"))
    body = (tmp_path / "decision" / "notes" / "anchoring.md").read_text()
    assert "First number dominates." in body and "Initial values bias estimates." in body
    assert second["hand_edited"] == []


def test_b2_corrected_definition_from_same_source_is_applied(tmp_path):
    build_master("Decision", [_book("Book A", ("Anchoring", "Old wording."))], tmp_path)
    build_master("Decision", [_book("Book A", ("Anchoring", "CORRECTED wording."))],
                 tmp_path, existing=load_master(tmp_path / "decision"))
    assert "CORRECTED" in (tmp_path / "decision" / "notes" / "anchoring.md").read_text()


def test_b2_hand_edit_survives_and_is_flagged_when_sources_change(tmp_path):
    build_master("Decision", [_book("Book A", ("Anchoring", "Old wording."))], tmp_path)
    path = tmp_path / "decision" / "notes" / "anchoring.md"
    path.write_text(path.read_text().replace("Old wording.", "MY OWN WORDS."))
    summary = build_master("Decision", [_book("Book B", ("Anchoring", "Other wording."))],
                           tmp_path, existing=load_master(tmp_path / "decision"))
    assert "MY OWN WORDS." in path.read_text()
    assert summary["hand_edited_needing_review"] == ["Anchoring"]


def test_b2_legacy_contested_body_is_not_mistaken_for_a_hand_edit():
    note = {"definitions": ["X", "Z"], "sources": ["A", "C"]}
    old = {"body": "- X (source: A)\n- Z (source: C)"}
    assert not master._was_hand_edited(old, note, None)


# --- B3: hand-added notes kept; an interrupted build changes nothing ----------

def test_b3_hand_added_note_survives_fold_in(tmp_path):
    build_master("Decision", [_book("Book A", ("Anchoring", "def"))], tmp_path)
    notes = tmp_path / "decision" / "notes"
    (notes / "my-insight.md").write_text(
        "# My insight\n\n- id: my-insight\n- kind: glossary\n- status: single-source\n"
        "- facets: topic: x; technique: ; level: basic; source_type: book\n"
        "- sources:\n  - my notebook\n\n## Content\n\nWritten by hand.\n")
    summary = build_master("Decision", [_book("Book B", ("Framing", "def"))],
                           tmp_path, existing=load_master(tmp_path / "decision"))
    assert (notes / "my-insight.md").exists()
    assert summary["kept_manual_notes"] == ["My insight"]


def test_b3_stale_builder_note_is_still_dropped(tmp_path):
    build_master("Decision", [_book("Book A", ("Anchoring", "def"))], tmp_path)
    build_master("Decision", [_book("Book A", ("Framing", "def"))],
                 tmp_path, existing=load_master(tmp_path / "decision"))
    names = sorted(p.name for p in (tmp_path / "decision" / "notes").glob("*.md"))
    assert names == ["framing.md"]


def test_b3_interrupted_build_leaves_old_master_intact(tmp_path, monkeypatch):
    build_master("Demo", [_book("Book A", ("Alpha", "a"), ("Beta", "b"), ("Gamma", "g"))], tmp_path)
    existing = load_master(tmp_path / "demo")
    original = master.render_note_md
    calls = {"n": 0}

    def flaky(note):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return original(note)

    monkeypatch.setattr(master, "render_note_md", flaky)
    with pytest.raises(KeyboardInterrupt):
        build_master("Demo", [_book("Book B", ("Delta", "d"))], tmp_path, existing=existing)
    names = sorted(p.name for p in (tmp_path / "demo" / "notes").glob("*.md"))
    assert names == ["alpha.md", "beta.md", "gamma.md"]
    assert not [p for p in tmp_path.iterdir() if ".tmp-" in p.name]


# --- B4: pdf-inspector records carry every key main() reads ------------------

def test_b4_main_survives_pdf_inspector_acceptance(tmp_path, monkeypatch):
    stub = tmp_path / "stub" / "pdf_inspector"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text(
        "from types import SimpleNamespace\n"
        "def process_pdf(path):\n"
        "    return SimpleNamespace(pdf_type='text_based', confidence=0.99,\n"
        "        pages_needing_ocr=[], has_encoding_issues=False,\n"
        "        markdown='# Chapter 1: Intro\\n\\n' + 'Robots estimate state. ' * 80,\n"
        "        ocr_reasons_by_page=[], pages_with_tables=[], pages_with_columns=[],\n"
        "        is_complex_layout=False, page_count=3)\n")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%fake\n")
    work = tmp_path / "work"
    env = {"PYTHONPATH": str(stub.parent), "BOOK_SKILL_WORKDIR": str(work),
           "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "extract.py"),
                           str(pdf), "--install-missing", "no"],
                          capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-800:]
    meta = json.loads((work / "metadata.json").read_text())
    assert meta["extraction_method"] == "pdf-inspector"
    assert meta["sources"][0]["fallback_reason"] is None


# --- B5: web main content ----------------------------------------------------

ARTICLE = """<html><head><title>KF</title></head><body><nav>Home</nav>
<article><div class="entry-header"><h1>The Kalman filter</h1></div>
<p class="lead">The Kalman filter is an optimal recursive estimator.</p>
<img class="lead-image" src="kf.png">
<p>It alternates predict and correct steps using the Kalman gain.</p>
<div class="ad-slot">Buy now</div><div class="comments">Nice post</div>
<p>The extended variant linearizes nonlinear models.</p></article>
<footer>Copyright</footer></body></html>"""


def test_b5_header_lead_and_void_tags_do_not_hide_the_article():
    text = web_input.strip_boilerplate(ARTICLE)
    for kept in ("The Kalman filter", "optimal recursive estimator",
                 "predict and correct", "extended variant"):
        assert kept in text
    for dropped in ("Buy now", "Nice post", "Home", "Copyright"):
        assert dropped not in text


class _Resp:
    def __init__(self, body, ctype):
        self.body, self.ctype = body, ctype
        self.headers = self

    def get_content_type(self):
        return self.ctype

    def get_content_charset(self):
        return "utf-8"

    def read(self, amt=None):
        return self.body if amt is None else self.body[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_b5_pdf_url_is_handed_to_the_pdf_extractor(monkeypatch):
    monkeypatch.setattr(web_input.urllib.request, "urlopen",
                        lambda req, timeout=30: _Resp(b"%PDF-1.4 data", "application/octet-stream"))
    source = web_input.fetch_web_source("https://example.test/paper.pdf")
    path = Path(source["document_path"])
    try:
        assert source["format"] == "pdf" and path.read_bytes().startswith(b"%PDF")
    finally:
        path.unlink()


def test_b5_oversized_download_is_refused(monkeypatch):
    monkeypatch.setattr(web_input, "MAX_BYTES", 10)
    monkeypatch.setattr(web_input.urllib.request, "urlopen",
                        lambda req, timeout=30: _Resp(b"<html>" + b"x" * 100, "text/html"))
    with pytest.raises(web_input.WebFetchError):
        web_input.fetch_web_source("https://example.test/huge")


# --- B6: concept identity across symbols and scripts --------------------------

@pytest.mark.parametrize("a, b", [("C++", "C#"), ("RRT*", "RRT"), ("A*", "A"),
                                  ("Đường", "Uống"), ("Фильтр Калмана", "Граф поз"),
                                  ("卡尔曼滤波", "粒子滤波")])
def test_b6_distinct_titles_stay_distinct(a, b):
    assert normalize_title(a) != normalize_title(b)
    assert slugify(a) != slugify(b)


def test_b6_ascii_titles_normalize_as_before():
    assert normalize_title("Extended Kalman Filter (EKF)") == "extended kalman filter ekf"
    assert slugify("Extended Kalman Filter (EKF)") == "extended-kalman-filter-ekf"
    assert slugify("Định vị toàn cục") == "dinh-vi-toan-cuc"


def test_b6_non_latin_terms_get_their_own_notes():
    merged = merge_sources([_book("RU", ("Фильтр Калмана", "a"), ("Граф поз", "b"),
                                  ("Частичный фильтр", "c"))])
    assert len(merged["notes"]) == 3
    assert len({n["id"] for n in merged["notes"]}) == 3


def test_b6_vietnamese_question_ranks_vietnamese_note():
    notes = merge_sources([_book("VN", ("Bộ lọc Kalman", "Bộ ước lượng đệ quy tối ưu."),
                                 ("Lập kế hoạch chuyển động", "Tìm đường đi không va chạm."))])["notes"]
    for n in notes:
        n["links"] = []
    ranked = rank_notes(notes, [], [], "Bộ lọc Kalman là gì?", k=1)
    assert ranked[0]["note"]["title"] == "Bộ lọc Kalman" and ranked[0]["score"] > 0


# --- B7: chapter detection ----------------------------------------------------

def test_b7_vietnamese_prose_after_chapter_number_is_not_a_heading():
    text = "\n\n".join(["Chương 1. Tổng quan", BODY,
                        "Chương 2 đã trình bày các khái niệm cơ bản.", BODY])
    assert [c["number"] for c in detect_chapters(text)["chapters"]
            if c["strategy"] == "explicit"] == [1]
    assert detect_structure(text)["chapters_detected"] < 2


def test_b7_three_digit_chapter_numbers():
    text = "\n\n".join(f"Chapter {n}: Topic {n}\n\n{BODY}" for n in (99, 100, 101))
    assert detect_structure(text)["chapters_detected"] == 3


def test_b7_merged_chapters_are_in_document_order():
    text = "\n\n".join(["Chapter 1: Loomings", BODY, "THE CARPET-BAG", BODY,
                        "Chapter 2: The Spouter-Inn", BODY])
    titles = [c["title"] for c in detect_chapters(text)["chapters"]]
    assert titles == ["chapter 1: loomings", "the carpet-bag", "chapter 2: the spouter-inn"]


# --- B9: decorative figures are not OCRed --------------------------------------

def _png(w, h, shade=0):
    raw = b"".join(b"\x00" + bytes([shade]) * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_b9_logo_repeated_on_every_page_and_tiny_icons_are_skipped():
    logo, figure, icon = _png(120, 40, 10), _png(400, 300, 20), _png(16, 16, 30)
    images = [{"page": p, "index": 1, "data": logo} for p in range(1, 21)]
    images += [{"page": 5, "index": 2, "data": figure},
               {"page": 9, "index": 2, "data": figure},
               {"page": 7, "index": 3, "data": icon}]
    kept, counts = ocr.select_figures(images)
    assert [(k["page"], k["width"]) for k in kept] == [(5, 400)]
    assert counts == {"figures_repeated_skipped": 20, "figures_small_skipped": 1,
                      "figures_duplicate_skipped": 1}


# --- B10: decompression bombs ---------------------------------------------------

def test_b10_highly_compressed_docx_is_refused_before_parsing(tmp_path, monkeypatch):
    from atom_brain.parsers import zip_limits
    monkeypatch.setattr(zip_limits, "MAX_MEMBER_UNCOMPRESSED", 1024 * 1024)
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", "<w:document>" + " " * (2 * 1024 * 1024) + "</w:document>")
    with pytest.raises(ExtractionError, match="limit"):
        validate_docx_xml_safety(str(path))


# --- B11: metadata.json stays small -------------------------------------------

def test_b11_full_chapter_list_goes_to_chapters_json(tmp_path):
    book = tmp_path / "book.md"
    book.write_text("".join(f"Chapter {c}: Topic {c}\n\n" + "".join(
        f"## {c}.{s} Section {s}\n\n{BODY}\n\n" for s in range(1, 6)) for c in range(1, 31)))
    work = tmp_path / "work"
    proc = subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "extract.py"),
                           str(book), "--install-missing", "no"], capture_output=True, text=True,
                          env={"BOOK_SKILL_WORKDIR": str(work), "PATH": "/usr/bin:/bin",
                               "HOME": str(tmp_path)}, timeout=120)
    assert proc.returncode == 0, proc.stderr[-800:]
    meta = json.loads((work / "metadata.json").read_text())
    full = json.loads((work / "chapters.json").read_text())
    assert len(meta["chapter_details"]) <= 60 < meta["chapter_details_total"]
    assert len(full["overall"]) == meta["chapter_details_total"]
    assert "chapter_details" not in meta["sources"][0]
    assert meta["chapters_detected"] == 30


# --- B12 / B13: query threshold, CLI flags, manifest checks ---------------------

def test_b12_off_topic_question_says_the_master_has_no_answer(tmp_path):
    build_master("Robotics", [_book("PoRA", ("Kalman filter", "Optimal recursive estimator."))], tmp_path)
    proc = subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "build_master.py"),
                           "--masters-root", str(tmp_path), "--question",
                           "How do I bake sourdough bread?"], capture_output=True, text=True)
    assert "does not hold an answer" in proc.stdout


def test_b13_install_missing_does_not_swallow_the_input_path(tmp_path):
    from atom_brain.utils import parse_arguments
    paths, _mode, install = parse_arguments(["x", "--install-missing", "book.md"])
    assert paths == ["book.md"] and install == "yes"
    paths, _mode, install = parse_arguments(["x", "--install-missing", "no", "book.md"])
    assert paths == ["book.md"] and install == "no"


def test_b13_manifest_field_is_used_and_bad_keys_are_reported(tmp_path):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"field": "Demo", "sources": [_book("A", ("Term", "def"))]}))
    proc = subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "build_master.py"),
                           "--masters-root", str(tmp_path / "root"), "--skills-json", str(manifest)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    problems = validate_manifest({"sources": [{"name": "X", "kind": "book",
                                               "glossary": [{"name": "T", "def": "d"}]}]})
    assert any("missing" in p for p in problems) and any("unknown" in p for p in problems)


# --- B14: bs4 path keeps sentences whole ----------------------------------------

def test_b14_inline_tags_do_not_split_sentences():
    bs4 = pytest.importorskip("bs4")
    from atom_brain.parsers.html import soup_text
    soup = bs4.BeautifulSoup("<h2>Chapter 1: Intro</h2><p>The <em>Kalman filter</em> "
                             "is <b>optimal</b>.</p>", "html.parser")
    assert soup_text(soup) == "Chapter 1: Intro\nThe Kalman filter is optimal."


# --- Generator <-> builder contract and master skill validity -----------------

def test_parse_skill_dir_reads_the_glossary_format_skill_md_prescribes(tmp_path):
    skill = tmp_path / "demo"
    skill.mkdir()
    (skill / "glossary.md").write_text(
        "# Glossary\n\n**Anchoring** — People rely on the first number (Ch 1)\n")
    (skill / "patterns.md").write_text("# Patterns\n\n## Pre-mortem\n**How**: imagine failure\n")
    source = parse_skill_dir(skill)
    assert source["glossary"] == [{"term": "Anchoring",
                                   "definition": "People rely on the first number",
                                   "chapter": "1"}]
    assert source["patterns"][0]["description"] == "**How**: imagine failure"


def test_master_skill_md_is_a_valid_skill(tmp_path):
    build_master("Pháp luật", [_book("A", ("Term", "def"))], tmp_path)
    skill_md = tmp_path / "phap-luat" / "SKILL.md"
    proc = subprocess.run([sys.executable, str(ROOT_DIR / "tools" / "validate_skill.py"),
                           str(skill_md)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_hand_edited_facet_and_hand_added_link_survive_fold_in(tmp_path):
    src = {"name": "Book A", "kind": "book",
           "glossary": [{"term": "Bayes filter", "definition": "Recursive belief update.", "topic": "Filtering"},
                        {"term": "Predict-correct recursion", "definition": "Two-step loop.", "topic": "Filtering"}]}
    build_master("Demo", [src], tmp_path)
    notes = tmp_path / "demo" / "notes"
    p = notes / "predict-correct-recursion.md"
    p.write_text(p.read_text().replace("topic: Filtering", "topic: Estimation")
                 + "\n## Links\n\n- used_in -> bayes-filter (my reading)\n")
    summary = build_master("Demo", [src], tmp_path, existing=load_master(tmp_path / "demo"))
    text = p.read_text()
    assert "topic: Estimation" in text
    assert "used_in -> bayes-filter (my reading)" in text
    graph = json.loads((tmp_path / "demo" / "graph.json").read_text())
    assert any(e["from"] == "predict-correct-recursion" and e["to"] == "bayes-filter"
               and e["type"] == "used_in" for e in graph["edges"])
    assert summary["hand_edited"] == ["Predict-correct recursion"]
