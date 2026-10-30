"""Tests for the shared ranked retrieval in atom_brain.master.

Synthetic notes only: these assert the ranking mechanisms (concept
identity, bounded title coverage, chapter/graph expansion, related
suggestions), not the Robotics master's measured scores.
"""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from atom_brain.master import rank_notes, suggest_related  # noqa: E402


def note(note_id, title, body, sources=(), kind="glossary", topic=""):
    return {"id": note_id, "title": title, "body": body,
            "sources": list(sources), "status": "single-source",
            "kind": kind, "facets": {"topic": topic}}


NOTES = [
    note("bayes", "Bayes Filter",
         "A recursive algorithm for maintaining a belief distribution.",
         ["https://example.com/filtering/"], topic="Filtering"),
    note("kalman", "Kalman Filter",
         "The canonical parametric filter for linear Gaussian systems.",
         ["https://example.com/filtering/"], topic="Filtering"),
    note("overview", "Filtering Overview",
         "Bayes filter overview for state estimation.",
         ["https://example.com/filtering/"], topic="Filtering"),
    note("predict", "Predict-Correct Recursion",
         "Predict and correct a belief using the filter.",
         ["https://example.com/filtering/"], topic="Filtering"),
    note("planning", "Motion Planning",
         "The problem of finding a collision-free path for a robot.",
         ["https://example.com/planning/"], topic="Planning"),
]

TAXONOMY = [
    {"label": "Filtering", "sources": ["https://example.com/filtering/"]},
    {"label": "Planning", "sources": ["https://example.com/planning/"]},
]

EDGES = [{"from": "bayes", "to": "kalman", "type": "prerequisite_of",
          "sources": []}]


def test_concept_named_in_question_ranks_first():
    ranked = rank_notes(NOTES, TAXONOMY, EDGES,
                        "How does the Bayes filter relate to Kalman?")
    assert ranked[0]["note"]["id"] == "bayes"
    assert ranked[0]["concept_boost"] == 1.0


def test_title_coverage_cannot_override_a_hit_gap():
    # "Filter Design" shares one title term; "Bayes Filter" shares two.
    # Coverage is bounded at +1, so it can never overturn the hit gap.
    notes = [
        note("design", "Filter Design", "Designing filters.",
             ["https://example.com/other/"]),
        note("bayes", "Bayes Filter", "A recursive belief filter.",
             ["https://example.com/filtering/"]),
    ]
    ranked = rank_notes(notes, TAXONOMY, EDGES,
                        "What is the Bayes filter?")
    assert ranked[0]["note"]["id"] == "bayes"


def test_chapter_expansion_lifts_same_chapter_note_only():
    ranked = rank_notes(NOTES, TAXONOMY, EDGES,
                        "How does the Bayes filter work?")
    ids = [r["note"]["id"] for r in ranked]
    predict = next(r for r in ranked if r["note"]["id"] == "predict")
    # Same chapter as the seeds and a positive base score: bonus applies.
    assert predict["expansion_bonus"] == 4.0
    # The unrelated planning note has a zero base score: no bonus, and
    # it stays below every filtering note.
    planning = next(r for r in ranked if r["note"]["id"] == "planning")
    assert planning["expansion_bonus"] == 0.0
    assert ids.index("predict") < ids.index("planning")


def test_suggestions_are_real_exclude_retrieved_and_capped():
    retrieved = ["bayes", "kalman"]
    suggestions = suggest_related(NOTES, EDGES, retrieved, limit=5)
    ids = [s["id"] for s in suggestions]
    assert len(ids) == len(set(ids)) <= 5
    assert not (set(ids) & set(retrieved))
    assert set(ids) <= {n["id"] for n in NOTES}
    # Graph neighbor (predict is not adjacent; planning shares nothing)
    # -- here the same-chapter route supplies predict.
    assert "predict" in ids


def test_area_edges_never_suggested():
    edges = [{"from": "bayes", "to": "area-filtering", "type": "part_of",
              "sources": []}]
    suggestions = suggest_related(NOTES, edges, ["bayes"], limit=5)
    assert all(s["id"] != "area-filtering" for s in suggestions)


# ---------------------------------------------------------------------------
# Per-book bound layer: --source restricts every path to one source.
# ---------------------------------------------------------------------------

from atom_brain.master import bound_view, query_notes  # noqa: E402


def test_query_notes_source_filter_composes_with_term():
    notes = [
        note("a1", "Alpha One", "Body one.",
             ["Book A, ch. 1"], topic="T"),
        note("a2", "Alpha Two", "Body two.",
             ["Book A, ch. 2"], topic="T"),
        note("b1", "Alpha Three", "Body three.",
             ["Book B, ch. 1"], topic="T"),
    ]
    assert [n["id"] for n in query_notes(notes, source="book a")] == [
        "a1", "a2"]
    assert [n["id"] for n in query_notes(notes, term="alpha",
                                         source="BOOK B")] == ["b1"]
    assert query_notes(notes, source="book c") == []


def test_rank_notes_bound_never_returns_other_book():
    notes = [
        note("a1", "Bayes Filter", "Bayes filter belief update.",
             ["Book A, ch. 1"], topic="Filtering"),
        note("b1", "Bayes Filter", "Bayes filter belief update.",
             ["Book B, ch. 1"], topic="Filtering"),
    ]
    ranked = rank_notes(notes, TAXONOMY, EDGES,
                        "What is the Bayes filter?", source="book a")
    assert [r["note"]["id"] for r in ranked] == ["a1"]


def test_suggest_related_bound_stays_inside_source():
    notes = [
        note("a1", "Bayes Filter", "Body.",
             ["https://example.com/book-a/ch1"], topic="Filtering"),
        note("a2", "Kalman Filter", "Body.",
             ["https://example.com/book-a/ch1"], topic="Filtering"),
        note("b1", "Other Filter", "Body.",
             ["https://example.com/book-b/ch9"], topic="Filtering"),
    ]
    edges = [{"from": "a1", "to": "b1", "type": "used_in", "sources": []}]
    suggestions = suggest_related(notes, edges, ["a1"], source="book-a")
    assert [s["id"] for s in suggestions] == ["a2"]
    assert "b1" not in [s["id"] for s in suggestions]


def test_bound_view_contested_shows_only_bound_definition():
    contested = {
        "id": "law", "title": "Control Law", "status": "contested",
        "kind": "glossary", "facets": {},
        "body": "- Open-loop definition. "
                "(source: Book A, ch. 2)\n"
                "- Closed-loop definition. "
                "(source: Book B, ch. 3)",
        "sources": ["Book A, ch. 2", "Book B, ch. 3"],
    }
    view = bound_view(contested, "book a")
    assert "Open-loop" in view["body"]
    assert "Closed-loop" not in view["body"]
    assert view["matching_sources"] == ["Book A, ch. 2"]
    assert view["hidden_sources"] == ["Book B, ch. 3"]


def test_bound_view_non_contested_marks_hidden_sources():
    plain = note("x", "Topic", "Body text.",
                 ["Book A, ch. 1", "Book B, ch. 1"])
    view = bound_view(plain, "book b")
    assert view["body"] == "Body text."
    assert view["matching_sources"] == ["Book B, ch. 1"]
    assert view["hidden_sources"] == ["Book A, ch. 1"]
