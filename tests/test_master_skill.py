"""Master skills: atomic notes as source of truth, merged glossary and
patterns, consensus vs contested, typed graph edges, facets, taxonomy
views, multi-field isolation, registry queries, and incremental fold-in."""

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from book_to_skill.master import (
    build_master,
    list_masters,
    load_master,
    query_across_masters,
    query_notes,
    render_taxonomy_md,
)

BOOK_A = {
    "name": "Book A", "kind": "book",
    "chapters": [{"title": "Judgment under uncertainty"}],
    "glossary": [
        {"term": "Anchoring",
         "definition": "People rely too heavily on the first number they see.",
         "chapter": "Judgment under uncertainty", "level": "basic"},
        {"term": "Loss aversion",
         "definition": "Losses hurt about twice as much as gains feel good.",
         "chapter": "Judgment under uncertainty"},
    ],
    "patterns": [
        {"name": "Framing",
         "description": "Present the same choice as a gain or a loss to change decisions.",
         "chapter": "Judgment under uncertainty", "level": "advanced"},
        {"name": "Scarcity",
         "description": "Limited availability increases perceived value.",
         "chapter": "Judgment under uncertainty"},
    ],
    "relations": [{"from": "Anchoring", "type": "prerequisite_of",
                   "to": "Loss aversion"}],
}

BOOK_B = {
    "name": "Book B", "kind": "book",
    "chapters": [{"title": "Judgment under uncertainty"}],
    "glossary": [
        {"term": "Anchoring",
         "definition": "People rely too heavily on the first number they see.",
         "chapter": "Judgment under uncertainty"},
        {"term": "Loss aversion",
         "definition": "Losses loom larger than gains, by a factor near two.",
         "chapter": "Judgment under uncertainty"},
    ],
    "patterns": [
        {"name": "Framing",
         "description": "Present the same choice as a gain or a loss to change decisions.",
         "chapter": "Judgment under uncertainty", "level": "advanced"},
    ],
}


def _note(master, title):
    return next(n for n in master["notes"] if n["title"] == title)


class TestMerge:
    def test_same_definition_is_consensus_different_is_contested(self, tmp_path):
        summary = build_master("Decision Science", [BOOK_A, BOOK_B], tmp_path)
        master = load_master(Path(summary["path"]))
        assert _note(master, "Anchoring")["status"] == "consensus"
        loss = _note(master, "Loss aversion")
        assert loss["status"] == "contested"
        assert len(loss["definitions"]) == 2
        assert len(master["conflicts"]) == 1
        assert master["conflicts"][0]["term"] == "Loss aversion"

    def test_shared_pattern_not_duplicated_single_source_kept(self, tmp_path):
        summary = build_master("Decision Science", [BOOK_A, BOOK_B], tmp_path)
        master = load_master(Path(summary["path"]))
        framing = [n for n in master["notes"] if n["title"] == "Framing"]
        assert len(framing) == 1
        assert framing[0]["status"] == "consensus"
        scarcity = _note(master, "Scarcity")
        assert scarcity["status"] == "single-source"
        assert len(scarcity["sources"]) == 1

    def test_each_note_holds_exactly_one_concept(self, tmp_path):
        summary = build_master("Decision Science", [BOOK_A, BOOK_B], tmp_path)
        master = load_master(Path(summary["path"]))
        titles = [n["title"] for n in master["notes"]]
        assert len(titles) == len(set(titles))
        anchoring = _note(master, "Anchoring")
        assert "Scarcity" not in anchoring["body"]
        assert "Loss aversion" not in anchoring["body"]

    def test_graph_edge_has_type_and_sources(self, tmp_path):
        summary = build_master("Decision Science", [BOOK_A, BOOK_B], tmp_path)
        master = load_master(Path(summary["path"]))
        edge = next(e for e in master["graph"]["edges"]
                    if e["type"] == "prerequisite_of")
        assert edge["from"] == "anchoring"
        assert edge["to"] == "loss-aversion"
        assert edge["sources"]
        relations = (Path(summary["path"]) / "relations.md").read_text()
        assert "prerequisite_of" in relations

    def test_facet_filter(self, tmp_path):
        summary = build_master("Decision Science", [BOOK_A, BOOK_B], tmp_path)
        master = load_master(Path(summary["path"]))
        hits = query_notes(master["notes"],
                           {"technique": "Framing", "level": "advanced"})
        assert [n["title"] for n in hits] == ["Framing"]
        assert query_notes(master["notes"], {"level": "advanced"})[0]["title"] == "Framing"

    def test_taxonomy_and_views_render_from_notes(self, tmp_path):
        summary = build_master("Decision Science", [BOOK_A, BOOK_B], tmp_path)
        master = load_master(Path(summary["path"]))
        taxonomy = (Path(summary["path"]) / "taxonomy.md").read_text()
        for title in ("Anchoring", "Loss aversion", "Framing"):
            assert title in taxonomy
        glossary = (Path(summary["path"]) / "glossary.md").read_text()
        assert "Anchoring" in glossary and "Framing" not in glossary
        patterns = (Path(summary["path"]) / "patterns.md").read_text()
        assert "Framing" in patterns and "Anchoring" not in patterns
        # The rendered taxonomy is the same node set as master.json.
        assert render_taxonomy_md("Decision Science", master["taxonomy"]) == taxonomy


