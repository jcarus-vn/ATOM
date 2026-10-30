#!/usr/bin/env python3
"""Measure page OCR on synthetic scan-like pages with known text.

Each page is drawn with Pillow from sentences written for this tool (no
book text), so the exact reading-order text is known. The page then goes
through the same tesseract call the extractor uses for a scanned page, and
the output is scored by character error rate (CER): edit distance between
the OCR text and the true text, divided by the true text length, after
collapsing whitespace. Text read in the wrong order (two columns read
across) scores badly even when every word is recognised.

Usage:
  python3 tools/eval_ocr.py [--json out.json] [--only two-column,skew-2]

Needs Pillow and tesseract (eval-only; neither is a runtime requirement).
Prints one row per page and method. ``psm6`` is a single tesseract call in
PSM 6, which is how scanned pages were read before ``ocr_page_bytes``;
``page`` is the path scanned PDF pages take now, when this checkout has it.
"""

from __future__ import annotations

import argparse
import io
import json
import random
import shutil
import sys
import time
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atom_brain import ocr  # noqa: E402

PAGE_W, PAGE_H = 1654, 2339          # A4 at 200 dpi, the extractor's default
MARGIN = 150
FONT_PX = 30                          # about 11 pt at 200 dpi
LINE_PX = 42
GUTTER = 70
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

# Written for this tool. Plain technical prose, varied word lengths.
SENTENCES = [
    "A bounded queue protects a service by refusing work it cannot finish.",
    "When the queue is full, the caller learns early and can retry later.",
    "Retries need a budget, or a short outage turns into a long one.",
    "Each request carries a deadline that shrinks as it crosses services.",
    "A cache hides latency until the moment it is cold again.",
    "Warm the cache before traffic arrives, not after the alarms fire.",
    "Logs answer what happened; metrics answer how often it happens.",
    "Traces connect the two by following one request end to end.",
    "A slow dependency is worse than a dead one, because it holds threads.",
    "Timeouts should be shorter than the patience of the person waiting.",
    "Load shedding drops the cheapest work first and keeps the core alive.",
    "Every alert should name the action a person is expected to take.",
    "Capacity plans start from the peak hour, never from the daily mean.",
    "A rollout that cannot be rolled back is not finished being designed.",
    "Feature flags let a team ship code before it is switched on.",
    "Small batches fail in small ways and are easier to explain.",
    "The robot estimates its position from wheels, then corrects with landmarks.",
    "A Kalman filter blends a prediction with a noisy measurement.",
    "Odometry drifts slowly, so the map must pull it back now and then.",
    "Sensor fusion trusts each source in proportion to its precision.",
    "Path planning searches a grid, while control follows the chosen path.",
    "A controller with too much gain overshoots and starts to oscillate.",
    "Integral action removes steady error but can wind up during saturation.",
    "Simulation is cheap, yet only hardware shows the friction you forgot.",
]
TITLE = "Notes on Reliable Systems and Mobile Robots"


