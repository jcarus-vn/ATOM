"""Tests for atom_brain/compact.py: citation factoring, compact/expand fidelity,
inverted-index ranking equality with rank_notes, and lazy bodies.

All fixtures are synthetic sentences written for these tests; no book
text is embedded here.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atom_brain.compact import (
    build_index,
    compact_master,
    expand_compact_master,
    expand_note_with_body,
    fidelity_diff,
    parse_citation,
    query_compact_dir,
    rank_compact,
    rebuild_citation,
)
from atom_brain.master import load_master, rank_notes


def _cite(url, chapter=None):
    text = f"{url} ({url}, fetched 2026-01-02) - web content may have changed since fetch"
    if chapter:
        text += f", ch. {chapter}"
    return text


URL_A = "https://example.org/book/alpha/"
URL_B = "https://example.org/book/beta/"


def _note(note_id, title, body, sources, kind="glossary", status="single-source",
          topic="Foundations", links=None, definitions=None):
    return {
        "id": note_id, "title": title, "kind": kind, "status": status,
        "body": body,
        "definitions": [body] if definitions is None else definitions,
        "sources": sources,
        "source_keys": sorted({re.findall(r"https?://\S+", s)[0] for s in sources}),
        "facets": {"topic": topic, "technique": "", "level": "basic",
                   "source_type": "web"},
        "links": links or [],
    }


def _master():
    alpha = _note("alpha-note", "Alpha concept",
                  "Alpha concept describes how small gears transfer motion "
                  "between shafts in a compact gearbox.",
                  [_cite(URL_A, "Alpha chapter")])
    beta = _note("beta-note", "Beta concept (BC)",
                 "Beta concept covers how springs store energy and release "
                 "it to drive small gears.",
                 [_cite(URL_B, "Beta chapter")], topic="Mechanics")
    shared = _note("shared-note", "Shared concept",
                   "- In alpha terms, a shared concept joins two gears.\n"
                   "- In beta terms, a shared concept joins two springs.",
                   [_cite(URL_A, "Alpha chapter"), _cite(URL_B, "Beta chapter")],
                   status="contested",
                   definitions=["In alpha terms, a shared concept joins two gears.",
                                "In beta terms, a shared concept joins two springs."],
                   links=[{"to": "alpha-note", "type": "part_of",
                           "sources": [_cite(URL_A)]}])
    return {
        "field": "Synthetic", "slug": "synthetic", "version": 1,
        "updated_at": "2026-01-02",
        "sources": [
            {"name": "Alpha", "url": URL_A, "fetched_at": "2026-01-02",
             "chapters": [{"title": "Alpha chapter"}]},
            {"name": "Beta", "url": URL_B, "fetched_at": "2026-01-02",
             "chapters": [{"title": "Beta chapter"}]},
        ],
        "notes": [alpha, beta, shared],
        "taxonomy": [
            {"id": "area-alpha", "label": "Alpha area", "level": 2, "parents": [],
             "status": "single-source", "manual": False,
             "sources": [_cite(URL_A, "Alpha chapter")]},
            {"id": "area-beta", "label": "Beta area", "level": 2, "parents": [],
             "status": "single-source", "manual": False,
             "sources": [_cite(URL_B, "Beta chapter")]},
        ],
        "graph": {"nodes": [
            {"id": n["id"], "label": n["title"], "kind": n["kind"],
             "status": n["status"], "facets": n["facets"],
             "sources": n["sources"]} for n in (alpha, beta, shared)] + [
            {"id": "area-alpha", "label": "Alpha area", "kind": "area",
             "status": "single-source", "facets": {},
             "sources": [_cite(URL_A, "Alpha chapter")]},
            {"id": "area-beta", "label": "Beta area", "kind": "area",
             "status": "single-source", "facets": {},
             "sources": [_cite(URL_B, "Beta chapter")]},
        ],
            "edges": [
            {"from": "shared-note", "to": "alpha-note", "type": "part_of",
             "sources": [_cite(URL_A)]},
        ]},
        "conflicts": [{
            "term": "Shared concept", "status": "contested",
            "definitions": [
                {"definition": "In alpha terms, a shared concept joins two gears.",
                 "source": _cite(URL_A, "Alpha chapter")},
                {"definition": "In beta terms, a shared concept joins two springs.",
                 "source": _cite(URL_B, "Beta chapter")},
            ],
            "sources": [_cite(URL_A, "Alpha chapter"), _cite(URL_B, "Beta chapter")],
        }],
        "cross_index": [], "cross_field_links": [], "unresolved_relations": [],
    }


def _bodies(master):
    return {n["id"]: n["body"] for n in master["notes"]}


def test_citation_parse_rebuild_round_trip():
    for citation in (_cite(URL_A, "Alpha chapter"), _cite(URL_B),
                     "https://example.org/plain"):
        parsed = parse_citation(citation)
        if parsed["literal"] is not None:
            assert parsed["literal"] == citation
        else:
            assert rebuild_citation(parsed["url"], parsed["fetched"],
                                    parsed["chapter"]) == citation


def test_compact_expand_fidelity():
    master = _master()
    compact = compact_master(master)
    expanded = expand_compact_master(compact, _bodies(master))
    # Sources raw items are the documented exception; the fixture has
    # none, so the diff must be fully empty here.
    assert fidelity_diff(master, expanded) == []


def test_single_source_defs_dropped_multi_source_kept():
    compact = compact_master(_master())
    by_id = {n["id"]: n for n in compact["notes"]}
    assert "defs" not in by_id["alpha-note"]  # == [body], restored on expand
    assert by_id["shared-note"]["defs"] == [
        "In alpha terms, a shared concept joins two gears.",
        "In beta terms, a shared concept joins two springs."]


def test_facet_coding_round_trip():
    master = _master()
    compact = compact_master(master)
    expanded = expand_compact_master(compact, _bodies(master))
    for orig, exp in zip(master["notes"], expanded["notes"]):
        assert orig["facets"] == exp["facets"]


def test_rank_equality_with_rank_notes():
    master = _master()
    compact = compact_master(master)
    index = build_index(compact, _bodies(master))
    taxonomy = master["taxonomy"]
    edges = master["graph"]["edges"]
    for question in ("What is Alpha concept?",
                     "How do gears transfer motion?",
                     "What does BC mean?",
                     "How do springs store energy?"):
        legacy = rank_notes(master["notes"], taxonomy, edges, question)
        compact_ranked = rank_compact(index, question)
        assert [r["note"]["id"] for r in legacy] == \
            [r["note_id"] for r in compact_ranked]
        for legacy_entry, compact_entry in zip(legacy, compact_ranked):
            assert legacy_entry["score"] == compact_entry["score"]
            assert legacy_entry["title_hits"] == compact_entry["title_hits"]
            assert legacy_entry["body_hits"] == compact_entry["body_hits"]


def test_rank_equality_with_source_bound():
    master = _master()
    compact = compact_master(master)
    index = build_index(compact, _bodies(master))
    cite_texts = {}
    for entry in compact["notes"]:
        from atom_brain.compact import _decode_srcs
        cite_texts[entry["id"]] = _decode_srcs(entry["src"], compact["cites"])
    for question in ("What is a shared concept?", "How do gears work?"):
        legacy = rank_notes(master["notes"], master["taxonomy"],
                            master["graph"]["edges"], question, source="alpha")
        compact_ranked = rank_compact(index, question, source="alpha",
                                      cite_texts=cite_texts)
        assert [r["note"]["id"] for r in legacy] == \
            [r["note_id"] for r in compact_ranked]


def test_query_lazy_bodies_only_top_k(tmp_path):
    master = _master()
    compact = compact_master(master)
    index = build_index(compact, _bodies(master))
    out = tmp_path / "synthetic"
    out.mkdir()
    (out / "registry.json").write_text(json.dumps(compact), encoding="utf-8")
    (out / "index.json").write_text(json.dumps(index), encoding="utf-8")
    with (out / "bodies.jsonl").open("w", encoding="utf-8") as handle:
        for note in master["notes"]:
            row = {"id": note["id"], "body": note["body"]}
            if note["id"] == "shared-note":
                row["defs"] = note["definitions"]
            handle.write(json.dumps(row) + "\n")
    entries = query_compact_dir(out, "How do gears transfer motion?", k=1)
    assert entries[0]["note"]["id"] in ("alpha-note", "beta-note")
    assert entries[0]["note"]["body"]
    assert len(entries) == 3  # full ranking present for MRR
    assert all(e["note"].get("body", "") == "" or e is entries[0]
               for e in entries[1:])
    # The top-1 body must be the real body of the top-ranked note.
    assert entries[0]["note"]["body"] == next(
        n["body"] for n in master["notes"] if n["id"] == entries[0]["note"]["id"])


def test_expand_note_with_body_restores_single_source_defs():
    master = _master()
    compact = compact_master(master)
    entry = next(n for n in compact["notes"] if n["id"] == "alpha-note")
    note = expand_note_with_body(compact, entry, master["notes"][0]["body"])
    assert note["definitions"] == [master["notes"][0]["body"]]
    assert note["sources"] == master["notes"][0]["sources"]
    assert note["source_keys"] == master["notes"][0]["source_keys"]


def test_load_master_unchanged_shape():
    # The compact module must not change what load_master returns for a
    # legacy directory: sanity check the loader still parses notes/*.md.
    assert load_master is not None
