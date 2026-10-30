"""Optional OCR and embedded-figure handling for PDFs.

The historical extractor aborts on a scanned (image-only) PDF and drops
embedded images silently. This module adds, all behind optional tools:

- Scanned-page detection: a page whose extracted text has almost no
  characters is treated as needing OCR.
- Page OCR, preferring ``ocrmypdf`` when it is installed, falling back to
  rendering the page (PyMuPDF, else ``pdftoppm``) and running ``tesseract``.
- Page triage: a rendered page whose ink coverage is below
  ``BLANK_INK_THRESHOLD`` (pure blank, or a page number alone) is skipped
  without paying for an OCR run. Triage is deliberately conservative: an
  undecodable image is treated as NOT blank, so triage can only skip
  pages, never content.
- Language auto-detection: when no language is requested, the installed
  tesseract language data decides. ``vie+eng`` is used when both are
  present (measured: Vietnamese CER 0.217 with ``eng`` alone, 0.013 with
  ``vie+eng``, English unchanged at 0.002), otherwise ``eng``.
- Bounded parallel page OCR and a page-hash cache: re-running OCR over
  the same pages never re-OCRs a page whose render bytes, language and
  PSM already produced a cached result.
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
  downloaded by this module. The hook contract (env var, image path in,
  description on stdout) is unchanged.

Nothing here is a required dependency. When no OCR tool is present, every
function degrades to "unavailable" and the caller keeps the old behaviour.

Measured on this machine (tesseract 5.3.4, clean synthetic renders,
2026-10-27): raising render DPI from 150 to 400 roughly doubled wall time
with no CER change, and PSM 3/4/6 gave identical CER, so the defaults
stay at 200 dpi and PSM 6. Those knobs remain overridable for degraded
real-world scans, where the clean-render measurement does not apply.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from concurrent.futures import ThreadPoolExecutor

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

# Render/OCR defaults. DPI and PSM were measured, not guessed (see module
# docstring): on clean renders neither moves CER, so the old values stand.
DEFAULT_DPI = 200
DEFAULT_PSM = 6
DEFAULT_TIMEOUT = 120
DEFAULT_OCR_TIMEOUT = 600

# Pages below this ink fraction carry no prose: measured on the synthetic
# sample, a blank page is 0.0, a page-number-only page is 0.000068, and a
# full text page is 0.012. The threshold sits an order of magnitude above
# the page-number case and far below any real paragraph.
BLANK_INK_THRESHOLD = 0.0002

# Languages preferred when no language is requested and the data exists.
PREFERRED_LANGS = "vie+eng"
FALLBACK_LANG = "eng"

# Cache location when the caller does not pass one. Pass cache_dir="" to
# disable caching, or set ATOM_OCR_CACHE_DIR to move it.
CACHE_ENV_VAR = "ATOM_OCR_CACHE_DIR"
DEFAULT_CACHE_DIRNAME = os.path.join(".cache", "atom-ocr")


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


def detect_ocr_languages(preferred: str = PREFERRED_LANGS) -> str:
    """Best tesseract language string available, or ``eng`` as fallback.

    Runs ``tesseract --list-langs`` once (tens of milliseconds, negligible
    next to a multi-second OCR run) and returns *preferred* only when every
    language it names is installed. Any failure, including a tesseract
    binary that cannot be probed, falls back to ``eng``. Deliberately not
    cached across calls: caching would let a mocked subprocess in one
    test leak into the next.
    """
    try:
        result = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True, timeout=30,
        )
    except Exception:
        return FALLBACK_LANG
    if getattr(result, "returncode", 0) != 0:
        return FALLBACK_LANG
    raw = _as_text(getattr(result, "stdout", ""))
    err = _as_text(getattr(result, "stderr", ""))
    langs = set()
    for line in (raw + "\n" + err).splitlines():
        line = line.strip()
        if not line or line.startswith("List of"):
            continue
        langs.add(line)
    if not langs:
        return FALLBACK_LANG
    wanted = [part for part in preferred.split("+") if part]
    if wanted and all(part in langs for part in wanted):
        return preferred
    if FALLBACK_LANG in langs:
        return FALLBACK_LANG
    return FALLBACK_LANG


def resolve_lang(lang: str | None) -> str:
    """Resolve a requested language: None/empty means auto-detect."""
    if lang:
        return lang
    return detect_ocr_languages()


def _as_text(value) -> str:
    """Decode subprocess output that may be bytes or str (or a mock)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value is not None else ""


