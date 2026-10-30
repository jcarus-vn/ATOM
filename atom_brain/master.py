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

import hashlib
import json
import os
import re
import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

GRAPH_EDGE_TYPES = (
    "prerequisite_of", "contrasts_with", "example_of", "part_of", "used_in",
)
NOTE_STATUSES = ("single-source", "consensus", "contested")


_SYMBOL_WORDS = (("++", " plus plus "), ("+", " plus "), ("#", " sharp "),
                 ("*", " star "))


def _strip_latin_marks(text: str) -> str:
    """Drop combining marks that sit on a Latin base letter only.

    "Định vị" -> "Dinh vi", while Devanagari vowel signs, Thai tone marks
    and other scripts' combining marks are kept (dropping them would change
    the word, not just its accent).
    """
    out: list[str] = []
    base_is_latin = False
    for ch in unicodedata.normalize("NFKD", text):
        if unicodedata.combining(ch):
            if not base_is_latin:
                out.append(ch)
            continue
        base_is_latin = "LATIN" in unicodedata.name(ch, "")
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def _is_word_char(ch: str) -> bool:
    """Letters and digits of any script, plus combining marks (vowel signs,
    viramas, tone marks) so an Indic or Thai word is not cut in pieces."""
    return ch.isalnum() or unicodedata.category(ch).startswith("M")


def _word_runs(text: str, keep: str = "") -> list[str]:
    """Maximal runs of word characters. A *keep* symbol stays glued to the
    word it follows ("c++", "rrt*") or forms its own run ("+")."""
    runs: list[str] = []
    cur: list[str] = []
    for ch in text:
        if _is_word_char(ch):
            if cur and cur[-1] in keep:
                runs.append("".join(cur))
                cur = []
            cur.append(ch)
        elif ch in keep:
            cur.append(ch)
        elif cur:
            runs.append("".join(cur))
            cur = []
    if cur:
        runs.append("".join(cur))
    return runs


def slugify(value: str) -> str:
    """Stable, readable slug for file names and ids.

    Latin accents are stripped ("Định vị" -> "dinh-vi", "đ" -> "d"); the
    symbols that distinguish technical names are spelled out ("C++" ->
    "c-plus-plus", "C#" -> "c-sharp", "RRT*" -> "rrt-star"); letters of
    other scripts are kept ("Фильтр Калмана" -> "фильтр-калмана"), so a
    non-Latin title never collapses to the same slug as another one.
    ASCII titles without those symbols slug exactly as before.
    """
    text = value or ""
    for sym, word in _SYMBOL_WORDS:
        text = text.replace(sym, word)
    text = _strip_latin_marks(text).replace("đ", "d").replace("Đ", "D")
    text = "-".join(_word_runs(text.lower()))
    return text or "concept"


def normalize_title(value: str) -> str:
    """Title key for dedupe and matching.

    NFKC + casefold, words of every script kept, accents kept (in
    Vietnamese "bàn" and "bán" are different words), and the symbols that
    make a different concept kept on their word: "C++" != "C#" != "C",
    "RRT*" != "RRT". Punctuation and spacing are ignored, so ASCII titles
    without those symbols normalize exactly as before.
    """
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(_word_runs(text, keep="+#*"))


def citation_for(source: dict, chapter: str | None = None,
                 page: int | None = None) -> str:
    """One traceable citation string for a source (+ chapter/page)."""
    name = source.get("name", "unknown source")
    if source.get("kind") == "web":
        from atom_brain.web_input import web_citation
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


def group_sides(sides: list[dict]) -> list[dict]:
    """One entry per distinct definition, with every source that gave it
    (joined with "; "), in first-seen order. Pairs are never re-matched by
    position, so a definition is always shown with its own sources."""
    grouped: dict[str, list[str]] = {}
    for side in sides:
        cites = grouped.setdefault(side["definition"], [])
        if side["source"] not in cites:
            cites.append(side["source"])
    return [{"definition": d, "source": "; ".join(c)} for d, c in grouped.items()]


def render_contested_body(sides: list[dict]) -> str:
    """One bullet per distinct definition, followed by its own sources."""
    return "\n".join(f"- {s['definition']} (source: {s['source']})"
                     for s in group_sides(sides))


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
                        # (definition, citation) pairs, kept together from
                        # the moment they are read: a contested note shows
                        # each definition beside the source that gave it.
                        "sides": [],
                        # Source chapters the concept was read from; used to
                        # file it under the matching taxonomy area.
                        "chapters": [],
                        "sources": [],
                        "source_keys": [],
                        "facets": _facets_for(item, kind, [src]),
                    }
                    note_kinds[key] = set()
                    order.append(key)
                note = notes[key]
                if chapter and str(chapter) not in note["chapters"]:
                    note["chapters"].append(str(chapter))
                if cite not in note["sources"]:
                    note["sources"].append(cite)
                skey = _source_key(src)
                if skey and skey not in note["source_keys"]:
                    note["source_keys"].append(skey)
                    note_kinds[key] |= kinds_by_key.get(skey, set())
                if text and text not in note["definitions"]:
                    note["definitions"].append(text)
                if text:
                    side = {"definition": text, "source": cite}
                    if side not in note["sides"]:
                        note["sides"].append(side)
                kinds = note_kinds[key]
                note["facets"]["source_type"] = (
                    "book" if kinds == {"book"} else
                    "web" if kinds == {"web"} else "mixed")
                if note["facets"].get("topic") in (None, "general") and item.get("topic"):
                    note["facets"]["topic"] = item["topic"]
    used_ids: set[str] = set()
    for key in order:
        note = notes[key]
        base = note["id"]
        candidate = base
        if candidate in used_ids:
            candidate = f"{base}-{note['kind']}"
        n = 2
        while candidate in used_ids:
            candidate = f"{base}-{note['kind']}-{n}"
            n += 1
        note["id"] = candidate
        used_ids.add(candidate)
    merged_notes = []
    for key in order:
        note = notes[key]
        note["status"] = _status_for(note["definitions"], len(note["source_keys"]))
        if note["status"] == "contested":
            note["body"] = render_contested_body(note["sides"])
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
         "definitions": group_sides(n["sides"]),
         "sources": list(n["sources"])}
        for n in merged_notes if n["status"] == "contested"
    ]
    return {"notes": merged_notes, "cross_index": cross_index,
            "conflicts": conflicts, "unresolved_relations": unresolved}


