#!/usr/bin/env python3
"""Measure answer quality for a master skill, not just retrieval cost.

``tools/eval_taxonomy_lookup.py`` answers "how cheap is lookup". This tool
answers the next question: "does the retrieved evidence actually answer
the question". It is deterministic and dependency-free (stdlib only), so
it runs today with no model key.

Three layers, in order of strength:

1. Retrieval quality. A question is ranked with the shared path
   ``atom_brain.master.rank_notes`` (title + taxonomy + facet-topic overlap,
   concept-identity and definitional-glossary boosts, chapter/graph
   expansion) -- the same function the product query
   (``scripts/build_master.py --question``) uses, so a measured
   improvement is a product improvement. Scores are hit@k, recall@k,
   precision@k (the conciseness companion to recall), MRR over the
   full ranking, and facts covered per token of retrieved context.
2. Fact coverage. Each gold question carries a checklist of short,
   checkable facts traced to its gold notes. A fact counts as covered
   when its normalized text appears in the retrieved notes, or when all
   of its content words appear there (word-order-insensitive fallback).
   Per-question coverage is reported so a low score traces to specific
   missed facts, not to a mystery average.
3. Generation quality (harness only). ``--judge-cmd`` runs an external
   judge: it receives JSON on stdin (question, gold facts, retrieved
   notes as the candidate evidence) and must print JSON with numeric
   ``accuracy``, ``hallucination`` and ``citation`` scores in [0, 1].
   With no judge configured, generation scoring is SKIPPED and reported
   as skipped -- never invented. The candidate scored here is the
   retrieved evidence itself; scoring a generated prose answer needs a
   generator step, which is out of scope for this tool.

Usage:
  python3 tools/eval_answer_quality.py <master-dir> --qa-file tools/eval_qa_robotics.json
  python3 tools/eval_answer_quality.py <master-dir> --qa-file qa.json --k 5 --output-json results.json
  python3 tools/eval_answer_quality.py <master-dir> --qa-file qa.json --judge-cmd "my-judge --stdin"

QA file format: a JSON list of {"id", "question", "source_chapter",
"gold_notes": [note ids], "gold_facts": [short checkable phrases]}.
Gold note ids must exist in the master; missing ones are reported and
excluded from the recall denominator, never silently counted as misses.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atom_brain.master import load_master, normalize_title, rank_notes  # noqa: E402
from atom_brain.compact import query_compact_dir  # noqa: E402
from atom_brain.utils import estimate_tokens  # noqa: E402

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "what", "which", "who", "whom", "when", "where", "why", "how",
    "do", "does", "did", "can", "could", "should", "would", "will",
    "of", "in", "on", "at", "to", "for", "from", "with", "without",
    "by", "and", "or", "but", "not", "no", "than", "then", "into",
    "its", "it", "as", "that", "this", "these", "those", "there",
    "between", "under", "over", "each", "other", "both", "per",
}

URL_RE = re.compile(r"https?://\S+")
TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens of *text*."""
    return TOKEN_RE.findall(normalize_title(text or ""))


def content_words(text: str) -> list[str]:
    """Question/fact terms worth matching: tokens minus stopwords."""
    return [t for t in tokenize(text) if t not in STOPWORDS and len(t) >= 2]


def fact_covered(fact: str, evidence: str) -> bool:
    """True when *fact* is supported by *evidence* text.

    Normalized containment first (exact phrase, punctuation-insensitive);
    otherwise every content word of the fact must appear in the evidence
    (order-insensitive, so a paraphrase with the same key terms counts).
    A fact with no content words (e.g. a bare formula) only matches by
    containment, never vacuously.
    """
    norm_fact = normalize_title(fact or "")
    norm_evidence = normalize_title(evidence or "")
    if norm_fact and norm_fact in norm_evidence:
        return True
    words = content_words(fact)
    if not words:
        return False
    evidence_tokens = set(tokenize(evidence))
    return all(w in evidence_tokens for w in words)


def extract_urls(citation: str) -> set[str]:
    return set(URL_RE.findall(citation or ""))


def retrieve(notes: list[dict], taxonomy_nodes: list[dict],
             question: str, k: int, graph_edges: list[dict] | None = None,
             rank_params: dict | None = None) -> list[dict]:
    """Rank notes for *question* via the shared path (atom_brain.master).

    This is a thin wrapper over :func:`atom_brain.master.rank_notes` so the
    eval measures exactly what the product query does. The full ranking
    is returned (callers slice to top-k), preserving MRR semantics.
    ``rank_params`` passes ranker-knob overrides through (see
    ``rank_notes``); ``None`` means the shipped defaults.
    """
    return rank_notes(notes, taxonomy_nodes, graph_edges or [], question,
                      params=rank_params)


