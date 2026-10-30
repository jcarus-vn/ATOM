#!/usr/bin/env python3
"""Measure taxonomy-index lookup vs flat lookup on a master skill.

For each concept in the master (or each question in a JSON list of concept
titles), compare two ways of answering "where is this concept defined, and
with what sources":

- flat: scan notes in file order until the concept is found. Cost is the
  number of notes scanned and their estimated tokens.
- taxonomy: follow the taxonomy index straight to the concept node, then
  load only that note. Cost is the taxonomy index size (amortized per
  question) plus that one note's tokens.

Usage:
  python3 tools/eval_taxonomy_lookup.py <master-dir> [--questions q.json]

Prints hit rate for both methods and the average notes/tokens loaded.
A taxonomy that cannot find a concept the flat scan finds counts as a
miss for the taxonomy method; the script never claims a win it did not
measure.
"""

import argparse
import json
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atom_brain.master import load_master, normalize_title  # noqa: E402
from atom_brain.utils import estimate_tokens  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("master_dir", type=Path)
    parser.add_argument("--questions", type=Path, default=None,
                        help="JSON list of concept titles to look up")
    args = parser.parse_args()
    master = load_master(args.master_dir)
    if not master:
        print(f"no master found at {args.master_dir}", file=sys.stderr)
        return 1
    notes = master.get("notes", [])
    if args.questions:
        questions = json.loads(args.questions.read_text(encoding="utf-8"))
    else:
        questions = [n["title"] for n in notes]
    taxonomy_nodes = master.get("taxonomy", [])
    # Cost the index the agent actually loads: the rendered taxonomy.md
    # when it exists, else labels + sources as older masters stored them.
    taxonomy_md = args.master_dir / "taxonomy.md"
    if taxonomy_md.exists():
        taxonomy_index_tokens = estimate_tokens(
            taxonomy_md.read_text(encoding="utf-8"))
    else:
        taxonomy_index_tokens = estimate_tokens(
            "\n".join(f"{n['label']} {' '.join(n.get('sources', []))}"
                      for n in taxonomy_nodes))
    flat_hits = taxonomy_hits = 0
    flat_notes = flat_tokens = tax_tokens = 0
    for question in questions:
        wanted = normalize_title(question)
        # Flat scan.
        found = None
        scanned = 0
        scanned_tokens = 0
        for note in notes:
            scanned += 1
            scanned_tokens += estimate_tokens(note.get("body", ""))
            if normalize_title(note["title"]) == wanted:
                found = note
                break
        if found is not None:
            flat_hits += 1
            flat_notes += scanned
            flat_tokens += scanned_tokens
        # Taxonomy lookup: node exists and points at a note with sources.
        node = next((n for n in taxonomy_nodes
                     if normalize_title(n["label"]) == wanted), None)
        if node is not None and node.get("sources"):
            taxonomy_hits += 1
            target = next((n for n in notes
                           if normalize_title(n["title"]) == wanted), None)
            tax_tokens += (taxonomy_index_tokens // max(len(questions), 1)
                           + (estimate_tokens(target.get("body", ""))
                              if target else 0))
    n = max(len(questions), 1)
    print(f"questions: {len(questions)}")
    print(f"flat lookup:     hit rate {flat_hits}/{len(questions)} "
          f"({flat_hits / n:.0%}), avg notes scanned "
          f"{flat_notes / max(flat_hits, 1):.1f}, avg tokens "
          f"{flat_tokens / max(flat_hits, 1):.0f}")
    print(f"taxonomy lookup: hit rate {taxonomy_hits}/{len(questions)} "
          f"({taxonomy_hits / n:.0%}), avg tokens "
          f"{tax_tokens / max(taxonomy_hits, 1):.0f} "
          f"(index amortized + 1 note)")
    # The amortized figure assumes the index stays in context for every
    # question of the run. A fresh session pays the whole index once.
    avg_note = (tax_tokens / max(taxonomy_hits, 1)
                - taxonomy_index_tokens // max(len(questions), 1))
    print(f"taxonomy lookup, first question of a session (index not amortized): "
          f"{taxonomy_index_tokens + avg_note:.0f} tokens "
          f"(index {taxonomy_index_tokens} + 1 note)")
    print("token counts: words/0.75 heuristic; real BPE counts run higher on "
          "URLs, code and non-English text")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
