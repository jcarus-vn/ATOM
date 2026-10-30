from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections import Counter

# A bare page number sitting alone on a line: Arabic, or a Roman numeral of the
# kind used to number front matter.
#
# The Roman branch spells out the SHAPE of a canonical numeral instead of
# listing the letters one may contain. `[ivxlcdm]{1,7}` matched any short word
# built from those letters, so "MIX", "CIVIL", "DIM", "MILD" and "VIVID" were
# all silently deleted whenever they landed on a page's first or last non-blank
# line — a one-word line is exactly what a part title or a display heading looks
# like. Deleting real text is a worse failure than leaving a stray numeral, so
# the pattern is now exact.
#
# The range is 1-99, which is what front matter uses; "c"/"d"/"m" therefore no
# longer match on their own, so a lone "C" or "M" line is now kept as text.
# `(?=[ivxl])` is the non-empty guard: both groups are individually optional, so
# without it the pattern would match a blank line.
_ROMAN_1_99 = r"(?=[ivxl])(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
_PDF_PAGE_NUM = re.compile(rf"^\s*(?:\d{{1,4}}|{_ROMAN_1_99})\s*$", re.IGNORECASE)
_PDF_HYPHEN_WRAP = re.compile(r"(\w)-\n(\w)")


def clean_pdftotext(text: str) -> str:
    """Clean pdftotext '-layout' output (pages are form-feed delimited): drop
    repeated running headers/footers and edge page numbers, and join words split
    across a line by a hyphen."""
    pages = text.split("\f")
    if len(pages) >= 3:
        # A top/bottom line repeated on > half the pages is boilerplate.
        edge = Counter()
        for p in pages:
            nb = [ln.strip() for ln in p.splitlines() if ln.strip()]
            if nb:
                edge[nb[0]] += 1
                # On a single-line page the first and last line are the same
                # line. Counting it twice would let one page cast two votes
                # toward the "more than half the pages" threshold below, so a
                # part-divider page occurring twice in four pages would reach 4
                # votes instead of 2 and be stripped as boilerplate.
                if len(nb) > 1:
                    edge[nb[-1]] += 1
        boiler = {ln for ln, c in edge.items() if c > len(pages) / 2}
        kept = []
        for p in pages:
            lines = p.splitlines()
            nb_idx = [i for i, ln in enumerate(lines) if ln.strip()]
            first = nb_idx[0] if nb_idx else None
            last = nb_idx[-1] if nb_idx else None
            for i, ln in enumerate(lines):
                # Running headers/footers and page numbers only ever occur at a
                # page edge -- which is also the only place `boiler` is
                # collected from. Removing a boilerplate string from every line
                # meant that when a running header repeated the section title
                # (common typesetting), the genuine mid-page heading was deleted
                # along with the headers.
                if i in (first, last):
                    s = ln.strip()
                    if s in boiler or _PDF_PAGE_NUM.match(s):
                        continue
                kept.append(ln)
        text = "\n".join(kept)
    else:
        text = text.replace("\f", "\n")
    # ponytail: naive dehyphenation; may join a genuinely-hyphenated wrapped
    # compound ("well-\nknown" -> "wellknown"). Dictionary-aware split if it bites.
    return _PDF_HYPHEN_WRAP.sub(r"\1\2", text)


