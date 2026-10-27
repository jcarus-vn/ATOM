"""ATOM ships English-only docs: no translated READMEs are kept.

The old project carried Russian and Chinese README translations that
drifted from the English source and still pointed at the old repository.
ATOM drops them outright rather than shipping stale translations: this
test pins that decision so a re-added translation file fails loudly
instead of silently going stale again.
"""

from pathlib import Path

ROOT = Path(__file__).parents[1]

REMOVED_TRANSLATIONS = ("README.ru.md", "README.zh-CN.md")


def test_no_translated_readmes_remain():
    for name in REMOVED_TRANSLATIONS:
        assert not (ROOT / name).exists(), (
            f"{name} is back in the tree; ATOM docs are English-only — "
            "do not re-add a translation without a sync policy"
        )


def test_english_readme_has_no_translation_links():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in REMOVED_TRANSLATIONS:
        assert name not in text
    assert "Русский" not in text
    assert "简体中文" not in text
