# OCR and embedded figures

Previously a scanned (image-only) PDF failed extraction outright, and
embedded images and diagrams were dropped silently. Both are now handled
behind optional tools - nothing here is a required dependency.

## Scanned pages

A page whose extracted text has almost no characters is treated as
scanned. In `--ocr auto` (the default), a scanned PDF is OCRed instead
of failing: `ocrmypdf` first when installed, otherwise each page is
rendered (PyMuPDF, else `pdftoppm`) and read with `tesseract`.
`--ocr off` restores the historical failure; `--ocr force` OCRs every
page regardless of native text (slow, only for damaged text layers).

```bash
python3 scripts/extract.py scanned.pdf                 # auto: OCR if needed
python3 scripts/extract.py scanned.pdf --ocr off        # old behaviour
python3 scripts/extract.py book.pdf --ocr force         # OCR every page
python3 scripts/extract.py --check                      # shows OCR tool status
```

Rendered pages are read with tesseract's automatic layout (PSM 3), so a
page set in two columns comes out column by column. PSM 3 also cuts some
one-column pages into side-by-side blocks that are not columns: a table
into its columns, or a list after a repeated first word. Real columns are
wide and separated by a gutter; when no pair of side-by-side blocks looks
like that, the page is read again as one block, row by row (PSM 6).
`tools/eval_ocr.py` measures this on synthetic pages with known text
(character error rate, lower is better; tesseract 5.3.4, English):

| Page | PSM 6 only (before) | Now |
| --- | ---: | ---: |
| one column, with or without title | 0.00% | 0.00% |
| one column, prose with a small table | 0.00% | 0.00% |
| full-page table | 0.00% | 0.00% |
| list, table of contents, lines with a repeated start | 0.00% | 0.00% |
| one column, skewed 2° | 0.03% | 0.03% |
| one column, blurred and noisy | 1.16% | 0.98% |
| two columns | 68.50% | 0.05% |
| two columns, title across both | 67.08% | 0.05% |
| two columns, baselines offset | 71.10% | 0.05% |
| two columns, skewed 1° | 68.40% | 0.10% |
| two columns, blurred and noisy | 69.08% | 0.92% |

Reading took 23% longer over the whole set (23.1 s against 18.8 s, one
run). Pages read twice cost the most: the full-page table took 2.5 times
as long, the table of contents 2.2 times. The pages are synthetic: real
scans will score worse on every row.

A page that still reads with low word confidence after that -- and has
ink, so it is not a blank page -- is read once more with tesseract's
orientation detection (PSM 1), and the higher-confidence read is kept, so
an upside-down scan comes out upright. This recovery pass runs only on a
page that needs it: in the benchmark no normal page triggers it. Measured
(CER, PSM 6 was the only reader before):

| Page | Before | Now |
| --- | ---: | ---: |
| one column, upside down | 84% | 0% |
| two columns, upside down | 83% | 0% |

A page turned 90 degrees was already read correctly. Whole scanned PDFs
sent through `ocrmypdf` are straightened by it instead: ATOM passes
`--rotate-pages --deskew` (Leptonica, no extra tool). Set
`ATOM_OCRMYPDF_ARGS` to change that list, e.g. to add `--clean` once
`unpaper` is installed.

A page that is tilted is straightened before OCR. A tilt throws off
tesseract's line finding, and a mild tilt is often read confidently yet
wrong, so this does not wait for low confidence: every page with ink has
its tilt estimated (a cheap projection-profile search on a small copy,
about 5% of one OCR pass), and a page tilted past 5 degrees is rotated
level and read again, keeping the higher-confidence read. It needs
Pillow; without it the estimate returns zero and nothing changes.
Measured (CER, the synthetic page tilted both ways):

| Page tilt | Before | Now |
| --- | ---: | ---: |
| 4 degrees | 0% | 0% |
| 7 degrees | 32% | 0% |
| 10 degrees | 91% | 0% |

In the benchmark only the tilted page pays the extra read; a straight
page pays just the estimate. Real scans tilt further and less evenly than
these, so treat the numbers as the direction, not a promise.

## Figures and diagrams