_SOURCE_KEYS = {"name", "kind", "url", "fetched_at", "chapters", "glossary",
                "patterns", "relations", "aliases", "taxonomy"}
_ITEM_KEYS = {
    "glossary": ({"term", "definition"}, {"chapter", "page", "topic", "level", "technique"}),
    "patterns": ({"name", "description"}, {"chapter", "page", "topic", "level", "technique"}),
    "relations": ({"from", "type", "to"}, {"chapter"}),
}


def validate_manifest(manifest: dict) -> list[str]:
    """Problems that would make build_master silently drop content.

    The builder reads fixed keys; a manifest with "def" for "definition"
    or "title" for "name" used to build a master with those items missing
    and exit 0. Returns readable problems (empty list = fine).
    """
    problems: list[str] = []
    if not isinstance(manifest, dict) or not isinstance(manifest.get("sources"), list):
        return ['top level must be an object with a "sources" list']
    for i, src in enumerate(manifest["sources"]):
        where = f"sources[{i}] ({src.get('name', '?') if isinstance(src, dict) else '?'})"
        if not isinstance(src, dict):
            problems.append(f"{where}: must be an object")
            continue
        if not src.get("name"):
            problems.append(f"{where}: missing \"name\"")
        if src.get("kind") not in ("book", "web"):
            problems.append(f"{where}: \"kind\" must be \"book\" or \"web\"")
        if src.get("kind") == "web" and not src.get("url"):
            problems.append(f"{where}: a web source needs \"url\"")
        for key in sorted(set(src) - _SOURCE_KEYS):
            problems.append(f"{where}: unknown key \"{key}\"")
        for list_key, (required, optional) in _ITEM_KEYS.items():
            for j, item in enumerate(src.get(list_key) or []):
                if not isinstance(item, dict):
                    problems.append(f"{where}.{list_key}[{j}]: must be an object")
                    continue
                missing = sorted(required - set(item))
                unknown = sorted(set(item) - required - optional)
                if missing:
                    problems.append(f"{where}.{list_key}[{j}]: missing {missing}")
                if unknown:
                    problems.append(f"{where}.{list_key}[{j}]: unknown {unknown}")
                if list_key == "relations" and item.get("type") not in GRAPH_EDGE_TYPES:
                    problems.append(f"warning: {where}.relations[{j}]: type "
                                    f"{item.get('type')!r} is not one of "
                                    f"{', '.join(GRAPH_EDGE_TYPES)}; it will be "
                                    "listed as unresolved, not drawn")
    return problems


def build_taxonomy(notes: list[dict], sources: list[dict],
                   manifest_taxonomy: dict | None = None,
                   existing_nodes: list[dict] | None = None) -> list[dict]:
    """Build (or incrementally update) the taxonomy node list.

    Levels: field root is implicit (the master directory itself); nodes
    here are level 2 areas and level 3 concepts. A concept may list several
    parent areas (polyhierarchy). When existing_nodes are given (fold-in),
    their labels are kept, nodes marked ``"manual": true`` keep their
    parents, other nodes are re-placed from the current sources, and new
    concepts are appended.
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
        note_chapters = {normalize_title(c) for c in note.get("chapters", [])}
        for area_name, topics in areas:
            area_id = area_ids.get(normalize_title(area_name))
            if not area_id:
                continue
            if any(normalize_title(t) == normalize_title(note["title"]) for t in topics):
                parents.append(area_id)
            elif normalize_title(note["facets"].get("topic", "")) == normalize_title(area_name):
                parents.append(area_id)
            elif normalize_title(area_name) in note_chapters:
                parents.append(area_id)
        # No matching area: the concept stays unplaced (listed as such in
        # taxonomy.md) instead of being filed under whichever area came
        # first, which put 94 of 118 Robotics concepts under "Preface".
        existing = next((nd for nd in nodes if nd["id"] == note["id"]), None)
        if existing is not None:
            existing["status"] = note["status"]
            existing["sources"] = list(note["sources"])
            # A node placed by hand ("manual": true) keeps its parents; a
            # builder-placed node is re-placed from the current sources, so
            # a wrong placement from an older build does not stick forever.
            if parents and not existing.get("manual"):
                existing["parents"] = list(dict.fromkeys(parents))
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
                term: str | None = None, status: str | None = None,
                source: str | None = None) -> list[dict]:
    """Filter notes by facet (topic/technique/level/source_type), term,
    status, and source. The source filter is a case-insensitive
    substring match over a note's source citations (and source keys
    when present), so it composes with every other filter: bounding a
    query to one book never silently re-admits another book's notes.
    """
    filters = filters or {}
    wanted = (source or "").strip().lower()
    out = []
    for note in notes:
        if status and note["status"] != status:
            continue
        if term and normalize_title(term) not in normalize_title(note["title"]):
            continue
        if wanted and not _matches_source(note, wanted):
            continue
        ok = True
        for key, value in filters.items():
            facet_value = (note["facets"].get(key) or "").lower()
            wanted_value = str(value).lower()
            if key == "technique" and wanted_value == normalize_title(note["title"]):
                continue  # a pattern is its own technique
            if facet_value != wanted_value:
                ok = False
                break
        if ok:
            out.append(note)
    return out


def _matches_source(note: dict, wanted_lower: str) -> bool:
    """True when *wanted_lower* names one of the note's sources."""
    for key in note.get("source_keys", []):
        if wanted_lower in str(key).lower():
            return True
    for citation in note.get("sources", []):
        if wanted_lower in str(citation).lower():
            return True
    return False


