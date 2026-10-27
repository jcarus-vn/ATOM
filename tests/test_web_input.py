"""Web input: boilerplate stripping, fetch metadata, graceful failure,
and mixing a web source into a master skill."""

import sys
import urllib.error
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from book_to_skill import web_input
from book_to_skill.master import build_master, load_master, query_across_masters

HTML = """<html><head><title>Decision Lab Notes</title></head><body>
<nav><ul><li>Home</li><li>About</li></ul></nav>
<aside class="sidebar-ads">Buy now! Special offer!</aside>
<article><h1>Anchoring in practice</h1>
<p>People rely too heavily on the first number they see.</p>
<p>The first offer sets the range for everything after it.</p></article>
<footer>Copyright 2026, contact us, privacy policy</footer>
</body></html>"""


class FakeHeaders:
    def get_content_type(self):
        return "text/html"

    def get_content_charset(self):
        return "utf-8"


class FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")
        self.headers = FakeHeaders()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(monkeypatch, body=HTML):
    monkeypatch.setattr(
        web_input.urllib.request, "urlopen",
        lambda req, timeout=30: FakeResponse(body))


class TestWebFetch:
    def test_main_content_extracted_boilerplate_dropped(self, monkeypatch):
        _fake_urlopen(monkeypatch)
        source = web_input.fetch_web_source("https://example.test/anchoring")
        assert source["title"] == "Decision Lab Notes"
        assert source["url"] == "https://example.test/anchoring"
        assert source["fetched_at"]
        assert "People rely too heavily" in source["text"]
        assert "Buy now" not in source["text"]
        assert "Home" not in source["text"]

    def test_failed_fetch_raises_and_does_not_kill_caller(self, monkeypatch):
        def boom(req, timeout=30):
            raise urllib.error.URLError("network down")

        monkeypatch.setattr(web_input.urllib.request, "urlopen", boom)
        with pytest.raises(web_input.WebFetchError):
            web_input.fetch_web_source("https://example.test/down")

    def test_urls_file_parsing(self, tmp_path):
        path = tmp_path / "links.urls"
        path.write_text("# comment\nhttps://a.test/1\n\nhttps://b.test/2\n")
        assert web_input.read_urls_file(str(path)) == [
            "https://a.test/1", "https://b.test/2"]


class TestWebInMaster:
    def test_book_plus_web_mix_with_traceable_citation(self, tmp_path):
        book = {
            "name": "Thinking Fast and Slow", "kind": "book",
            "chapters": [{"title": "Judgment under uncertainty"}],
            "glossary": [{"term": "Anchoring",
                          "definition": "People rely too heavily on the first number they see.",
                          "chapter": "Judgment under uncertainty"}],
            "patterns": [],
        }
        web = {
            "name": "Decision Lab Notes", "kind": "web",
            "url": "https://example.test/anchoring",
            "fetched_at": "2026-10-27T01:00:00+00:00",
            "glossary": [{"term": "Anchoring",
                          "definition": "People rely too heavily on the first number they see."}],
            "patterns": [],
        }
        summary = build_master("Decision Science", [book, web], tmp_path)
        master = load_master(Path(summary["path"]))
        anchoring = next(n for n in master["notes"] if n["title"] == "Anchoring")
        assert anchoring["status"] == "consensus"
        assert anchoring["facets"]["source_type"] == "mixed"
        assert any("https://example.test/anchoring" in s
                   and "2026-10-27" in s for s in anchoring["sources"])
        assert any("may have changed" in s for s in anchoring["sources"])
        assert query_across_masters(tmp_path, status="consensus")
