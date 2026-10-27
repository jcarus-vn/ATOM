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
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from book_to_skill.parsers.html import extract_html_content

USER_AGENT = "book-to-skill/1.4 (+local fetch, no login, no paywall bypass)"
TIMEOUT_SECONDS = 30

_AD_LIKE = re.compile(
    r"(ad|advert|banner|cookie|newsletter|popup|sidebar|related|comment|promo)",
    re.IGNORECASE,
)
_SKIP_TAGS = {"nav", "aside", "footer", "script", "style", "noscript", "form"}


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
        self._in_main = False
        self._main_parts: list[str] = []
        self._parts: list[str] = []

    def _ad_like(self, attrs) -> bool:
        for key, value in attrs:
            if key in ("class", "id") and value and _AD_LIKE.search(value):
                return True
        return False

    def handle_starttag(self, tag, attrs):
        if tag in ("article", "main"):
            self._in_main = True
        if tag in _SKIP_TAGS or self._ad_like(attrs):
            self._skip_stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ("article", "main"):
            self._in_main = False
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
        if self._in_main:
            self._main_parts.append(text)

    def get_text(self) -> str:
        if self._main_parts:
            return "\n".join(self._main_parts)
        return "\n".join(self._parts)


def strip_boilerplate(raw_html: str) -> str:
    """Best-effort main-content extraction without trafilatura."""
    parser = _MainContentParser()
    try:
        parser.feed(raw_html)
    except Exception:
        return ""
    return parser.get_text()


def fetch_html(url: str, timeout: int = TIMEOUT_SECONDS) -> str:
    """Download one URL as text, raising WebFetchError on any failure."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        raise WebFetchError(f"could not fetch {url}: {exc}") from exc
    charset = "utf-8"
    content_type = ""
    try:
        content_type = response.headers.get_content_type()  # noqa: F841
        charset = response.headers.get_content_charset() or "utf-8"
    except Exception:
        pass
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def fetch_web_source(url: str) -> dict:
    """Fetch one URL and return a source dict for the extraction pipeline.

    Keys: url, title, fetched_at (UTC ISO), text, format ("web"), method.
    Raises WebFetchError when the fetch fails or no main content remains.
    """
    raw_html = fetch_html(url)
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
    return {
        "url": url,
        "title": title,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
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