def bound_view(note: dict, source: str) -> dict:
    """The bound view of one note for a source filter.

    Architecture: the bound is a filtered VIEW inside the master,
    never a detour to the per-book extract skill. The master stays the
    answering layer (merged notes, consensus/contested status,
    taxonomy, graph); the bound only scopes which of the master's own
    notes and definitions are shown. The per-book skill remains input
    material only and is never loaded by any bound query path.

    A merged multi-source note never silently mixes books: for a
    contested note only the bullet definitions whose citation matches
    the bound are shown; for any note the matching and hidden sources
    are listed separately. Returns ``body``, ``matching_sources`` and
    ``hidden_sources``.
    """
    wanted = (source or "").strip().lower()
    matching = [s for s in note.get("sources", [])
                if wanted in str(s).lower()]
    hidden = [s for s in note.get("sources", [])
              if wanted not in str(s).lower()]
    body = note.get("body", "")
    if note.get("status") == "contested" and wanted:
        bullets = [line for line in body.splitlines()
                   if line.strip().startswith("- ")]
        kept = [line for line in bullets if wanted in line.lower()]
        if bullets and kept:
            body = "\n".join(kept)
    return {"body": body, "matching_sources": matching,
            "hidden_sources": hidden}

# ---------------------------------------------------------------------------
# Ranked retrieval: the shared question-answering path.
# ---------------------------------------------------------------------------
# Used by both the product query (scripts/build_master.py --question)
# and the answer-quality eval (tools/eval_answer_quality.py), so a
# measured improvement is a product improvement, not an eval artifact.
#
# Scoring, in order of mechanism (all weights fixed here, none tuned
# per question):
# - term overlap: 3 per title-term hit, 2 per taxonomy-label hit,
#   1 per body-term hit (the pre-existing weights, preserved);
# - title coverage: at most +1 for a note whose whole title is named
#   by the question. Bounded on purpose: full coverage rescoring
#   (replacing hit counts with coverage) was prototyped and made
#   old-set recall worse (0.77 -> 0.51), because long pattern titles
#   that restate the question then outranked every definition;
# - facet topic: 2 per matching term from the note's own declared
#   topic facet, the same routing idea as taxonomy labels;
# - concept identity: +6 when the question names the note's concept
#   exactly (full title, or a parenthesized acronym in the title such
#   as "(SLAM)"). Canonical definition notes used to lose to
#   keyword-dense pattern notes that merely mention the concept;
# - definitional glossary bonus: +3 for glossary notes when the
#   question asks what something is ("what", "define", "mean").
#   Definitions should answer definition questions;
# - expansion: +4 for a note that shares a source chapter, or a typed
#   concept-graph edge, with a top-3 seed, provided it already has a
#   positive base score. The bonus is fixed, not proportional to the
#   seed: a proportional bonus (0.5 x seed) was prototyped and flooded
#   the top-k with mediocre same-chapter notes. Area ("part_of")
#   edges are excluded: area nodes are chapter buckets, not concepts,
#   and expanding through them would promote whole chapters at once.
# A note's chapter keys are the base URLs of its source citations.

_RANK_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "what", "which", "who", "whom", "when", "where", "why", "how",
    "do", "does", "did", "can", "could", "should", "would", "will",
    "of", "in", "on", "at", "to", "for", "from", "with", "without",
    "by", "and", "or", "but", "not", "no", "than", "then", "into",
    "its", "it", "as", "that", "this", "these", "those", "there",
    "between", "under", "over", "each", "other", "both", "per",
    # Vietnamese function words (syllables), so a Vietnamese question is
    # not ranked by "là", "của", "như thế nào".
    "là", "gì", "của", "và", "các", "những", "một", "có", "không", "được",
    "trong", "cho", "với", "để", "này", "đó", "khi", "nào", "thế", "như",
    "sao", "tại", "vì", "thì", "mà", "nên", "hay", "hoặc", "ra", "bị",
    "ở", "về", "từ", "đến", "theo", "nó", "chúng", "gồm", "làm",
}
# Words that mark a definition question ("what is", "là gì", "định nghĩa").
_DEFINITIONAL = {"what", "define", "definition", "mean", "means",
                 "gì", "nghĩa"}