def score_question(question_item: dict, ranked: list[dict], k: int) -> dict:
    """hit@k, recall@k, precision@k, MRR, and per-fact coverage.

    Precision@k is the share of the top-k that is gold: it is the
    conciseness companion to recall (a top-k stuffed with irrelevant
    notes can have perfect recall and still make a bloated answer).
    Facts-per-token divides covered facts by the estimated tokens of
    the top-k bodies: covered facts per unit of context the answer
    must carry.
    """
    gold = [g for g in question_item.get("gold_notes", [])]
    ranked_ids = [r["note"].get("id", "") for r in ranked]
    present = [g for g in gold if g in ranked_ids]
    missing = [g for g in gold if g not in ranked_ids]
    top_k = ranked_ids[:k]
    hit = any(g in top_k for g in present)
    recall = (len([g for g in present if g in top_k]) / len(present)
              if present else 0.0)
    precision = (len([g for g in present if g in top_k]) / k) if k else 0.0
    mrr = 0.0
    for rank, note_id in enumerate(ranked_ids, start=1):
        if note_id in present:
            mrr = 1.0 / rank
            break
    top_bodies = [r["note"].get("body", "") for r in ranked[:k]]
    evidence = "\n".join(top_bodies)
    fact_results = [{"fact": fact, "covered": fact_covered(fact, evidence)}
                    for fact in question_item.get("gold_facts", [])]
    covered = sum(1 for f in fact_results if f["covered"])
    coverage = (covered / len(fact_results)) if fact_results else 0.0
    top_tokens = sum(estimate_tokens(body) for body in top_bodies)
    facts_per_token = (covered / top_tokens) if top_tokens else 0.0
    return {
        "id": question_item.get("id", ""),
        "question": question_item.get("question", ""),
        "source_chapter": question_item.get("source_chapter", ""),
        "gold_notes": gold,
        "gold_present": present,
        "gold_missing": missing,
        "retrieved_top_k": top_k,
        "hit_at_k": hit,
        "recall_at_k": recall,
        "precision_at_k": precision,
        "mrr": mrr,
        "fact_coverage": coverage,
        "facts_covered": covered,
        "facts_total": len(fact_results),
        "facts_per_token": facts_per_token,
        "top_k_tokens": top_tokens,
        "fact_results": fact_results,
    }


def run_judge(judge_cmd: str, payload: dict) -> dict:
    """Run an external judge; any failure yields a skipped result, never a score."""
    try:
        proc = subprocess.run(
            shlex.split(judge_cmd), input=json.dumps(payload),
            capture_output=True, text=True, timeout=180)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"skipped": f"judge command failed to run: {exc}"}
    if proc.returncode != 0:
        return {"skipped": f"judge exited {proc.returncode}: "
                           f"{proc.stderr.strip()[:200]}"}
    try:
        result = json.loads(proc.stdout.strip())
    except ValueError:
        return {"skipped": "judge did not print valid JSON"}
    scores = {}
    for key in ("accuracy", "hallucination", "citation"):
        value = result.get(key)
        if not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
            return {"skipped": f"judge score {key!r} missing or out of [0, 1]"}
        scores[key] = value
    return scores


