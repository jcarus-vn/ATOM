"""Taxonomy lookup evaluation: with a fixture master, looking a concept up
through the taxonomy index must find the same concepts as a flat scan of
all notes, while loading fewer tokens (index amortized + one note vs the
notes scanned until the hit)."""

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from book_to_skill.master import build_master, load_master, normalize_title
from book_to_skill.utils import estimate_tokens

SOURCE = {
    "name": "Book A", "kind": "book",
    "chapters": [{"title": "Judgment"}, {"title": "Choice"}],
    "glossary": [
        {"term": f"Concept {i:02d}",
         "definition": f"Distilled definition number {i} with enough words "
                       f"to make token counting meaningful across notes. " * 3,
         "chapter": "Judgment" if i % 2 == 0 else "Choice"}
        for i in range(1, 13)
    ],
    "patterns": [],
}


def _measure(master):
    notes = master["notes"]
    taxonomy = master["taxonomy"]
    index_tokens = estimate_tokens(
        "\n".join(f"{n['label']} {' '.join(n.get('sources', []))}"
                  for n in taxonomy))
    questions = [n["title"] for n in notes]
    flat_hits = tax_hits = 0
    flat_tokens = tax_tokens = 0
    for question in questions:
        wanted = normalize_title(question)
        scanned_tokens = 0
        for note in notes:
            scanned_tokens += estimate_tokens(note.get("body", ""))
            if normalize_title(note["title"]) == wanted:
                flat_hits += 1
                flat_tokens += scanned_tokens
                break
        node = next((n for n in taxonomy
                     if normalize_title(n["label"]) == wanted), None)
        if node is not None and node.get("sources"):
            tax_hits += 1
            target = next(n for n in notes
                          if normalize_title(n["title"]) == wanted)
            tax_tokens += (index_tokens // len(questions)
                           + estimate_tokens(target.get("body", "")))
    return {
        "questions": len(questions),
        "flat_hits": flat_hits,
        "taxonomy_hits": tax_hits,
        "flat_avg_tokens": flat_tokens / max(flat_hits, 1),
        "taxonomy_avg_tokens": tax_tokens / max(tax_hits, 1),
    }


def test_taxonomy_lookup_matches_flat_with_fewer_tokens(tmp_path):
    summary = build_master("Decision Science", [SOURCE], tmp_path)
    master = load_master(Path(summary["path"]))
    result = _measure(master)
    assert result["taxonomy_hits"] == result["flat_hits"] == result["questions"]
    assert result["taxonomy_avg_tokens"] < result["flat_avg_tokens"]
    # Keep the numbers visible in test output for the docs.
    print(f"\nEVAL taxonomy lookup: {result}")