CONCEPT_BOOST = 6.0
GLOSSARY_BONUS = 3.0
EXPANSION_BONUS = 4.0
EXPANSION_SEEDS = 3


_CJK_RUN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _raw_terms(text: str) -> list[str]:
    """Normalized word runs of *text* (any script), symbols dropped.

    ASCII text yields exactly the tokens the old ``[a-z0-9]+`` scan did.
    Han/kana runs carry no spaces, so they are split into overlapping
    character bigrams ("卡尔曼滤波" -> "卡尔", "尔曼", ...), the usual
    dependency-free way to match CJK text.
    """
    out: list[str] = []
    for run in _word_runs(normalize_title(text or "")):
        if _CJK_RUN.search(run) and len(run) > 2:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
        else:
            out.append(run)
    return out


def _rank_terms(text: str) -> list[str]:
    """Content terms of *text*: normalized tokens minus stopwords."""
    return [t for t in _raw_terms(text)
            if t not in _RANK_STOPWORDS
            and (len(t) >= 2 or not t.isascii())]


def _chapter_keys(note: dict) -> set[str]:
    """Base URLs of a note's source citations (one key per chapter)."""
    keys = set()
    for citation in note.get("sources", []):
        for url in re.findall(r"https?://\S+", citation or ""):
            keys.add(url.rstrip("/").split("(")[0])
    return keys


def _concept_adjacency(notes: list[dict],
                       graph_edges: list[dict]) -> dict[str, list[str]]:
    """Concept-to-concept adjacency from graph edges and note links.

    Area nodes (ids starting with "area-") are scaffolding, not
    concepts, so edges touching them are ignored here.
    """
    adjacency: dict[str, list[str]] = {}
    pairs = [(e.get("from", ""), e.get("to", ""))
             for e in (graph_edges or [])]
    for note in notes:
        for link in note.get("links", []):
            pairs.append((note.get("id", ""), link.get("to", "")))
    for source, target in pairs:
        if not source or not target:
            continue
        if source.startswith("area-") or target.startswith("area-"):
            continue
        adjacency.setdefault(source, []).append(target)
        adjacency.setdefault(target, []).append(source)
    return adjacency


def rank_notes(notes: list[dict], taxonomy_nodes: list[dict],
               graph_edges: list[dict], question: str,
               k: int | None = None, source: str | None = None,
               params: dict | None = None) -> list[dict]:
    """Rank notes for a natural-language *question*.

    Returns the full ranking by default (callers slice to top-k);
    pass ``k`` to truncate here. With ``source`` set, the candidate
    pool is restricted to notes from that source BEFORE ranking, so
    seeds, chapter/graph expansion, and every returned note stay
    inside the bound: a bound answer can never smuggle in another
    book's notes through expansion. Each entry carries ``note``,
    ``score`` (final score, expansion included), ``title_hits``,
    ``tax_hits``, ``body_hits``, ``concept_boost`` and
    ``expansion_bonus``. Ties break by note id so rankings are
    reproducible.

    ``params`` overrides individual ranker knobs without changing the
    shipped defaults: ``concept_boost`` (default 6.0),
    ``glossary_bonus`` (3.0), ``expansion_bonus`` (4.0),
    ``expansion_seeds`` (3), ``title_norm`` (True, whether the
    title-coverage term counts). Unknown keys are ignored, so a
    variant grid can pass partial overrides.
    """
    if source and str(source).strip():
        wanted = str(source).strip().lower()
        notes = [n for n in notes if _matches_source(n, wanted)]
    cfg = {
        "concept_boost": CONCEPT_BOOST,
        "glossary_bonus": GLOSSARY_BONUS,
        "expansion_bonus": EXPANSION_BONUS,
        "expansion_seeds": EXPANSION_SEEDS,
        "title_norm": True,
    }
    if params:
        for key in cfg:
            if key in params:
                cfg[key] = params[key]
    qterms = set(_rank_terms(question))
    qnorm = normalize_title(question)
    qraw = set(_raw_terms(question))
    node_labels: dict[str, set[str]] = {}
    for node in taxonomy_nodes or []:
        label_terms = set(_rank_terms(node.get("label", "")))
        for url in node.get("sources", []):
            for found in re.findall(r"https?://\S+", url):
                node_labels.setdefault(found, set()).update(label_terms)
    definitional = bool(qraw & _DEFINITIONAL)
    base: dict[str, dict] = {}
    for note in notes:
        title_terms = set(_rank_terms(note.get("title", "")))
        body_terms = set(_rank_terms(note.get("body", "")))
        title_hits = len(qterms & title_terms)
        coverage = title_hits / len(title_terms) if title_terms else 0.0
        tax_terms: set[str] = set()
        for citation in note.get("sources", []):
            for url in re.findall(r"https?://\S+", citation):
                tax_terms.update(node_labels.get(url, set()))
        tax_hits = len(qterms & tax_terms)
        body_hits = len(qterms & body_terms)
        topic_terms = set(_rank_terms((note.get("facets") or {}).get("topic", "")))
        topic_hits = len(qterms & topic_terms)
        title_norm = normalize_title(note.get("title", ""))
        concept = 0.0
        if title_norm and title_norm in qnorm:
            concept = 1.0
        else:
            match = re.search(r"\(([^)]+)\)", note.get("title", ""))
            if match and normalize_title(match.group(1)) in qnorm:
                concept = 1.0
        glossary = 1.0 if (definitional
                            and note.get("kind") == "glossary") else 0.0
        score = (3 * title_hits
                 + (coverage if cfg["title_norm"] else 0.0)
                 + 2 * tax_hits + body_hits
                 + 2 * topic_hits + cfg["concept_boost"] * concept
                 + cfg["glossary_bonus"] * glossary)
        base[note.get("id", "")] = {
            "note": note, "score": score, "title_hits": title_hits,
            "tax_hits": tax_hits, "body_hits": body_hits,
            "concept_boost": concept, "expansion_bonus": 0.0,
        }
    order = sorted(base.values(),
                   key=lambda r: (-r["score"], r["note"].get("id", "")))
    seeds = order[:cfg["expansion_seeds"]]
    seed_chapters: set[str] = set()
    for seed in seeds:
        seed_chapters.update(_chapter_keys(seed["note"]))
    adjacency = _concept_adjacency(notes, graph_edges)
    seed_ids = {s["note"].get("id", "") for s in seeds}
    final: dict[str, float] = {}
    for note_id, entry in base.items():
        bonus = 0.0
        if note_id not in seed_ids and entry["score"] > 0:
            if _chapter_keys(entry["note"]) & seed_chapters:
                bonus = cfg["expansion_bonus"]
            elif any(nb in seed_ids for nb in adjacency.get(note_id, [])):
                bonus = cfg["expansion_bonus"]
        entry["expansion_bonus"] = bonus
        final[note_id] = entry["score"] + bonus
    ranked = sorted(base.values(),
                    key=lambda r: (-final[r["note"].get("id", "")],
                                   r["note"].get("id", "")))
    for entry in ranked:
        entry["score"] = final[entry["note"].get("id", "")]
    if k is not None and k > 0:
        return ranked[:k]
    return ranked


