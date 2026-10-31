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

  # Let a master answer questions asked in another language: export every
  # concept and label, add names (e.g. the Vietnamese terms), import them.
  python3 scripts/build_master.py --field "Robotics" --export-aliases names.json
  python3 scripts/build_master.py --field "Robotics" --import-aliases names.json

  # Experimental: semantic retrieval with a local embedding model
  # (pip install "atom-brain[semantic]"; see docs/master-skill.md).
  python3 scripts/build_master.py --field "Robotics" --embed --embed-model ~/models/e5
  ATOM_EMBED_MODEL=~/models/e5 python3 scripts/build_master.py --question "..."

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

from atom_brain.master import (  # noqa: E402
    alias_template,
    load_aliases,
    merge_aliases,
    save_aliases,
    validate_manifest,
    bound_view,
    build_master,
    list_masters,
    load_master,
    master_dir_for,
    parse_skill_dir,
    query_across_masters,
    fuse_hybrid,
    rank_notes_multi,
    suggest_related,
)
from atom_brain.compact import (  # noqa: E402
    expand_compact_master,
    load_compact_dir,
    query_compact_dir,
)


# Measured on the Robotics master: every gold question's top hit scores
# >= 12.2 and every gold note in a top 5 >= 8.0, while off-topic questions
# ("What is the capital of France?") top out at 7.0. Below this a hit is
# keyword noise, and answering from it would be inventing an answer.
MIN_ANSWER_SCORE = 8.0


def _confident(ranked: list[dict], min_score: float,
               min_cosine: float | None = None) -> list[dict]:
    """Hits a person can be answered from: lexical score at the threshold,
    or (with semantic retrieval) cosine similarity at its threshold."""
    return [hit for hit in ranked
            if hit["score"] >= min_score
            or (min_cosine is not None and hit.get("cosine") is not None
                and hit["cosine"] >= min_cosine)]


def _scores(hit: dict) -> str:
    text = f"score {hit['score']:.2f}"
    if hit.get("cosine") is not None:
        text += f", cosine {hit['cosine']:.2f}"
    return text


_EMBEDDERS: dict = {}


def _note(message: str) -> None:
    print(f"  [semantic] {message}", file=sys.stderr)


def _semantic_hits(index_dir: Path, notes, wordings: list[str], args):
    """(semantic entries, cosine threshold) for one master or compact dir,
    or (None, None) with the reason printed. Never raises: semantic
    retrieval only ever adds to the lexical answer."""
    from atom_brain import semantic
    if not semantic.has_index(index_dir):
        return None, None
    model_dir = semantic.model_dir_from(args.embed_model)
    if model_dir is None:
        _note(f"{index_dir.name} has an embedding index but no model is set "
              f"(--embed-model or ${semantic.MODEL_ENV}); lexical only")
        return None, None
    missing = semantic.missing_dependencies()
    if missing:
        _note(f"needs {', '.join(missing)} (pip install \"atom-brain[semantic]\"); "
              "lexical only")
        return None, None
    loaded = semantic.load_index(index_dir)
    if loaded is None:
        _note(f"cannot read the embedding index in {index_dir}; rerun --embed")
        return None, None
    meta, matrix = loaded
    try:
        key = str(model_dir)
        if key not in _EMBEDDERS:
            _EMBEDDERS[key] = semantic.Embedder(model_dir)
        embedder = _EMBEDDERS[key]
    except semantic.SemanticUnavailable as exc:
        _note(f"{exc}; lexical only")
        return None, None
    if embedder.identity != meta.get("model_id"):
        _note("the index was built with another model; rerun --embed")
        return None, None
    if notes is not None:
        stale = semantic.stale_notes(meta, notes)
        if stale:
            _note(f"{len(stale)} note(s) changed since the index was built; "
                  "rerun --embed")
    vectors = embedder.embed(wordings, kind="query")
    return (semantic.rank_semantic(meta, matrix, vectors),
            semantic.min_cosine_for(meta, args.min_cosine))


