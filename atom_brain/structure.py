"""Structure optimization for ATOM master skills.

This module is intentionally read-only toward :mod:`atom_brain.master`: it
imports only the stable title helpers (``normalize_title``,
``slugify``) and operates on plain note dicts (the shape ``master.json``
stores: id, title, kind, status, facets, sources, source_keys,
definitions, body, links), so it keeps working while the retrieval path
in ``atom_brain.master`` is being reworked in parallel. No function in
``atom_brain.master`` is modified by this module.

It implements five structure rules:

1. Slim taxonomy/index: node labels plus note references only. Full
   citations live in the notes themselves, never repeated per node.
2. One canonical definition note per concept, so concept-identity ranking
   has a single target; pattern notes link back to the definitions they
   use instead of duplicating them.
3. Bound views as lightweight indexes: a bound view is a list of note
   slugs plus the per-source definitions for merged notes, generated from
   the master. It never copies full notes and never mixes in other books.
4. Fragment merge: a thin web source attaches as an additional
   definition/facet to an EXISTING concept and spawns a new note only when
   the concept is genuinely new, so many thin sources cannot sprawl the
   note count.
5. Coverage metadata: a label computed from existing metadata only
   (concept count, distinct works, single-source ratio, contested count,
   body words), so a thin master is visibly thin.
"""

from __future__ import annotations

import re

from atom_brain.master import normalize_title as norm_title
from atom_brain.master import slugify as _slug

__all__ = [
    "bound_view_for_source",
    "canonical_definition_map",
    "coverage_report",
    "dedupe_report",
    "find_duplicate_concepts",
    "link_patterns_to_definitions",
    "merge_fragment",
    "merge_fragments",
    "near_duplicate_candidates",
    "norm_title",
    "render_bound_view_md",
    "render_slim_taxonomy",
    "slim_index_text",
]

# ``norm_title`` and ``_slug`` are imported from atom_brain.master above
# (single source of truth for title normalization and slugs).


# ---------------------------------------------------------------------------
# 1. Slim taxonomy / index
# ---------------------------------------------------------------------------

def render_slim_taxonomy(field: str, nodes: list[dict]) -> str:
    """Render a taxonomy index with labels and note references only.

    Contrast with the legacy render, which repeats the full ``Sources:``
    citation list under every area and every concept. Here each concept
    line points at its note file, where the citations live exactly once.
    """
    children: dict[str, list[dict]] = {}
    for node in nodes:
        for parent in node.get("parents", []) or []:
            children.setdefault(parent, []).append(node)
    areas = [n for n in nodes if n.get("level") == 2]
    concepts = {n["id"]: n for n in nodes if n.get("level") == 3}
    lines = [
        f"# Taxonomy - {field}",
        "",
        "Navigation index only: labels and note references. Citations live "
        "in the notes themselves (notes/<id>.md), never repeated here.",
        "",
    ]
    for area in sorted(areas, key=lambda n: str(n.get("label", "")).lower()):
        lines.append(f"## {area['label']} [{area.get('status', '')}]")
        for concept in sorted(children.get(area["id"], []),
                              key=lambda n: str(n.get("label", "")).lower()):
            shared = " (shared)" if len(concept.get("parents", []) or []) > 1 else ""
            lines.append(
                f"- {concept['label']} [{concept.get('status', '')}]"
                f" -> notes/{concept['id']}.md{shared}"
            )
        lines.append("")
    orphaned = [n for n in concepts.values() if not n.get("parents")]
    if orphaned:
        lines.append("## Unplaced concepts")
        for concept in sorted(orphaned, key=lambda n: str(n.get("label", "")).lower()):
            lines.append(
                f"- {concept['label']} [{concept.get('status', '')}]"
                f" -> notes/{concept['id']}.md"
            )
        lines.append("")
    return "\n".join(lines)


def slim_index_text(nodes: list[dict]) -> str:
    """One line per taxonomy node (label + id) for token measurement.

    This is the lookup payload a retriever actually needs: find the node,
    then load exactly one note. It deliberately excludes citations.
    """
    return "\n".join(f"{n.get('label', '')} {n.get('id', '')}" for n in nodes)


