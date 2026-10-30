"""Compact master storage and lazy two-phase query.

A legacy master directory repeats the same content many times over: the
full citation string (about 150 characters) appears once per note, per
taxonomy node and per graph edge that cites it; every note body is stored
twice (``body`` and ``definitions == [body]`` for single-source notes);
``master.json`` embeds the raw source inputs (glossary + patterns) that
were already merged into the notes; and ``graph.json`` plus the
``glossary.md`` / ``patterns.md`` / ``taxonomy.md`` / ``relations.md``
renders repeat content derivable from the notes.

The compact form stores each fact once:

- ``registry.json``: source registry (each citation string factored to a
  short id), per-note metadata (title, kind, status, coded facets,
  source references, links), the taxonomy skeleton (labels + structure,
  no repeated citations), graph edges as id pairs, and conflicts.
- ``index.json``: a shared vocabulary plus an inverted index (term id
  -> note ordinals), per-note title/topic term ids, the taxonomy
  label-term map, and graph adjacency. No bodies.
- ``bodies.jsonl``: one JSON object per line (``id``, ``body``, and
  ``defs`` only when the definitions differ from ``[body]``), read by
  seeking to the lines a query actually needs.

Query is two-phase: phase 1 ranks from registry + index alone (every
input to :func:`atom_brain.master.rank_notes` is derivable from metadata and
the index, so the ranking is identical to the full scan — see
:func:`rank_compact` and its equality tests); phase 2 fetches bodies
for the top-k only. A question therefore loads kilobytes, not the
whole master.

Deliberate non-goals:

- The compact form is a *query* artifact, not a build artifact. It
  drops the raw source inputs (``glossary`` / ``patterns`` items) from
  ``master['sources']``: those live in the per-source skill directories
  and are needed for fold-in re-merges. :func:`expand_compact_master`
  restores everything except those raw items, and
  :func:`fidelity_diff` reports exactly that as the single known
  difference.
- No acronym legend and no phrasal factoring: measured on the Robotics
  master, bodies contain zero acronym parentheticals and no repeated
  6-word window occurs more than 3 times (citation fragments aside).
  Factoring them would save nothing and would break concept matching,
  which relies on the parenthesised acronym in titles.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from atom_brain.master import (
    CONCEPT_BOOST,
    EXPANSION_BONUS,
    EXPANSION_SEEDS,
    GLOSSARY_BONUS,
    _DEFINITIONAL,
    _rank_terms,
    _raw_terms,
    normalize_title,
)

COMPACT_VERSION = 1

_CITATION_RE = re.compile(
    r"^(?P<url>\S+)\s*\((?P<url2>\S+),\s*fetched\s+(?P<fetched>[^)]+)\)"
    r"(?:\s*-\s*(?P<rest>.*))?$"
)
_URL_RE = re.compile(r"https?://\S+")
_DISCLAIMER = "web content may have changed since fetch"


def parse_citation(citation: str) -> dict:
    """Split a citation string into url / fetched / chapter.

    Handles both shapes seen in masters: with a chapter suffix
    (``..., ch. <chapter>``) and without one (edges, note links).
    Unparseable strings are kept verbatim under ``literal`` so
    reconstruction stays lossless.
    """
    text = (citation or "").strip()
    m = _CITATION_RE.match(text)
    if not m:
        return {"url": text, "fetched": "", "chapter": None, "literal": text}
    rest = m.group("rest") or ""
    chapter = None
    if ", ch. " in rest:
        chapter = rest.split(", ch. ", 1)[1]
    return {"url": m.group("url"), "fetched": m.group("fetched"),
            "chapter": chapter, "literal": None}


def rebuild_citation(url: str, fetched: str, chapter: str | None) -> str:
    """Rebuild exactly the citation string :func:`parse_citation` parses."""
    text = f"{url} ({url}, fetched {fetched}) - {_DISCLAIMER}"
    if chapter:
        text += f", ch. {chapter}"
    return text


def _cite_key(parsed: dict) -> tuple:
    if parsed["literal"] is not None:
        return ("literal", parsed["literal"])
    return ("cite", parsed["url"], parsed["fetched"])


def build_cite_registry(citations: list[str]) -> tuple[list[dict], dict]:
    """Assign a short id to every distinct (url, fetched) citation pair.

    Returns (registry, key -> id). Registry entries keep the url and
    fetched string so citations rebuild exactly.
    """
    registry: list[dict] = []
    index: dict = {}
    for citation in citations:
        parsed = parse_citation(citation)
        key = _cite_key(parsed)
        if key not in index:
            index[key] = len(registry)
            entry = {"id": f"C{len(registry):02d}", "url": parsed["url"],
                     "fetched": parsed["fetched"]}
            if parsed["literal"] is not None:
                entry["literal"] = parsed["literal"]
            registry.append(entry)
    return registry, index


def _all_citations(master: dict) -> list[str]:
    out: list[str] = []
    for note in master.get("notes", []):
        out += note.get("sources", [])
        for link in note.get("links", []):
            out += link.get("sources", [])
    for node in master.get("taxonomy", []):
        out += node.get("sources", [])
    for edge in master.get("graph", {}).get("edges", []):
        out += edge.get("sources", [])
    for conflict in master.get("conflicts", []):
        out += conflict.get("sources", [])
        for definition in conflict.get("definitions", []):
            if definition.get("source"):
                out.append(definition["source"])
    return out


def _encode_srcs(citations: list[str], cite_index: dict) -> list:
    """Citation strings -> [cite_id, chapter] pairs, order preserved."""
    out = []
    for citation in citations:
        parsed = parse_citation(citation)
        out.append([cite_index[_cite_key(parsed)], parsed["chapter"]])
    return out


def _decode_srcs(pairs: list, registry: list[dict]) -> list[str]:
    out = []
    for cite_id, chapter in pairs:
        entry = registry[cite_id]
        if entry.get("literal") is not None:
            out.append(entry["literal"])
        else:
            out.append(rebuild_citation(entry["url"], entry["fetched"], chapter))
    return out


def _encode_facets(notes: list[dict]) -> tuple[dict, list[dict]]:
    """Code facet values through small registries (topics/levels/types).

    Returns (registries, coded facets per note in notes order). The
    decoded dict compares equal to the original facets dict.
    """
    topics: list[str] = []
    levels: list[str] = []
    types: list[str] = []

    def _idx(table: list[str], value: str) -> int:
        if value not in table:
            table.append(value)
        return table.index(value)

    coded = []
    for note in notes:
        facets = note.get("facets", {}) or {}
        coded.append([
            _idx(topics, facets.get("topic", "")),
            facets.get("technique", ""),
            _idx(levels, facets.get("level", "")),
            _idx(types, facets.get("source_type", "")),
        ])
    return ({"topics": topics, "levels": levels, "source_types": types}, coded)


def _decode_facets(coded: list, registries: dict) -> dict:
    topic_idx, technique, level_idx, type_idx = coded
    return {
        "topic": registries["topics"][topic_idx],
        "technique": technique,
        "level": registries["levels"][level_idx],
        "source_type": registries["source_types"][type_idx],
    }


def compact_master(master: dict) -> dict:
    """Convert a legacy master dict to the compact form.

    Lossless except for ``master['sources']`` raw ``glossary`` /
    ``patterns`` items, which are build inputs, not master content (see
    module docstring). Single-source notes store no ``defs``: their
    definitions are exactly ``[body]`` and expand restores them.
    """
    notes = master.get("notes", [])
    cite_registry, cite_index = build_cite_registry(_all_citations(master))
    facet_regs, coded_facets = _encode_facets(notes)

    compact_notes = []
    for note, coded in zip(notes, coded_facets):
        entry: dict = {
            "id": note.get("id", ""),
            "title": note.get("title", ""),
            "kind": note.get("kind", ""),
            "status": note.get("status", ""),
            "facets": coded,
            "src": _encode_srcs(note.get("sources", []), cite_index),
        }
        definitions = note.get("definitions", [])
        if not (len(definitions) == 1 and definitions[0] == note.get("body", "")):
            entry["defs"] = definitions
        links = []
        for link in note.get("links", []):
            item = {"to": link.get("to", ""), "type": link.get("type", ""),
                    "src": _encode_srcs(link.get("sources", []), cite_index)}
            for key, value in link.items():
                if key not in ("to", "type", "sources"):
                    item[key] = value
            links.append(item)
        if links:
            entry["links"] = links
        compact_notes.append(entry)

    taxonomy = []
    for node in master.get("taxonomy", []):
        taxonomy.append({
            "id": node.get("id", ""),
            "label": node.get("label", ""),
            "level": node.get("level", 0),
            "parents": list(node.get("parents", [])),
            "status": node.get("status", ""),
            "manual": bool(node.get("manual", False)),
            "src": _encode_srcs(node.get("sources", []), cite_index),
        })

    edges = []
    for edge in master.get("graph", {}).get("edges", []):
        edges.append({
            "from": edge.get("from", ""),
            "to": edge.get("to", ""),
            "type": edge.get("type", ""),
            "src": _encode_srcs(edge.get("sources", []), cite_index),
        })

    conflicts = []
    for conflict in master.get("conflicts", []):
        conflicts.append({
            "term": conflict.get("term", ""),
            "status": conflict.get("status", ""),
            "definitions": [
                {"definition": d.get("definition", ""),
                 "src": _encode_srcs([d["source"]], cite_index)[0]
                 if d.get("source") else None}
                for d in conflict.get("definitions", [])
            ],
            "src": _encode_srcs(conflict.get("sources", []), cite_index),
        })

    sources_meta = []
    for src in master.get("sources", []):
        sources_meta.append({
            "name": src.get("name", ""),
            "url": src.get("url", ""),
            "fetched_at": src.get("fetched_at", ""),
            "chapters": src.get("chapters", []),
        })

    return {
        "format": "compact",
        "version": COMPACT_VERSION,
        "field": master.get("field", ""),
        "slug": master.get("slug", ""),
        "master_version": master.get("version", 0),
        "updated_at": master.get("updated_at", ""),
        "cites": cite_registry,
        "sources": sources_meta,
        "facet_registries": facet_regs,
        "notes": compact_notes,
        "taxonomy": taxonomy,
        "edges": edges,
        "conflicts": conflicts,
        "cross_index": master.get("cross_index", []),
        "cross_field_links": master.get("cross_field_links", []),
        "unresolved_relations": master.get("unresolved_relations", []),
    }


def _expand_note(compact: dict, entry: dict) -> dict:
    """Expand one compact note back to legacy shape (body excluded).

    Bodies live in bodies.jsonl, not in the registry; pass a body in via
    :func:`expand_note_with_body` when the full note is needed.
    """
    definitions = entry.get("defs")  # None means [body]; resolved by caller
    note = {
        "id": entry["id"],
        "title": entry["title"],
        "kind": entry["kind"],
        "status": entry["status"],
        "facets": _decode_facets(entry["facets"], compact["facet_registries"]),
        "sources": _decode_srcs(entry["src"], compact["cites"]),
        "source_keys": [],
        "links": [],
    }
    seen_urls: list[str] = []
    for cite_id, _chapter in entry["src"]:
        url = compact["cites"][cite_id]["url"]
        if url not in seen_urls:
            seen_urls.append(url)
    note["source_keys"] = seen_urls
    for link in entry.get("links", []):
        item = {"to": link["to"], "type": link["type"],
                "sources": _decode_srcs(link["src"], compact["cites"])}
        for key, value in link.items():
            if key not in ("to", "type", "src"):
                item[key] = value
        note["links"].append(item)
    if definitions is not None:
        note["definitions"] = definitions
    return note


def expand_note_with_body(compact: dict, entry: dict, body: str) -> dict:
    """Expand one compact note to full legacy shape, body included."""
    note = _expand_note(compact, entry)
    note["body"] = body
    if "defs" not in entry:
        note["definitions"] = [body]
    return note


def expand_compact_master(compact: dict, bodies: dict[str, str]) -> dict:
    """Expand a compact master back to legacy shape.

    ``bodies`` maps note id -> body text (from bodies.jsonl). Everything
    is restored exactly except ``sources`` raw ``glossary`` / ``patterns``
    items, which the compact form intentionally does not carry.
    """
    notes = []
    for entry in compact["notes"]:
        body = bodies.get(entry["id"], "")
        notes.append(expand_note_with_body(compact, entry, body))
    taxonomy = []
    for node in compact["taxonomy"]:
        taxonomy.append({
            "id": node["id"],
            "label": node["label"],
            "level": node["level"],
            "parents": list(node["parents"]),
            "status": node["status"],
            "manual": node["manual"],
            "sources": _decode_srcs(node["src"], compact["cites"]),
        })
    graph_nodes = []
    for note in notes:
        graph_nodes.append({
            "id": note["id"],
            "label": note["title"],
            "kind": note["kind"],
            "status": note["status"],
            "facets": note["facets"],
            "sources": note["sources"],
        })
    # Legacy graph also carries the area (taxonomy) nodes after the
    # concept nodes, with kind "area" and empty facets.
    for node in taxonomy:
        if node["id"].startswith("area-"):
            graph_nodes.append({
                "id": node["id"],
                "label": node["label"],
                "kind": "area",
                "status": node["status"],
                "facets": {},
                "sources": node["sources"],
            })
    graph_edges = []
    for edge in compact["edges"]:
        graph_edges.append({
            "from": edge["from"],
            "to": edge["to"],
            "type": edge["type"],
            "sources": _decode_srcs(edge["src"], compact["cites"]),
        })
    conflicts = []
    for conflict in compact["conflicts"]:
        definitions = []
        for definition in conflict["definitions"]:
            item = {"definition": definition["definition"]}
            if definition["src"] is not None:
                item["source"] = _decode_srcs(
                    [definition["src"]], compact["cites"])[0]
            definitions.append(item)
        conflicts.append({
            "term": conflict["term"],
            "status": conflict["status"],
            "definitions": definitions,
            "sources": _decode_srcs(conflict["src"], compact["cites"]),
        })
    return {
        "field": compact["field"],
        "slug": compact["slug"],
        "version": compact["master_version"],
        "updated_at": compact["updated_at"],
        "sources": compact["sources"],
        "notes": notes,
        "taxonomy": taxonomy,
        "graph": {"nodes": graph_nodes, "edges": graph_edges},
        "conflicts": conflicts,
        "cross_index": compact["cross_index"],
        "cross_field_links": compact["cross_field_links"],
        "unresolved_relations": compact["unresolved_relations"],
    }


def fidelity_diff(original: dict, expanded: dict) -> list[str]:
    """Compare an expanded compact master against the legacy original.

    Returns a list of mismatch descriptions; empty means identical
    apart from the documented ``sources`` raw-item loss (which is
    reported separately, not counted as a mismatch).
    """
    problems: list[str] = []
    orig_notes = {n["id"]: n for n in original.get("notes", [])}
    exp_notes = {n["id"]: n for n in expanded.get("notes", [])}
    if set(orig_notes) != set(exp_notes):
        problems.append(
            f"note id sets differ: only-original={sorted(set(orig_notes) - set(exp_notes))[:5]}, "
            f"only-expanded={sorted(set(exp_notes) - set(orig_notes))[:5]}")
    for note_id, orig in orig_notes.items():
        exp = exp_notes.get(note_id)
        if exp is None:
            continue
        for field in ("title", "kind", "status", "facets", "body",
                      "definitions", "sources", "source_keys", "links"):
            if orig.get(field) != exp.get(field):
                problems.append(f"note {note_id} field {field} differs")
    orig_tax = {n["id"]: n for n in original.get("taxonomy", [])}
    exp_tax = {n["id"]: n for n in expanded.get("taxonomy", [])}
    if set(orig_tax) != set(exp_tax):
        problems.append("taxonomy id sets differ")
    for node_id, orig in orig_tax.items():
        exp = exp_tax.get(node_id)
        if exp is None:
            continue
        for field in ("label", "level", "parents", "status", "manual", "sources"):
            if orig.get(field) != exp.get(field):
                problems.append(f"taxonomy {node_id} field {field} differs")
    orig_nodes = {n["id"]: n for n in original.get("graph", {}).get("nodes", [])}
    exp_nodes = {n["id"]: n for n in expanded.get("graph", {}).get("nodes", [])}
    if set(orig_nodes) != set(exp_nodes):
        problems.append("graph node id sets differ")
    for node_id, orig in orig_nodes.items():
        exp = exp_nodes.get(node_id)
        if exp is None:
            continue
        for field in ("label", "kind", "status", "facets", "sources"):
            if orig.get(field) != exp.get(field):
                problems.append(f"graph node {node_id} field {field} differs")

    def _edge_key(edge: dict) -> tuple:
        return (edge.get("from", ""), edge.get("to", ""), edge.get("type", ""),
                tuple(edge.get("sources", [])))

    if sorted(map(_edge_key, original.get("graph", {}).get("edges", []))) != \
            sorted(map(_edge_key, expanded.get("graph", {}).get("edges", []))):
        problems.append("graph edges differ")
    if original.get("conflicts", []) != expanded.get("conflicts", []):
        problems.append("conflicts differ")
    for key in ("field", "slug", "version", "updated_at",
                "cross_index", "cross_field_links", "unresolved_relations"):
        if original.get(key) != expanded.get(key):
            problems.append(f"top-level {key} differs")
    return problems


def build_bodies(compact: dict, bodies: dict[str, str]) -> list[dict]:
    """Bodies file rows: id + body, plus defs only when they differ."""
    rows = []
    for entry in compact["notes"]:
        row: dict = {"id": entry["id"], "body": bodies.get(entry["id"], "")}
        if "defs" in entry:
            row["defs"] = entry["defs"]
        rows.append(row)
    return rows


def build_index(compact: dict, bodies: dict[str, str]) -> dict:
    """Build the query index from a compact master + its bodies.

    The vocabulary maps terms to small ids; postings map term ids to
    note ordinals (not slugs), which keeps the index small. Per-note
    metadata carries everything ranking needs except bodies: title
    terms, topic terms, acronym, kind, source urls and chapter keys
    (computed with the exact procedure :func:`atom_brain.master._chapter_keys`
    uses, via rebuilt citations), and graph adjacency.
    """
    vocab: list[str] = []
    vocab_index: dict[str, int] = {}

    def _term_id(term: str) -> int:
        if term not in vocab_index:
            vocab_index[term] = len(vocab)
            vocab.append(term)
        return vocab_index[term]

    # Taxonomy label terms per source url (bare-url formulation; see
    # module notes in rank_compact for why this equals the legacy
    # string-key lookup on citation-shaped data).
    tax_map: dict[str, list[int]] = {}
    for node in compact["taxonomy"]:
        label_terms = sorted({_term_id(t) for t in _rank_terms(node["label"])})
        for cite_id, _chapter in node["src"]:
            url = compact["cites"][cite_id]["url"]
            merged = sorted(set(tax_map.get(url, [])) | set(label_terms))
            tax_map[url] = merged

    notes_meta: list[dict] = []
    postings: dict[int, list[int]] = {}
    for ordinal, entry in enumerate(compact["notes"]):
        body = bodies.get(entry["id"], "")
        body_term_ids = sorted({_term_id(t) for t in _rank_terms(body)})
        for term_id in body_term_ids:
            postings.setdefault(term_id, []).append(ordinal)
        title = entry["title"]
        acronym = None
        match = re.search(r"\(([^)]+)\)", title)
        if match:
            acronym = normalize_title(match.group(1))
        facets = _decode_facets(entry["facets"], compact["facet_registries"])
        # Chapter keys: same procedure as _chapter_keys on the rebuilt
        # citations (base url per citation, comma variants included in
        # the legacy set are harmless: shared chapters share both forms).
        chapters = sorted({
            compact["cites"][cite_id]["url"].rstrip("/").split("(")[0]
            for cite_id, _chapter in entry["src"]
        })
        source_urls = sorted({
            compact["cites"][cite_id]["url"] for cite_id, _chapter in entry["src"]
        })
        notes_meta.append({
            "id": entry["id"],
            "title_terms": sorted({_term_id(t) for t in _rank_terms(title)}),
            "topic_terms": sorted({_term_id(t) for t in _rank_terms(facets.get("topic", ""))}),
            "title_norm": normalize_title(title),
            "acronym": acronym,
            "kind": entry["kind"],
            "chapters": chapters,
            "source_urls": source_urls,
            "links": [link["to"] for link in entry.get("links", [])],
        })

    adjacency: dict[int, list[int]] = {}
    id_to_ordinal = {meta["id"]: i for i, meta in enumerate(notes_meta)}
    pairs: list[tuple[int, int]] = []
    for edge in compact["edges"]:
        if edge["from"] in id_to_ordinal and edge["to"] in id_to_ordinal:
            pairs.append((id_to_ordinal[edge["from"]], id_to_ordinal[edge["to"]]))
    for ordinal, meta in enumerate(notes_meta):
        for target in meta["links"]:
            if target in id_to_ordinal:
                pairs.append((ordinal, id_to_ordinal[target]))
    for source, target in pairs:
        if notes_meta[source]["id"].startswith("area-") or \
                notes_meta[target]["id"].startswith("area-"):
            continue
        adjacency.setdefault(source, []).append(target)
        adjacency.setdefault(target, []).append(source)
    adjacency = {k: sorted(set(v)) for k, v in adjacency.items()}

    return {
        "format": "compact-index",
        "version": COMPACT_VERSION,
        "vocab": vocab,
        "notes": notes_meta,
        "body_postings": {str(k): v for k, v in postings.items()},
        "tax_map": tax_map,
        "adjacency": {str(k): v for k, v in adjacency.items()},
    }


def rank_compact(index: dict, question: str, k: int | None = None,
                 source: str | None = None,
                 params: dict | None = None,
                 cite_texts: dict[str, list[str]] | None = None) -> list[dict]:
    """Rank notes using only the compact index (no bodies loaded).

    Mirrors :func:`atom_brain.master.rank_notes` exactly: same base formula,
    same params defaults, same seed/chapter/graph expansion, same
    (-score, id) tie-breaks. Returns entries with ``note_id`` instead
    of ``note`` (bodies are fetched separately for the top-k).
    """
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
    vocab = index["vocab"]
    notes_meta = index["notes"]
    postings = {int(t): ids for t, ids in index["body_postings"].items()}
    tax_map = index["tax_map"]
    adjacency = {int(k): v for k, v in index["adjacency"].items()}

    qterms = set(_rank_terms(question))
    vocab_lookup = {term: i for i, term in enumerate(vocab)}
    qterm_ids = {vocab_lookup[t] for t in qterms if t in vocab_lookup}
    qnorm = normalize_title(question)
    qraw = set(_raw_terms(question))
    definitional = bool(qraw & _DEFINITIONAL)

    # Body-hit counts from the inverted index (no bodies touched).
    body_hits: dict[int, int] = {}
    for term_id in qterm_ids:
        for ordinal in postings.get(term_id, []):
            body_hits[ordinal] = body_hits.get(ordinal, 0) + 1

    pool = range(len(notes_meta))
    if source and str(source).strip():
        wanted = str(source).strip().lower()

        def _in_bound(meta: dict) -> bool:
            if any(wanted in url.lower() for url in meta["source_urls"]):
                return True
            if cite_texts:
                return any(wanted in text.lower()
                           for text in cite_texts.get(meta["id"], []))
            return False

        pool = [i for i in pool if _in_bound(notes_meta[i])]

    base: dict[int, dict] = {}
    for ordinal in pool:
        meta = notes_meta[ordinal]
        title_terms = set(meta["title_terms"])
        title_hits = len(qterm_ids & title_terms)
        coverage = title_hits / len(title_terms) if title_terms else 0.0
        tax_terms: set[int] = set()
        for url in meta["source_urls"]:
            tax_terms.update(tax_map.get(url, []))
        tax_hits = len(qterm_ids & tax_terms)
        topic_hits = len(qterm_ids & set(meta["topic_terms"]))
        concept = 0.0
        if meta["title_norm"] and meta["title_norm"] in qnorm:
            concept = 1.0
        elif meta["acronym"] and meta["acronym"] in qnorm:
            concept = 1.0
        glossary = 1.0 if (definitional and meta["kind"] == "glossary") else 0.0
        score = (3 * title_hits
                 + (coverage if cfg["title_norm"] else 0.0)
                 + 2 * tax_hits + body_hits.get(ordinal, 0)
                 + 2 * topic_hits + cfg["concept_boost"] * concept
                 + cfg["glossary_bonus"] * glossary)
        base[ordinal] = {
            "note_id": meta["id"], "score": score, "title_hits": title_hits,
            "tax_hits": tax_hits, "body_hits": body_hits.get(ordinal, 0),
            "concept_boost": concept, "expansion_bonus": 0.0,
        }
    order = sorted(base.values(),
                   key=lambda r: (-r["score"], r["note_id"]))
    seeds = order[:cfg["expansion_seeds"]]
    id_to_ordinal = {meta["id"]: i for i, meta in enumerate(notes_meta)}
    seed_chapters: set[str] = set()
    for seed in seeds:
        seed_chapters.update(notes_meta[id_to_ordinal[seed["note_id"]]]["chapters"])
    seed_ids = {s["note_id"] for s in seeds}
    seed_ordinals = {id_to_ordinal[s["note_id"]] for s in seeds}
    final: dict[str, float] = {}
    for ordinal, entry in base.items():
        bonus = 0.0
        if entry["note_id"] not in seed_ids and entry["score"] > 0:
            if set(notes_meta[ordinal]["chapters"]) & seed_chapters:
                bonus = cfg["expansion_bonus"]
            elif any(nb in seed_ordinals for nb in adjacency.get(ordinal, [])):
                bonus = cfg["expansion_bonus"]
        entry["expansion_bonus"] = bonus
        final[entry["note_id"]] = entry["score"] + bonus
    ranked = sorted(base.values(),
                    key=lambda r: (-final[r["note_id"]], r["note_id"]))
    for entry in ranked:
        entry["score"] = final[entry["note_id"]]
    if k is not None and k > 0:
        return ranked[:k]
    return ranked


def load_compact_dir(compact_dir: Path) -> tuple[dict, dict]:
    """Load registry.json + index.json from a compact master directory."""
    compact_dir = Path(compact_dir)
    registry = json.loads((compact_dir / "registry.json").read_text(encoding="utf-8"))
    index = json.loads((compact_dir / "index.json").read_text(encoding="utf-8"))
    return registry, index


def fetch_bodies(compact_dir: Path, note_ids: list[str]) -> dict[str, str]:
    """Fetch bodies for exactly the given ids from bodies.jsonl.

    Reads the file once line by line and keeps only requested lines:
    no other note's body text is retained.
    """
    wanted = set(note_ids)
    out: dict[str, str] = {}
    bodies_path = Path(compact_dir) / "bodies.jsonl"
    if not bodies_path.exists():
        return out
    with bodies_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row["id"] in wanted:
                out[row["id"]] = row["body"]
                if len(out) == len(wanted):
                    break
    return out


def query_compact_dir(compact_dir: Path, question: str,
                      k: int | None = None, source: str | None = None,
                      params: dict | None = None) -> list[dict]:
    """Two-phase query over a compact directory.

    Phase 1 ranks from registry + index (no bodies). Phase 2 fetches
    bodies for the top-k only and returns rank_notes-shaped entries:
    full legacy note dicts for the top-k, id-only dicts beyond it (the
    eval only reads bodies of the top-k; ids carry the full ranking
    for MRR).
    """
    compact_dir = Path(compact_dir)
    registry, index = load_compact_dir(compact_dir)
    cite_texts = None
    if source and str(source).strip():
        cite_texts = {}
        for entry in registry["notes"]:
            cite_texts[entry["id"]] = _decode_srcs(entry["src"], registry["cites"])
    ranked = rank_compact(index, question, k=None, source=source,
                          params=params, cite_texts=cite_texts)
    top = ranked[:k] if k is not None and k > 0 else ranked
    bodies = fetch_bodies(compact_dir, [entry["note_id"] for entry in top])
    by_id = {entry["id"]: entry for entry in registry["notes"]}
    entries: list[dict] = []
    for entry in ranked:
        note_id = entry["note_id"]
        if note_id in bodies:
            note = expand_note_with_body(registry, by_id[note_id], bodies[note_id])
        else:
            note = {"id": note_id}
        entries.append({
            "note": note,
            "score": entry["score"],
            "title_hits": entry["title_hits"],
            "tax_hits": entry["tax_hits"],
            "body_hits": entry["body_hits"],
            "concept_boost": entry["concept_boost"],
            "expansion_bonus": entry["expansion_bonus"],
        })
    return entries