def pdftotext_pages(pdf_path: str, layout: bool = True) -> list[str] | None:
    """Raw pdftotext output split into pages (form feeds), or None."""
    if not shutil.which("pdftotext"):
        return None
    args = ["pdftotext"] + (["-layout"] if layout else []) + [
        "-enc", "UTF-8", os.path.abspath(pdf_path), "-"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120,
                                encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  [warn] extract_with_pdftotext failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    pages = result.stdout.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()  # pdftotext ends every page, the last one included, with \f
    return pages


_RIGHT_START = re.compile(r" {3,}(?=\S)")


def _right_column_start(lines: list[str]) -> int | None:
    """Character position where a second column starts on this page, or None.

    In ``-layout`` output the right column begins at (almost) the same
    position on most lines, after a run of spaces. Positions in the middle
    band of the page are counted in buckets of 3 characters; a bucket hit
    by at least 40% of the text lines (and 6 of them) is a column edge. A
    table inside a one-column page touches too few lines to qualify.
    """
    text_lines = [ln for ln in lines if ln.strip()]
    if len(text_lines) < 8:
        return None
    width = max(len(ln.rstrip()) for ln in text_lines)
    lo, hi = int(width * 0.25), int(width * 0.75)
    buckets: dict[int, list[int]] = {}
    for ln in text_lines:
        for m in _RIGHT_START.finditer(ln):
            x = m.end()
            if lo <= x <= hi and len(ln) - x >= 8:
                buckets.setdefault(x // 3, []).append(x)
                break
    if not buckets:
        return None
    key = max(buckets, key=lambda k: len(buckets[k]) + len(buckets.get(k - 1, []))
              + len(buckets.get(k + 1, [])))
    hits = buckets[key] + buckets.get(key - 1, []) + buckets.get(key + 1, [])
    if len(hits) < max(6, 0.4 * len(text_lines)):
        return None
    return min(hits)


def split_columns(page_text: str) -> str | None:
    """Rewrite a two-column ``-layout`` page as left column, then right.

    Returns None for a one-column page. A line that runs across the column
    edge (a title or a full-width figure caption) stays whole in place.
    """
    lines = page_text.splitlines()
    pos = _right_column_start(lines)
    if pos is None:
        return None
    left: list[str] = []
    right: list[str] = []
    for ln in lines:
        cut = None
        for k in range(max(1, pos - 2), min(len(ln), pos + 3)):
            if ln[k - 1] == " " and ln[k] != " " and not ln[max(0, k - 2):k].strip():
                cut = k
                break
        if cut is None and len(ln) > pos and ln[pos - 1] != " ":
            left.append(ln.rstrip())          # spans both columns
        elif cut is None:
            left.append(ln.rstrip())          # left column only (or blank)
        else:
            left.append(ln[:cut].rstrip())
            right.append(ln[cut:].strip())
    return "\n".join(left).rstrip() + "\n\n" + "\n".join(r for r in right)


def looks_multicolumn(page_text: str) -> bool:
    """True when a ``-layout`` page is set in two columns side by side."""
    return _right_column_start(page_text.splitlines()) is not None


def _page_has_text(page_text: str, min_chars: int = 30) -> bool:
    return sum(1 for ch in page_text if ch.isalnum()) >= min_chars


def extract_with_pdftotext_detail(pdf_path: str, ocr_pages_fn=None):
    """pdftotext extraction, page by page. Returns (text, info) or (None, info).

    - Pages laid out in two columns are rewritten column by column (left
      column whole, then the right one), so sentences are not interleaved.
    - Pages with no text layer (a scanned page inside a digital PDF) are
      sent to *ocr_pages_fn* (page numbers -> {page: text}) when given, and
      their text goes back in place, not at the end of the document.
    info: ``column_pages`` and ``ocr_pages`` (1-based page numbers) and
    ``empty_pages`` (no text and not OCRed).
    """
    info = {"column_pages": [], "ocr_pages": [], "empty_pages": []}
    pages = pdftotext_pages(pdf_path, layout=True)
    if pages is None:
        return None, info
    for i, page in enumerate(pages):
        columns = split_columns(page)
        if columns is not None:
            pages[i] = columns
            info["column_pages"].append(i + 1)
    blank = [i + 1 for i, page in enumerate(pages) if not _page_has_text(page)]
    if blank and ocr_pages_fn is not None and len(blank) < len(pages):
        recovered = ocr_pages_fn(blank) or {}
        for number, text in recovered.items():
            if text and text.strip():
                pages[number - 1] = text
                info["ocr_pages"].append(number)
    info["empty_pages"] = [n for n in blank if n not in info["ocr_pages"]]
    joined = "\f".join(pages)
    if not joined.strip():
        return None, info
    return clean_pdftotext(joined), info


def extract_with_pdftotext(pdf_path: str) -> str | None:
    text, _info = extract_with_pdftotext_detail(pdf_path)
    return text


def looks_image_only(pdf_path: str, pages: int = 5) -> bool:
    """True when the first `pages` pages yield no extractable text — the signature
    of a scanned/image-only PDF. Cheap pre-flight so a scan fails in a second
    instead of after the whole extraction chain has run. Best-effort: without
    pdftotext it reports False and the normal chain (plus the final empty-text
    guard) still applies."""
    if not shutil.which("pdftotext"):
        return False
    try:
        result = subprocess.run(
            ["pdftotext", "-f", "1", "-l", str(pages), "-enc", "UTF-8", os.path.abspath(pdf_path), "-"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        return result.returncode == 0 and not result.stdout.strip()
    except Exception:
        return False


def extract_with_pypdf(pdf_path: str) -> str | None:
    try:
        import pypdf
        text_parts = []
        with open(pdf_path, "rb") as f:
            reader = pypdf.PdfReader(f)
            for page in reader.pages:
                try:
                    text_parts.append(page.extract_text() or "")
                except Exception:
                    text_parts.append("")
        # Join pages with a form feed so clean_pdftotext can strip repeated
        # per-page headers/footers, not just dehyphenate.
        return clean_pdftotext("\f".join(text_parts))
    except ImportError:
        return None
    except Exception as e:
        print(f"  [warn] extract_with_pypdf failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def extract_with_pdfminer(pdf_path: str) -> str | None:
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(pdf_path)  # already form-feed delimited per page
        return clean_pdftotext(text) if text else text
    except ImportError:
        return None
    except Exception as e:
        print(f"  [warn] extract_with_pdfminer failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def extract_with_docling(pdf_path: str) -> str | None:
    """Layout-aware extraction using Docling. Best for technical books with tables and code."""
    try:
        from docling.document_converter import DocumentConverter
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import PdfFormatOption
    except ImportError:
        return None

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = False
    pipeline_options.do_table_structure = True

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )
    result = converter.convert(pdf_path)
    return result.document.export_to_markdown()


def count_pages(pdf_path: str) -> int:
    """Return the number of pages in a PDF using progressively safer fallbacks."""
    # Try pdfinfo first.
    if shutil.which("pdfinfo"):
        try:
            pdf_path = os.path.abspath(pdf_path)
            result = subprocess.run(
                ["pdfinfo", pdf_path],
                capture_output=True,
                text=True,
                timeout=15,
            )
            for line in result.stdout.splitlines():
                if line.startswith("Pages:"):
                    return int(line.split(":")[1].strip())
        except Exception:
            pass

    # Fallback: count pages with pypdf.
    try:
        import pypdf

        with open(pdf_path, "rb") as f:
            return len(pypdf.PdfReader(f).pages)
    except Exception:
        pass

    # Final fallback: pdfminer.extract_text() preserves page boundaries
    # using form-feed characters, so the number of pages can be derived
    # without requiring a separate PDF page-counting dependency.
    try:
        from pdfminer.high_level import extract_text

        text = extract_text(pdf_path)
        if text:
            return text.count("\f") + (0 if text.endswith("\f") else 1)
    except Exception:
        # All page-counting methods are best-effort; preserve the historical 0 result.
        pass

    return 0
