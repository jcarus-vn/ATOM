"""Web sources: fetch a URL (or a list of URLs) into the same extraction
pipeline as a book.

A fetched page is NOT trusted blindly: the main content is separated from
navigation, advertising, footers and other boilerplate (trafilatura when
installed, otherwise a stdlib pass that drops nav/aside/footer and ad-like
subtrees, falling back to the repo's HTML extractor). Every web source
records its original URL, the page title, and the fetch time, because web
content can change after it is fetched; citations of a web source in a
skill or master skill must carry the URL plus the fetch date and a note
that the live page may since have changed.

Rights boundary, stated here and in docs/web-input.md: this tool only
fetches publicly readable pages the user has the right to read. It sends no
credentials, does not log in, and does not bypass paywalls. A fetch that
fails (network error, HTTP error, empty main content) is reported and the
source is skipped; it never aborts the whole run.
"""

from __future__ import annotations

import html.parser
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

from atom.parsers.html import extract_html_content

USER_AGENT = "atom/1.4 (+local fetch, no login, no paywall bypass)"
TIMEOUT_SECONDS = 30

MAX_BYTES = 50 * 1024 * 1024  # a page or document larger than this is refused

# Class/id WORDS that mark page chrome. Matched per word of a class or id
# ("ad-slot" -> "ad", "slot"), never as a substring: a substring test drops
# "header", "lead", "heading", "shadow", "thread", "download"... and with
# them the article itself.
_AD_WORDS = frozenset({
    "ad", "ads", "advert", "adverts", "advertisement", "advertising",
    "adsbygoogle", "banner", "cookie", "cookies", "consent", "newsletter",
    "popup", "modal", "sidebar", "related", "comment", "comments", "promo",
    "promoted", "sponsor", "sponsored", "share", "social", "subscribe",
})
_SKIP_TAGS = {"nav", "aside", "footer", "script", "style", "noscript", "form"}
# Void elements never get an end tag; pushing one on the skip stack would
# hide every later sibling of the page.
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "param", "source", "track", "wbr"}
_HTML_TYPES = {"text/html", "application/xhtml+xml", "text/plain", ""}


class WebFetchError(Exception):
    """A URL could not be fetched or yielded no usable main content."""