# ---------------------------------------------------------------------------
# 2. Canonical definitions, duplicates, pattern links
# ---------------------------------------------------------------------------

_STATUS_RANK = {"contested": 0, "consensus": 1, "single-source": 2}
_KIND_RANK = {"glossary": 0, "pattern": 1}


def find_duplicate_concepts(notes: list[dict]) -> dict[str, list[str]]:
    """Groups of note ids sharing one normalized title (exact duplicates).

    Exact-normalized duplicates are safe to canonicalize automatically.
    Near-duplicates (subset titles) are NOT merged here; see
    :func:`near_duplicate_candidates`, which flags them for human review,
    because titles like "Kalman filter" and "Extended Kalman filter" are
    genuinely different concepts.
    """
    groups: dict[str, list[str]] = {}
    for note in notes:
        groups.setdefault(norm_title(note.get("title", "")), []).append(note["id"])
    return {title: ids for title, ids in groups.items() if len(ids) > 1}


def near_duplicate_candidates(notes: list[dict]) -> list[tuple[str, str]]:
    """Pairs where one normalized title's tokens are a strict subset of
    another's. Candidates for human review only, never auto-merged."""
    by_title: dict[str, str] = {}
    for note in notes:
        by_title.setdefault(norm_title(note.get("title", "")), note["id"])
    titles = sorted(by_title)
    pairs: list[tuple[str, str]] = []
    for i, first in enumerate(titles):
        first_tokens = set(first.split())
        if not first_tokens:
            continue
        for second in titles[i + 1:]:
            second_tokens = set(second.split())
            if first_tokens < second_tokens:
                pairs.append((by_title[first], by_title[second]))
            elif second_tokens < first_tokens:
                pairs.append((by_title[second], by_title[first]))
    return pairs


def canonical_definition_map(notes: list[dict]) -> dict[str, str]:
    """One canonical note id per normalized concept title.

    Preference order: glossary over pattern (a pattern describes a
    procedure, it is not the definition), then contested over consensus
    over single-source within the same kind (the contested note already
    holds both sides, so it is the complete definition), then smallest id
    as a deterministic tie-break.
    """
    best: dict[str, dict] = {}
    for note in notes:
        key = norm_title(note.get("title", ""))
        if not key:
            continue
        rank = (_KIND_RANK.get(note.get("kind", ""), 9),
                _STATUS_RANK.get(note.get("status", ""), 9),
                str(note.get("id", "")))
        if key not in best or rank < best[key][0]:
            best[key] = (rank, note["id"])
    return {key: note_id for key, (_, note_id) in best.items()}


def link_patterns_to_definitions(
    notes: list[dict],
) -> tuple[dict[str, list[dict]], dict[str, list[str]]]:
    """Link pattern notes to the definition notes they use.

    A pattern note's body names the concepts it applies (e.g. an autonomy
    stack pattern names motion planning and trajectory optimization). For
    each glossary concept whose normalized title appears in the pattern
    body as a phrase (multi-word titles) or whole word (single-word
    titles), a ``used_in`` edge is proposed on the DEFINITION note
    pointing at the pattern: the definition is used in that pattern.
    Returns (links_by_definition_id, pattern_id -> [definition_ids]).
    Nothing is mutated; the caller decides which links to append.
    """
    glossary = [n for n in notes if n.get("kind") == "glossary"]
    links: dict[str, list[dict]] = {}
    pattern_map: dict[str, list[str]] = {}
    for pattern in notes:
        if pattern.get("kind") != "pattern":
            continue
        body = norm_title(pattern.get("body", ""))
        body_words = set(body.split())
        used: list[str] = []
        for definition in glossary:
            title = norm_title(definition.get("title", ""))
            if not title or len(title) < 3:
                continue
            if " " in title:
                hit = title in body
            else:
                hit = title in body_words
            if hit and definition["id"] != pattern["id"]:
                used.append(definition["id"])
        if used:
            pattern_map[pattern["id"]] = used
            for definition_id in used:
                links.setdefault(definition_id, []).append({
                    "type": "used_in",
                    "to": pattern["id"],
                    "sources": list(pattern.get("sources", [])),
                })
    return links, pattern_map


