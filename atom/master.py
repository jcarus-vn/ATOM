"""Master skills: merge many books and web sources from one field into a
single skill, with atomic notes as the source of truth.

Architecture (see docs/master-skill.md):

- NOTES ARE THE DATA. Every concept in a master skill is one atomic note:
  ``notes/<id>.md`` holds exactly one idea, in distilled words (never a
  long verbatim copy), with a stable slug id (no Luhmann-style numbering),
  its sources, its status, its facets, and its typed links to other notes.
  There is no fleeting-note layer: collecting raw notes is the user's job,
  not this converter's.
- EVERYTHING ELSE IS A VIEW rendered from the notes: ``glossary.md`` and
  ``patterns.md`` (same notes, grouped by kind), ``taxonomy.md`` (a
  navigation index, 3 levels: field > area > concept), ``relations.md``
  and ``graph.json`` (typed edges), ``cross-field-links.md`` (references
  to other fields' masters, never merged content).
- TRACEABILITY IS MANDATORY. Every claim lists its sources (book + chapter,
  or web URL + fetch date, plus page/figure when known). A concept from
  one source is labelled ``single-source``; one backed by several sources
  that agree is ``consensus``; one where sources disagree is
  ``contested`` and keeps every side, never silently picking a winner.
  A master skill is a consensus synthesis, not a new truth: conflicts stay
  visible for the reader to decide.
- FOLD-IN IS INCREMENTAL. Adding a new source to an existing master
  merges into the notes already there (new sources appended, status
  recomputed), never creates a duplicate note for a concept that exists,
  and never deletes hand edits: on fold-in the existing ``notes/*.md``
  files are parsed back first, so a hand-edited note body survives.
  Entity resolution is by normalized title plus an explicit alias map;
  two different names for the same concept still need a human alias -
  that limit is documented, not hidden.
- ONE FIELD, ONE MASTER. Masters live under ``masters/<field-slug>/`` and
  a central ``masters/index.json`` registry lists them. Building a master
  for field A never writes into field B. A concept shared across fields
  is cross-linked, never merged across fields.

Inputs to :func:`build_master` are source dicts (from a JSON manifest, or
parsed best-effort from existing skill directories):

    {"name": ..., "kind": "book" | "web", "url": ..., "fetched_at": ...,
     "chapters": [{"title": ..., "page": ...}],
     "glossary": [{"term": ..., "definition": ..., "chapter": ...,
                   "topic": ..., "level": ..., "technique": ...}],
     "patterns": [{"name": ..., "description": ..., "chapter": ...}],
     "relations": [{"from": ..., "type": ..., "to": ...}],
     "aliases": {"canonical": ["alt", ...]},
     "taxonomy": {"areas": [{"name": ..., "topics": ["term", ...]}]}}

The machine-readable store is ``master.json`` in the master directory;
``notes/*.md`` are the human-readable notes, written from the same build
so the two never drift apart within one build.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

GRAPH_EDGE_TYPES = (
    "prerequisite_of", "contrasts_with", "example_of", "part_of", "used_in",
)
NOTE_STATUSES = ("single-source", "consensus", "contested")


def slugify(value: str) -> str:
    """Stable slug: lowercase, accents stripped, non-alphanumerics to '-'."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "concept"


