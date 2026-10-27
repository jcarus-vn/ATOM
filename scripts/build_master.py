#!/usr/bin/env python3
"""Build, update, list, and query field master skills.

Examples:
  # Build a master from a JSON manifest (see docs/master-skill.md):
  python3 scripts/build_master.py --field "Behavioral Economics" \
      --skills-json manifest.json

  # Fold a new skill directory into an existing master:
  python3 scripts/build_master.py --field "Behavioral Economics" \
      --skill-dir ~/.agents/skills/cialdini-influence

  # List masters and query contested concepts across all of them:
  python3 scripts/build_master.py --list-masters
  python3 scripts/build_master.py --query --status contested
  python3 scripts/build_master.py --query --facet level=advanced --facet topic=pricing

Masters live under <masters-root>/<field-slug>/; the registry is
<masters-root>/index.json. Default root: ~/.agents/skills/masters.
Override with --masters-root or BOOK_SKILL_MASTERS_ROOT.
"""

import argparse
import json
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atom.master import (  # noqa: E402
    bound_view,
    build_master,
    list_masters,
    load_master,
    master_dir_for,
    parse_skill_dir,
    query_across_masters,
    rank_notes,
    suggest_related,
)
from atom.compact import (  # noqa: E402
    expand_compact_master,
    load_compact_dir,
    query_compact_dir,
)


def default_root() -> Path:
    env = os.environ.get("BOOK_SKILL_MASTERS_ROOT", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    return Path.home() / ".agents" / "skills" / "masters"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build and query master skills.")
    parser.add_argument("--field", help="Field label for the master skill")
    parser.add_argument("--skills-json", help="JSON manifest with sources")
    parser.add_argument("--skill-dir", action="append", default=[],
                        help="Existing skill directory to merge (repeatable)")
    parser.add_argument("--masters-root", type=Path, default=None)
    parser.add_argument("--list-masters", action="store_true")
    parser.add_argument("--query", action="store_true")
    parser.add_argument("--question",
                        help="Natural-language question: rank notes with "
                             "the shared ranker and show related keywords")
    parser.add_argument("--source",
                        help="Bound results to one source: case-insensitive "
                             "substring over source citations; composes "
                             "with --term/--facet/--status and --question")
    parser.add_argument("--top", type=int, default=5,
                        help="Notes to show for --question (default 5)")
    parser.add_argument("--compact-dir", type=Path, default=None,
                        help="With --question: answer from one compact master"
                             " directory (registry.json + index.json + "
                             "bodies.jsonl, see tools/build_compact.py)"
                             " instead of the masters root")
    parser.add_argument("--status", help="Filter query by note status")
    parser.add_argument("--term", help="Filter query by concept term")
    parser.add_argument("--facet", action="append", default=[],
                        help="Facet filter key=value (repeatable)")
    args = parser.parse_args(argv)
    root = args.masters_root or default_root()

    if args.list_masters:
        for entry in list_masters(root):
            print(f"{entry['field']} [{entry['slug']}] v{entry['version']} "
                  f"- {entry['concept_count']} concepts, "
                  f"{entry['contested_count']} contested, "
                  f"updated {entry['updated_at'][:10]}")
            print(f"  sources: {', '.join(entry['sources'])}")
        return 0

    if args.query:
        filters = {}
        for item in args.facet:
            if "=" in item:
                key, value = item.split("=", 1)
                filters[key.strip()] = value.strip()
        for hit in query_across_masters(root, args.status, args.term, filters,
                                        args.source):
            print(f"[{hit['field']}] {hit['concept']} [{hit['status']}]")
            print(f"  sources: {'; '.join(hit['sources'])}")
        return 0

    if args.question:
        if args.compact_dir is not None:
            registry, _index = load_compact_dir(args.compact_dir)
            ranked = query_compact_dir(args.compact_dir, args.question,
                                       k=args.top, source=args.source)
            ranked = ranked[:args.top]
            bound_note = f" [bound to source: {args.source}]" \
                if args.source else ""
            print(f"[compact] top {args.top} for: {args.question}{bound_note}")
            for hit in ranked:
                note = hit["note"]
                print(f"  - {note['title']} [{note['status']}] "
                      f"(score {hit['score']:.2f})")
                if args.source:
                    view = bound_view(note, args.source)
                    print(f"    {view['body']}")
                    if view["hidden_sources"]:
                        print(f"    [bound view: {len(view['hidden_sources'])} "
                              f"other source(s) not shown]")
            lite = expand_compact_master(registry, {})
            related = suggest_related(
                lite["notes"], lite["graph"]["edges"],
                [h["note"].get("id", "") for h in ranked],
                source=args.source)
            if related:
                print("  Related keywords (separate from the answer):")
                for item in related:
                    print(f"    * {item['title']} -- {item['reason']}")
            return 0
        found = False
        for entry in list_masters(root):
            master = load_master(Path(root) / entry.get("path", ""))
            if not master:
                continue
            found = True
            ranked = rank_notes(master.get("notes", []),
                                master.get("taxonomy", []),
                                master.get("graph", {}).get("edges", []),
                                args.question, k=args.top,
                                source=args.source)
            bound_note = f" [bound to source: {args.source}]" \
                if args.source else ""
            print(f"[{entry['field']}] top {args.top} for: {args.question}"
                  f"{bound_note}")
            for hit in ranked:
                note = hit["note"]
                print(f"  - {note['title']} [{note['status']}] "
                      f"(score {hit['score']:.2f})")
                if args.source:
                    view = bound_view(note, args.source)
                    print(f"    {view['body']}")
                    if view["hidden_sources"]:
                        print(f"    [bound view: {len(view['hidden_sources'])} "
                              f"other source(s) not shown]")
            related = suggest_related(
                master.get("notes", []),
                master.get("graph", {}).get("edges", []),
                [h["note"].get("id", "") for h in ranked],
                source=args.source)
            if related:
                print("  Related keywords (separate from the answer):")
                for item in related:
                    print(f"    * {item['title']} -- {item['reason']}")
        if not found:
            print("no masters found", file=sys.stderr)
            return 1
        return 0

    if not args.field:
        parser.error("--field is required to build a master skill")
    sources = []
    manifest_taxonomy = None
    if args.skills_json:
        manifest = json.loads(Path(args.skills_json).read_text(encoding="utf-8"))
        sources.extend(manifest.get("sources", []))
        manifest_taxonomy = manifest.get("taxonomy")
        if manifest.get("field") and not args.field:
            args.field = manifest["field"]
    for skill_dir in args.skill_dir:
        sources.append(parse_skill_dir(Path(skill_dir)))
    if not sources:
        parser.error("no sources: pass --skills-json and/or --skill-dir")
    out_dir = master_dir_for(root, args.field)
    existing = load_master(out_dir)
    summary = build_master(args.field, sources, root,
                           existing=existing,
                           manifest_taxonomy=manifest_taxonomy)
    print(f"Master skill '{summary['field']}' v{summary['version']}: "
          f"{summary['concept_count']} concepts, "
          f"{summary['contested_count']} contested -> {summary['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