With `--ocr-figures on` (default when OCR is not off), embedded images
are extracted per page (PyMuPDF first, `pypdf` fallback), OCRed with
tesseract, and appended to the extracted text as marked blocks:

```
[FIGURE page 3, figure 2 | 800x600 png | source: ocr]
<text read from inside the figure>
```

A figure whose OCR comes back empty keeps the placeholder "hình này
chưa đọc được nội dung, cần mô tả bằng vision" instead of invented
content. `metadata.json` records `figures_extracted`,
`figures_with_ocr`, and `vision_used` per source.

## Vision hook: reading diagrams, not just letters

Tesseract reads letters inside a figure; it does not understand a
diagram. For real diagram understanding, set a command that takes a
figure file path and prints a description to stdout:

```bash
export BOOK_TO_SKILL_VISION_CMD="my-vision-tool describe"
```

Its output is added to the figure block labelled "vision-derived, cần
kiểm chứng". The hook is opt-in by design: no API key is hardcoded, no
cloud call is made by default, and no heavy model is downloaded. The
generator spec (SKILL.md) treats figure blocks as first-class knowledge
- rules read off a diagram go into patterns/cheatsheet with the figure
page cited - but a claim that comes only from a vision description
stays marked unverified.

## Mixed PDFs, columns and decoration

- **A scanned page inside a digital PDF** (a pasted-in scan, a photographed
  appendix) is found page by page: a page with no text layer is OCRed and
  its text goes back at that page's position, not at the end. With
  `--ocr off` the run lists those pages instead of dropping them silently.
- **Two-column pages** are detected in pdftotext's layout output and
  rewritten column by column, so sentences from the two columns are not
  interleaved on one line. A title spanning both columns stays in place.
- **Figures that are decoration are skipped before OCR**: an image that
  repeats on at least 20% of the pages (and 3 pages or more) is a logo or
  running graphic; an image under 48 px on both sides is an icon; the same
  image is OCRed once. Pages already OCRed whole are not read again as
  figures. The run prints how many images it skipped.
- A figure block still sits at the end of its source's text, tagged with
  its page number.

## A hard page: external OCR hook

Some pages defeat tesseract no matter how they are straightened -- most of
all handwriting. Point ATOM at your own OCR command and it routes exactly
those pages to it:

```bash
export BOOK_TO_SKILL_OCR_CMD="my-ocr"   # receives a page image path, prints its text
python3 scripts/extract.py scanned.pdf
```

ATOM bundles no model. The command is yours: a Vietnamese handwriting
model (for example a TrOCR/VietOCR checkpoint, or HTR-ConvText), a cloud
OCR, anything that reads an image path and prints text. It is called only
on a page whose built-in read stays below the confidence threshold after
the orientation and tilt recovery above, so a normal book never invokes
it; when it returns text, that text is used for the page. Measure your
model before trusting it -- handwriting OCR is far less accurate than
print, and the result is not verified against the page.

Clear the OCR cache (`atom --clear-ocr-cache`) after setting or changing
the hook: a page's text is cached by its image, not by the hook, so a page
read before the hook was set keeps its old text otherwise.

## Cache

Page renders, OCR text and vision descriptions are cached under
`~/.cache/atom-ocr` (or `$ATOM_OCR_CACHE_DIR`), so a re-run over the same
PDF is fast. The cache holds the text of the documents you converted:

- it is capped at 1 GB, oldest files removed first
  (`ATOM_OCR_CACHE_MAX_MB` changes the cap);
- `atom --clear-ocr-cache` (or `scripts/extract.py --clear-ocr-cache`)
  deletes it;
- `ATOM_OCR_CACHE_DIR=off` turns it off.

## Limits

- OCR quality is the tool's quality: tesseract misreads small, skewed,
  or handwritten text, and the extractor does not spell-check it.
- Without ocrmypdf, tesseract, PyMuPDF, and pdftoppm all missing,
  scanned PDFs fail exactly as before and figures are skipped; the run
  reports this instead of pretending otherwise.
- Vision descriptions are only as good as the command you point the
  hook at, and they are never verified against the figure by this tool.