class TestFoldIn:
    def test_new_source_merges_into_existing_note(self, tmp_path):
        first = build_master("Decision Science", [BOOK_A], tmp_path)
        book_c = {
            "name": "Book C", "kind": "book",
            "glossary": [{"term": "Anchoring",
                          "definition": "People rely too heavily on the first number they see."}],
            "patterns": [],
        }
        second = build_master("Decision Science", [book_c], tmp_path,
                              existing=load_master(Path(first["path"])))
        master = load_master(Path(second["path"]))
        assert second["version"] == 2
        anchoring = [n for n in master["notes"] if n["title"] == "Anchoring"]
        assert len(anchoring) == 1
        assert anchoring[0]["status"] == "consensus"
        assert len(anchoring[0]["sources"]) == 2

    def test_hand_edited_note_body_survives_fold_in(self, tmp_path):
        first = build_master("Decision Science", [BOOK_A], tmp_path)
        note_path = Path(first["path"]) / "notes" / "anchoring.md"
        text = note_path.read_text()
        text = text.replace("People rely too heavily",
                            "HAND-EDITED: people lean on the first number")
        note_path.write_text(text)
        book_c = {
            "name": "Book C", "kind": "book",
            "glossary": [{"term": "Anchoring",
                          "definition": "People rely too heavily on the first number they see."}],
            "patterns": [],
        }
        second = build_master("Decision Science", [book_c], tmp_path,
                              existing=load_master(Path(first["path"])))
        master = load_master(Path(second["path"]))
        assert "HAND-EDITED" in _note(master, "Anchoring")["body"]


class TestMultiFieldAndRegistry:
    def test_fields_are_isolated_and_registry_lists_both(self, tmp_path):
        build_master("Decision Science", [BOOK_A, BOOK_B], tmp_path)
        other = {
            "name": "Cookbook X", "kind": "book",
            "glossary": [{"term": "Mise en place",
                          "definition": "Everything in its place before cooking."}],
            "patterns": [],
        }
        build_master("Cooking", [other], tmp_path)
        entries = list_masters(tmp_path)
        assert {e["field"] for e in entries} == {"Decision Science", "Cooking"}
        cooking = load_master(tmp_path / "cooking")
        assert [n["title"] for n in cooking["notes"]] == ["Mise en place"]
        assert "Anchoring" not in [n["title"] for n in cooking["notes"]]

    def test_registry_query_finds_contested_concepts(self, tmp_path):
        build_master("Decision Science", [BOOK_A, BOOK_B], tmp_path)
        hits = query_across_masters(tmp_path, status="contested")
        assert [h["concept"] for h in hits] == ["Loss aversion"]
        assert hits[0]["field"] == "Decision Science"

    def test_shared_concept_is_cross_linked_not_merged(self, tmp_path):
        build_master("Decision Science", [BOOK_A, BOOK_B], tmp_path)
        shared = {
            "name": "Cookbook X", "kind": "book",
            "glossary": [{"term": "Anchoring",
                          "definition": "Set the first number in a negotiation."}],
            "patterns": [],
        }
        build_master("Cooking", [shared], tmp_path)
        # Rebuild the first field so its cross-links pick up the new master.
        first = load_master(tmp_path / "decision-science")
        build_master("Decision Science", [], tmp_path, existing=first)
        links = (tmp_path / "decision-science" / "cross-field-links.md").read_text()
        assert "Cooking" in links and "Anchoring" in links
        cooking = load_master(tmp_path / "cooking")
        assert len([n for n in cooking["notes"] if n["title"] == "Anchoring"]) == 1
        assert "Loss aversion" not in [n["title"] for n in cooking["notes"]]