def suggest_related(notes: list[dict], graph_edges: list[dict],
                    retrieved_ids: list[str], limit: int = 5,
                    source: str | None = None) -> list[dict]:
    """Suggest 3-5 related keywords for an answer, from the master only.

    Suggestions are real notes, never invented: graph neighbors of the
    retrieved notes (1 hop, then 2 hops via typed concept edges) first,
    then notes sharing a source chapter with a retrieved note
    (taxonomy siblings). Retrieved notes themselves are excluded, and
    suggestions are presented separately from the answer so the answer
    stays concise. With ``source`` set, the suggestion pool is
    restricted to that source's notes, so bound mode cannot leak
    another book's keywords into the suggestion list. Returns dicts
    with ``id``, ``title`` and ``reason``.
    """
    if source and str(source).strip():
        wanted = str(source).strip().lower()
        notes = [n for n in notes if _matches_source(n, wanted)]
    by_id = {n.get("id", ""): n for n in notes}
    retrieved = [i for i in retrieved_ids if i in by_id]
    retrieved_set = set(retrieved)
    adjacency = _concept_adjacency(notes, graph_edges)
    chapters: dict[str, set[str]] = {}
    for note in notes:
        for key in _chapter_keys(note):
            chapters.setdefault(key, set()).add(note.get("id", ""))
    one_hop: dict[str, list[str]] = {}
    for rid in retrieved:
        for nb in adjacency.get(rid, []):
            if nb in by_id and nb not in retrieved_set:
                one_hop.setdefault(nb, []).append(rid)
    two_hop: dict[str, list[str]] = {}
    for rid in retrieved:
        for mid in adjacency.get(rid, []):
            for nb in adjacency.get(mid, []):
                if nb in by_id and nb not in retrieved_set \
                        and nb not in one_hop:
                    two_hop.setdefault(nb, []).append(mid)
    siblings: dict[str, list[str]] = {}
    for rid in retrieved:
        for key in _chapter_keys(by_id[rid]):
            for nid in chapters.get(key, set()):
                if nid not in retrieved_set and nid not in one_hop \
                        and nid not in two_hop:
                    siblings.setdefault(nid, []).append(rid)
    tiers = [
        (one_hop, lambda nid, srcs: (
            f"graph: { _edge_type(graph_edges, notes, srcs[0], nid) }"
            f" of '{by_id[srcs[0]]['title']}'")),
        (two_hop, lambda nid, srcs: (
            f"graph: 2 hops via '{by_id[srcs[0]]['title']}'")),
        (siblings, lambda nid, srcs: (
            f"same chapter as '{by_id[srcs[0]]['title']}'")),
    ]
    out: list[dict] = []
    for tier, reason_fn in tiers:
        ordered = sorted(tier.items(),
                         key=lambda kv: (-len(kv[1]), kv[0]))
        for nid, srcs in ordered:
            if len(out) >= limit:
                break
            out.append({"id": nid, "title": by_id[nid].get("title", ""),
                        "reason": reason_fn(nid, srcs)})
        if len(out) >= limit:
            break
    return out


