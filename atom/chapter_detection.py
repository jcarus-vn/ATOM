"""Flexible chapter detection.

The historical detector in ``atom.utils`` only counts explicit
"Chapter N" style headings (plus a structural Markdown fallback). Books that
organize chapters as bare titles (Moby-Dick: "Loomings", "The Carpet-Bag"),
Roman numerals without the word "Chapter", or a table of contents with page
numbers were under-counted or missed entirely.

This module layers several strategies, each with a confidence score, and
merges their results:

1. ``explicit`` - "Chapter N", "Chuong N", "Phan N", "Bai N", "Hoi N",
   "Part N", "Unit N", "Lesson N" with Arabic or Roman numerals, plus
   standalone Roman headings ("I: Loomings").
2. ``toc`` - parse the table of contents (title + page number), then anchor
   each entry at its first matching heading-like line in the body.
3. ``heuristic`` - short headings that look structural: ALL-CAPS lines,
   bare title lines surrounded by blank lines, ATX headings, and numbered
   headings ("1. Introduction") whose section carries real body text.
4. ``fallback`` - when nothing above yields at least two chapters, split on
   section titles (ATX / numbered headings) instead of returning one block,
   and say so in the metadata.

Every returned chapter records which strategies found it and a confidence
value, so a consumer can tell a TOC-anchored chapter from a guessed one.
The module is dependency-free and does not import ``atom.utils``
(the reverse import would be circular); ``utils.detect_structure`` calls in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Chapter:
    """One detected chapter boundary."""

    title: str
    number: int | None = None
    page: int | None = None
    line: int = 0
    strategy: str = "heuristic"
    confidence: float = 0.5
    strategies: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "number": self.number,
            "page": self.page,
            "line": self.line,
            "strategy": self.strategy,
            "confidence": self.confidence,
            "strategies": list(self.strategies) or [self.strategy],
        }


_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
_ROMAN_TABLE = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
                (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
                (5, "V"), (4, "IV"), (1, "I")]


def roman_to_int(text: str) -> int | None:
    """Convert a canonical Roman numeral to int, else None."""
    s = text.upper()
    if not s or any(ch not in _ROMAN_VALUES for ch in s):
        return None
    total = prev = 0
    for ch in reversed(s):
        value = _ROMAN_VALUES[ch]
        total += -value if value < prev else value
        prev = max(prev, value)
    if total == 0 or total > 200:
        return None
    out = []
    n = total
    for val, sym in _ROMAN_TABLE:
        while n >= val:
            out.append(sym)
            n -= val
    return total if "".join(out) == s else None


# Explicit chapter words: English + Vietnamese (chuong/phan/bai/hoi) + the
# generic part/unit/lesson family. The tail rule mirrors utils: a heading's
# number is followed by end-of-line, punctuation, or a capitalized title word;
# a lowercase continuation ("Chapter 6 explores...") is prose, not a heading.
_EXPLICIT_WORDS = (
    r"chapter|unit|lesson|module|lecture|part|"
    r"ch\u01b0\u01a1ng|chuong|ph\u1ea7n|phan|b\u00e0i|bai|h\u1ed3i|hoi"
)
_EXPLICIT = re.compile(
    r"^\s*(?:#+\s+)?(?:"
    + _EXPLICIT_WORDS
    + r")\s*(?:(\d{1,2})|([IVXLCDMivxlcdm]{1,7}))\b(.*)$",
    re.IGNORECASE,
)
_HEADING_TAIL = re.compile(r"^\s*$|^\s*[.:\-]|^\s+(?![a-z])")
_ROMAN_HEAD = re.compile(r"^\s*(?:#+\s+)?([IVXLCDM]+)\s*[:.]\s+([A-Z][^\n]{0,70})$")
_PLAIN_NUMBERED = re.compile(r"^\s*(\d{1,2})\s{2,}(\S.*)$")
_DOTTED_NUMBERED = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})?)[.)]\s+(\S.*)$")
_ATX = re.compile(r"^(#{1,6}|={1,6})\s+(.+?)\s*#*$")

_TOC_HEADERS = (
    "table of contents", "contents", "m\u1ee5c l\u1ee5c", "muc luc",
    "indice", "sumario", "inhaltsverzeichnis", "sommario",
)
_TOC_HEADER_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:" + "|".join(re.escape(h) for h in _TOC_HEADERS) + r")\s*$",
    re.IGNORECASE,
)
# ToC entry: title, then dots/spaces, then a page number at end of line.
_TOC_ENTRY = re.compile(r"^(.{2,80}?)(?:\.{2,}|\s{2,}|\s+\|\s+)\s*(\d{1,4})\s*$")
_NON_CHAPTER_TITLES = frozenset({
    "preface", "foreword", "acknowledgements", "acknowledgments",
    "dedication", "copyright", "epigraph", "prologue", "bibliography",
    "references", "works cited", "further reading", "index", "glossary",
    "notes", "endnotes", "footnotes", "colophon", "about the author",
    "about the authors", "table of contents", "contents",
    "m\u1ee5c l\u1ee5c",
})
_MIN_BODY_CHARS = 200
_MIN_BARE_BODY_CHARS = 300


def _norm(title: str) -> str:
    s = title.strip()
    # The heading-marker strip only applies when the line starts with a
    # marker character; most body lines do not, so skip the regex there.
    # " ".join(split()) collapses whitespace runs exactly like the old
    # re.sub(r"\s+", " ", ...) after the strip, but runs in C.
    if s[:1] in "#=":
        s = re.sub(r"^#{1,6}\s+|^={1,6}\s+", "", s)
    return " ".join(s.split()).lower().strip(" .:-\u2013\u2014")


def _pages(text: str) -> list[tuple[int, list[str]]]:
    """Split text into (page_number, lines) pairs; form feed = page break."""
    pages = []
    for i, chunk in enumerate(text.split("\f"), 1):
        pages.append((i, chunk.splitlines()))
    return pages


def _flatten(text: str) -> list[tuple[int, int, str]]:
    """Return [(page, line_index_within_page, line)] for the whole text."""
    rows = []
    for page, lines in _pages(text):
        for idx, line in enumerate(lines):
            rows.append((page, idx, line))
    return rows


def _explicit_chapters(rows: list[tuple[int, int, str]]) -> list[Chapter]:
    out = []
    for page, _idx, line in rows:
        s = line.strip()
        if len(s) > 80:
            continue
        m = _EXPLICIT.match(s)
        if m:
            rest = m.group(3) or ""
            if not _HEADING_TAIL.match(rest):
                continue
            if m.group(1):
                number = int(m.group(1))
            else:
                number = roman_to_int(m.group(2))
                if number is None:
                    continue
            title = _norm(s)
            if not title:
                continue
            out.append(Chapter(title=title, number=number, page=page,
                               line=len(out), strategy="explicit",
                               confidence=0.95, strategies=("explicit",)))
            continue
        rm = _ROMAN_HEAD.match(s)
        if rm:
            number = roman_to_int(rm.group(1))
            if number is None:
                continue
            out.append(Chapter(title=_norm(s), number=number, page=page,
                               line=len(out), strategy="explicit",
                               confidence=0.9, strategies=("explicit",)))
    return out


def _toc_chapters(rows: list[tuple[int, int, str]]) -> list[Chapter]:
    """Parse the table of contents, then anchor entries in the body."""
    # Locate the ToC header within the first 300 lines.
    toc_at = None
    for i, (_page, _idx, line) in enumerate(rows[:300]):
        if _TOC_HEADER_RE.match(line.strip()):
            toc_at = i
            break
    if toc_at is None:
        return []
    entries: list[tuple[str, int]] = []
    for _page, _idx, line in rows[toc_at + 1:toc_at + 400]:
        s = line.strip()
        if not s:
            continue
        # A ToC region ends at the first long prose paragraph.
        if len(s) > 90 and not _TOC_ENTRY.match(s):
            break
        m = _TOC_ENTRY.match(s)
        if not m:
            continue
        title = _norm(m.group(1))
        if len(title) < 2 or title in _NON_CHAPTER_TITLES:
            continue
        entries.append((title, int(m.group(2))))
    if len(entries) < 2:
        return []
    # Anchor each entry at its first heading-like body line after the ToC.
    # Index the body once (normalized line -> first row) instead of
    # re-scanning the whole body and re-normalizing every line per entry,
    # which was quadratic in (entries x body lines).
    body = rows[toc_at + 1:]
    first_body_row: dict[str, tuple[int, int, str]] = {}
    for row in body:
        first_body_row.setdefault(_norm(row[2]), row)
    out = []
    for title, toc_page in entries:
        row = first_body_row.get(title)
        if row is not None:
            page = row[0]
            out.append(Chapter(title=title, number=None, page=page,
                               line=len(out), strategy="toc",
                               confidence=0.8, strategies=("toc",)))
        else:
            # Entry never reappears as a heading: keep it, but say so with a
            # lower confidence and the ToC page number, not a body page.
            out.append(Chapter(title=title, number=None, page=toc_page,
                               line=len(out), strategy="toc",
                               confidence=0.45, strategies=("toc",)))
    return out


def _body_len(rows: list[tuple[int, int, str]], start: int, stop: int) -> int:
    return sum(len(rows[i][2]) for i in range(start, stop))


def _heuristic_chapters(rows: list[tuple[int, int, str]]) -> list[Chapter]:
    """Heading-shaped lines: ALL-CAPS, bare titles, ATX, numbered."""
    n = len(rows)
    candidates: list[tuple[int, Chapter]] = []
    for i, (page, _idx, line) in enumerate(rows):
        s = line.strip()
        if not s or len(s) > 80:
            continue
        prev_blank = i == 0 or not rows[i - 1][2].strip()
        next_blank = i + 1 >= n or not rows[i + 1][2].strip()
        norm = _norm(s)
        if not norm or norm in _NON_CHAPTER_TITLES:
            continue
        # ATX headings. Digit-led ATX titles ("## 5 Setup") are held to the
        # same body-text gate as numbered headings: the legacy detector
        # deliberately does not count them, and a bare digit heading with
        # no body is a label, not a chapter.
        atx = _ATX.match(s)
        if atx:
            title = _norm(atx.group(2))
            if title and title not in _NON_CHAPTER_TITLES:
                if title[0].isdigit():
                    stop = min(n, i + 60)
                    nxt = next((j for j in range(i + 1, stop)
                                if rows[j][2].strip() and (
                                    _ATX.match(rows[j][2].strip())
                                    or _DOTTED_NUMBERED.match(rows[j][2].strip()))), stop)
                    if _body_len(rows, i + 1, nxt) < _MIN_BODY_CHARS:
                        continue
                candidates.append((i, Chapter(title=title, page=page,
                                              strategy="heuristic",
                                              confidence=0.75,
                                              strategies=("heuristic",))))
            continue
        # Numbered headings ("1. Introduction", "1.1 Setup"): need body text
        # so a numbered list item is not mistaken for a chapter.
        dm = _DOTTED_NUMBERED.match(s)
        if dm and dm.group(2)[0].isupper():
            stop = min(n, i + 60)
            nxt = next((j for j in range(i + 1, stop)
                        if rows[j][2].strip() and (
                            _ATX.match(rows[j][2].strip())
                            or _DOTTED_NUMBERED.match(rows[j][2].strip()))), stop)
            if _body_len(rows, i + 1, nxt) >= _MIN_BODY_CHARS:
                candidates.append((i, Chapter(title=norm, page=page,
                                              number=int(dm.group(1).split(".")[0])
                                              if dm.group(1).split(".")[0].isdigit() else None,
                                              strategy="heuristic",
                                              confidence=0.7,
                                              strategies=("heuristic",))))
            continue
        # ALL-CAPS short line, e.g. "LOOMINGS" in a typeset edition.
        cased = [ch for ch in s if ch.isalpha()]
        if cased and all(ch.isupper() for ch in cased) and len(s) <= 60:
            if prev_blank and _body_len(rows, i + 1, min(n, i + 60)) >= _MIN_BODY_CHARS:
                candidates.append((i, Chapter(title=norm, page=page,
                                              strategy="heuristic",
                                              confidence=0.7,
                                              strategies=("heuristic",))))
            continue
        # Bare title: short, Title Case, blank lines on both sides, no
        # terminal punctuation, and a real body paragraph after it. This is
        # the Moby-Dick shape: chapters named only by their title. Words
        # after the first must start uppercase (small connectors like "of",
        # "and", "the" may stay lowercase), so a wrapped prose sentence
        # ("The cat sat on the mat") is not mistaken for a title, while
        # "The Carpet-Bag" and "War and Peace" still count.
        words = norm.split()
        orig_words = s.split()
        _connectors = {"of", "the", "and", "a", "an", "in", "on", "for", "to"}
        title_case = (
            orig_words
            and orig_words[0][:1].isupper()
            and all(
                w[:1].isupper() or w.lower() in _connectors for w in orig_words[1:]
            )
        )
        if (
            prev_blank and next_blank and title_case
            and len(s) <= 50 and 1 <= len(words) <= 6
            and not s.endswith((".", "!", "?", ";", ":", ","))
            and _body_len(rows, i + 1, min(n, i + 80)) >= _MIN_BARE_BODY_CHARS
        ):
            candidates.append((i, Chapter(title=norm, page=page,
                                          strategy="heuristic",
                                          confidence=0.55,
                                          strategies=("heuristic",))))
    # Keep candidates in document order; drop near-duplicates of the same
    # title a few lines apart (a title repeated as a running head).
    out: list[Chapter] = []
    seen: dict[str, int] = {}
    for i, ch in candidates:
        last = seen.get(ch.title)
        if last is not None and i - last <= 5:
            continue
        seen[ch.title] = i
        ch.line = len(out)
        out.append(ch)
    return out


def _fallback_sections(rows: list[tuple[int, int, str]]) -> list[Chapter]:
    """Section titles as a last resort, so the result is never one block.

    Numbered lines still need body text: a numbered list ("1. Compared to
    characters...") is not a section, and the fallback must not resurrect
    the false positives the main strategies rejected.
    """
    out: list[Chapter] = []
    seen: set[str] = set()
    n = len(rows)
    for i, (page, _idx, line) in enumerate(rows):
        s = line.strip()
        atx = _ATX.match(s)
        title = _norm(atx.group(2)) if atx else None
        if title is not None and title[:1].isdigit():
            # Digit-led ATX titles need body text too, or "## 5 Setup"
            # followed by "## 6 Teardown" would count as two chapters.
            stop = min(n, i + 60)
            nxt = next((j for j in range(i + 1, stop)
                        if rows[j][2].strip() and (
                            _ATX.match(rows[j][2].strip())
                            or _DOTTED_NUMBERED.match(rows[j][2].strip()))), stop)
            if _body_len(rows, i + 1, nxt) < _MIN_BODY_CHARS:
                title = None
        if title is None:
            dm = _DOTTED_NUMBERED.match(s)
            if dm and dm.group(2)[0].isupper():
                stop = min(n, i + 60)
                nxt = next((j for j in range(i + 1, stop)
                            if rows[j][2].strip() and (
                                _ATX.match(rows[j][2].strip())
                                or _DOTTED_NUMBERED.match(rows[j][2].strip()))), stop)
                if _body_len(rows, i + 1, nxt) >= _MIN_BODY_CHARS:
                    title = _norm(s)
        if title and title not in seen and title not in _NON_CHAPTER_TITLES:
            seen.add(title)
            out.append(Chapter(title=title, page=page, line=len(out),
                               strategy="fallback", confidence=0.4,
                               strategies=("fallback",)))
    return out


def _merge(lists: list[list[Chapter]]) -> list[Chapter]:
    """Merge strategy outputs: same title+page wins once, best confidence."""
    by_key: dict[tuple[str, int | None], Chapter] = {}
    order: list[tuple[str, int | None]] = []
    for chapters in lists:
        for ch in chapters:
            key = (_norm(ch.title), ch.page)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = ch
                order.append(key)
            else:
                strategies = tuple(dict.fromkeys(existing.strategies + ch.strategies))
                existing.strategies = strategies
                if ch.confidence > existing.confidence:
                    existing.confidence = ch.confidence
                    existing.strategy = ch.strategy
                if existing.number is None:
                    existing.number = ch.number
    merged = [by_key[k] for k in order]
    # Renumber sequentially where no explicit number exists, preserving any
    # explicit numbers already found.
    next_num = 1
    for ch in merged:
        if ch.number is None:
            ch.number = next_num
        next_num = max(next_num, ch.number + 1)
    for i, ch in enumerate(merged):
        ch.line = i
    return merged


def detect_chapters(text: str, mode: str = "auto") -> dict:
    """Detect chapters in *text* with the strategies selected by *mode*.

    mode: "auto" (all strategies, default), "explicit", "toc", or
    "heuristic". The "fallback" section split applies in every mode when the
    chosen strategies yield fewer than two chapters, and is recorded as
    such in the returned ``method``.
    """
    mode = (mode or "auto").lower()
    if mode not in ("auto", "explicit", "toc", "heuristic"):
        raise ValueError(
            f"unknown chapter mode {mode!r}; expected auto, explicit, toc, or heuristic"
        )
    rows = _flatten(text)
    strategies: list[str] = []
    pools: list[list[Chapter]] = []
    if mode in ("auto", "explicit"):
        pools.append(_explicit_chapters(rows))
        strategies.append("explicit")
    if mode in ("auto", "toc"):
        pools.append(_toc_chapters(rows))
        strategies.append("toc")
    if mode in ("auto", "heuristic"):
        pools.append(_heuristic_chapters(rows))
        strategies.append("heuristic")
    merged = _merge(pools)
    fallback = False
    if len(merged) < 2:
        sections = _fallback_sections(rows)
        if len(sections) >= 2:
            merged = sections
            fallback = True
            strategies.append("fallback")
    if fallback:
        method = "fallback"
    elif len(merged) >= 2:
        method = "flexible" if mode == "auto" else mode
    elif merged:
        method = "flexible" if mode == "auto" else mode
    else:
        method = "none"
    return {
        "chapters": [ch.as_dict() for ch in merged],
        "chapters_count": len(merged),
        "method": method,
        "strategies_used": strategies,
        "fallback": fallback,
    }
