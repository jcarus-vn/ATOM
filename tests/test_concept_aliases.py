"""Other names for concepts (aliases.json): a question asked in another
language finds notes written in English, and nothing else changes.

All fixtures are synthetic sentences written for these tests.
"""

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "scripts"))

import build_master as cli  # noqa: E402
from atom_brain.compact import (  # noqa: E402
    build_index,
    compact_master,
    expand_compact_master,
    fidelity_diff,
    rank_compact,
)
from atom_brain.master import (  # noqa: E402
    _alias_units,
    build_master,
    load_aliases,
    load_master,
    merge_aliases,
    rank_notes,
    save_aliases,
)


def _src(name, chapter, terms):
    return {"name": name, "kind": "book", "chapters": [{"title": chapter}],
            "glossary": [{"term": term, "definition": definition,
                          "chapter": chapter, "topic": chapter}
                         for term, definition in terms]}


SOURCES = [
    _src("Planning Book", "Motion Planning", [
        ("Motion planning", "Finding a collision-free path for a robot "
                            "through its configuration space."),
        ("Configuration space", "The set of all poses a robot can take."),
    ]),
    _src("Models Book", "Robot Models", [
        ("Differential drive model", "Two wheels driven independently "
                                     "steer the robot by speed difference."),
        ("Numerical integration", "Stepping the state forward in small "
                                  "time increments to simulate dynamics."),
        ("Loop closure", "Recognizing a revisited place to remove "
                         "accumulated drift from the map."),
    ]),
]
NAMES = {
    "notes": {
        "motion-planning": ["lập kế hoạch chuyển động", "hoạch định chuyển động"],
        "configuration-space": ["không gian cấu hình"],
        "differential-drive-model": ["mô hình truyền động vi sai"],
        "numerical-integration": ["tích phân số"],
        "loop-closure": ["đóng vòng lặp", "phát hiện đóng vòng"],
    },
    "labels": {"Motion Planning": ["lập kế hoạch chuyển động"]},
}


def _master_dir(tmp_path, names=True):
    build_master("Robots", SOURCES, tmp_path)
    master_dir = tmp_path / "robots"
    if names:
        save_aliases(master_dir, NAMES)
    return master_dir


def _top(master, question, k=3):
    ranked = rank_notes(master["notes"], master["taxonomy"],
                        master["graph"]["edges"], question)
    return [r["note"]["id"] for r in ranked[:k]], ranked


def test_units_pair_syllables_and_keep_acronyms():
    differential = _alias_units("mô hình truyền động vi sai")
    assert "vi sai" in differential
    assert "sai" not in differential and "vi" not in differential   # no lone syllables
    assert not differential & _alias_units("sai số tích lũy", query=True)
    assert "ekf" in _alias_units("định vị bằng EKF")
    assert "ekf" in _alias_units("ekf là gì", query=True)            # typed in lowercase
    assert _alias_units("middleware") == {"middleware"}             # one-word name


def test_vietnamese_question_finds_english_note_only_with_names(tmp_path):
    question = "Lập kế hoạch chuyển động là gì?"
    without, _ = _top(load_master(_master_dir(tmp_path, names=False)), question)
    with_names, ranked = _top(load_master(_master_dir(tmp_path)), question)
    assert "motion-planning" not in without[:1]
    assert with_names[0] == "motion-planning"
    assert ranked[0]["concept_boost"] == 1.0          # the whole name is in the question


def test_shared_syllable_does_not_match_another_concept(tmp_path):
    # "sai số" (error) shares "sai" with "vi sai" (differential) and "số"
    # with "tích phân số" (numerical integration): neither may win.
    master = load_master(_master_dir(tmp_path))
    top, _ = _top(master, "Làm sao sửa sai số tích lũy bằng đóng vòng lặp?", k=1)
    assert top == ["loop-closure"]


def test_english_ranking_unchanged_by_names(tmp_path):
    plain = load_master(_master_dir(tmp_path / "a", names=False))
    named = load_master(_master_dir(tmp_path / "b"))
    for question in ("What is motion planning?", "How does a differential drive steer?",
                     "Why integrate numerically?", "What is loop closure?"):
        a = [(r["note"]["id"], r["score"]) for r in rank_notes(
            plain["notes"], plain["taxonomy"], plain["graph"]["edges"], question)]
        b = [(r["note"]["id"], r["score"]) for r in rank_notes(
            named["notes"], named["taxonomy"], named["graph"]["edges"], question)]
        assert a == b, question