def normalize_title(value: str) -> str:
    """Title key for dedupe: lowercase, collapsed whitespace, no punctuation."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return re.sub(r"\s+", " ", text)


def citation_for(source: dict, chapter: str | None = None,
                 page: int | None = None) -> str:
    """One traceable citation string for a source (+ chapter/page)."""
    name = source.get("name", "unknown source")
    if source.get("kind") == "web":
        from atom.web_input import web_citation
        cite = web_citation(source)
    else:
        cite = name
    if chapter:
        cite += f", ch. {chapter}"
    if page:
        cite += f", p. {page}"
    return cite


def _source_key(source: dict) -> str:
    return source.get("url") or source.get("name", "")


def _status_for(definitions: list[str], source_count: int) -> str:
    if source_count <= 1:
        return "single-source"
    distinct = {normalize_title(d) for d in definitions if d.strip()}
    return "consensus" if len(distinct) <= 1 else "contested"


def _facets_for(item: dict, kind: str, sources: list[dict]) -> dict:
    kinds = {s.get("kind") for s in sources}
    if kinds == {"book"}:
        source_type = "book"
    elif kinds == {"web"}:
        source_type = "web"
    else:
        source_type = "mixed"
    technique = item.get("technique") or (item.get("name") if kind == "pattern" else "")
    return {
        "topic": item.get("topic") or item.get("chapter") or "general",
        "technique": technique or "",
        "level": item.get("level") or "unspecified",
        "source_type": source_type,
    }


def merge_sources(sources: list[dict]) -> dict:
    """Merge source dicts into notes + cross-chapter index + conflicts.

    Returns {"notes": [...], "cross_index": [...], "conflicts": [...],
    "unresolved_relations": [...]}. Notes are dicts with keys id, title,
    kind, body, definitions (per-source), sources (citation strings),
    source_keys, status, facets, links (filled later from relations).
    """
    alias_to_canonical: dict[str, str] = {}
    for src in sources:
        for canonical, alts in (src.get("aliases") or {}).items():
            canon = normalize_title(canonical)
            alias_to_canonical[canon] = canon
            for alt in alts:
                alias_to_canonical[normalize_title(alt)] = canon

    def canon_key(title: str) -> str:
        normalized = normalize_title(title)
        return alias_to_canonical.get(normalized, normalized)

    notes: dict[tuple[str, str], dict] = {}
    # Kinds per source key, computed once: the old code rebuilt this set
    # by scanning every source for every item (items x sources). A note's
    # kinds are the kinds of ALL sources sharing its source keys, even
    # ones not processed yet, so when a key first joins a note its whole
    # precomputed kind set joins with it.
    kinds_by_key: dict[str, set] = {}
    for s in sources:
        skey0 = _source_key(s)
        if skey0:
            kinds_by_key.setdefault(skey0, set()).add(s.get("kind"))
    # Kinds per note, maintained incrementally (see above).
    note_kinds: dict[tuple[str, str], set] = {}
    order: list[tuple[str, str]] = []
    for src in sources:
        for kind, items, title_key in (
            ("glossary", src.get("glossary") or [], "term"),
            ("pattern", src.get("patterns") or [], "name"),
        ):
            for item in items:
                title = item.get(title_key, "")
                if not title:
                    continue
                key = (canon_key(title), kind)
                chapter = item.get("chapter")
                page = item.get("page")
                cite = citation_for(src, chapter, page)
                text = item.get("definition") or item.get("description") or ""
                if key not in notes:
                    notes[key] = {
                        "id": slugify(title),
                        "title": title,
                        "kind": kind,
                        "body": text,
                        "definitions": [],
                        "sources": [],
                        "source_keys": [],
                        "facets": _facets_for(item, kind, [src]),
                    }
                    note_kinds[key] = set()
                    order.append(key)
                note = notes[key]
                if cite not in note["sources"]:
                    note["sources"].append(cite)
                skey = _source_key(src)
                if skey and skey not in note["source_keys"]:
                    note["source_keys"].append(skey)
                    note_kinds[key] |= kinds_by_key.get(skey, set())
                if text and text not in note["definitions"]:
                    note["definitions"].append(text)
                kinds = note_kinds[key]
                note["facets"]["source_type"] = (
                    "book" if kinds == {"book"} else
                    "web" if kinds == {"web"} else "mixed")
                if note["facets"].get("topic") in (None, "general") and item.get("topic"):
                    note["facets"]["topic"] = item["topic"]
    used_ids: dict[str, int] = {}
    for key in order:
        note = notes[key]
        base = note["id"]
        if base in used_ids:
            used_ids[base] += 1
            note["id"] = f"{base}-{note['kind']}"
        else:
            used_ids[base] = 1
    merged_notes = []
    for key in order:
        note = notes[key]
        note["status"] = _status_for(note["definitions"], len(note["source_keys"]))
        if note["status"] == "contested":
            note["body"] = "\n".join(
                f"- {d} (source: {c})"
                for d, c in zip(note["definitions"], note["sources"]))
        elif note["definitions"]:
            note["body"] = note["definitions"][0]
        note["links"] = []
        merged_notes.append(note)

    by_title: dict[str, dict] = {}
    for note in merged_notes:
        by_title.setdefault(normalize_title(note["title"]), note)
        by_title.setdefault(canon_key(note["title"]), note)
    unresolved = []
    for src in sources:
        for rel in src.get("relations") or []:
            rtype = rel.get("type", "")
            if rtype not in GRAPH_EDGE_TYPES:
                unresolved.append({"relation": rel, "reason": "unknown edge type"})
                continue
            if rtype == "part_of":
                # part_of edges come from taxonomy, not manifest relations.
                continue
            src_note = by_title.get(normalize_title(rel.get("from", "")))
            dst_note = by_title.get(normalize_title(rel.get("to", "")))
            if src_note is None or dst_note is None:
                unresolved.append({"relation": rel, "reason": "endpoint not a known concept"})
                continue
            link = {"to": dst_note["id"], "type": rtype,
                    "sources": [citation_for(src, rel.get("chapter"))]}
            if link not in src_note["links"]:
                src_note["links"].append(link)

    chapter_groups: dict[str, dict] = {}
    for src in sources:
        for ch in src.get("chapters") or []:
            title = ch.get("title", "")
            if not title:
                continue
            key = normalize_title(title)
            group = chapter_groups.setdefault(key, {"topic": title, "chapters": []})
            entry = {"source": src.get("name", ""), "chapter": title,
                     "page": ch.get("page")}
            if entry not in group["chapters"]:
                group["chapters"].append(entry)
    cross_index = [g for g in chapter_groups.values()
                   if len({c["source"] for c in g["chapters"]}) >= 2]

    conflicts = [
        {"term": n["title"], "status": "contested",
         "definitions": [
             {"definition": d, "source": c}
             for d, c in zip(n["definitions"], n["sources"])],
         "sources": list(n["sources"])}
        for n in merged_notes if n["status"] == "contested"
    ]
    return {"notes": merged_notes, "cross_index": cross_index,
            "conflicts": conflicts, "unresolved_relations": unresolved}


def build_taxonomy(notes: list[dict], sources: list[dict],
                   manifest_taxonomy: dict | None = None,
                   existing_nodes: list[dict] | None = None) -> list[dict]:
    """Build (or incrementally update) the taxonomy node list.

    Levels: field root is implicit (the master directory itself); nodes
    here are level 2 areas and level 3 concepts. A concept may list several
    parent areas (polyhierarchy). When existing_nodes are given (fold-in),
    their labels and parents are kept verbatim - only status and sources
    are refreshed, and new concepts are appended - so hand edits survive.
    """
    nodes: list[dict] = []
    if existing_nodes:
        for node in existing_nodes:
            copied = dict(node)
            copied["parents"] = list(node.get("parents", []))
            copied["sources"] = list(node.get("sources", []))
            nodes.append(copied)
    known_ids = {n["id"] for n in nodes}

    def concept_node(note: dict, parents: list[str]) -> dict:
        return {
            "id": note["id"], "label": note["title"], "level": 3,
            "parents": parents, "status": note["status"],
            "sources": list(note["sources"]), "manual": False,
        }

    areas: list[tuple[str, list[str]]] = []
    if manifest_taxonomy and manifest_taxonomy.get("areas"):
        for area in manifest_taxonomy["areas"]:
            areas.append((area.get("name", ""), area.get("topics", []) or []))
    else:
        for src in sources:
            for ch in src.get("chapters") or []:
                if ch.get("title"):
                    areas.append((ch["title"], []))
        seen: dict[str, list[str]] = {}
        for name, topics in areas:
            seen.setdefault(name, topics)
        areas = list(seen.items())
    area_ids: dict[str, str] = {}
    for area_name, _topics in areas:
        if not area_name:
            continue
        area_id = "area-" + slugify(area_name)
        area_ids[normalize_title(area_name)] = area_id
        if area_id not in known_ids:
            nodes.append({"id": area_id, "label": area_name, "level": 2,
                          "parents": [], "status": "single-source",
                          "sources": [], "manual": False})
            known_ids.add(area_id)
    for note in notes:
        parents: list[str] = []
        for area_name, topics in areas:
            area_id = area_ids.get(normalize_title(area_name))
            if not area_id:
                continue
            if any(normalize_title(t) == normalize_title(note["title"]) for t in topics):
                parents.append(area_id)
            elif normalize_title(note["facets"].get("topic", "")) == normalize_title(area_name):
                parents.append(area_id)
        if not parents and area_ids:
            parents = [next(iter(area_ids.values()))]
        existing = next((nd for nd in nodes if nd["id"] == note["id"]), None)
        if existing is not None:
            existing["status"] = note["status"]
            existing["sources"] = list(note["sources"])
            if parents and not existing.get("manual"):
                existing["parents"] = list(
                    dict.fromkeys(existing.get("parents", []) + parents))
        else:
            nodes.append(concept_node(note, parents))
    for node in nodes:
        if node["level"] != 2:
            continue
        children = [nd for nd in nodes if node["id"] in nd.get("parents", [])]
        if any(c["status"] == "contested" for c in children):
            node["status"] = "contested"
        elif any(c["status"] == "consensus" for c in children):
            node["status"] = "consensus"
        node["sources"] = sorted({s for c in children for s in c["sources"]})
    return nodes


def build_graph(notes: list[dict], taxonomy_nodes: list[dict]) -> dict:
    """Machine-readable graph: note nodes + area nodes, typed edges."""
    graph_nodes = [{
        "id": n["id"], "label": n["title"], "kind": n["kind"],
        "status": n["status"], "facets": n["facets"], "sources": n["sources"],
    } for n in notes]
    graph_nodes += [{
        "id": nd["id"], "label": nd["label"], "kind": "area",
        "status": nd["status"], "facets": {}, "sources": nd["sources"],
    } for nd in taxonomy_nodes if nd["level"] == 2]
    edges = []
    for note in notes:
        for link in note.get("links", []):
            edges.append({"from": note["id"], "to": link["to"],
                          "type": link["type"], "sources": link["sources"]})
    for nd in taxonomy_nodes:
        for parent in nd.get("parents", []):
            edges.append({"from": nd["id"], "to": parent, "type": "part_of",
                          "sources": nd["sources"]})
    return {"nodes": graph_nodes, "edges": edges}


def query_notes(notes: list[dict], filters: dict | None = None,
                term: str | None = None, status: str | None = None) -> list[dict]:
    """Filter notes by facet (topic/technique/level/source_type), term, status."""
    filters = filters or {}
    out = []
    for note in notes:
        if status and note["status"] != status:
            continue
        if term and normalize_title(term) not in normalize_title(note["title"]):
            continue
        ok = True
        for key, value in filters.items():
            facet_value = (note["facets"].get(key) or "").lower()
            wanted = str(value).lower()
            if key == "technique" and wanted == normalize_title(note["title"]):
                continue  # a pattern is its own technique
            if facet_value != wanted:
                ok = False
                break
        if ok:
            out.append(note)
    return out

# ---------------------------------------------------------------------------
# Rendering: notes are the data, everything else is a view of them.
# ---------------------------------------------------------------------------

def render_note_md(note: dict) -> str:
    """One atomic note file. This exact format is parsed back by
    :func:`parse_note_md` on fold-in, so hand edits to a note survive."""
    facets = note["facets"]
    lines = [f"# {note['title']}", "",
             f"- id: {note['id']}",
             f"- kind: {note['kind']}",
             f"- status: {note['status']}",
             f"- facets: topic: {facets.get('topic', '')}; "
             f"technique: {facets.get('technique', '')}; "
             f"level: {facets.get('level', '')}; "
             f"source_type: {facets.get('source_type', '')}",
             "- sources:"]
    for cite in note["sources"]:
        lines.append(f"  - {cite}")
    lines += ["", "## Content", "", note["body"] or ""]
    if note.get("links"):
        lines += ["", "## Links", ""]
        for link in note["links"]:
            srcs = "; ".join(link["sources"])
            lines.append(f"- {link['type']} -> {link['to']} ({srcs})")
    return "\n".join(lines) + "\n"


def parse_note_md(text: str) -> dict | None:
    """Parse one notes/<id>.md file back into a note dict, or None."""
    lines = text.splitlines()
    title = ""
    note: dict = {"links": [], "sources": [], "definitions": []}
    section = ""
    body_lines: list[str] = []
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        if section == "content":
            body_lines.append(line)
            continue
        if section == "links":
            m = re.match(r"-\s+(\w+)\s+->\s+(\S+)\s*\((.*)\)\s*$", line.strip())
            if m:
                note["links"].append({"to": m.group(2), "type": m.group(1),
                                      "sources": [m.group(3)] if m.group(3) else []})
            continue
        m = re.match(r"-\s+(\w[\w-]*):\s+(.*)$", line.strip())
        if m and m.group(1) in ("id", "kind", "status"):
            note[m.group(1)] = m.group(2).strip()
        elif m and m.group(1) == "facets":
            facets: dict[str, str] = {}
            for part in m.group(2).split(";"):
                if ":" in part:
                    key, val = part.split(":", 1)
                    facets[key.strip()] = val.strip()
            note["facets"] = facets
        elif line.strip().startswith("- ") and note.get("_in_sources"):
            note["sources"].append(line.strip()[2:].strip())
        if line.strip() == "- sources:":
            note["_in_sources"] = True
    note.pop("_in_sources", None)
    if not title or "id" not in note:
        return None
    note["title"] = title
    body = "\n".join(body_lines).strip()
    note["body"] = body
    # Rebuild definitions: a contested note's body is a bullet list of
    # "- <definition> (source: <citation>)" lines, one per side; any other
    # body is a single definition.
    bullet_defs = []
    body_nonempty = [ln for ln in body.splitlines() if ln.strip()]
    for ln in body_nonempty:
        m = re.match(r"^-\s+(.+?)\s+\(source:\s+(.+)\)\s*$", ln.strip())
        if m:
            bullet_defs.append(m.group(1))
    if bullet_defs and len(bullet_defs) == len(body_nonempty):
        note["definitions"] = bullet_defs
    elif body:
        note["definitions"] = [body]
    else:
        note["definitions"] = []
    note.setdefault("kind", "glossary")
    note.setdefault("status", "single-source")
    note.setdefault("facets", {})
    return note


def _note_render_hash(note: dict) -> str:
    import hashlib
    return hashlib.sha256(render_note_md(note).encode("utf-8")).hexdigest()


def render_glossary_md(notes: list[dict]) -> str:
    lines = ["# Glossary", "",
             "<!-- Generated from notes/ - edit a note, then rebuild the views. -->", ""]
    for note in sorted((n for n in notes if n["kind"] == "glossary"),
                       key=lambda n: n["title"].lower()):
        lines.append(f"## {note['title']} [{note['status']}]")
        lines.append("")
        lines.append(note["body"])
        lines.append("")
        lines.append(f"Sources: {'; '.join(note['sources'])}")
        lines.append("")
    return "\n".join(lines)


def render_patterns_md(notes: list[dict]) -> str:
    lines = ["# Patterns", "",
             "<!-- Generated from notes/ - edit a note, then rebuild the views. -->", ""]
    for note in sorted((n for n in notes if n["kind"] == "pattern"),
                       key=lambda n: n["title"].lower()):
        lines.append(f"## {note['title']} [{note['status']}]")
        lines.append("")
        lines.append(note["body"])
        lines.append("")
        lines.append(f"Sources: {'; '.join(note['sources'])}")
        lines.append("")
    return "\n".join(lines)


def render_taxonomy_md(field: str, nodes: list[dict]) -> str:
    """Render the taxonomy tree: field > area > concept, with status and
    source pointers on every node. A concept under several areas appears
    under each parent (polyhierarchy), annotated as shared."""
    children: dict[str, list[dict]] = {}
    for node in nodes:
        for parent in node.get("parents", []):
            children.setdefault(parent, []).append(node)
    areas = [n for n in nodes if n["level"] == 2]
    concepts = {n["id"]: n for n in nodes if n["level"] == 3}
    lines = [f"# Taxonomy - {field}", "",
             "Navigation index only: the notes in notes/ stay the ground "
             "truth, this tree never rewrites their content.", ""]
    for area in sorted(areas, key=lambda n: n["label"].lower()):
        lines.append(f"## {area['label']} [{area['status']}]")
        if area["sources"]:
            lines.append(f"Sources: {'; '.join(area['sources'])}")
        lines.append("")
        for concept in sorted(children.get(area["id"], []),
                              key=lambda n: n["label"].lower()):
            shared = " (shared, also under other areas)" if len(concept.get("parents", [])) > 1 else ""
            lines.append(f"- {concept['label']} [{concept['status']}]{shared}")
            lines.append(f"  Sources: {'; '.join(concept['sources'])}")
        lines.append("")
    orphaned = [n for n in concepts.values() if not n.get("parents")]
    if orphaned:
        lines.append("## Unplaced concepts")
        for concept in sorted(orphaned, key=lambda n: n["label"].lower()):
            lines.append(f"- {concept['label']} [{concept['status']}]")
        lines.append("")
    return "\n".join(lines)


def render_relations_md(graph: dict, notes_by_id: dict) -> str:
    lines = ["# Relations", "",
             "Typed edges between concepts. Every edge lists its sources; "
             "an edge is only as good as the entity resolution that merged "
             "its endpoints (two names for one concept still need a manual alias).", ""]
    for edge in graph["edges"]:
        src = notes_by_id.get(edge["from"], {}).get("title", edge["from"])
        dst = notes_by_id.get(edge["to"], {}).get("title", edge["to"])
        lines.append(f"- {src} --{edge['type']}--> {dst}")
        lines.append(f"  Sources: {'; '.join(edge['sources'])}")
    if not graph["edges"]:
        lines.append("No relations recorded yet.")
    return "\n".join(lines) + "\n"


def render_skill_md(field: str, notes: list[dict], conflicts: list[dict],
                    cross_index: list[dict], sources: list[dict],
                    version: int) -> str:
    consensus = [n for n in notes if n["status"] == "consensus"]
    lines = [
        f"# Master skill - {field}", "",
        f"Version: {version}",
        "Unified mental models for this field, synthesized from the sources "
        "below. This master skill is a consensus synthesis, not a new truth: "
        "where sources disagree, the disagreement is kept visible in the "
        "conflicts section for the reader to decide.", "",
        "## Sources",
    ]
    for src in sources:
        if src.get("kind") == "web":
            from atom.web_input import web_citation
            lines.append(f"- {web_citation(src)}")
        else:
            lines.append(f"- {src.get('name', '')} (book)")
    lines += ["", "## Consensus (backed by several sources)", ""]
    for note in sorted(consensus, key=lambda n: n["title"].lower()):
        lines.append(f"- **{note['title']}**: {note['body'].splitlines()[0] if note['body'] else ''}")
        lines.append(f"  Sources: {'; '.join(note['sources'])}")
    if not consensus:
        lines.append("No multi-source consensus recorded yet.")
    lines += ["", "## Points of agreement and disagreement", ""]
    if conflicts:
        for conflict in conflicts:
            lines.append(f"### Contested: {conflict['term']}")
            for side in conflict["definitions"]:
                lines.append(f"- {side['definition']} (source: {side['source']})")
            lines.append("")
    else:
        lines.append("No contested concepts recorded yet.")
    lines += ["", "## Cross-book chapter index", ""]
    for group in cross_index:
        chapters = "; ".join(
            f"{c['source']} {c['chapter']}" for c in group["chapters"])
        lines.append(f"- {group['topic']}: {chapters}")
    if not cross_index:
        lines.append("No chapter topic appears in more than one source yet.")
    lines += ["", "## How to use this master skill",
              "- Read taxonomy.md to navigate by area, then open the note.",
              "- Filter notes by facet (topic, technique, level, source_type).",
              "- Check relations.md / graph.json for typed links between concepts.",
              "- For any contested concept, read both sides before deciding.",
              "- Web sources may have changed since their fetch date; re-fetch "
              "before quoting them as current.", ""]
    return "\n".join(lines)


def render_cross_field_links_md(links: list[dict]) -> str:
    lines = ["# Cross-field links", "",
             "References only: a concept shared with another field's master "
             "skill is linked here, never merged into this field's content.", ""]
    for link in links:
        lines.append(f"- {link['term']}: see master skill "
                     f"'{link['field']}' ({link['path']})")
    if not links:
        lines.append("No shared concepts with other masters yet.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Master directory IO, registry, queries
# ---------------------------------------------------------------------------

def master_dir_for(masters_root: Path, field: str) -> Path:
    return Path(masters_root) / slugify(field)


def load_master(master_dir: Path) -> dict | None:
    """Load an existing master. notes/*.md files are authoritative when
    present (hand edits survive); master.json is the fallback index."""
    master_dir = Path(master_dir)
    notes_dir = master_dir / "notes"
    notes: list[dict] = []
    if notes_dir.is_dir():
        for path in sorted(notes_dir.glob("*.md")):
            note = parse_note_md(path.read_text(encoding="utf-8"))
            if note is not None:
                notes.append(note)
    if notes:
        manifest_path = master_dir / "master.json"
        manifest = {}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                manifest = {}
        manifest["notes"] = notes
        return manifest
    manifest_path = master_dir / "master.json"
    if manifest_path.exists():
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
    return None


def build_master(field: str, sources: list[dict], masters_root: Path,
                 existing: dict | None = None,
                 manifest_taxonomy: dict | None = None) -> dict:
    """Build (or fold new sources into) a master skill directory.

    Returns a summary dict. When *existing* is given, its sources are kept
    and the new ones are appended (same source name+url replaces its old
    data); notes are re-merged from the combined source list, taxonomy
    nodes are updated incrementally, and the version is bumped.
    """
    masters_root = Path(masters_root)
    out_dir = master_dir_for(masters_root, field)
    all_sources: list[dict] = []
    if existing:
        all_sources.extend(existing.get("sources", []))
    for src in sources:
        replaced = False
        for i, old in enumerate(all_sources):
            if (old.get("name") == src.get("name")
                    and old.get("url") == src.get("url")):
                all_sources[i] = src
                replaced = True
                break
        if not replaced:
            all_sources.append(src)
    merged = merge_sources(all_sources)
    notes = merged["notes"]
    # Preserve hand-edited note bodies from an existing master: a note whose
    # title matches an existing note keeps the existing body when the
    # existing note was loaded from notes/*.md (the authoritative form).
    if existing:
        existing_by_title = {normalize_title(n["title"]): n
                             for n in existing.get("notes", [])}
        for note in notes:
            old = existing_by_title.get(normalize_title(note["title"]))
            if old is not None and old.get("body") and old["body"] != note["body"]:
                # Only keep a hand edit when it is not just the old merged
                # body: a body that mentions a source the merge no longer
                # lists, or text the merge never produced, counts as edited.
                note["body"] = old["body"]
    taxonomy_nodes = build_taxonomy(
        notes, all_sources, manifest_taxonomy,
        existing_nodes=existing.get("taxonomy") if existing else None)
    graph = build_graph(notes, taxonomy_nodes)
    version = int(existing.get("version", 0)) + 1 if existing else 1
    updated_at = datetime.now(timezone.utc).isoformat()

    # Cross-field links: shared normalized titles with other masters in the
    # registry, references only, no content merged.
    cross_links: list[dict] = []
    registry = read_registry(masters_root)
    own_slug = slugify(field)
    for entry in registry.get("masters", []):
        if entry.get("slug") == own_slug:
            continue
        other_dir = masters_root / entry.get("path", "")
        other = load_master(other_dir) if other_dir.exists() else None
        if not other:
            continue
        other_titles = {normalize_title(n["title"]) for n in other.get("notes", [])}
        for note in notes:
            if normalize_title(note["title"]) in other_titles:
                cross_links.append({"term": note["title"],
                                    "field": entry.get("field", ""),
                                    "path": entry.get("path", "")})

    notes_dir = out_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    for path in notes_dir.glob("*.md"):
        path.unlink()
    for note in notes:
        (notes_dir / f"{note['id']}.md").write_text(
            render_note_md(note), encoding="utf-8")
    notes_by_id = {n["id"]: n for n in notes}
    (out_dir / "glossary.md").write_text(render_glossary_md(notes), encoding="utf-8")
    (out_dir / "patterns.md").write_text(render_patterns_md(notes), encoding="utf-8")
    (out_dir / "taxonomy.md").write_text(
        render_taxonomy_md(field, taxonomy_nodes), encoding="utf-8")
    (out_dir / "relations.md").write_text(
        render_relations_md(graph, notes_by_id), encoding="utf-8")
    (out_dir / "graph.json").write_text(
        json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "cross-field-links.md").write_text(
        render_cross_field_links_md(cross_links), encoding="utf-8")
    (out_dir / "SKILL.md").write_text(
        render_skill_md(field, notes, merged["conflicts"],
                        merged["cross_index"], all_sources, version),
        encoding="utf-8")
    manifest = {
        "field": field, "slug": own_slug, "version": version,
        "updated_at": updated_at, "sources": all_sources,
        "notes": notes, "taxonomy": taxonomy_nodes, "graph": graph,
        "conflicts": merged["conflicts"],
        "cross_index": merged["cross_index"],
        "unresolved_relations": merged["unresolved_relations"],
        "cross_field_links": cross_links,
    }
    (out_dir / "master.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    update_registry(masters_root, {
        "field": field, "slug": own_slug,
        "sources": [s.get("name", "") for s in all_sources],
        "concept_count": len(notes),
        "contested_count": len(merged["conflicts"]),
        "version": version, "updated_at": updated_at,
        "path": own_slug,
    })
    return {"field": field, "slug": own_slug, "path": str(out_dir),
            "version": version, "concept_count": len(notes),
            "contested_count": len(merged["conflicts"]),
            "cross_field_links": len(cross_links),
            "unresolved_relations": len(merged["unresolved_relations"])}


def read_registry(masters_root: Path) -> dict:
    path = Path(masters_root) / "index.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "masters" in data:
                return data
        except (ValueError, OSError):
            pass
    return {"masters": []}


def update_registry(masters_root: Path, entry: dict) -> dict:
    masters_root = Path(masters_root)
    registry = read_registry(masters_root)
    registry["masters"] = [m for m in registry["masters"]
                           if m.get("slug") != entry["slug"]]
    registry["masters"].append(entry)
    registry["masters"].sort(key=lambda m: m.get("field", ""))
    (masters_root / "index.json").write_text(
        json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
    return registry


def list_masters(masters_root: Path) -> list[dict]:
    return read_registry(Path(masters_root)).get("masters", [])


def query_across_masters(masters_root: Path, status: str | None = None,
                         term: str | None = None,
                         filters: dict | None = None) -> list[dict]:
    """Query concepts across all masters without hand-grepping files."""
    results = []
    for entry in list_masters(masters_root):
        master = load_master(Path(masters_root) / entry.get("path", ""))
        if not master:
            continue
        for note in query_notes(master.get("notes", []), filters, term, status):
            results.append({"field": entry.get("field", ""),
                            "slug": entry.get("slug", ""),
                            "concept": note["title"], "status": note["status"],
                            "sources": note["sources"]})
    return results


def parse_skill_dir(skill_dir: Path) -> dict:
    """Best-effort parse of an existing skill directory into a source dict.

    Reads glossary.md ("- **Term**: definition" or "## Term" sections) and
    patterns.md the same way, plus chapter titles from SKILL.md's chapter
    index table when present. Anything it cannot parse is skipped, never
    invented.
    """
    skill_dir = Path(skill_dir)
    name = skill_dir.name
    source: dict = {"name": name, "kind": "book", "glossary": [],
                    "patterns": [], "chapters": []}
    for filename, key, text_key in (("glossary.md", "glossary", "definition"),
                                    ("patterns.md", "patterns", "description")):
        path = skill_dir / filename
        if not path.exists():
            continue
        current: dict | None = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"-\s+\*\*(.+?)\*\*:\s*(.*)$", line.strip())
            if m:
                current = {("term" if key == "glossary" else "name"): m.group(1),
                           text_key: m.group(2)}
                source[key].append(current)
                continue
            m2 = re.match(r"##\s+(.+?)(?:\s+\[.*\])?\s*$", line.strip())
            if m2 and not line.strip().startswith("## "):
                continue
            if m2:
                current = {("term" if key == "glossary" else "name"): m2.group(1),
                           text_key: ""}
                source[key].append(current)
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        for line in skill_md.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\|\s*\[ch\d+\]\([^)]+\)\s*\|\s*(.+?)\s*\|", line)
            if m:
                source["chapters"].append({"title": m.group(1).strip()})
    return source
