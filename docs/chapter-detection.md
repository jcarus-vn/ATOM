# Flexible chapter detection

The legacy detector only counted explicit "Chapter N" headings (plus a
Markdown structural fallback). It under-counted books that organize
chapters as bare titles (Moby-Dick: "Loomings"), Roman numerals without
the word "Chapter", or section-style headings (Pro Git). The flexible
detector in `atom/chapter_detection.py` layers strategies with
confidence scores and merges them, deduped and sorted by page:

1. **explicit** (0.9-0.95): "Chapter N", "Unit N", "Part N", "Lesson N",
   and Vietnamese "Chương N", "Phần N", "Bài N", "Hồi N", with Arabic or
   Roman numerals, plus standalone Roman headings ("I: Loomings").
2. **toc** (0.8 anchored, 0.45 unanchored): parse the table of contents
   (title + page number), then anchor each entry at its first matching
   heading-like line in the body.
3. **heuristic** (0.55-0.75): ALL-CAPS short lines, bare title lines
   (short, Title Case, blank lines on both sides, real body after them),
   ATX headings, and numbered headings whose section carries body text.
4. **fallback** (0.4): when nothing above yields at least two chapters,
   split on section titles instead of returning one block, and record
   that the result is a fallback.

## Usage

```bash
python3 scripts/extract.py book.pdf --chapter-mode auto      # default
python3 scripts/extract.py book.pdf --chapter-mode explicit  # legacy behaviour
python3 scripts/extract.py book.pdf --chapter-mode toc
python3 scripts/extract.py book.pdf --chapter-mode heuristic
```

`metadata.json` records, per source and overall: `chapter_details`
(title, page, strategy, confidence, strategies) and `chapter_detection`
(mode, method, strategies_used, fallback). When the legacy count finds
fewer than two chapters but the flexible detector finds at least two,
the flexible count wins and `chapters_method` becomes "flexible".

## Limits, stated plainly

- A bare title is a guess from shape, not proof: a short display line in
  the middle of prose can be mistaken for a chapter, and a chapter title
  that looks like an ordinary sentence will be missed. Confidence scores
  exist so a consumer can distrust the low end.
- Numbered list items are excluded by a body-text gate (a section needs
  real body text before the next heading), but a long numbered list with
  paragraph-length items can still pass the gate.
- Ambiguous titles that repeat as running heads are deduped by title and
  page; a book that genuinely repeats a chapter title will lose one copy.
- ToC entries that never reappear as body headings are kept with the
  ToC page number and low confidence, not dropped silently.
