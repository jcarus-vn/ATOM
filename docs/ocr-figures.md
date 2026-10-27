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

## Limits

- OCR quality is the tool's quality: tesseract misreads small, skewed,
  or handwritten text, and the extractor does not spell-check it.
- Without ocrmypdf, tesseract, PyMuPDF, and pdftoppm all missing,
  scanned PDFs fail exactly as before and figures are skipped; the run
  reports this instead of pretending otherwise.
- Vision descriptions are only as good as the command you point the
  hook at, and they are never verified against the figure by this tool.
