"""Structure optimization: slim taxonomy, canonical definitions, bound
views, fragment merge, coverage labels. Fixture-only: nothing here touches
the Robotics master or the parallel retrieval task's files."""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from atom_brain.master import render_taxonomy_md  # noqa: E402  (before-cost only)
from atom_brain.structure import (  # noqa: E402
    bound_view_for_source,
    canonical_definition_map,
    coverage_report,
    dedupe_report,
    find_duplicate_concepts,
    link_patterns_to_definitions,
    merge_fragment,
    merge_fragments,
    near_duplicate_candidates,
    render_bound_view_md,
    render_slim_taxonomy,
)
from atom_brain.utils import estimate_tokens  # noqa: E402

LONG_CITE = ("https://porabook.com/book/slam/ "
             "(https://porabook.com/book/slam/, fetched 2026-10-27) - web "
             "content may have changed since fetch, ch. Localization & Mapping")


def _note(note_id, title, kind="glossary", status="single-source",
          body="", sources=None, definitions=None, facets=None,
          source_keys=None):
    return {
        "id": note_id, "title": title, "kind": kind, "status": status,
        "facets": facets or {"topic": "Localization", "technique": "",
                             "level": "basic", "source_type": "web"},
        "sources": sources if sources is not None else [LONG_CITE],
        "source_keys": source_keys if source_keys is not None else ["https://porabook.com/book/slam/"],
        "definitions": definitions if definitions is not None else [body],
        "body": body, "links": [],
    }


def _nodes():
    return [
        {"id": "area-localization", "label": "Localization",
         "level": 2, "parents": [], "status": "single-source", "sources": [LONG_CITE]},
        {"id": "slam", "label": "SLAM", "level": 3,
         "parents": ["area-localization"], "status": "single-source",
         "sources": [LONG_CITE]},
        {"id": "kalman-filter", "label": "Kalman filter", "level": 3,
         "parents": ["area-localization"], "status": "single-source",
         "sources": [LONG_CITE]},
    ]


def test_slim_taxonomy_drops_citations_and_links_notes():
    slim = render_slim_taxonomy("Robotics", _nodes())
    assert "Sources:" not in slim
    assert LONG_CITE not in slim
    assert "-> notes/slam.md" in slim
    assert "-> notes/kalman-filter.md" in slim


def test_production_taxonomy_render_is_slim_too():
    # The build's taxonomy.md now links notes instead of repeating every
    # citation, so it costs about what the slim index does.
    built = render_taxonomy_md("Robotics", _nodes())
    slim = render_slim_taxonomy("Robotics", _nodes())
    assert LONG_CITE not in built and "Sources:" not in built
    assert "(notes/slam.md)" in built
    assert estimate_tokens(built) <= 2 * estimate_tokens(slim)


def test_duplicate_concepts_grouped_and_canonical_prefers_glossary():
    notes = [
        _note("slam", "SLAM", kind="glossary"),
        _note("slam-pattern", "SLAM", kind="pattern"),
        _note("kalman-filter", "Kalman filter"),
    ]
    assert find_duplicate_concepts(notes) == {"slam": ["slam", "slam-pattern"]}
    assert canonical_definition_map(notes)["slam"] == "slam"


def test_canonical_prefers_contested_within_glossary():
    notes = [
        _note("control-law-a", "Control law", status="single-source"),
        _note("control-law", "Control law", status="contested"),
    ]
    assert canonical_definition_map(notes)["control law"] == "control-law"


def test_near_duplicates_flagged_not_merged():
    notes = [_note("kalman-filter", "Kalman filter"),
             _note("ekf", "Extended Kalman filter")]
    pairs = near_duplicate_candidates(notes)
    assert ("kalman-filter", "ekf") in pairs
    assert find_duplicate_concepts(notes) == {}


def test_pattern_links_to_definitions_named_in_body():
    notes = [
        _note("slam", "SLAM"),
        _note("kalman-filter", "Kalman filter"),
        _note("slam-pipeline", "SLAM pipeline", kind="pattern",
              body="Run SLAM with a Kalman filter to track the pose."),
    ]
    links, pattern_map = link_patterns_to_definitions(notes)
    assert pattern_map["slam-pipeline"] == ["slam", "kalman-filter"]
    assert links["slam"][0]["type"] == "used_in"
    assert links["slam"][0]["to"] == "slam-pipeline"


