#!/usr/bin/env python3
"""Repeatable measure -> fix -> re-measure loop for ATOM answer quality.

One round measures the current ranker parameters on BOTH gold QA sets
(dev and holdout) at k=5, tries a small declared grid of ranker
variants on the DEV set only, validates the single best dev variant
ONCE on the holdout, and keeps it only if the holdout does not regress
on recall AND fact coverage. That single-holdout-check-per-round rule
is the overfitting guard: the holdout never selects among variants,
it only vetoes one pre-selected winner.

What the loop will NOT do, by construction:

- It never edits a gold QA file or a master note. QA hashes are
  recorded before and after every round and the round aborts if one
  changed. Data gaps (gold notes missing from the master, facet
  mis-filings) are REPORTED in the log, never repaired by editing
  notes or gold.
- It never rewrites the shipped ranker defaults in ``atom_brain/master.py``.
  A kept variant becomes the loop's working parameters, stored in
  the state file; promoting it to product defaults is a separate,
  human-approved commit.
- It never pushes anything anywhere. All outputs are local files.

State and resume: the loop keeps ``state.json`` (round number,
working parameters, stall count, stop status). A later invocation
with the same ``--state`` path resumes from the next round instead
of restarting, and appends to the same log. ``--fresh-state`` discards
an existing state file and starts at round 1.

Stop conditions (the loop ends when ANY hits):

- Targets met: recall@5 >= 0.90 AND fact coverage >= 0.95 AND
  precision@5 >= 0.60 on BOTH sets, and taxonomy tokens <= 100 avg.
- Max rounds reached (``--max-rounds``, default 5).
- Stall: 2 consecutive rounds with no holdout improvement, where
  improvement means the kept holdout (recall + coverage) strictly
  exceeds that round's own baseline holdout sum.

On a limit stop the report says so plainly: stopping on a round or
stall limit is NOT success, only the targets-met stop is.

Usage:
  python3 tools/optimize_loop.py --max-rounds 1
  python3 tools/optimize_loop.py --max-rounds 5 --targets '{"recall": 0.9}'
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MASTER = Path.home() / "workspace" / "atom" / "masters" / "robotics"
DEFAULT_LOG = Path.home() / "workspace" / "atom" / "optimize-loop-log.md"
DEFAULT_STATE = Path.home() / "workspace" / "atom" / "optimize-loop-state.json"
DEFAULT_RUNS = Path.home() / "workspace" / "atom" / "optimize-loop-runs"

DEFAULT_TARGETS = {"recall": 0.90, "coverage": 0.95, "precision": 0.60,
                   "tokens": 100.0}
EPS = 1e-9

# Declared variant grid: partial ranker-knob overrides, tried on the
# DEV set only. Values are absolute knob values merged over the
# round's working parameters, not deltas. Deliberately small and
# fixed: the loop may not invent new variants mid-round to chase a
# gold set, and it never raises k (a larger k would hide weakness
# behind more context, the thing ATOM exists to avoid).
GRID = [
    ("concept-boost-4", {"concept_boost": 4.0}),
    ("concept-boost-8", {"concept_boost": 8.0}),
    ("expansion-seeds-2", {"expansion_seeds": 2}),
    ("expansion-seeds-5", {"expansion_seeds": 5}),
    ("expansion-off", {"expansion_bonus": 0.0}),
    ("expansion-bonus-2", {"expansion_bonus": 2.0}),
    ("title-norm-off", {"title_norm": False}),
    ("concept-boost-8-seeds-5",
     {"concept_boost": 8.0, "expansion_seeds": 5}),
]

TAXONOMY_TOKEN_RE = re.compile(r"taxonomy lookup:.*avg tokens (\d+)")


def dev_score(summary: dict) -> float:
    """Selection score on one set: completeness (recall + coverage)."""
    return summary["mean_recall"] + summary["mean_fact_coverage"]


def holdout_sum(summary: dict) -> float:
    """Holdout improvement signal: recall + coverage of one set."""
    return summary["mean_recall"] + summary["mean_fact_coverage"]


def pick_winner(baseline: dict, variants: dict) -> tuple | None:
    """Best dev variant, or None when none strictly beats baseline.

    ``variants`` maps name -> (params, summary). Ranking key is
    (dev score, precision, MRR): completeness first, conciseness and
    rank quality as tiebreaks. A tie with baseline is NOT a win.
    """
    base_score = dev_score(baseline)
    best = None
    for name, (params, summary) in variants.items():
        key = (dev_score(summary), summary["mean_precision"],
               summary["mean_mrr"])
        if key[0] > base_score + EPS and (best is None or key > best[0]):
            best = (key, name, params, summary)
    if best is None:
        return None
    return best[1], best[2], best[3]


def keep_decision(winner_holdout: dict, baseline_holdout: dict) -> bool:
    """Holdout veto: keep only if neither recall nor coverage regress."""
    return (winner_holdout["mean_recall"]
            >= baseline_holdout["mean_recall"] - EPS
            and winner_holdout["mean_fact_coverage"]
            >= baseline_holdout["mean_fact_coverage"] - EPS)


def targets_met(dev: dict, holdout: dict, tokens: float | None,
                targets: dict) -> bool:
    """All target bars on BOTH sets, plus the token ceiling."""
    if tokens is None or tokens > targets["tokens"]:
        return False
    for summary in (dev, holdout):
        if (summary["mean_recall"] < targets["recall"] - EPS
                or summary["mean_fact_coverage"] < targets["coverage"] - EPS
                or summary["mean_precision"] < targets["precision"] - EPS):
            return False
    return True


def diagnose(questions: list[dict]) -> dict:
    """Classify misses from one eval run's per-question results.

    - ``data-gap``: a gold note id absent from the master entirely.
      Reported only; the loop never repairs it by editing notes/gold.
    - ``ranking-miss``: a gold note present in the master but outside
      top-k, or an uncovered fact whose question is missing at least
      one gold note from top-k (the evidence was not retrieved).
    - ``coverage-miss``: an uncovered fact on a question whose gold
      notes were ALL retrieved (the evidence was present but the fact
      wording is not supported by it).
    """
    counts = {"data-gap": 0, "ranking-miss": 0, "coverage-miss": 0}
    examples: dict[str, list[str]] = {key: [] for key in counts}
    for question in questions:
        top = set(question.get("retrieved_top_k", []))
        missing = set(question.get("gold_missing", []))
        for gold in question.get("gold_notes", []):
            if gold in missing:
                counts["data-gap"] += 1
                examples["data-gap"].append(
                    f"{question['id']}:{gold}")
            elif gold not in top:
                counts["ranking-miss"] += 1
                examples["ranking-miss"].append(
                    f"{question['id']}:{gold}")
        all_retrieved = all(
            g in top or g in missing
            for g in question.get("gold_notes", [])) and not missing
        for fact in question.get("fact_results", []):
            if not fact["covered"]:
                kind = ("coverage-miss" if all_retrieved
                        else "ranking-miss")
                counts[kind] += 1
                examples[kind].append(
                    f"{question['id']}:{fact['fact'][:60]}")
    return {"counts": counts, "examples": examples}


def sha256_file(path: Path) -> str:
    """Hex digest of a file, for proving gold files were not edited."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_eval(master_dir: Path, qa_file: Path, k: int, params: dict,
             out_path: Path) -> dict:
    """One measurement: the eval tool as a subprocess, JSON parsed."""
    cmd = [sys.executable, str(REPO_ROOT / "tools" / "eval_answer_quality.py"),
           str(master_dir), "--qa-file", str(qa_file), "--k", str(k),
           "--rank-params", json.dumps(params),
           "--output-json", str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"eval failed for {qa_file} with {params}: "
                           f"{proc.stderr.strip()[:500]}")
    data = json.loads(out_path.read_text(encoding="utf-8"))
    return {"summary": data["summary"], "questions": data["questions"]}


