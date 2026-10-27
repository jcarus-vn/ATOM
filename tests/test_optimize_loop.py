"""Tests for tools/optimize_loop.py -- round decisions, not live scores.

Deterministic, stdlib-only, synthetic summaries: these tests assert
the loop's decision logic (variant selection, holdout veto, stop
conditions, miss diagnosis, token parsing). Real round numbers are
measured by running the loop itself against the Robotics master.
"""

import importlib.util
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
spec = importlib.util.spec_from_file_location(
    "optimize_loop", TOOLS_DIR / "optimize_loop.py")
ol = importlib.util.module_from_spec(spec)
sys.modules["optimize_loop"] = ol
spec.loader.exec_module(ol)


def summary(recall, precision, coverage, mrr=0.8):
    """Minimal eval summary dict with the fields the loop reads."""
    return {"mean_recall": recall, "mean_precision": precision,
            "mean_fact_coverage": coverage, "mean_mrr": mrr,
            "facts_covered": 0, "facts_total": 0,
            "mean_facts_per_token": 0.0}


def test_dev_score_is_recall_plus_coverage():
    assert ol.dev_score(summary(0.8, 0.5, 0.9)) == 0.8 + 0.9


def test_pick_winner_requires_strict_dev_improvement():
    baseline = summary(0.8, 0.5, 0.85)
    tied = {"v": ({"concept_boost": 8.0}, summary(0.8, 0.9, 0.85))}
    assert ol.pick_winner(baseline, tied) is None
    better = {"v": ({"concept_boost": 8.0}, summary(0.81, 0.5, 0.85))}
    name, params, _ = ol.pick_winner(baseline, better)
    assert name == "v" and params == {"concept_boost": 8.0}


def test_pick_winner_tiebreak_is_precision_then_mrr():
    baseline = summary(0.8, 0.5, 0.85)
    variants = {
        "low": ({"a": 1}, summary(0.85, 0.4, 0.85, mrr=0.9)),
        "high": ({"a": 2}, summary(0.85, 0.6, 0.85, mrr=0.5)),
    }
    name, _, _ = ol.pick_winner(baseline, variants)
    assert name == "high"


def test_keep_decision_vetoes_any_holdout_regression():
    baseline = summary(0.8, 0.5, 0.85)
    assert ol.keep_decision(summary(0.8, 0.5, 0.85), baseline) is True
    assert ol.keep_decision(summary(0.79, 0.9, 0.9), baseline) is False
    assert ol.keep_decision(summary(0.9, 0.9, 0.84), baseline) is False


def test_targets_met_needs_both_sets_and_token_ceiling():
    targets = {"recall": 0.9, "coverage": 0.95, "precision": 0.6,
               "tokens": 100.0}
    good = summary(0.92, 0.65, 0.96)
    assert ol.targets_met(good, good, 71.0, targets) is True
    assert ol.targets_met(good, good, None, targets) is False
    assert ol.targets_met(good, good, 101.0, targets) is False
    weak = summary(0.89, 0.65, 0.96)
    assert ol.targets_met(weak, good, 71.0, targets) is False


def test_diagnose_classes():
    questions = [
        {"id": "q1", "gold_notes": ["gone", "missed", "found"],
         "gold_missing": ["gone"], "retrieved_top_k": ["found"],
         "fact_results": [{"fact": "uncovered despite retrieval",
                           "covered": False}]},
        {"id": "q2", "gold_notes": ["there"],
         "gold_missing": [], "retrieved_top_k": ["there"],
         "fact_results": [{"fact": "present but unsupported",
                           "covered": False}]},
    ]
    result = ol.diagnose(questions)
    assert result["counts"]["data-gap"] == 1
    assert result["counts"]["ranking-miss"] == 2  # missed note + q1 fact
    assert result["counts"]["coverage-miss"] == 1  # q2 fact only


def test_parse_taxonomy_tokens():
    text = ("questions: 118\nflat lookup: avg tokens 2201\n"
            "taxonomy lookup: hit rate 118/118 (100%), avg tokens 71 "
            "(index amortized + 1 note)\n")
    assert ol.parse_taxonomy_tokens(text) == 71.0
    assert ol.parse_taxonomy_tokens("no match here") is None


def test_grid_is_small_declared_and_keyed():
    assert len(ol.GRID) == 8
    allowed = {"concept_boost", "glossary_bonus", "expansion_bonus",
               "expansion_seeds", "title_norm"}
    for name, override in ol.GRID:
        assert isinstance(name, str) and name
        assert override and set(override) <= allowed


def test_resume_state_terminal_vs_max_rounds_cap():
    stopped_targets = {"round": 2, "status": "stopped",
                       "stop_reason": "targets met"}
    _, can_run = ol.resume_state(dict(stopped_targets), 5)
    assert can_run is False
    stopped_cap = {"round": 1, "status": "stopped",
                   "stop_reason": "max rounds reached"}
    state, can_run = ol.resume_state(dict(stopped_cap), 5)
    assert can_run is True and state["status"] == "active"
    _, can_run = ol.resume_state(dict(stopped_cap), 1)
    assert can_run is False
    active = {"round": 1, "status": "active", "stop_reason": None}
    _, can_run = ol.resume_state(dict(active), 5)
    assert can_run is True
