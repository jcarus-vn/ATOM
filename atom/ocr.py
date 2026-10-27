"""Optional OCR and embedded-figure handling for PDFs.

The historical extractor aborts on a scanned (image-only) PDF and drops
embedded images silently. This module adds, all behind optional tools:

- Scanned-page detection: a page whose extracted text has almost no
  characters is treated as needing OCR.
- Page OCR, preferring ``ocrmypdf`` when it is installed, falling back to
  rendering the page (PyMuPDF, else ``pdftoppm``) and running ``tesseract``.
- Embedded-figure extraction (PyMuPDF first, ``pypdf`` fallback): each
  figure is OCRed with tesseract and appended to the extracted text as a
  clearly marked figure block. A figure whose OCR comes back empty keeps a
  placeholder saying it could not be read yet.
- A vision-describer hook: when the ``BOOK_TO_SKILL_VISION_CMD`` environment
  variable holds a command, it is run once per figure with the figure file
  path as its only argument, and its stdout is added to the figure block
  labelled as vision-derived and unverified. Tesseract reads letters inside
  a figure; it does not understand a diagram. The hook is where a real
  vision model plugs in, and it is deliberately opt-in: no API key is ever
  hardcoded, no cloud call is made by default, and no heavy model is
  downloaded by this module.

Nothing here is a required dependency. When no OCR tool is present, every
function degrades to "unavailable" and the caller keeps the old behaviour.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile

VISION_ENV_VAR = "BOOK_TO_SKILL_VISION_CMD"
FIGURE_PLACEHOLDER = (
    "h\u00ecnh n\u00e0y ch\u01b0a \u0111\u1ecdc \u0111\u01b0\u1ee3c n\u1ed9i dung, "
    "c\u1ea7n m\u00f4 t\u1ea3 b\u1eb1ng vision"
)
VISION_LABEL = "vision-derived, c\u1ea7n ki\u1ec3m ch\u1ee9ng"

# A page with fewer alphanumeric characters than this is treated as scanned.
# Running headers alone ("12", "Chapter") stay well below it, while any real
# paragraph clears it by an order of magnitude.
SCANNED_PAGE_MIN_CHARS = 30


def ocr_status() -> dict:
    """Report which optional OCR/figure tools are available."""
    return {
        "ocrmypdf": shutil.which("ocrmypdf") is not None,
        "tesseract": shutil.which("tesseract") is not None,
        "fitz": _has_fitz(),
        "pdftoppm": shutil.which("pdftoppm") is not None,
    }


def _has_fitz() -> bool:
    try:
        import fitz  # noqa: F401
        return True
    except ImportError:
        return False


def is_scanned_text(text: str) -> bool:
    """True when *text* has too few characters to be a real page of prose."""
    if not text:
        return True
    alnum = sum(1 for ch in text if ch.isalnum())
    return alnum < SCANNED_PAGE_MIN_CHARS


def ocr_image_bytes(data: bytes, lang: str = "eng", timeout: int = 120) -> str | None:
    """OCR one image's bytes with tesseract, or None when unavailable."""
    if not shutil.which("tesseract") or not data:
        return None
    try:
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "6", "-l", lang],
            input=data, capture_output=True, timeout=timeout,
        )
    except Exception as exc:
        print(f"  [warn] tesseract failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", errors="replace").strip()
    return text or None


