"""Tests for tools/eval_answer_quality.py -- retrieval and fact metrics.

Deterministic, stdlib-only, synthetic notes: these tests assert the metric
logic (hit/recall/MRR/fact matching/ranking), not the Robotics master's
scores, which are measured by running the tool itself.
"""

import importlib.util
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
spec = importlib.util.spec_from_file_location(
    "eval_answer_quality", TOOLS_DIR / "eval_answer_quality.py")
eaq = importlib.util.module_from_spec(spec)
sys.modules["eval_answer_quality"] = eaq
spec.loader.exec_module(eaq)


def note(note_id, title, body, sources=()):
    return {"id": note_id, "title": title, "body": body,
            "sources": list(sources), "status": "single-source",
            "facets": {}}


NOTES = [
    note("slam", "Simultaneous Localization and Mapping (SLAM)",
         "The joint estimation problem of concurrently inferring a robot's "
         "own state and building a map from noisy sensor data.",
         ["https://example.com/slam/"]),
    note("front-end", "SLAM Front-End",
         "The layer that processes raw sensor data and extracts constraints "
         "between states and map elements.",
         ["https://example.com/slam/"]),
    note("planning", "Motion Planning",
         "The problem of finding a collision-free path for a robot.",
         ["https://example.com/planning/"]),
]

TAXONOMY = [
    {"label": "Simultaneous Localization and Mapping (SLAM)",
     "sources": ["https://example.com/slam/"]},
    {"label": "Motion Planning",
     "sources": ["https://example.com/planning/"]},
]


def test_fact_covered_containment_and_word_fallback():
    evidence = "The joint estimation problem of concurrently inferring a state."
    assert eaq.fact_covered("joint estimation problem", evidence)
    # Same key words, different order: word-set fallback covers it.
    assert eaq.fact_covered("state joint estimation", evidence)
    assert not eaq.fact_covered("loop closure drift correction", evidence)
    # A fact with no content words never matches vacuously.
    assert not eaq.fact_covered("u(t) = pi(x(t), t)",
                                "unrelated text about wheels")


def test_fact_covered_formula_matches_itself():
    evidence = "a closed-loop law u(t) = pi(x(t), t) depends on state"
    assert eaq.fact_covered("u(t) = pi(x(t), t)", evidence)


def test_retrieve_ranks_title_and_taxonomy_matches_first():
    ranked = eaq.retrieve(NOTES, TAXONOMY, "What is SLAM?", k=3)
    # Scoring semantics changed with the shared ranker: the note whose
    # concept the question names exactly ("(SLAM)" acronym) outranks a
    # note that merely mentions it, and the same-chapter front-end note
    # follows via expansion. Both SLAM notes still outrank planning.
    assert [r["note"]["id"] for r in ranked] == ["slam", "front-end",
                                                 "planning"]
    assert (ranked[0]["score"] > ranked[1]["score"]
            > ranked[2]["score"])


def test_score_question_hit_recall_mrr():
    ranked = eaq.retrieve(NOTES, TAXONOMY, "What is SLAM?", k=2)
    scored = eaq.score_question(
        {"id": "q1", "question": "What is SLAM?",
         "gold_notes": ["slam", "front-end"],
         "gold_facts": ["joint estimation problem"]},
        ranked, k=2)
    assert scored["hit_at_k"] is True
    assert scored["recall_at_k"] == 1.0
    assert scored["mrr"] == 1.0
    assert scored["fact_coverage"] == 1.0


def test_score_question_miss_and_missing_gold():
    ranked = eaq.retrieve(NOTES, TAXONOMY, "What is SLAM?", k=1)
    scored = eaq.score_question(
        {"id": "q2", "question": "What is SLAM?",
         "gold_notes": ["planning", "no-such-note"],
         "gold_facts": ["collision-free path", "absent fact here"]},
        ranked, k=1)
    assert scored["gold_missing"] == ["no-such-note"]
    # planning is present in the master but outside top-1: recall 0, MRR < 1.
    assert scored["recall_at_k"] == 0.0
    assert 0.0 < scored["mrr"] < 1.0
    assert scored["fact_coverage"] == 0.0


def test_run_judge_failure_is_skip_not_score():
    result = eaq.run_judge("definitely-not-a-real-command-xyz", {})
    assert "skipped" in result