def _edge_type(graph_edges: list[dict], notes: list[dict],
               source: str, target: str) -> str:
    """Typed edge label between two notes, from graph edges or note links."""
    for edge in graph_edges or []:
        if {edge.get("from", ""), edge.get("to", "")} == {source, target}:
            return edge.get("type", "linked")
    by_id = {n.get("id", ""): n for n in notes}
    for link in (by_id.get(source) or {}).get("links", []):
        if link.get("to") == target:
            return link.get("type", "linked")
    return "linked"


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
    sides = []
    body_nonempty = [ln for ln in body.splitlines() if ln.strip()]
    for ln in body_nonempty:
        m = re.match(r"^-\s+(.+?)\s+\(source:\s+(.+)\)\s*$", ln.strip())
        if m:
            bullet_defs.append(m.group(1))
            sides.append({"definition": m.group(1), "source": m.group(2)})
    if bullet_defs and len(bullet_defs) == len(body_nonempty):
        note["definitions"] = list(dict.fromkeys(bullet_defs))
        note["sides"] = sides
    elif body:
        note["definitions"] = [body]
    else:
        note["definitions"] = []
    note.setdefault("kind", "glossary")
    note.setdefault("status", "single-source")
    note.setdefault("facets", {})
    return note


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
    """Render the taxonomy tree: field > area > concept.

    Navigation only, so it stays small: each concept line carries its
    status and a link to its note, where the citations live once. (The
    old render repeated every citation under every area and concept: on
    the Robotics master that was ~9,900 tokens for 140 nodes.) A concept
    under several areas appears under each (polyhierarchy), marked shared.
    """
    children: dict[str, list[dict]] = {}
    for node in nodes:
        for parent in node.get("parents", []):
            children.setdefault(parent, []).append(node)
    areas = [n for n in nodes if n["level"] == 2]
    concepts = {n["id"]: n for n in nodes if n["level"] == 3}
    lines = [f"# Taxonomy - {field}", "",
             "Navigation index only: open notes/<id>.md for the content and its "
             "sources. This tree never rewrites a note.", ""]
    for area in sorted(areas, key=lambda n: n["label"].lower()):
        kids = sorted(children.get(area["id"], []), key=lambda n: n["label"].lower())
        lines.append(f"## {area['label']} [{area['status']}] ({len(kids)})")
        lines.append("")
        for concept in kids:
            shared = " (shared)" if len(concept.get("parents", [])) > 1 else ""
            lines.append(f"- [{concept['label']}](notes/{concept['id']}.md) "
                         f"[{concept['status']}]{shared}")
        lines.append("")
    orphaned = [n for n in concepts.values() if not n.get("parents")]
    if orphaned:
        lines.append(f"## Unplaced concepts ({len(orphaned)})")
        lines.append("")
        for concept in sorted(orphaned, key=lambda n: n["label"].lower()):
            lines.append(f"- [{concept['label']}](notes/{concept['id']}.md) "
                         f"[{concept['status']}]")
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


def skill_name_for(slug: str) -> str:
    """A host-valid skill name (lowercase ASCII letters, digits, hyphens,
    at most 64): the slug when it already is one, else a stable ASCII
    form of it."""
    if re.fullmatch(r"[a-z0-9-]{1,64}", slug or ""):
        return slug
    digest = hashlib.sha256((slug or "").encode("utf-8")).hexdigest()[:8]
    ascii_part = re.sub(r"[^a-z0-9]+", "-", (slug or "").encode(
        "ascii", "ignore").decode().lower()).strip("-")[:40]
    return f"master-{ascii_part + '-' if ascii_part else ''}{digest}"


