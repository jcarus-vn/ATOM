#!/usr/bin/env python3
"""Build a compact master directory from a legacy master directory.

Reads ``master.json`` from the legacy dir (notes/taxonomy/graph as
stored there, not the notes/*.md re-parse) and writes:

- ``registry.json``: the compact master (sources factored to ids,
  facets coded, taxonomy skeleton, edges as id pairs, conflicts).
- ``index.json``: vocabulary + inverted index + per-note ranking
  metadata (no bodies).
- ``bodies.jsonl``: one JSON object per line (id + body, plus defs
  only when they differ from [body]).
- ``SKILL.md``: copied verbatim when present.

The compact directory is a query artifact: it drops the raw source
inputs (glossary/patterns items) that live in the per-source skill
directories. See atom_brain/compact.py for the format contract.

Usage:
  python3 tools/build_compact.py <legacy_master_dir> <compact_out_dir>
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

if __name__ == "__main__":
    # No bytecode writes: this tool is an entry point, see
    # tests/test_no_bytecode_pollution.py. The guard must sit ahead of
    # the package import below.
    sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atom_brain.compact import build_bodies, build_index, compact_master  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a compact master directory.")
    parser.add_argument("legacy_dir", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()
    master_path = args.legacy_dir / "master.json"
    if not master_path.exists():
        print(f"no master.json in {args.legacy_dir}", file=sys.stderr)
        return 1
    master = json.loads(master_path.read_text(encoding="utf-8"))
    compact = compact_master(master)
    bodies = {n["id"]: n.get("body", "") for n in master.get("notes", [])}
    index = build_index(compact, bodies)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    # Machine artifacts use compact separators: they are never human-read
    # (jq still works), and pretty-printing inflates the integer-heavy
    # index by ~2x in bytes. Note: the repo's estimate_tokens is a
    # whitespace metric, so it UNDERCOUNTS dense JSON badly; for these
    # files bytes are the honest measure (see the optimization doc).
    (args.out_dir / "registry.json").write_text(
        json.dumps(compact, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    (args.out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    with (args.out_dir / "bodies.jsonl").open("w", encoding="utf-8") as handle:
        for row in build_bodies(compact, bodies):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    skill_md = args.legacy_dir / "SKILL.md"
    if skill_md.exists():
        shutil.copyfile(skill_md, args.out_dir / "SKILL.md")
    print(f"compact master: {len(compact['notes'])} notes, "
          f"{len(compact['cites'])} citations factored, "
          f"{len(index['vocab'])} index terms -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