def render_markdown(results: list[dict], summary: dict, k: int,
                    judge_summary: dict | None) -> str:
    lines = [f"# Answer quality (k={k})", "",
             "| question | hit@k | recall@k | precision@k | MRR | fact coverage | missed facts |",
             "|---|---|---|---|---|---|---|"]
    for r in results:
        missed = ", ".join(f["fact"] for f in r["fact_results"]
                           if not f["covered"]) or "-"
        lines.append(f"| {r['id']} | {int(r['hit_at_k'])} | "
                     f"{r['recall_at_k']:.2f} | {r['precision_at_k']:.2f} | "
                     f"{r['mrr']:.2f} | "
                     f"{r['facts_covered']}/{r['facts_total']} | {missed} |")
    lines += ["",
              f"Summary: {summary['n']} questions, "
              f"hit@{k} {summary['hit_rate']:.2f}, "
              f"mean recall@{k} {summary['mean_recall']:.2f}, "
              f"mean precision@{k} {summary['mean_precision']:.2f}, "
              f"mean MRR {summary['mean_mrr']:.2f}, "
              f"mean fact coverage {summary['mean_fact_coverage']:.2f} "
              f"({summary['facts_covered']}/{summary['facts_total']} facts), "
              f"mean facts/token {summary['mean_facts_per_token']:.4f}."]
    if summary["gold_missing"]:
        lines.append(f"Gold notes missing from master (excluded from recall): "
                     f"{', '.join(summary['gold_missing'])}.")
    if judge_summary is None:
        lines.append("Generation quality: SKIPPED -- no --judge-cmd given, "
                     "no judge score invented.")
    else:
        lines.append(f"Generation quality (judge): {judge_summary}.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure answer quality for a master skill.")
    parser.add_argument("master_dir", type=Path)
    parser.add_argument("--qa-file", type=Path, required=True)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--judge-cmd", default=None,
                        help="External judge command reading JSON on stdin")
    parser.add_argument("--rank-params", default=None,
                        help="JSON object of ranker-knob overrides passed to "
                             "atom_brain.master.rank_notes (see its docstring); "
                             "omitted means the shipped defaults")
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--compact", action="store_true",
                        help="master_dir is a compact directory (registry.json"
                             " + index.json + bodies.jsonl, see tools/"
                             "build_compact.py); rank via the inverted index"
                             " and fetch bodies only for top-k")
    args = parser.parse_args()
    rank_params = None
    if args.rank_params:
        try:
            rank_params = json.loads(args.rank_params)
        except ValueError:
            parser.error("--rank-params must be a JSON object")
        if not isinstance(rank_params, dict):
            parser.error("--rank-params must be a JSON object")
    master = None
    notes, taxonomy_nodes, graph_edges = [], [], []
    if not args.compact:
        master = load_master(args.master_dir)
        if not master:
            print(f"no master found at {args.master_dir}", file=sys.stderr)
            return 1
        notes = master.get("notes", [])
        taxonomy_nodes = master.get("taxonomy", [])
        graph_edges = master.get("graph", {}).get("edges", [])
    qa = json.loads(args.qa_file.read_text(encoding="utf-8"))
    results = []
    for item in qa:
        if args.compact:
            ranked = query_compact_dir(args.master_dir, item["question"],
                                       k=args.k, params=rank_params)
        else:
            ranked = retrieve(notes, taxonomy_nodes, item["question"], args.k,
                              graph_edges, rank_params)
        scored = score_question(item, ranked, args.k)
        if args.judge_cmd:
            evidence_notes = [
                {"id": r["note"].get("id", ""),
                 "title": r["note"].get("title", ""),
                 "body": r["note"].get("body", "")}
                for r in ranked[:args.k]]
            scored["judge"] = run_judge(args.judge_cmd, {
                "question": item["question"],
                "gold_facts": item.get("gold_facts", []),
                "candidate_notes": evidence_notes})
        results.append(scored)
    n = max(len(results), 1)
    summary = {
        "n": len(results),
        "hit_rate": sum(r["hit_at_k"] for r in results) / n,
        "mean_recall": sum(r["recall_at_k"] for r in results) / n,
        "mean_precision": sum(r["precision_at_k"] for r in results) / n,
        "mean_mrr": sum(r["mrr"] for r in results) / n,
        "mean_fact_coverage": sum(r["fact_coverage"] for r in results) / n,
        "mean_facts_per_token": sum(r["facts_per_token"] for r in results) / n,
        "facts_covered": sum(r["facts_covered"] for r in results),
        "facts_total": sum(r["facts_total"] for r in results),
        "gold_missing": sorted({g for r in results for g in r["gold_missing"]}),
    }
    judge_summary = None
    if args.judge_cmd:
        judged = [r["judge"] for r in results
                  if isinstance(r.get("judge"), dict)
                  and "skipped" not in r["judge"]]
        if judged:
            judge_summary = {
                key: round(sum(j[key] for j in judged) / len(judged), 3)
                for key in ("accuracy", "hallucination", "citation")}
        else:
            judge_summary = {"skipped": "judge produced no usable scores"}
    print(render_markdown(results, summary, args.k, judge_summary))
    if args.output_json:
        args.output_json.write_text(json.dumps(
            {"master_dir": str(args.master_dir), "k": args.k,
             "rank_params": rank_params, "compact": args.compact,
             "summary": summary, "judge_summary": judge_summary,
             "questions": results}, indent=2, ensure_ascii=False),
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