def _font(size, sans=False):
    from PIL import ImageFont
    candidates = FONT_CANDIDATES[1:] if sans else FONT_CANDIDATES
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(words, font, width):
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if font.getlength(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _text(n_sentences, seed):
    rng = random.Random(seed)
    picks = [rng.choice(SENTENCES) for _ in range(n_sentences)]
    return " ".join(picks).split()


TABLE = [
    ["Sensor", "Rate (Hz)", "Noise", "Drift"],
    ["Wheel encoder", "100", "low", "high"],
    ["IMU", "400", "medium", "medium"],
    ["GPS", "10", "high", "none"],
    ["Lidar", "20", "low", "none"],
    ["Camera", "30", "medium", "none"],
]


def _table_page(rows=30):
    """A full page of a four-column table, read row by row."""
    from PIL import Image, ImageDraw
    img = Image.new("L", (PAGE_W, PAGE_H), 255)
    draw = ImageDraw.Draw(img)
    font = _font(FONT_PX)
    truth, y = [], MARGIN
    for r in range(rows):
        row = [f"Item {r:02d}", f"{(r * 37) % 100}.{r % 10}",
               f"{(r * 13) % 7} units", "ok" if r % 3 else "check"]
        for x, cell in zip((MARGIN, 550, 900, 1250), row):
            draw.text((x, y), cell, font=font, fill=0)
        truth.append(" ".join(row))
        y += 50
    return img, " ".join(truth)


TOC_TITLES = ["Bounded queues", "Retry budgets", "Deadlines", "Caches", "Logs and metrics",
              "Tracing", "Timeouts", "Load shedding", "Alerts", "Capacity", "Rollouts",
              "Feature flags", "Localization", "Kalman filters"]


def _lines_page(kind):
    """One column of lines that repeat a leading word: a list or a table of
    contents. PSM 3 tends to cut these after the repeated word."""
    from PIL import Image, ImageDraw
    img = Image.new("L", (PAGE_W, PAGE_H), 255)
    draw = ImageDraw.Draw(img)
    font = _font(FONT_PX + 4, sans=kind == "repeat")
    truth, y = [], 200
    if kind == "repeat":
        for i in range(12):
            line = f"Scanned line number {i} of this test page."
            draw.text((MARGIN, y), line, font=font, fill=0)
            truth.append(line)
            y += 60
        return img, " ".join(truth)
    for i, title in enumerate(TOC_TITLES):
        line = (f"Chapter {i + 1} {title} {7 + 9 * i}" if kind == "toc"
                else f"Release check number {i + 1} covers {title.lower()} today.")
        if kind == "toc":
            draw.text((MARGIN, y), f"Chapter {i + 1}", font=font, fill=0)
            draw.text((MARGIN + 240, y), title, font=font, fill=0)
            draw.text((PAGE_W - MARGIN - 80, y), str(7 + 9 * i), font=font, fill=0)
        else:
            draw.text((MARGIN, y), line, font=font, fill=0)
        truth.append(line)
        y += 60
    return img, " ".join(truth)


def _page(columns=1, title=False, offset=0, seed=0, lines_per_col=26, table=False):
    """Return (PIL image, true text in reading order)."""
    from PIL import Image, ImageDraw
    img = Image.new("L", (PAGE_W, PAGE_H), 255)
    draw = ImageDraw.Draw(img)
    font = _font(FONT_PX)
    y0 = MARGIN
    truth = []
    if title:
        tfont = _font(44)
        draw.text((MARGIN, y0), TITLE, font=tfont, fill=0)
        truth.append(TITLE)
        y0 += 90
    col_w = (PAGE_W - 2 * MARGIN - (columns - 1) * GUTTER) // columns
    words = _text(60, seed)
    lines = _wrap(words, font, col_w)
    for c in range(columns):
        chunk = lines[c * lines_per_col:(c + 1) * lines_per_col]
        x = MARGIN + c * (col_w + GUTTER)
        y = y0 + (offset if c else 0)
        for i, ln in enumerate(chunk):
            if table and i == 12:
                y = _draw_table(draw, font, x, y + LINE_PX, truth)
            draw.text((x, y), ln, font=font, fill=0)
            y += LINE_PX
            truth.append(ln)
    return img, " ".join(truth)


def _draw_table(draw, font, x, y, truth):
    """A small table read row by row; returns the y below it."""
    col_x = [x, x + 330, x + 600, x + 820]
    for row in TABLE:
        for cx, cell in zip(col_x, row):
            draw.text((cx, y), cell, font=font, fill=0)
        truth.append(" ".join(row))
        y += LINE_PX
    return y + LINE_PX


def _degrade(img, skew=0.0, blur=0.0, noise=0.0, seed=0):
    from PIL import ImageFilter
    out = img
    if skew:
        out = out.rotate(skew, resample=3, expand=False, fillcolor=255)
    if blur:
        out = out.filter(ImageFilter.GaussianBlur(blur))
    if noise:
        rng = random.Random(seed)
        px = out.load()
        w, h = out.size
        for _ in range(int(w * h * noise)):
            x, y = rng.randrange(w), rng.randrange(h)
            px[x, y] = 0 if rng.random() < 0.5 else 255
    return out


PAGES = {
    "one-column":           dict(columns=1, lines_per_col=40),
    "one-column-title":     dict(columns=1, title=True, lines_per_col=38),
    "two-column":           dict(columns=2),
    "two-column-title":     dict(columns=2, title=True),
    "two-column-offset":    dict(columns=2, offset=LINE_PX // 2),
    "one-column-table":     dict(columns=1, lines_per_col=30, table=True),
    "table-page":           dict(_table=True),
    "list-page":            dict(_lines="list"),
    "toc-page":             dict(_lines="toc"),
    "repeated-start":       dict(_lines="repeat"),
    "one-column-skew-2":    dict(columns=1, lines_per_col=40, _skew=2.0),
    "two-column-skew-1":    dict(columns=2, _skew=1.0),
    "one-column-noisy":     dict(columns=1, lines_per_col=40, _blur=1.2, _noise=0.004),
    "two-column-noisy":     dict(columns=2, _blur=1.2, _noise=0.004),
}


def build(name, seed=7):
    spec = dict(PAGES[name])
    skew = spec.pop("_skew", 0.0)
    blur = spec.pop("_blur", 0.0)
    noise = spec.pop("_noise", 0.0)
    lines = spec.pop("_lines", None)
    if spec.pop("_table", False):
        img, truth = _table_page()
    elif lines:
        img, truth = _lines_page(lines)
    else:
        img, truth = _page(seed=seed, **spec)
    img = _degrade(img, skew=skew, blur=blur, noise=noise, seed=seed)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), truth


def _norm(text):
    return " ".join((text or "").split())


def cer(hyp, ref):
    """Levenshtein distance / len(ref) on whitespace-collapsed text."""
    a, b = _norm(hyp), _norm(ref)
    if not b:
        return 0.0 if not a else 1.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1] / len(b)


