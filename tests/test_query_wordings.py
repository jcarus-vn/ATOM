"""A question can be ranked in several wordings (as asked, and translated
into the notes' language) and the rankings fused.

All fixtures are synthetic sentences written for these tests.
"""

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "scripts"))
sys.path.insert(0, str(ROOT_DIR / "tests"))

import build_master as cli  # noqa: E402
from atom_brain.compact import build_index, compact_master  # noqa: E402
from atom_brain.master import (  # noqa: E402
    build_master,
    fuse_rankings,
    load_master,
    rank_notes,
    rank_notes_multi,
    render_skill_md,
)
from test_concept_aliases import SOURCES  # noqa: E402

VI = "Lập kế hoạch chuyển động là gì?"
EN = "What is motion planning?"


def _master(tmp_path, web=False):
    sources = SOURCES
    if web:
        sources = [dict(src, kind="web", url=f"https://example.org/{i}/",
                        fetched_at="2026-01-02T00:00:00+00:00")
                   for i, src in enumerate(SOURCES)]
    build_master("Robots", sources, tmp_path)
    return load_master(tmp_path / "robots")


def _rank(master, wordings, **kw):
    return rank_notes_multi(master["notes"], master["taxonomy"],
                            master["graph"]["edges"], wordings, **kw)


def test_one_wording_is_exactly_rank_notes(tmp_path):
    master = _master(tmp_path)
    assert _rank(master, [EN, "  "]) == rank_notes(
        master["notes"], master["taxonomy"], master["graph"]["edges"], EN)


def test_translation_finds_the_note_the_original_misses(tmp_path):
    master = _master(tmp_path)
    assert _rank(master, [VI])[0]["note"]["id"] != "motion-planning"
    top = _rank(master, [VI, EN])[0]
    assert top["note"]["id"] == "motion-planning"
    assert "fused" in top


def test_a_wording_that_matches_nothing_changes_nothing(tmp_path):
    master = _master(tmp_path)
    alone = [e["note"]["id"] for e in _rank(master, [EN])]
    fused = [e["note"]["id"] for e in _rank(master, [EN, "xyzzy plugh"])]
    assert fused == alone


def test_score_stays_a_single_wording_score(tmp_path):
    # The answer threshold was calibrated on one wording: fusing two must
    # not add their scores into "score".
    master = _master(tmp_path)
    single = rank_notes(master["notes"], master["taxonomy"],
                        master["graph"]["edges"], EN)[0]["score"]
    fused = _rank(master, [EN, EN])[0]
    assert fused["score"] == single
    assert fused["fused"] == 2 * single


def test_rrf_orders_by_rank_not_score():
    a = [{"note_id": "x", "score": 100.0}, {"note_id": "y", "score": 1.0}]
    b = [{"note_id": "y", "score": 0.9}, {"note_id": "x", "score": 0.1}]
    c = [{"note_id": "y", "score": 0.5}]
    assert [e["note_id"] for e in fuse_rankings([a, b, c], method="rrf")] == ["y", "x"]
    assert [e["note_id"] for e in fuse_rankings([a, b, c], method="sum")] == ["x", "y"]


def test_compact_query_fuses_wordings_like_rank_notes(tmp_path):
    _master(tmp_path, web=True)
    master = json.loads((tmp_path / "robots" / "master.json").read_text())
    bodies = {n["id"]: n["body"] for n in master["notes"]}
    compact = compact_master(master)
    out = tmp_path / "compact"
    out.mkdir()
    (out / "registry.json").write_text(json.dumps(compact), encoding="utf-8")
    (out / "index.json").write_text(json.dumps(build_index(compact, bodies)),
                                    encoding="utf-8")
    with (out / "bodies.jsonl").open("w", encoding="utf-8") as handle:
        for note_id, body in bodies.items():
            handle.write(json.dumps({"id": note_id, "body": body}) + "\n")
    from atom_brain.compact import query_compact_dir
    legacy = [(e["note"]["id"], e["score"], e["fused"])
              for e in _rank(master, [VI, EN])]
    fast = [(e["note"]["id"], e["score"], e["fused"])
            for e in query_compact_dir(out, VI, also=[EN])]
    assert legacy == fast


def test_cli_also(tmp_path, capsys):
    _master(tmp_path)
    assert cli.main(["--masters-root", str(tmp_path), "--question", VI,
                     "--also", EN, "--top", "1"]) == 0
    out = capsys.readouterr().out
    assert "(+ 1 other wording(s): What is motion planning?)" in out
    assert "- Motion planning" in out


def test_off_topic_question_stays_unanswered_with_a_translation(tmp_path, capsys):
    _master(tmp_path)
    cli.main(["--masters-root", str(tmp_path), "--question",
              "Thủ đô của Pháp là gì?", "--also", "What is the capital of France?"])
    assert "does not hold an answer" in capsys.readouterr().out


def test_master_skill_md_tells_the_agent_to_add_a_translation():
    text = render_skill_md("Robots", [], [], [], [], 1)
    assert '--also "<translation>"' in text