def dedupe_report(notes: list[dict]) -> dict:
    """Duplicate groups, near-duplicate candidates, canonical map."""
    return {
        "duplicate_groups": find_duplicate_concepts(notes),
        "near_duplicate_candidates": near_duplicate_candidates(notes),
        "canonical_map": canonical_definition_map(notes),
        "concepts": len({norm_title(n.get("title", "")) for n in notes if norm_title(n.get("title", ""))}),
        "notes": len(notes),
    }


# ---------------------------------------------------------------------------
# 3. Bound views (lightweight indexes, never copied notes)
# ---------------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _key_matches(candidate: str, source_key: str) -> bool:
    """Match a source key against a citation or source key by token
    subsequence, not raw substring. Raw substring matching is wrong:
    "book-a" is a substring of "porabook-area0" but is not that source.
    Token-subsequence matching accepts "porabook" and full URL paths
    while rejecting accidental infixes."""
    key_tokens = _tokens(source_key)
    if not key_tokens:
        return False
    cand_tokens = _tokens(candidate)
    width = len(key_tokens)
    return any(cand_tokens[i:i + width] == key_tokens
               for i in range(len(cand_tokens) - width + 1))


def _citation_matches(citation: str, source_key: str) -> bool:
    return _key_matches(citation, source_key)


def _note_matches_source(note: dict, source_key: str) -> bool:
    if not (source_key or "").strip():
        return False
    if any(_key_matches(str(k), source_key)
           for k in note.get("source_keys", []) or []):
        return True
    return any(_citation_matches(c, source_key)
               for c in note.get("sources", []) or [])


def bound_view_for_source(
    notes: list[dict], source_key: str, field: str = ""
) -> dict:
    """Build a bound view for one book-like source inside a master.

    The view is an index, not a copy: note slugs in the source, plus the
    per-source definitions for merged notes (definitions are stored
    parallel to sources, so the bound source's own definition can be
    extracted without the other books' text). A note whose per-source
    definition cannot be attributed exactly is listed under
    ``mixed_notes`` with no text, so a bound answer never silently mixes
    in other books' content.
    """
    slugs: list[str] = []
    per_source: dict[str, list[str]] = {}
    mixed: list[str] = []
    for note in notes:
        if not _note_matches_source(note, source_key):
            continue
        slugs.append(note["id"])
        definitions = note.get("definitions", []) or []
        sources = note.get("sources", []) or []
        keys = note.get("source_keys", []) or []
        picked: list[str] = []
        if len(definitions) == len(sources) and definitions:
            # Definitions are stored parallel to sources, so attribution is
            # positional: a definition belongs to the bound source only
            # when its own index's key or citation matches.
            for i, (definition, citation) in enumerate(zip(definitions, sources)):
                key_at_index = keys[i] if i < len(keys) else ""
                if (_citation_matches(citation, source_key)
                        or _key_matches(str(key_at_index), source_key)):
                    picked.append(definition)
        if picked:
            per_source[note["id"]] = picked
        elif len(definitions) == 1 and len(sources) == 1:
            per_source[note["id"]] = definitions
        elif not definitions and note.get("body") and len(sources) == 1:
            per_source[note["id"]] = [note["body"]]
        else:
            mixed.append(note["id"])
    return {
        "field": field,
        "source_key": source_key,
        "note_slugs": sorted(slugs),
        "note_count": len(slugs),
        "per_source_definitions": per_source,
        "mixed_notes": sorted(mixed),
    }