def test_names_survive_rebuild_and_stay_out_of_notes(tmp_path):
    master_dir = _master_dir(tmp_path)
    existing = load_master(master_dir)
    build_master("Robots", SOURCES[:1], tmp_path, existing=existing)   # fold-in
    assert load_aliases(master_dir) == load_aliases(tmp_path / ".robots.prev")
    assert "lập kế hoạch" not in (master_dir / "notes" / "motion-planning.md").read_text()
    manifest = json.loads((master_dir / "master.json").read_text())
    assert not any("aliases" in n for n in manifest["notes"])
    assert not any("aliases" in n for n in manifest["taxonomy"])
    by_id = {n["id"]: n for n in load_master(master_dir)["notes"]}
    assert by_id["motion-planning"]["aliases"] == NAMES["notes"]["motion-planning"]
    assert by_id["motion-planning"]["topic_aliases"] == ["lập kế hoạch chuyển động"]


def test_broken_aliases_file_is_ignored(tmp_path):
    master_dir = _master_dir(tmp_path, names=False)
    (master_dir / "aliases.json").write_text("{not json", encoding="utf-8")
    master = load_master(master_dir)
    assert not any(n.get("aliases") for n in master["notes"])


def test_merge_dedupes_by_normalized_form():
    merged, added = merge_aliases(
        {"notes": {"a": ["Lập kế hoạch"]}, "labels": {}},
        {"notes": {"a": ["lập  kế hoạch", "hoạch định", 7], "b": []},
         "labels": {"L": "nhãn"}})
    assert merged == {"notes": {"a": ["Lập kế hoạch", "hoạch định"]},
                      "labels": {"L": ["nhãn"]}}
    assert added == 2


def test_cli_export_then_import(tmp_path, capsys):
    root = tmp_path
    _master_dir(root, names=False)
    template = tmp_path / "names.json"
    assert cli.main(["--masters-root", str(root), "--field", "Robots",
                     "--export-aliases", str(template)]) == 0
    data = json.loads(template.read_text())
    assert data["notes"]["motion-planning"] == {"title": "Motion planning", "aliases": []}
    assert "Motion Planning" in data["labels"]
    data["notes"]["motion-planning"]["aliases"] = ["lập kế hoạch chuyển động"]
    data["notes"]["no-such-note"] = ["x"]
    data["labels"]["Motion Planning"] = ["lập kế hoạch chuyển động"]
    template.write_text(json.dumps(data, ensure_ascii=False))
    for _ in range(2):                                   # importing twice adds nothing new
        assert cli.main(["--masters-root", str(root), "--field", "Robots",
                         "--import-aliases", str(template)]) == 0
    out = capsys.readouterr()
    assert "skipped unknown note id: no-such-note" in out.err
    assert "added 0 name(s)" in out.out
    assert load_aliases(root / "robots") == {
        "notes": {"motion-planning": ["lập kế hoạch chuyển động"]},
        "labels": {"Motion Planning": ["lập kế hoạch chuyển động"]}}


def test_compact_carries_names_and_ranks_identically(tmp_path):
    # The compact ranker mirrors rank_notes on web citations (its chapter
    # and taxonomy keys are URLs), so this master is built from web sources.
    web = [dict(src, kind="web", url=f"https://example.org/{i}/",
                fetched_at="2026-01-02T00:00:00+00:00")
           for i, src in enumerate(SOURCES)]
    build_master("Robots", web, tmp_path)
    master_dir = tmp_path / "robots"
    save_aliases(master_dir, NAMES)
    master = json.loads((master_dir / "master.json").read_text())
    from atom_brain.master import attach_aliases
    attach_aliases(master, load_aliases(master_dir))
    bodies = {n["id"]: n["body"] for n in master["notes"]}
    compact = compact_master(master)
    expanded = expand_compact_master(compact, bodies)
    assert not [p for p in fidelity_diff(master, expanded) if "aliases" in p]
    assert {n["id"]: n.get("aliases") for n in expanded["notes"]} == \
        {n["id"]: n.get("aliases") for n in master["notes"]}
    assert {n["id"]: n.get("aliases") for n in expanded["taxonomy"]} == \
        {n["id"]: n.get("aliases") for n in master["taxonomy"]}
    index = build_index(compact, bodies)
    for question in ("Lập kế hoạch chuyển động là gì?", "không gian cấu hình",
                     "Làm sao sửa sai số tích lũy bằng đóng vòng lặp?",
                     "What is motion planning?"):
        legacy = [(r["note"]["id"], r["score"], r["title_hits"]) for r in rank_notes(
            master["notes"], master["taxonomy"], master["graph"]["edges"], question)]
        fast = [(r["note_id"], r["score"], r["title_hits"])
                for r in rank_compact(index, question)]
        assert legacy == fast, question