def _tesseract_args(psm: int, lang: str) -> list[str]:
    return ["tesseract", "stdin", "stdout", "--psm", str(psm), "-l", lang]


def _warn(message: str) -> None:
    print(f"  [warn] {message}", file=sys.stderr)


def ocr_image_bytes(data: bytes, lang: str | None = None,
                    timeout: int = DEFAULT_TIMEOUT, psm: int = DEFAULT_PSM) -> str | None:
    """OCR one image's bytes with tesseract, or None when unavailable.

    *lang* None means auto-detect (``vie+eng`` when installed, else
    ``eng``). Kept backwards compatible: the old default was ``eng``,
    which is exactly what auto-detection returns on machines without
    Vietnamese data.
    """
    if not shutil.which("tesseract") or not data:
        return None
    resolved = resolve_lang(lang)
    try:
        result = subprocess.run(
            _tesseract_args(psm, resolved),
            input=data, capture_output=True, timeout=timeout,
        )
    except Exception as exc:
        _warn(f"tesseract failed: {type(exc).__name__}: {exc}")
        return None
    if getattr(result, "returncode", 0) != 0:
        _warn("tesseract exited non-zero; page treated as unread")
        return None
    text = _as_text(getattr(result, "stdout", "")).strip()
    return text or None


def ocr_image_file(path: str, lang: str | None = None,
                   timeout: int = DEFAULT_TIMEOUT, psm: int = DEFAULT_PSM) -> str | None:
    """OCR one image file with tesseract, or None when unavailable."""
    if not shutil.which("tesseract"):
        return None
    resolved = resolve_lang(lang)
    try:
        result = subprocess.run(
            ["tesseract", path, "stdout", "--psm", str(psm), "-l", resolved],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except Exception as exc:
        _warn(f"tesseract failed: {type(exc).__name__}: {exc}")
        return None
    if getattr(result, "returncode", 0) != 0:
        _warn("tesseract exited non-zero; figure treated as unread")
        return None
    text = _as_text(getattr(result, "stdout", "")).strip()
    return text or None


def render_page_png(pdf_path: str, page_number: int, dpi: int = DEFAULT_DPI) -> bytes | None:
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
        _warn(f"PyMuPDF render failed: {type(exc).__name__}: {exc}")
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
            _warn(f"pdftoppm failed: {type(exc).__name__}: {exc}")
            return None
        for name in sorted(os.listdir(tmp)):
            if name.endswith(".png"):
                with open(os.path.join(tmp, name), "rb") as fh:
                    return fh.read()
    return None


# ---------------------------------------------------------------------------
# Page triage: ink coverage of a rendered PNG.
# ---------------------------------------------------------------------------

def _png_ink_ratio(data: bytes) -> float | None:
    """Ink fraction of a PNG (dark pixels / all pixels), or None if the PNG
    cannot be decoded with the stdlib decoder. Only 8-bit, non-interlaced
    gray/RGB/RGBA images are supported; anything else returns None, which
    callers treat as "not blank" (the safe direction)."""
    try:
        if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        pos = 8
        width = height = None
        bit_depth = color_type = interlace = None
        idat = bytearray()
        while pos + 8 <= len(data):
            (length,) = struct.unpack(">I", data[pos:pos + 4])
            chunk = data[pos + 4:pos + 8]
            body = data[pos + 8:pos + 8 + length]
            if len(body) != length:
                return None
            if chunk == b"IHDR":
                width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                    ">IIBBBBB", body)
            elif chunk == b"IDAT":
                idat.extend(body)
            elif chunk == b"IEND":
                break
            pos += 8 + length + 4
        if width is None or bit_depth != 8 or interlace != 0:
            return None
        channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
        if channels is None:
            return None
        raw = zlib.decompress(bytes(idat))
        stride = width * channels
        prev = bytearray(stride)
        dark = 0
        total = 0
        offset = 0
        for _row in range(height):
            if offset >= len(raw):
                return None
            filter_type = raw[offset]
            offset += 1
            line = bytearray(raw[offset:offset + stride])
            offset += stride
            if len(line) != stride:
                return None
            bpp = channels
            if filter_type == 1:  # Sub
                for i in range(bpp, stride):
                    line[i] = (line[i] + line[i - bpp]) & 0xFF
            elif filter_type == 2:  # Up
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 0xFF
            elif filter_type == 3:  # Average
                for i in range(stride):
                    a = line[i - bpp] if i >= bpp else 0
                    line[i] = (line[i] + ((a + prev[i]) // 2)) & 0xFF
            elif filter_type == 4:  # Paeth
                for i in range(stride):
                    a = line[i - bpp] if i >= bpp else 0
                    b = prev[i]
                    c = prev[i - bpp] if i >= bpp else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    line[i] = (line[i] + pr) & 0xFF
            elif filter_type != 0:
                return None
            for px in range(width):
                base = px * channels
                if channels == 1:
                    value = line[base]
                else:
                    value = (line[base] + line[base + 1] + line[base + 2]) // 3
                if value < 128:
                    dark += 1
                total += 1
            prev = line
        if total == 0:
            return None
        return dark / total
    except Exception:
        return None


def ink_ratio(data: bytes) -> float | None:
    """Ink fraction of a rendered page image, or None if undecodable.

    Prefers Pillow when installed (handles every PNG flavour); falls back
    to the stdlib PNG decoder, which returns None for formats it cannot
    prove. None always means "treat as not blank".
    """
    if not data:
        return None
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            gray = im.convert("L")
            hist = gray.histogram()
            total = gray.width * gray.height
            if total == 0:
                return None
            return sum(hist[:128]) / total
    except Exception:
        pass
    return _png_ink_ratio(data)


def is_blank_image(data: bytes, threshold: float = BLANK_INK_THRESHOLD) -> bool:
    """True when a rendered page has too little ink to carry prose.

    A page whose ink cannot be measured is NOT blank: triage must never
    discard content it cannot see.
    """
    if not data:
        return True
    ratio = ink_ratio(data)
    if ratio is None:
        return False
    return ratio < threshold


# ---------------------------------------------------------------------------
# Page-hash OCR cache.
# ---------------------------------------------------------------------------

def default_cache_dir() -> str:
    """Cache directory: $ATOM_OCR_CACHE_DIR, else ~/.cache/atom-ocr."""
    override = os.environ.get(CACHE_ENV_VAR, "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), DEFAULT_CACHE_DIRNAME)


def _cache_key(png: bytes, lang: str, psm: int) -> str:
    digest = hashlib.sha256()
    digest.update(png)
    digest.update(b"\0")
    digest.update(lang.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(psm).encode("ascii"))
    return digest.hexdigest()


def _cache_get(cache_dir: str | None, png: bytes, lang: str, psm: int) -> str | None:
    if not cache_dir:
        return None
    try:
        with open(os.path.join(cache_dir, _cache_key(png, lang, psm) + ".txt"),
                  encoding="utf-8") as fh:
            text = fh.read().strip()
            return text or None
    except OSError:
        return None


def _cache_put(cache_dir: str | None, png: bytes, lang: str, psm: int, text: str) -> None:
    if not cache_dir or not text:
        return
    try:
        os.makedirs(cache_dir, exist_ok=True)
        target = os.path.join(cache_dir, _cache_key(png, lang, psm) + ".txt")
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except OSError:
        pass  # A cache failure must never break OCR.


def ocr_pdf_with_ocrmypdf(pdf_path: str, timeout: int = DEFAULT_OCR_TIMEOUT) -> str | None:
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
                capture_output=True, timeout=timeout, check=True,
            )
        except Exception as exc:
            _warn(f"ocrmypdf failed: {type(exc).__name__}: {exc}")
            return None
        return extract_with_pdftotext(out_path)


def _sha256_file(path: str) -> str | None:
    """SHA-256 of a file, or None when it cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _render_cache_path(cache_dir: str, pdf_hash: str, page: int, dpi: int) -> str:
    return os.path.join(cache_dir, f"render-{pdf_hash[:16]}-p{page}-d{dpi}.png")


def _render_cached(pdf_path: str, page: int, dpi: int,
                   cache_dir: str | None, pdf_hash: str | None) -> bytes | None:
    """Render one page, reusing a cached render when the PDF, page and DPI
    match. Render caching is what makes a cached re-run fast: on this VM a
    pdftoppm render costs ~3s/page, more than the OCR it feeds is saved
    by skipping. Cache failures fall through to a fresh render."""
    if cache_dir and pdf_hash:
        try:
            with open(_render_cache_path(cache_dir, pdf_hash, page, dpi), "rb") as fh:
                data = fh.read()
                if data:
                    return data
        except OSError:
            pass
    png = render_page_png(pdf_path, page, dpi=dpi)
    if png and cache_dir and pdf_hash:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            target = _render_cache_path(cache_dir, pdf_hash, page, dpi)
            tmp = target + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(png)
            os.replace(tmp, target)
        except OSError:
            pass
    return png


def _ocr_one_page(pdf_path: str, page: int, dpi: int, lang: str, psm: int,
                   cache_dir: str | None, timeout: int, triage: bool,
                   pdf_hash: str | None = None) -> tuple[int, str | None]:
    """Render + (triage) + OCR one page. Returns (page, text or None)."""
    png = _render_cached(pdf_path, page, dpi, cache_dir, pdf_hash)
    if png is None:
        return page, None
    if triage and is_blank_image(png):
        return page, None
    cached = _cache_get(cache_dir, png, lang, psm)
    if cached is not None:
        return page, cached
    text = ocr_image_bytes(png, lang=lang, timeout=timeout, psm=psm)
    if text:
        _cache_put(cache_dir, png, lang, psm, text)
    return page, text


def ocr_pdf_pages(pdf_path: str, page_count: int,
                  dpi: int = DEFAULT_DPI, lang: str | None = None,
                  psm: int = DEFAULT_PSM, jobs: int | None = None,
                  cache_dir: str | None = None, timeout: int = DEFAULT_TIMEOUT,
                  triage: bool = True) -> list[tuple[int, str]]:
    """OCR pages 1..page_count via render + tesseract; skips failed pages.

    Keyword arguments are all optional and backwards compatible with the
    old ``(pdf_path, page_count)`` call: *lang* None auto-detects,
    *jobs* None uses up to 4 workers (bounded; tesseract runs are
    subprocess-bound), *cache_dir* None uses ``default_cache_dir()``
    (pass ``""`` to disable caching), and *triage* skips blank and
    near-blank pages before OCR. The cache also stores renders keyed by
    (PDF hash, page, DPI), because on this VM a render costs ~3s/page:
    without it a cached re-run would still pay full render time.
    Returned pairs stay in page order
    regardless of worker count. Failures are logged to stderr and the
    page is skipped, exactly like the old sequential version.
    """
    resolved_lang = resolve_lang(lang)
    if cache_dir is None:
        cache_dir = default_cache_dir()
    if jobs is None:
        jobs = min(4, os.cpu_count() or 1)
    jobs = max(1, jobs)
    pdf_hash = _sha256_file(pdf_path) if cache_dir else None
    args = [(pdf_path, page, dpi, resolved_lang, psm, cache_dir, timeout, triage, pdf_hash)
            for page in range(1, page_count + 1)]
    if jobs == 1:
        results = [_ocr_one_page(*a) for a in args]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(lambda a: _ocr_one_page(*a), args))
    return [(page, text) for page, text in results if text]


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
        _warn(f"PyMuPDF image extraction failed: {type(exc).__name__}: {exc}")
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
        _warn(f"pypdf image extraction failed: {type(exc).__name__}: {exc}")
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
        _warn(f"bad {VISION_ENV_VAR}: {exc}")
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
        _warn(f"vision command failed: {type(exc).__name__}: {exc}")
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()[-1:] or [""]
        _warn(f"vision command exited {result.returncode}; figure left "
              f"undescribed. {detail[0][:200]}")
        return None
    text = result.stdout.strip()
    return text or None


def _vision_cache_path(image_bytes: bytes) -> str | None:
    cmd = os.environ.get(VISION_ENV_VAR, "").strip()
    cache_dir = default_cache_dir()
    if not cmd or not cache_dir:
        return None
    digest = hashlib.sha256(cmd.encode("utf-8") + b"\0" + image_bytes).hexdigest()
    return os.path.join(cache_dir, f"vision-{digest}.txt")


def describe_with_vision_cached(image_bytes: bytes) -> str | None:
    """describe_with_vision, cached per (command, image): a re-run over the
    same PDF does not pay the vision model again for figures it described."""
    path = _vision_cache_path(image_bytes)
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                cached = fh.read().strip()
                if cached:
                    return cached
        except OSError:
            pass
    text = describe_with_vision(image_bytes)
    if text and path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except OSError:
            pass
    return text


# A figure smaller than this on both sides is an icon or bullet, not content.
MIN_FIGURE_SIDE = 48
# The same image on at least this share of the pages (and on 3 pages or
# more) is page decoration: a logo, a running header graphic. A diagram
# reused on a few pages of a long book is still a figure (kept once).
REPEATED_FIGURE_SHARE = 0.2
REPEATED_FIGURE_MIN_PAGES = 3


def _image_size(data: bytes, meta: dict) -> tuple[int, int]:
    width, height = int(meta.get("width") or 0), int(meta.get("height") or 0)
    if width and height:
        return width, height
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            return im.width, im.height
    except Exception:
        pass
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    return 0, 0


def select_figures(images: list[dict]) -> tuple[list[dict], dict]:
    """Drop decoration before paying for OCR: images repeated on several
    pages (logos) and tiny ones (icons). Duplicates of a kept figure are
    dropped too. Returns (kept, counts)."""
    pages_by_hash: dict[str, set] = {}
    for img in images:
        img["_hash"] = hashlib.sha256(img.get("data") or b"").hexdigest()
        pages_by_hash.setdefault(img["_hash"], set()).add(img.get("page"))
    total_pages = max((img.get("page") or 0 for img in images), default=0)
    repeat_floor = max(REPEATED_FIGURE_MIN_PAGES,
                       int(REPEATED_FIGURE_SHARE * total_pages + 0.999))
    kept, seen = [], set()
    counts = {"figures_repeated_skipped": 0, "figures_small_skipped": 0,
              "figures_duplicate_skipped": 0}
    for img in images:
        h = img["_hash"]
        if len(pages_by_hash[h]) >= repeat_floor:
            counts["figures_repeated_skipped"] += 1
            continue
        w, hgt = _image_size(img.get("data") or b"", img)
        if w and hgt and w < MIN_FIGURE_SIDE and hgt < MIN_FIGURE_SIDE:
            counts["figures_small_skipped"] += 1
            continue
        if h in seen:
            counts["figures_duplicate_skipped"] += 1
            continue
        seen.add(h)
        img["width"], img["height"] = w, hgt
        kept.append(img)
    return kept, counts


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


def process_pdf_figures(pdf_path: str, lang: str | None = None,
                        jobs: int | None = None) -> tuple[str, dict]:
    """Extract and OCR the embedded figures of a PDF.

    Repeated page decoration and tiny icons are skipped first
    (:func:`select_figures`), the OCR language is resolved once for the
    whole document, figure OCR runs on a small worker pool, and vision
    descriptions are cached per image. Returns (blocks_text, info); info
    counts figures, figures with OCR text, what was skipped, and whether
    any vision description was used.
    """
    images, skipped = select_figures(extract_pdf_images(pdf_path))
    resolved = resolve_lang(lang) if images and shutil.which("tesseract") else lang
    if jobs is None:
        jobs = min(4, os.cpu_count() or 1)
    if len(images) > 1 and jobs > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            ocr_texts = list(pool.map(
                lambda img: ocr_image_bytes(img["data"], lang=resolved), images))
    else:
        ocr_texts = [ocr_image_bytes(img["data"], lang=resolved) for img in images]
    blocks = []
    with_ocr = 0
    vision_used = False
    for img, ocr_text in zip(images, ocr_texts):
        if ocr_text:
            with_ocr += 1
        vision_text = describe_with_vision_cached(img["data"])
        if vision_text:
            vision_used = True
        blocks.append(figure_block(img["page"], img["index"], img,
                                   ocr_text, vision_text))
    info = {
        "figures_extracted": len(images),
        "figures_with_ocr": with_ocr,
        "vision_used": vision_used,
        "vision_configured": bool(os.environ.get(VISION_ENV_VAR, "").strip()),
        **skipped,
    }
    return ("\n\n".join(blocks), info)