def render_bound_view_md(view: dict) -> str:
    """Render a bound view as a small markdown index."""
    lines = [
        f"# Bound view - {view.get('source_key', '')}",
        "",
        f"Notes in this bound: {view.get('note_count', 0)} "
        f"(index only; full notes stay in notes/).",
        "",
    ]
    for slug in view.get("note_slugs", []):
        lines.append(f"- {slug} -> notes/{slug}.md")
        for definition in view.get("per_source_definitions", {}).get(slug, []):
            lines.append(f"  - {definition}")
    if view.get("mixed_notes"):
        lines.append("")
        lines.append("Notes listed without text (per-source definition "
                     "could not be attributed exactly):")
        for slug in view["mixed_notes"]:
            lines.append(f"- {slug}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 4. Fragment merge (thin web sources attach, rarely spawn notes)
# ---------------------------------------------------------------------------

def merge_fragment(
    notes: list[dict],
    title: str,
    definition: str,
    citation: str,
    source_key: str = "",
    conflicts: bool = False,
    facets: dict | None = None,
) -> tuple[str, dict | None]:
    """Merge one thin-source fragment into a note list.

    Outcomes: ``duplicate`` (that citation is already on the concept),
    ``attached`` (definition + citation appended to the existing concept
    note, status recomputed), ``new_note`` (no existing concept matched,
    so a new glossary note is created). Facets only fill empty fields.
    The input list is mutated for ``attached``/``new_note``.
    """
    key = norm_title(title)
    target = next((n for n in notes if norm_title(n.get("title", "")) == key), None)
    if target is None:
        note = {
            "id": _slug(title),
            "title": title,
            "kind": "glossary",
            "status": "single-source",
            "facets": dict(facets or {}),
            "sources": [citation],
            "source_keys": [source_key] if source_key else [],
            "definitions": [definition],
            "body": definition,
            "links": [],
        }
        notes.append(note)
        return "new_note", note
    if any(citation == c for c in target.get("sources", [])):
        return "duplicate", target
    target.setdefault("definitions", []).append(definition)
    target.setdefault("sources", []).append(citation)
    if source_key and source_key not in target.get("source_keys", []):
        target.setdefault("source_keys", []).append(source_key)
    if conflicts:
        target["status"] = "contested"
    elif len(target["sources"]) >= 2:
        target["status"] = "consensus"
    for facet_key, facet_value in (facets or {}).items():
        if not (target.get("facets", {}) or {}).get(facet_key):
            target.setdefault("facets", {})[facet_key] = facet_value
    return "attached", target


def merge_fragments(notes: list[dict], fragments: list[dict]) -> dict:
    """Batch-merge fragments; returns outcome counts (sprawl metric)."""
    counts = {"attached": 0, "new_note": 0, "duplicate": 0}
    for fragment in fragments:
        outcome, _ = merge_fragment(
            notes,
            fragment["title"],
            fragment["definition"],
            fragment["citation"],
            fragment.get("source_key", ""),
            fragment.get("conflicts", False),
            fragment.get("facets"),
        )
        counts[outcome] += 1
    counts["notes_total"] = len(notes)
    return counts


# ---------------------------------------------------------------------------
# 5. Coverage metadata (computed from metadata only)
# ---------------------------------------------------------------------------

def coverage_report(notes: list[dict], works: int) -> dict:
    """Label a master's coverage from existing metadata only.

    Thresholds are breadth-first on purpose: a single authoritative book
    can be deep (many concepts, one work), so a high single-source ratio
    must NOT demote a master - corroboration share is reported as a
    qualifier, not used to demote. Labels: deep / solid / sketchy / stub.
    """
    concepts = len({norm_title(n.get("title", "")) for n in notes
                    if norm_title(n.get("title", ""))})
    total = len(notes)
    single = sum(1 for n in notes if n.get("status") == "single-source")
    contested = sum(1 for n in notes if n.get("status") == "contested")
    words = sum(len((n.get("body", "") or "").split()) for n in notes)
    if concepts >= 80 or (concepts >= 60 and works >= 5):
        label = "deep"
    elif concepts >= 40 or (concepts >= 20 and works >= 3):
        label = "solid"
    elif concepts >= 8:
        label = "sketchy"
    else:
        label = "stub"
    return {
        "label": label,
        "concepts": concepts,
        "notes": total,
        "works": works,
        "single_source_ratio": round(single / total, 3) if total else 0.0,
        "contested": contested,
        "total_body_words": words,
        "avg_words_per_note": round(words / total, 1) if total else 0.0,
        "qualifier": ("single-work depth" if works <= 1 and label in ("deep", "solid")
                      else "multi-work corroboration" if works >= 3 else ""),
    }