def ocr_image_file(path: str, lang: str = "eng", timeout: int = 120) -> str | None:
    """OCR one image file with tesseract, or None when unavailable."""
    if not shutil.which("tesseract"):
        return None
    try:
        result = subprocess.run(
            ["tesseract", path, "stdout", "--psm", "6", "-l", lang],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except Exception as exc:
        print(f"  [warn] tesseract failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def render_page_png(pdf_path: str, page_number: int, dpi: int = 200) -> bytes | None:
    """Render one PDF page (1-based) to PNG bytes, or None if no renderer."""
    try:
        import fitz
        doc = fitz.open(pdf_path)
        try:
            if page_number < 1 or page_number > len(doc):
                return None
            page = doc[page_number - 1]
            pix = page.get_pixmap(dpi=dpi)
            return pix.tobytes("png")
        finally:
            doc.close()
    except ImportError:
        pass
    except Exception as exc:
        print(f"  [warn] PyMuPDF render failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    if not shutil.which("pdftoppm"):
        return None
    with tempfile.TemporaryDirectory(prefix="book_skill_page_") as tmp:
        prefix = os.path.join(tmp, "page")
        try:
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(dpi),
                 "-f", str(page_number), "-l", str(page_number),
                 pdf_path, prefix],
                capture_output=True, timeout=120, check=True,
            )
        except Exception as exc:
            print(f"  [warn] pdftoppm failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return None
        for name in sorted(os.listdir(tmp)):
            if name.endswith(".png"):
                with open(os.path.join(tmp, name), "rb") as fh:
                    return fh.read()
    return None


def ocr_pdf_with_ocrmypdf(pdf_path: str) -> str | None:
    """OCR a whole PDF via ocrmypdf into a temp searchable PDF, then read it.

    Returns extracted text, or None when ocrmypdf is missing or fails. The
    temp file is removed by the caller-side TemporaryDirectory.
    """
    if not shutil.which("ocrmypdf"):
        return None
    try:
        from atom.parsers.pdf import extract_with_pdftotext
    except ImportError:
        return None
    with tempfile.TemporaryDirectory(prefix="book_skill_ocr_") as tmp:
        out_path = os.path.join(tmp, "ocr.pdf")
        try:
            subprocess.run(
                ["ocrmypdf", "--force-ocr", "--optimize", "0",
                 os.path.abspath(pdf_path), out_path],
                capture_output=True, timeout=600, check=True,
            )
        except Exception as exc:
            print(f"  [warn] ocrmypdf failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return None
        return extract_with_pdftotext(out_path)


def ocr_pdf_pages(pdf_path: str, page_count: int) -> list[tuple[int, str]]:
    """OCR pages 1..page_count via render + tesseract; skips failed pages."""
    out = []
    for page in range(1, page_count + 1):
        png = render_page_png(pdf_path, page)
        if png is None:
            continue
        text = ocr_image_bytes(png)
        if text:
            out.append((page, text))
    return out


def extract_pdf_images(pdf_path: str) -> list[dict]:
    """Extract embedded images per page: [{page, index, width, height,
    format, data}]. PyMuPDF first; pypdf fallback; [] when neither works."""
    images: list[dict] = []
    try:
        import fitz
        doc = fitz.open(pdf_path)
        try:
            for page_number, page in enumerate(doc, 1):
                for index, img in enumerate(page.get_images(full=True), 1):
                    xref = img[0]
                    try:
                        pix = fitz.Pixmap(doc, xref)
                        if pix.n > 4:
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        images.append({
                            "page": page_number, "index": index,
                            "width": pix.width, "height": pix.height,
                            "format": "png", "data": pix.tobytes("png"),
                        })
                    except Exception:
                        continue
        finally:
            doc.close()
        return images
    except ImportError:
        pass
    except Exception as exc:
        print(f"  [warn] PyMuPDF image extraction failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return images
    try:
        import pypdf
        with open(pdf_path, "rb") as fh:
            reader = pypdf.PdfReader(fh)
            for page_number, page in enumerate(reader.pages, 1):
                try:
                    page_images = list(page.images)
                except Exception:
                    continue
                for index, img in enumerate(page_images, 1):
                    try:
                        data = img.data
                    except Exception:
                        continue
                    images.append({
                        "page": page_number, "index": index,
                        "width": getattr(img, "width", 0) or 0,
                        "height": getattr(img, "height", 0) or 0,
                        "format": "png", "data": data,
                    })
    except ImportError:
        pass
    except Exception as exc:
        print(f"  [warn] pypdf image extraction failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    return images


def describe_with_vision(image_bytes: bytes) -> str | None:
    """Run the user-configured vision command on one figure, if configured.

    The command comes only from the BOOK_TO_SKILL_VISION_CMD environment
    variable, is split without a shell, and receives the figure file path as
    its single argument. Its stdout is the description. Returns None when the
    variable is unset or the command fails; the caller labels any returned
    text as vision-derived and unverified.
    """
    cmd = os.environ.get(VISION_ENV_VAR, "").strip()
    if not cmd or not image_bytes:
        return None
    try:
        argv = shlex.split(cmd)
    except ValueError as exc:
        print(f"  [warn] bad {VISION_ENV_VAR}: {exc}", file=sys.stderr)
        return None
    if not argv:
        return None
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            [*argv, tmp_path], capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="replace",
        )
    except Exception as exc:
        print(f"  [warn] vision command failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def figure_block(page: int, index: int, meta: dict,
                 ocr_text: str | None, vision_text: str | None) -> str:
    """Render one figure as a marked block for full_text.txt."""
    size = f"{meta.get('width', 0)}x{meta.get('height', 0)}"
    fmt = meta.get("format", "png")
    header = f"[FIGURE page {page}, figure {index} | {size} {fmt} | source: ocr]"
    lines = [header]
    if ocr_text:
        lines.append(ocr_text)
    else:
        lines.append(FIGURE_PLACEHOLDER)
    if vision_text:
        lines.append(f"[{VISION_LABEL}]")
        lines.append(vision_text)
    return "\n".join(lines)


def process_pdf_figures(pdf_path: str) -> tuple[str, dict]:
    """Extract and OCR every embedded figure in a PDF.

    Returns (blocks_text, info) where info counts figures, figures with OCR
    text, and whether any vision description was used. When no image tool is
    available, blocks_text is empty and info says why.
    """
    images = extract_pdf_images(pdf_path)
    blocks = []
    with_ocr = 0
    vision_used = False
    for img in images:
        ocr_text = ocr_image_bytes(img["data"])
        if ocr_text:
            with_ocr += 1
        vision_text = describe_with_vision(img["data"])
        if vision_text:
            vision_used = True
        blocks.append(figure_block(img["page"], img["index"], img,
                                   ocr_text, vision_text))
    info = {
        "figures_extracted": len(images),
        "figures_with_ocr": with_ocr,
        "vision_used": vision_used,
        "vision_configured": bool(os.environ.get(VISION_ENV_VAR, "").strip()),
    }
    return ("\n\n".join(blocks), info)