def is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def read_urls_file(path: str) -> list[str]:
    """Read one URL per line; blank lines and '#' comments are ignored."""
    urls = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def extract_title(raw_html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", raw_html,
                  re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
    return title


class _MainContentParser(html.parser.HTMLParser):
    """Stdlib main-content pass: drop nav/aside/footer/ad-like subtrees.

    If an <article> or <main> element exists, only its text is kept;
    otherwise the whole body minus the skipped subtrees is kept.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_stack: list[str] = []
        self._main_depth = 0
        self._main_parts: list[str] = []
        self._parts: list[str] = []

    def _ad_like(self, attrs) -> bool:
        for key, value in attrs:
            if key in ("class", "id") and value:
                words = re.split(r"[\s_\-]+", value.lower())
                if any(w in _AD_WORDS for w in words):
                    return True
        return False

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            return
        if tag in ("article", "main"):
            self._main_depth += 1
        if tag in _SKIP_TAGS or self._ad_like(attrs):
            self._skip_stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        return  # self-closing: no content, nothing to skip

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return
        if tag in ("article", "main") and self._main_depth:
            self._main_depth -= 1
        if self._skip_stack and self._skip_stack[-1] == tag:
            self._skip_stack.pop()
        elif tag in self._skip_stack:
            # Malformed nesting: drop everything up to the matching opener.
            while self._skip_stack and self._skip_stack[-1] != tag:
                self._skip_stack.pop()
            if self._skip_stack:
                self._skip_stack.pop()

    def handle_data(self, data):
        if self._skip_stack:
            return
        text = data.strip()
        if not text:
            return
        self._parts.append(text)
        if self._main_depth:
            self._main_parts.append(text)

    def get_text(self) -> str:
        if self._main_parts:
            return "\n".join(self._main_parts)
        return "\n".join(self._parts)


def strip_boilerplate_all(raw_html: str) -> str:
    """All visible text (script/style removed), to judge how much the
    main-content pass dropped."""
    from atom.parsers.html import _HTMLTextExtractor
    parser = _HTMLTextExtractor()
    try:
        parser.feed(raw_html)
    except Exception:
        return ""
    return parser.get_text()


def strip_boilerplate(raw_html: str) -> str:
    """Best-effort main-content extraction without trafilatura."""
    parser = _MainContentParser()
    try:
        parser.feed(raw_html)
    except Exception:
        return ""
    return parser.get_text()


def _download(url: str, timeout: int) -> tuple[bytes, str, str]:
    """GET *url*: (body, content type, charset). Refuses bodies over MAX_BYTES."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            try:
                ctype = response.headers.get_content_type() or ""
                charset = response.headers.get_content_charset() or "utf-8"
            except Exception:
                ctype, charset = "", "utf-8"
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        raise WebFetchError(f"could not fetch {url}: {exc}") from exc
    if len(raw) > MAX_BYTES:
        raise WebFetchError(
            f"{url} is larger than {MAX_BYTES // (1024 * 1024)} MB; download it "
            "and pass the file path instead")
    if raw[:4] == b"%PDF":
        ctype = "application/pdf"
    return raw, ctype.lower(), charset


def _decode(raw: bytes, charset: str) -> str:
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def fetch_html(url: str, timeout: int = TIMEOUT_SECONDS) -> str:
    """Download one URL as text, raising WebFetchError on any failure."""
    raw, ctype, charset = _download(url, timeout)
    if ctype not in _HTML_TYPES:
        raise WebFetchError(f"{url} is {ctype}, not a web page")
    return _decode(raw, charset)


def fetch_web_source(url: str) -> dict:
    """Fetch one URL and return a source dict for the extraction pipeline.

    Keys: url, title, fetched_at (UTC ISO), text, format ("web"), method.
    Raises WebFetchError when the fetch fails or no main content remains.
    """
    raw, ctype, charset = _download(url, TIMEOUT_SECONDS)
    fetched_at = datetime.now(timezone.utc).isoformat()
    if ctype == "application/pdf":
        # A document behind a URL goes through the PDF extractor, not the
        # HTML path (which turned the PDF's bytes into "text"). The caller
        # extracts ``document_path`` and removes it afterwards.
        fd, path = tempfile.mkstemp(prefix="atom_web_", suffix=".pdf")
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        name = url.rstrip("/").rsplit("/", 1)[-1] or url
        return {"url": url, "title": name, "fetched_at": fetched_at,
                "text": "", "format": "pdf", "method": "web-pdf",
                "document_path": path}
    if ctype not in _HTML_TYPES:
        raise WebFetchError(
            f"{url} is {ctype}, not a web page or PDF; download it and pass "
            "the file path instead")
    raw_html = _decode(raw, charset)
    title = extract_title(raw_html)
    text = strip_boilerplate(raw_html)
    method = "web-boilerplate-strip"
    if len(text.strip()) < 50:
        # The stdlib pass was too aggressive (or the page is JS-rendered):
        # fall back to the repo's HTML chain (trafilatura, then bs4).
        fallback = extract_html_content(raw_html) or ""
        if len(fallback.strip()) > len(text.strip()):
            text, method = fallback, "web-html-fallback"
    if not text.strip():
        raise WebFetchError(f"no usable main content extracted from {url}")
    visible = strip_boilerplate_all(raw_html)
    if visible and len(text.split()) < 0.2 * len(visible.split()):
        print(f"  [warn] {url}: kept {len(text.split())} of "
              f"{len(visible.split())} visible words as main content; "
              "check the extraction", file=sys.stderr)
    return {
        "url": url,
        "title": title,
        "fetched_at": fetched_at,
        "text": text,
        "format": "web",
        "method": method,
    }


def web_citation(source: dict) -> str:
    """Citation string for a web source: URL + fetch date + change warning."""
    fetched = (source.get("fetched_at") or "")[:10]
    base = f"{source.get('title') or source.get('url', '')} ({source.get('url', '')}"
    if fetched:
        base += f", fetched {fetched}"
    base += ") - web content may have changed since fetch"
    return base