def render_skill_md(field: str, notes: list[dict], conflicts: list[dict],
                    cross_index: list[dict], sources: list[dict],
                    version: int, slug: str | None = None,
                    masters_root: Path | None = None) -> str:
    consensus = [n for n in notes if n["status"] == "consensus"]
    slug = slug or slugify(field)
    name = skill_name_for(slug)
    root = str(masters_root) if masters_root is not None else "<masters-root>"
    description = (
        f"Master knowledge skill for {field}: {len(notes)} atomic notes from "
        f"{len(sources)} source(s), every claim cited, disagreements kept as "
        f"contested. Use when a question is about {field}; rank notes with "
        f"the ATOM query command before reading any note.")
    description = description.replace('"', "'")[:1000]
    lines = [
        "---",
        f"name: {name}",
        f'description: "{description}"',
        "---", "",
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
            from atom_brain.web_input import web_citation
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
              "- Answer a question by ranking notes first (cheapest path):",
              f'  `python3 <atom skill dir>/scripts/build_master.py --masters-root "{root}" '
              f'--question "<question>" --top 5`, then open notes/<id>.md of the hits.',
              "  With a compact build, add `--compact-dir <dir>` instead of reading files.",
              "- No hit above the threshold means the master does not hold the answer: say so.",
              "- Read taxonomy.md only to browse by area; it is large.",
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
    present (hand edits survive); master.json is the fallback index.

    The notes the last build itself wrote (from master.json) are kept
    under ``_built_notes`` so a fold-in can tell a hand edit or a
    hand-added note apart from builder output; that key is never written
    back to disk.
    """
    master_dir = Path(master_dir)
    notes_dir = master_dir / "notes"
    manifest_path = master_dir / "master.json"
    manifest: dict | None = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            manifest = None
    notes: list[dict] = []
    if notes_dir.is_dir():
        for path in sorted(notes_dir.glob("*.md")):
            note = parse_note_md(path.read_text(encoding="utf-8"))
            if note is not None:
                notes.append(note)
    if notes:
        manifest = dict(manifest or {})
        if "notes" in manifest:
            manifest["_built_notes"] = [
                {"id": n.get("id", ""), "title": n.get("title", ""),
                 "body": n.get("body", "")}
                for n in (manifest.get("notes") or [])]
        manifest["notes"] = notes
        return manifest
    return manifest


def _legacy_contested_body(note: dict) -> str:
    """The contested body older builds wrote (definitions zipped with
    citations by index). A note still carrying exactly that text was not
    edited by hand, only rendered by the old, misattributing code."""
    return "\n".join(f"- {d} (source: {c})"
                     for d, c in zip(note.get("definitions", []),
                                     note.get("sources", [])))


def _norm_body(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").strip().splitlines())


def _was_hand_edited(old: dict, generated: dict | None,
                     built_body: str | None) -> bool:
    """Did a person change this note's body since the builder wrote it?

    Compared against what the builder produces from the OLD sources
    (re-merged now, in both the current and the pre-fix contested format)
    and against the body master.json recorded. With neither reference a
    differing body is kept: the safe default.
    """
    body = _norm_body(old.get("body", ""))
    if not body:
        return False
    refs = []
    if generated is not None:
        refs += [generated.get("body", ""), _legacy_contested_body(generated)]
    if built_body is not None:
        refs.append(built_body)
    if not refs:
        return True
    return body not in {_norm_body(r) for r in refs}


def _write_atomically(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
    hand_edited: list[str] = []
    review: list[str] = []
    kept_manual: list[str] = []
    if existing:
        # What the builder produced last time, re-derived from the old
        # sources: a note body equal to it is builder output and takes the
        # new merge (a corrected or newly contested definition shows up);
        # a body that differs was edited by hand and is kept.
        old_generated: dict[str, dict] = {}
        if any(src.get("glossary") or src.get("patterns")
               for src in existing.get("sources", [])):
            for n in merge_sources(existing.get("sources", []))["notes"]:
                old_generated[normalize_title(n["title"])] = n
        built_notes = existing.get("_built_notes")
        built = {normalize_title(n.get("title", "")): n
                 for n in built_notes or []}
        built_ids = {n.get("id") for n in built_notes or []}
        existing_by_title = {normalize_title(n["title"]): n
                             for n in existing.get("notes", [])}
        merged_titles = {normalize_title(n["title"]) for n in notes}
        for note in notes:
            key = normalize_title(note["title"])
            old = existing_by_title.get(key)
            if old is None or not old.get("body"):
                continue
            built_body = built[key]["body"] if key in built else None
            old_gen = old_generated.get(key)
            if old_gen is not None:
                # Facets and typed links edited by hand survive too: a
                # facet that differs from what the old sources gave, and a
                # link the old sources never produced, were set by a person.
                old_facets = old.get("facets") or {}
                if old_facets and old_facets != old_gen.get("facets"):
                    note["facets"] = dict(old_facets)
                    if note["title"] not in hand_edited:
                        hand_edited.append(note["title"])
                generated_links = {(lk.get("to"), lk.get("type"))
                                   for lk in old_gen.get("links", [])}
                current_links = {(lk.get("to"), lk.get("type"))
                                 for lk in note.get("links", [])}
                for link in old.get("links", []):
                    pair = (link.get("to"), link.get("type"))
                    if pair not in generated_links and pair not in current_links:
                        note.setdefault("links", []).append(dict(link))
                        current_links.add(pair)
                        if note["title"] not in hand_edited:
                            hand_edited.append(note["title"])
            if not _was_hand_edited(old, old_gen, built_body):
                continue
            if _norm_body(note["body"]) != _norm_body(old["body"]) and (
                    old_gen is None
                    or old_gen.get("sources") != note["sources"]
                    or old_gen.get("status") != note["status"]):
                # The sources under a hand-edited note changed: the edit
                # wins, but say so, so a person re-reads it.
                review.append(note["title"])
            note["body"] = old["body"]
            note["hand_edited"] = True
            if note["title"] not in hand_edited:
                hand_edited.append(note["title"])
        # Notes a person added by hand (never written by a build) are
        # kept; notes an earlier build wrote whose concept no source gives
        # any more are dropped as stale.
        for old in existing.get("notes", []):
            key = normalize_title(old.get("title", ""))
            if key in merged_titles or old.get("id") in built_ids:
                continue
            kept = dict(old)
            kept.setdefault("facets", {})
            kept.setdefault("links", [])
            kept.setdefault("sources", [])
            kept.setdefault("source_keys", [])
            kept["manual"] = True
            notes.append(kept)
            kept_manual.append(kept.get("title", ""))
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

    manifest = {
        "field": field, "slug": own_slug, "version": version,
        "updated_at": updated_at, "sources": all_sources,
        "notes": notes, "taxonomy": taxonomy_nodes, "graph": graph,
        "conflicts": merged["conflicts"],
        "cross_index": merged["cross_index"],
        "unresolved_relations": merged["unresolved_relations"],
        "cross_field_links": cross_links,
    }
    notes_by_id = {n["id"]: n for n in notes}
    files = {
        "glossary.md": render_glossary_md(notes),
        "patterns.md": render_patterns_md(notes),
        "taxonomy.md": render_taxonomy_md(field, taxonomy_nodes),
        "relations.md": render_relations_md(graph, notes_by_id),
        "graph.json": json.dumps(graph, indent=2, ensure_ascii=False),
        "cross-field-links.md": render_cross_field_links_md(cross_links),
        "SKILL.md": render_skill_md(field, notes, merged["conflicts"],
                                    merged["cross_index"], all_sources,
                                    version, slug=own_slug,
                                    masters_root=masters_root),
        "master.json": json.dumps(manifest, indent=2, ensure_ascii=False),
    }
    _swap_in_master_dir(out_dir, notes, files)
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
            "unresolved_relations": len(merged["unresolved_relations"]),
            "hand_edited": hand_edited,
            "hand_edited_needing_review": review,
            "kept_manual_notes": kept_manual}


def _swap_in_master_dir(out_dir: Path, notes: list[dict],
                        files: dict[str, str]) -> None:
    """Write a whole master next to the old one, then swap it in.

    The old directory is copied first so files the builder does not own
    (a manifest, a person's own notes file) carry over; notes/ is rebuilt
    from scratch. A failure before the swap leaves the old master
    untouched. The previous version stays as ``.<slug>.prev`` beside it:
    one step of undo.
    """
    out_dir = Path(out_dir)
    parent = out_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f".{out_dir.name}.tmp-{os.getpid()}"
    prev = parent / f".{out_dir.name}.prev"
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        if out_dir.is_dir():
            shutil.copytree(out_dir, tmp, symlinks=True)
            if (tmp / "notes").exists():
                shutil.rmtree(tmp / "notes")
        else:
            tmp.mkdir()
        notes_dir = tmp / "notes"
        notes_dir.mkdir()
        for note in notes:
            (notes_dir / f"{note['id']}.md").write_text(
                render_note_md(note), encoding="utf-8")
        for name, text in files.items():
            (tmp / name).write_text(text, encoding="utf-8")
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if prev.exists():
        shutil.rmtree(prev)
    if out_dir.exists():
        os.replace(out_dir, prev)
    os.replace(tmp, out_dir)


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
    masters_root.mkdir(parents=True, exist_ok=True)
    _write_atomically(masters_root / "index.json",
                      json.dumps(registry, indent=2, ensure_ascii=False))
    return registry


def list_masters(masters_root: Path) -> list[dict]:
    return read_registry(Path(masters_root)).get("masters", [])


def query_across_masters(masters_root: Path, status: str | None = None,
                         term: str | None = None,
                         filters: dict | None = None,
                         source: str | None = None) -> list[dict]:
    """Query concepts across all masters without hand-grepping files."""
    results = []
    for entry in list_masters(masters_root):
        master = load_master(Path(masters_root) / entry.get("path", ""))
        if not master:
            continue
        for note in query_notes(master.get("notes", []), filters, term,
                                status, source):
            results.append({"field": entry.get("field", ""),
                            "slug": entry.get("slug", ""),
                            "concept": note["title"], "status": note["status"],
                            "sources": note["sources"]})
    return results


_GLOSSARY_LINE = re.compile(
    r"^(?:[-*]\s+)?\*\*(?P<term>.+?)\*\*\s*(?:[:\u2014\u2013]|-{1,2})\s*(?P<text>.*?)"
    r"(?:\s*\((?P<ch>(?:ch|chapter)\.?\s*[^)]*)\))?\s*$", re.IGNORECASE)


def parse_skill_dir(skill_dir: Path) -> dict:
    """Best-effort parse of an existing skill directory into a source dict.

    glossary.md: "**Term** — definition (Ch N)" (the format SKILL.md Step 8
    asks for) and "- **Term**: definition" both work. patterns.md: each
    "## Name" section, its lines kept as the description. Chapter titles
    come from SKILL.md's chapter index table. Anything it cannot parse is
    skipped, never invented.
    """
    skill_dir = Path(skill_dir)
    source: dict = {"name": skill_dir.name, "kind": "book", "glossary": [],
                    "patterns": [], "chapters": []}
    path = skill_dir / "glossary.md"
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _GLOSSARY_LINE.match(line.strip())
            if m and m.group("text"):
                item = {"term": m.group("term").strip(),
                        "definition": m.group("text").strip()}
                if m.group("ch"):
                    item["chapter"] = re.sub(r"^(?:ch|chapter)\.?\s*", "",
                                             m.group("ch"), flags=re.I).strip()
                source["glossary"].append(item)
    path = skill_dir / "patterns.md"
    if path.exists():
        current: dict | None = None
        body: list[str] = []

        def flush() -> None:
            if current is not None:
                current["description"] = " ".join(
                    ln.strip() for ln in body if ln.strip())
                source["patterns"].append(current)

        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^##\s+(.+?)(?:\s+\[.*\])?\s*$", line)
            if m:
                flush()
                current, body = {"name": m.group(1).strip()}, []
                continue
            if current is not None and not line.startswith("# "):
                body.append(line)
            elif current is None:
                gm = _GLOSSARY_LINE.match(line.strip())
                if gm and gm.group("text"):
                    source["patterns"].append({"name": gm.group("term").strip(),
                                               "description": gm.group("text").strip()})
        flush()
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        for line in skill_md.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\|\s*\[ch\d+\]\([^)]+\)\s*\|\s*(.+?)\s*\|", line)
            if m:
                source["chapters"].append({"title": m.group(1).strip()})
    return source