def methods():
    found = {"psm6": lambda png: ocr.ocr_image_bytes(png, lang="eng", psm=6)}
    if hasattr(ocr, "ocr_page_bytes"):
        found["page"] = lambda png: ocr.ocr_page_bytes(png, lang="eng")
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--only", default="", help="comma-separated page names")
    args = parser.parse_args()
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("needs Pillow (pip install pillow)", file=sys.stderr)
        return 2
    if not shutil.which("tesseract"):
        print("needs tesseract on PATH", file=sys.stderr)
        return 2
    names = [n for n in args.only.split(",") if n] or list(PAGES)
    rows = []
    print(f"{'page':<22} {'method':<9} {'CER':>7} {'sec':>6}")
    for name in names:
        png, truth = build(name)
        for method, fn in methods().items():
            start = time.perf_counter()
            text = fn(png)
            secs = time.perf_counter() - start
            score = cer(text, truth)
            rows.append({"page": name, "method": method, "cer": round(score, 4),
                         "seconds": round(secs, 2), "chars": len(_norm(truth))})
            print(f"{name:<22} {method:<9} {score:>7.2%} {secs:>6.2f}")
    for method in methods():
        mine = [r["cer"] for r in rows if r["method"] == method]
        print(f"{'mean':<22} {method:<9} {sum(mine) / len(mine):>7.2%}")
    if args.json:
        args.json.write_text(json.dumps({"tesseract": _tesseract_version(),
                                         "rows": rows}, indent=2) + "\n",
                             encoding="utf-8")
    return 0


def _tesseract_version():
    import subprocess
    try:
        out = subprocess.run(["tesseract", "--version"], capture_output=True,
                             text=True, timeout=30)
        return (out.stdout or out.stderr).splitlines()[0]
    except Exception:
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
