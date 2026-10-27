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

from book_to_skill.master import (  # noqa: E402
    build_master,
    list_masters,
    load_master,
    master_dir_for,
    parse_skill_dir,
    query_across_masters,
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
        for hit in query_across_masters(root, args.status, args.term, filters):
            print(f"[{hit['field']}] {hit['concept']} [{hit['status']}]")
            print(f"  sources: {'; '.join(hit['sources'])}")
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