def parse_taxonomy_tokens(text: str) -> float | None:
    """Average taxonomy-lookup tokens from eval_taxonomy_lookup output."""
    match = TAXONOMY_TOKEN_RE.search(text)
    return float(match.group(1)) if match else None


def run_taxonomy_tokens(master_dir: Path) -> float | None:
    """Token ceiling check, via the existing taxonomy eval tool."""
    cmd = [sys.executable,
           str(REPO_ROOT / "tools" / "eval_taxonomy_lookup.py"),
           str(master_dir)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        return None
    return parse_taxonomy_tokens(proc.stdout)


def fmt_summary(summary: dict) -> str:
    """One-line human rendering of an eval summary."""
    return (f"recall {summary['mean_recall']:.2f}, "
            f"precision {summary['mean_precision']:.2f}, "
            f"coverage {summary['mean_fact_coverage']:.2f} "
            f"({summary['facts_covered']}/{summary['facts_total']}), "
            f"MRR {summary['mean_mrr']:.2f}, "
            f"facts/token {summary['mean_facts_per_token']:.4f}")


def resume_state(state: dict, max_rounds: int) -> tuple:
    """Decide whether a saved state may continue, and into what state.

    A targets-met or stall stop is terminal: only ``--fresh-state``
    restarts those. A max-rounds stop is just a cap: raising
    ``--max-rounds`` above the recorded round resumes the loop from
    the next round with its working parameters intact.
    Returns (state, can_run).
    """
    if state.get("status") != "stopped":
        return state, True
    if (state.get("stop_reason") == "max rounds reached"
            and max_rounds > state["round"]):
        state = dict(state, status="active", stop_reason=None)
        return state, True
    return state, False


def load_state(state_path: Path) -> dict:
    """Loop state, or a fresh state when none exists."""
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return {"round": 0, "current_params": {}, "prev_holdout_sum": None,
            "stall_count": 0, "status": "active", "stop_reason": None}


def save_state(state_path: Path, state: dict) -> None:
    """Persist loop state (working params are loop config, not code)."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def append_log(log_path: Path, text: str) -> None:
    """Append one round's section to the loop log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def run_round(cfg: dict, state: dict, round_no: int) -> dict:
    """Execute exactly one measure -> try -> validate -> decide round."""
    master_dir = cfg["master_dir"]
    dev_qa, hold_qa = cfg["dev_qa"], cfg["holdout_qa"]
    runs_dir = cfg["runs_dir"]
    runs_dir.mkdir(parents=True, exist_ok=True)
    hashes_before = {str(p): sha256_file(p) for p in (dev_qa, hold_qa)}
    base_params = dict(state["current_params"])

    def measure(tag: str, qa_file: Path, params: dict) -> dict:
        out = runs_dir / f"round{round_no}-{tag}.json"
        return run_eval(master_dir, qa_file, cfg["k"], params, out)

    base_dev = measure("dev-baseline", dev_qa, base_params)
    base_hold = measure("holdout-baseline", hold_qa, base_params)
    variant_results = {}
    for name, override in GRID:
        params = {**base_params, **override}
        variant_results[name] = (
            params, measure(f"dev-{name}", dev_qa, params))

    winner = pick_winner(
        base_dev["summary"],
        {name: (params, result["summary"])
         for name, (params, result) in variant_results.items()})
    winner_hold = None
    if winner is None:
        kept_params, kept_dev, kept_hold = base_params, base_dev, base_hold
        decision = "no-change (no variant beat baseline on dev)"
    else:
        name, params, _summary = winner
        winner_hold = measure("holdout-winner", hold_qa, params)
        if keep_decision(winner_hold["summary"], base_hold["summary"]):
            kept_params, kept_dev, kept_hold = params, None, winner_hold
            kept_dev = variant_results[name][1]
            decision = f"keep {name} {params}"
        else:
            kept_params, kept_dev, kept_hold = base_params, base_dev, base_hold
            decision = (f"revert {name} {params} "
                        f"(holdout regressed vs baseline)")
    tokens = run_taxonomy_tokens(master_dir)
    kept_hold_sum = holdout_sum(kept_hold["summary"])
    improved = kept_hold_sum > holdout_sum(base_hold["summary"]) + EPS
    stall_count = 0 if improved else state["stall_count"] + 1
    met = targets_met(kept_dev["summary"], kept_hold["summary"], tokens,
                      cfg["targets"])
    if met:
        stop_reason = "targets met"
    elif stall_count >= 2:
        stop_reason = ("stall: 2 consecutive rounds without "
                       "holdout improvement")
    elif round_no >= cfg["max_rounds"]:
        stop_reason = "max rounds reached"
    else:
        stop_reason = None
    hashes_after = {str(p): sha256_file(p) for p in (dev_qa, hold_qa)}
    if hashes_before != hashes_after:
        raise RuntimeError("gold QA file changed during round; aborting")

    lines = [f"## Round {round_no}", "",
             f"- params before: {base_params}",
             f"- baseline dev: {fmt_summary(base_dev['summary'])}",
             f"- baseline holdout: {fmt_summary(base_hold['summary'])}"]
    for name, (params, result) in variant_results.items():
        lines.append(f"- variant {name} {params} dev: "
                     f"{fmt_summary(result['summary'])}")
    if winner is not None:
        lines.append(f"- winner on dev: {winner[0]} {winner[1]}; "
                     f"holdout check (once): "
                     f"{fmt_summary(winner_hold['summary'])}")
    lines += [f"- decision: {decision}",
              f"- kept dev: {fmt_summary(kept_dev['summary'])}",
              f"- kept holdout: {fmt_summary(kept_hold['summary'])}",
              f"- taxonomy tokens avg: "
              f"{tokens if tokens is not None else 'unknown'}",
              f"- holdout improved vs round baseline: {improved}; "
              f"stall count: {stall_count}",
              f"- diagnosis dev: "
              f"{diagnose(kept_dev['questions'])['counts']}",
              f"- diagnosis holdout: "
              f"{diagnose(kept_hold['questions'])['counts']}",
              f"- qa sha256 dev: {hashes_before[str(dev_qa)][:16]}..., "
              f"holdout: {hashes_before[str(hold_qa)][:16]}... "
              f"(verified unchanged after round)",
              f"- stop reason: {stop_reason or 'none, continuing'}", ""]
    append_log(cfg["log"], "\n".join(lines))
    new_state = {"round": round_no, "current_params": kept_params,
                 "prev_holdout_sum": kept_hold_sum,
                 "stall_count": stall_count,
                 "status": "stopped" if stop_reason else "active",
                 "stop_reason": stop_reason}
    save_state(cfg["state"], new_state)
    return {"round": round_no, "decision": decision,
            "kept_params": kept_params,
            "kept_dev": kept_dev["summary"],
            "kept_holdout": kept_hold["summary"],
            "baseline_holdout": base_hold["summary"],
            "tokens": tokens, "targets_met": met,
            "stall_count": stall_count, "stop_reason": stop_reason}


def main(argv=None) -> int:
    """Run the loop from the saved state up to --max-rounds."""
    parser = argparse.ArgumentParser(
        description="ATOM answer-quality optimization loop.")
    parser.add_argument("--master-dir", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--dev-qa", type=Path,
                        default=REPO_ROOT / "tools" / "eval_qa_robotics.json")
    parser.add_argument("--holdout-qa", type=Path,
                        default=REPO_ROOT / "tools"
                        / "eval_qa_robotics_holdout.json")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--targets", default=None,
                        help="JSON overrides for the target bars")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--fresh-state", action="store_true",
                        help="Discard an existing state file, start round 1")
    args = parser.parse_args(argv)
    targets = dict(DEFAULT_TARGETS)
    if args.targets:
        targets.update(json.loads(args.targets))
    if args.fresh_state and args.state.exists():
        args.state.unlink()
    state = load_state(args.state)
    state, can_run = resume_state(state, args.max_rounds)
    if not can_run:
        print(f"loop already stopped: {state.get('stop_reason')} "
              f"(round {state['round']}); use --fresh-state to restart")
        return 0
    cfg = {"master_dir": args.master_dir, "dev_qa": args.dev_qa,
           "holdout_qa": args.holdout_qa, "k": args.k,
           "max_rounds": args.max_rounds, "targets": targets,
           "log": args.log, "state": args.state, "runs_dir": args.runs_dir}
    last = None
    for round_no in range(state["round"] + 1, args.max_rounds + 1):
        last = run_round(cfg, state, round_no)
        print(f"Round {round_no}: {last['decision']}; "
              f"kept dev {fmt_summary(last['kept_dev'])}; "
              f"kept holdout {fmt_summary(last['kept_holdout'])}; "
              f"stop: {last['stop_reason'] or 'none'}")
        state = load_state(args.state)
        if last["stop_reason"]:
            break
    if last is None:
        print("nothing to run (state round already at --max-rounds)")
        return 0
    if last["targets_met"]:
        print("TARGETS MET on both sets.")
    else:
        print(f"loop stopped without meeting targets: "
              f"{last['stop_reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
