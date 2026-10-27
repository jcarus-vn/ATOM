"""Flexible chapter detection: Roman numerals, bare titles, Vietnamese
chapter words, ToC anchoring, and the section fallback."""

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from book_to_skill.chapter_detection import detect_chapters
from book_to_skill.utils import detect_structure

BODY = "prose carrying the section's actual content. " * 40


def _book(*sections: str) -> str:
    return "\n\n".join(sections)


class TestRomanNumerals:
    def test_roman_headings_without_chapter_word(self):
        text = _book(f"I: Loomings\n{BODY}", f"II. The Carpet-Bag\n{BODY}",
                     f"III: The Spouter-Inn\n{BODY}")
        result = detect_chapters(text)
        assert [c["number"] for c in result["chapters"]] == [1, 2, 3]
        assert all(c["strategy"] == "explicit" for c in result["chapters"])

    def test_non_canonical_roman_is_not_a_chapter(self):
        text = _book(f"IIII: Not Roman\n{BODY}", f"V: Real\n{BODY}")
        result = detect_chapters(text, mode="explicit")
        assert [c["number"] for c in result["chapters"]] == [5]


class TestBareTitles:
    def test_moby_dick_style_bare_titles(self):
        text = _book(f"Loomings\n\n{BODY}", f"The Carpet-Bag\n\n{BODY}",
                     f"The Spouter-Inn\n\n{BODY}")
        result = detect_chapters(text)
        assert [c["title"] for c in result["chapters"]] == [
            "loomings", "the carpet-bag", "the spouter-inn"]
        assert all(c["strategy"] == "heuristic" for c in result["chapters"])

    def test_wrapped_prose_sentence_is_not_a_title(self):
        text = f"The cat sat on the mat\n\n{BODY}\n\nMore prose here today\n\n{BODY}"
        assert detect_chapters(text)["chapters_count"] == 0


class TestVietnamese:
    def test_chuong_phan_bai_with_and_without_diacritics(self):
        text = _book(f"Chương 1: Mở đầu\n{BODY}", f"Phan 2: Than the\n{BODY}",
                     f"Bai 3: Ket luan\n{BODY}")
        result = detect_chapters(text, mode="explicit")
        assert [c["number"] for c in result["chapters"]] == [1, 2, 3]


class TestTocBased:
    def test_toc_entries_anchor_in_body(self):
        toc = ("Table of Contents\nLoomings .......... 1\n"
               "The Carpet-Bag .... 5\nThe Spouter-Inn ... 9\n\n")
        body = _book(f"Loomings\n\n{BODY}", f"The Carpet-Bag\n\n{BODY}",
                     f"The Spouter-Inn\n\n{BODY}")
        result = detect_chapters(toc + body, mode="toc")
        assert [c["title"] for c in result["chapters"]] == [
            "loomings", "the carpet-bag", "the spouter-inn"]
        assert result["method"] == "toc"


class TestFallbackAndModes:
    def test_fallback_splits_on_section_titles(self):
        text = _book(f"## Setup\n{BODY}", f"## Usage\n{BODY}",
                     f"## Reference\n{BODY}")
        result = detect_chapters(text)
        assert result["fallback"] is True or result["method"] in ("flexible", "fallback")
        assert result["chapters_count"] == 3

    def test_explicit_mode_keeps_old_behaviour(self):
        text = _book(f"Loomings\n\n{BODY}", f"The Carpet-Bag\n\n{BODY}")
        # Bare titles are a heuristic strategy; explicit mode must not see them.
        assert detect_chapters(text, mode="explicit")["chapters_count"] == 0

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError):
            detect_chapters("text", mode="nope")

    def test_detect_structure_promotes_flexible_count(self):
        text = _book(f"Loomings\n\n{BODY}", f"The Carpet-Bag\n\n{BODY}",
                     f"The Spouter-Inn\n\n{BODY}")
        result = detect_structure(text)
        assert result["chapters_detected"] == 3
        assert result["chapters_method"] == "flexible"
        assert len(result["chapter_details"]) == 3
        assert result["chapter_detection"]["strategies_used"]

    def test_numbered_list_items_still_not_chapters(self):
        text = ("1. Compared to characters, tokens allow the model to break words into\n"
                "2. Because there are fewer unique tokens than unique words, this reduces\n"
                "3. Tokens also help the model process unknown words, for instance a word\n")
        assert detect_structure(text)["chapters_detected"] == 0
