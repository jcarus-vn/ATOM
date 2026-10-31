#!/usr/bin/env python3
"""Measure retrieval for questions asked in another language than the notes.

Runs the gold questions in English and in each translated set against one
master, with and without the master's other names (aliases.json), and with
and without a translation fused in (``--also``), and prints recall@k,
precision@k and MRR per set and mode.

"+ translation" fuses each translated question with the English gold
question, i.e. a perfect translation: an upper bound for what an agent's
own translation brings.

Usage:
  python3 tools/eval_multilingual.py <master_dir> \
      [--questions tools/eval_qa_robotics.json tools/eval_qa_robotics_holdout.json] \
      [--translations tools/eval_qa_robotics_vi.json] [--k 5] [--json out.json]

The translations file maps question id -> {set name: question text}; see
tools/eval_qa_robotics_vi.json. "en" is always the questions' own text.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from atom_brain.master import load_master, rank_notes_multi  # noqa: E402
from eval_answer_quality import score_question  # noqa: E402

DEFAULT_QUESTIONS = [ROOT / "tools" / "eval_qa_robotics.json",
                     ROOT / "tools" / "eval_qa_robotics_holdout.json"]
DEFAULT_TRANSLATIONS = ROOT / "tools" / "eval_qa_robotics_vi.json"


def _without_names(master: dict) -> dict:
    notes = [{k: v for k, v in n.items() if k not in ("aliases", "topic_aliases")}
             for n in master.get("notes", [])]
    taxonomy = [{k: v for k, v in n.items() if k != "aliases"}
                for n in master.get("taxonomy", [])]
    return dict(master, notes=notes, taxonomy=taxonomy)


def question_sets(questions: list[dict], translations: dict) -> dict[str, dict]:
    sets: dict[str, dict] = {"en": {q["id"]: q["question"] for q in questions}}
    for qid, variants in translations.items():
        for name, text in variants.items():
            sets.setdefault(name, {})[qid] = text
    return sets


def _lexical(master: dict, wordings: list[str]) -> list[dict]:
    return rank_notes_multi(master.get("notes", []), master.get("taxonomy", []),
                            master.get("graph", {}).get("edges", []), wordings)


def evaluate(master: dict, questions: list[dict], sets: dict[str, dict],
             k: int, ranker=None, translate: bool = False) -> dict:
    """{set: {"recall", "precision", "mrr", "n", "per_question"}}.

    ``ranker(master, wordings)`` ranks a list of wordings of one question;
    with ``translate`` the English gold question is added to every
    non-English wording."""
    ranker = ranker or _lexical
    english = {q["id"]: q["question"] for q in questions}
    out: dict[str, dict] = {}
    for name, texts in sets.items():
        scores = []
        for q in questions:
            if q["id"] not in texts:
                continue
            item = dict(q, question=texts[q["id"]])
            wordings = [texts[q["id"]]]
            if translate and name != "en":
                wordings.append(english[q["id"]])
            scores.append(score_question(item, ranker(master, wordings), k))
        if not scores:
            continue
        n = len(scores)
        out[name] = {
            "n": n,
            "recall": sum(s["recall_at_k"] for s in scores) / n,
            "precision": sum(s["precision_at_k"] for s in scores) / n,
            "mrr": sum(s["mrr"] for s in scores) / n,
            "per_question": {s["id"]: s["recall_at_k"] for s in scores},
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("master_dir", type=Path)
    parser.add_argument("--questions", type=Path, nargs="+", default=DEFAULT_QUESTIONS)
    parser.add_argument("--translations", type=Path, default=DEFAULT_TRANSLATIONS)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    master = load_master(args.master_dir)
    if not master or not master.get("notes"):
        print(f"no master at {args.master_dir}", file=sys.stderr)
        return 1
    questions = []
    for path in args.questions:
        questions += json.loads(path.read_text(encoding="utf-8"))
    data = json.loads(args.translations.read_text(encoding="utf-8"))
    sets = question_sets(questions, data.get("questions", data))
    has_names = any(n.get("aliases") or n.get("topic_aliases")
                    for n in master["notes"])
    plain = _without_names(master)
    modes = {"no names": (plain, False)}
    if has_names:
        modes["names"] = (master, False)
    else:
        print("note: this master has no aliases.json; 'names' is not measured",
              file=sys.stderr)
    modes["+ translation"] = (plain, True)
    if has_names:
        modes["names + transl."] = (master, True)
    results = {mode: evaluate(m, questions, sets, args.k, translate=t)
               for mode, (m, t) in modes.items()}
    print(f"recall@{args.k} / precision@{args.k} / MRR, {len(questions)} questions")
    print(f"{'set':10} " + " ".join(f"{mode:>18}" for mode in results))
    for name in sets:
        cells = []
        for mode in results:
            r = results[mode].get(name)
            cells.append(f"{r['recall']:.2f} / {r['precision']:.2f} / {r['mrr']:.2f}"
                         if r else "-")
        print(f"{name:10} " + " ".join(f"{c:>18}" for c in cells))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