def _embed_command(args, root: Path) -> int:
    from atom_brain import semantic
    master_dir = master_dir_for(root, args.field)
    master = load_master(master_dir)
    if not master or not master.get("notes"):
        print(f"no master for field '{args.field}' at {master_dir}", file=sys.stderr)
        return 1
    model_dir = semantic.model_dir_from(args.embed_model)
    if model_dir is None:
        print(f"--embed needs a model directory: --embed-model DIR or "
              f"${semantic.MODEL_ENV}", file=sys.stderr)
        return 2
    try:
        report = semantic.build_index(master_dir, master["notes"],
                                      semantic.Embedder(model_dir))
    except semantic.SemanticUnavailable as exc:
        print(f"--embed: {exc}", file=sys.stderr)
        return 2
    print(f"embedding index: {report['notes']} notes ({report['embedded']} "
          f"embedded, {report['reused']} reused), {report['dim']} dimensions, "
          f"model {report['model_id']} -> {master_dir}")
    return 0


def _wordings_note(also: list[str]) -> str:
    wordings = [a for a in also if a and a.strip()]
    if not wordings:
        return ""
    return f" (+ {len(wordings)} other wording(s): {'; '.join(wordings)})"


def default_root() -> Path:
    env = os.environ.get("BOOK_SKILL_MASTERS_ROOT", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    return Path.home() / ".agents" / "skills" / "masters"


def _aliases_command(parser, args, root: Path) -> int:
    if not args.field:
        parser.error("--export-aliases / --import-aliases need --field")
    master_dir = master_dir_for(root, args.field)
    master = load_master(master_dir)
    if not master or not master.get("notes"):
        print(f"no master for field '{args.field}' at {master_dir}",
              file=sys.stderr)
        return 1
    current = load_aliases(master_dir)
    if args.export_aliases:
        template = alias_template(master, current)
        args.export_aliases.write_text(
            json.dumps(template, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        print(f"wrote {len(template['notes'])} concepts and "
              f"{len(template['labels'])} labels to {args.export_aliases}")
        return 0
    try:
        incoming = json.loads(args.import_aliases.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read {args.import_aliases}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(incoming, dict):
        print(f"{args.import_aliases}: expected an object with \"notes\" "
              "and/or \"labels\"", file=sys.stderr)
        return 2
    known = alias_template(master)
    notes_in = incoming.get("notes") if isinstance(incoming.get("notes"), dict) else {}
    labels_in = incoming.get("labels") if isinstance(incoming.get("labels"), dict) else {}
    unknown = [k for k in notes_in if k not in known["notes"]]
    unknown_labels = [k for k in labels_in if k not in known["labels"]]
    for key in unknown:
        print(f"  skipped unknown note id: {key}", file=sys.stderr)
    for key in unknown_labels:
        print(f"  skipped unknown label: {key}", file=sys.stderr)
    merged, added = merge_aliases(current, {
        "notes": {k: v for k, v in notes_in.items() if k in known["notes"]},
        "labels": {k: v for k, v in labels_in.items() if k in known["labels"]},
    })
    save_aliases(master_dir, merged)
    print(f"added {added} name(s): {len(merged['notes'])} concept(s) and "
          f"{len(merged['labels'])} label(s) now have other names "
          f"-> {master_dir / 'aliases.json'}")
    return 0


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
    parser.add_argument("--also", action="append", default=[], metavar="TEXT",
                        help="Another wording of --question, e.g. its "
                             "translation into the notes' language "
                             "(repeatable); the rankings are fused")
    parser.add_argument("--source",
                        help="Bound results to one source: case-insensitive "
                             "substring over source citations; composes "
                             "with --term/--facet/--status and --question")
    parser.add_argument("--top", type=int, default=5,
                        help="Notes to show for --question (default 5)")
    parser.add_argument("--min-score", type=float, default=MIN_ANSWER_SCORE,
                        help="Hide --question hits scoring below this "
                             f"(default {MIN_ANSWER_SCORE}); with none left "
                             "the master says it holds no answer")
    parser.add_argument("--compact-dir", type=Path, default=None,
                        help="With --question: answer from one compact master"
                             " directory (registry.json + index.json + "
                             "bodies.jsonl, see tools/build_compact.py)"
                             " instead of the masters root")
    parser.add_argument("--status", help="Filter query by note status")
    parser.add_argument("--term", help="Filter query by concept term")
    parser.add_argument("--facet", action="append", default=[],
                        help="Facet filter key=value (repeatable)")
    parser.add_argument("--embed", action="store_true",
                        help="Experimental, with --field: build or update the "
                             "master's semantic index with a local embedding "
                             "model (after the build when sources are given)")
    parser.add_argument("--embed-model", type=Path, default=None, metavar="DIR",
                        help="Model directory for --embed and --question "
                             "(model.onnx + tokenizer.json); default "
                             "$ATOM_EMBED_MODEL")
    parser.add_argument("--min-cosine", type=float, default=None,
                        help="Show a semantic hit without lexical support "
                             "only at or above this cosine (default: the "
                             "index's min_cosine, else 0.80, uncalibrated)")
    parser.add_argument("--export-aliases", type=Path, metavar="FILE",
                        help="With --field: write every note title and "
                             "topic/area label, with the names already "
                             "recorded, as a JSON template to fill in")
    parser.add_argument("--import-aliases", type=Path, metavar="FILE",
                        help="With --field: merge names from FILE (the "
                             "--export-aliases shape) into the master's "
                             "aliases.json; a rebuild keeps them")
    args = parser.parse_args(argv)
    root = args.masters_root or default_root()

    if args.export_aliases or args.import_aliases:
        return _aliases_command(parser, args, root)

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
            # Semantic hits outside a --source bound are dropped when they
            # are fused: only notes the lexical pool holds take part.
            sem, min_cos = _semantic_hits(args.compact_dir, None,
                                          [args.question] + args.also, args)
            ranked = query_compact_dir(args.compact_dir, args.question,
                                       k=args.top, source=args.source,
                                       also=args.also, semantic=sem)
            ranked = _confident(ranked[:args.top], args.min_score, min_cos)
            bound_note = f" [bound to source: {args.source}]" \
                if args.source else ""
            print(f"[compact] top {args.top} for: {args.question}"
                  f"{_wordings_note(args.also)}{bound_note}")
            if not ranked:
                print(f"  No note scores {args.min_score} or more: this master "
                      "does not hold an answer to that question.")
                return 0
            for hit in ranked:
                note = hit["note"]
                print(f"  - {note['title']} [{note['status']}] "
                      f"({_scores(hit)})")
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
            wordings = [args.question] + args.also
            ranked = rank_notes_multi(master.get("notes", []),
                                      master.get("taxonomy", []),
                                      master.get("graph", {}).get("edges", []),
                                      wordings, source=args.source)
            sem, min_cos = _semantic_hits(Path(root) / entry.get("path", ""),
                                          master.get("notes", []), wordings, args)
            if sem:
                ranked = fuse_hybrid(ranked, sem)
            ranked = _confident(ranked[:args.top], args.min_score, min_cos)
            bound_note = f" [bound to source: {args.source}]" \
                if args.source else ""
            print(f"[{entry['field']}] top {args.top} for: {args.question}"
                  f"{_wordings_note(args.also)}{bound_note}")
            if not ranked:
                print(f"  No note scores {args.min_score} or more: this master "
                      "does not hold an answer to that question.")
                continue
            for hit in ranked:
                note = hit["note"]
                print(f"  - {note['title']} [{note['status']}] "
                      f"({_scores(hit)})")
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

    sources = []
    manifest_taxonomy = None
    if args.skills_json:
        manifest = json.loads(Path(args.skills_json).read_text(encoding="utf-8"))
        problems = validate_manifest(manifest)
        for problem in problems:
            print(f"manifest: {problem}", file=sys.stderr)
        if any(not problem.startswith("warning:") for problem in problems):
            return 2
        sources.extend(manifest.get("sources", []))
        manifest_taxonomy = manifest.get("taxonomy")
        if manifest.get("field") and not args.field:
            args.field = manifest["field"]
    if not args.field:
        parser.error("--field is required (or a \"field\" in the manifest)")
    for skill_dir in args.skill_dir:
        sources.append(parse_skill_dir(Path(skill_dir)))
    if not sources and args.embed:
        return _embed_command(args, root)
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
    if summary.get("unresolved_relations"):
        print(f"  {summary['unresolved_relations']} relation(s) not resolved "
              "(see unresolved_relations in master.json)")
    if summary.get("kept_manual_notes"):
        print(f"  kept {len(summary['kept_manual_notes'])} hand-added note(s): "
              f"{', '.join(summary['kept_manual_notes'])}")
    if summary.get("hand_edited_needing_review"):
        print("  REVIEW: hand-edited note(s) kept although their sources changed: "
              f"{', '.join(summary['hand_edited_needing_review'])}")
    if args.embed:
        return _embed_command(args, root)
    from atom_brain.semantic import has_index
    if has_index(out_dir):
        print("  the semantic index may be stale now: rerun with --embed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