def test_bound_view_only_bound_source_and_its_definitions():
    book = "Principles of Robot Autonomy, ch. SLAM"
    web = "Some blog post"
    notes = [
        _note("slam", "SLAM", status="consensus",
              sources=[book, web],
              definitions=["Book definition of SLAM.",
                           "Blog definition of SLAM."],
              source_keys=["porabook", "blog"]),
        _note("kalman-filter", "Kalman filter", sources=[web],
              source_keys=["blog"]),
    ]
    view = bound_view_for_source(notes, "porabook", field="Robotics")
    assert view["note_slugs"] == ["slam"]
    assert view["per_source_definitions"]["slam"] == ["Book definition of SLAM."]
    assert view["mixed_notes"] == []
    rendered = render_bound_view_md(view)
    assert "Blog definition of SLAM." not in rendered
    assert "Book definition of SLAM." in rendered


def test_bound_view_does_not_match_substring_infixes():
    notes = [_note("c0", "Concept 0", source_keys=["porabook-area0"])]
    view = bound_view_for_source(notes, "book-a")
    assert view["note_slugs"] == []
    view = bound_view_for_source(notes, "porabook")
    assert view["note_slugs"] == ["c0"]


def test_bound_view_mixed_note_listed_without_text():
    notes = [_note("slam", "SLAM", sources=["porabook cite", "blog cite"],
                   definitions=["one merged body"],
                   source_keys=["porabook", "blog"])]
    view = bound_view_for_source(notes, "porabook")
    assert view["note_slugs"] == ["slam"]
    assert view["mixed_notes"] == ["slam"]
    assert "slam" not in view["per_source_definitions"]


def test_fragment_merge_attaches_duplicates_and_spawns():
    notes = [_note("slam", "SLAM")]
    outcome, _ = merge_fragment(notes, "SLAM", "New blog take.",
                                "blog cite", source_key="blog")
    assert outcome == "attached"
    assert len(notes) == 1
    assert notes[0]["status"] == "consensus"
    outcome, _ = merge_fragment(notes, "SLAM", "New blog take.",
                                "blog cite", source_key="blog")
    assert outcome == "duplicate"
    outcome, note = merge_fragment(notes, "Loop closure", "Revisit a place.",
                                   "blog cite", source_key="blog")
    assert outcome == "new_note"
    assert note["status"] == "single-source"
    counts = merge_fragments(notes, [
        {"title": "SLAM", "definition": "Third take.", "citation": "third cite"},
        {"title": "Loop closure", "definition": "Again.", "citation": "blog cite"},
    ])
    assert counts["attached"] == 1
    assert counts["duplicate"] == 1
    assert counts["notes_total"] == 2


def test_fragment_conflict_marks_contested():
    notes = [_note("slam", "SLAM")]
    outcome, _ = merge_fragment(notes, "SLAM", "Opposite claim.",
                                "rival cite", conflicts=True)
    assert outcome == "attached"
    assert notes[0]["status"] == "contested"


def test_coverage_labels():
    deep_notes = [_note(f"n{i}", f"Concept {i}") for i in range(118)]
    assert coverage_report(deep_notes, works=1)["label"] == "deep"
    thin_notes = [_note(f"w{i}", f"Web concept {i}") for i in range(12)]
    thin = coverage_report(thin_notes, works=6)
    assert thin["label"] == "sketchy"
    assert thin["works"] == 6
    assert coverage_report([], works=0)["label"] == "stub"
    solid_notes = [_note(f"s{i}", f"Solid concept {i}") for i in range(45)]
    assert coverage_report(solid_notes, works=2)["label"] == "solid"


def test_dedupe_report_counts():
    notes = [_note("slam", "SLAM"), _note("slam-2", "SLAM"),
             _note("kalman-filter", "Kalman filter")]
    report = dedupe_report(notes)
    assert report["notes"] == 3
    assert report["concepts"] == 2
