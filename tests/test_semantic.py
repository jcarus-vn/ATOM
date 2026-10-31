"""Experimental semantic retrieval (atom_brain/semantic.py).

The real-model path runs against a toy ONNX model built here with the
``onnx`` package: words of one concept, English or Vietnamese, share an
embedding axis, so a question worded with no overlapping word still lands
on the right note. That checks the plumbing (tokenize, pool, normalize,
index, fuse, threshold), not retrieval quality, which needs a real model
(tools/eval_multilingual.py --embed-model). Without onnx, onnxruntime,
tokenizers or numpy those tests are skipped; the pure-Python parts always
run. All fixtures are synthetic sentences written for these tests.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "scripts"))
sys.path.insert(0, str(ROOT_DIR / "tests"))

import build_master as cli  # noqa: E402
from atom_brain import semantic  # noqa: E402
from atom_brain.master import build_master, fuse_hybrid, load_master  # noqa: E402
from test_concept_aliases import SOURCES  # noqa: E402

CONCEPTS = {
    0: "motion planning path route collision free lập kế hoạch chuyển động đường đi",
    1: "loop closure revisited place drift map đóng vòng quay lại chỗ cũ trôi sai số",
    2: "differential drive wheels steer speed bánh xe vi lái tốc độ",
    3: "configuration space poses không gian cấu hình tư thế",
    4: "numerical integration time increments simulate tích phân số mô phỏng",
}


def _toy_model(model_dir: Path) -> Path:
    onnx = pytest.importorskip("onnx")
    np = pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    tokenizers = pytest.importorskip("tokenizers")
    from onnx import TensorProto, helper, numpy_helper

    vocab = {"[PAD]": 0, "[UNK]": 1}
    axis = {}
    for concept, words in CONCEPTS.items():
        for word in words.split():
            if word not in vocab:
                vocab[word] = len(vocab)
                axis[vocab[word]] = concept
    dim = 8
    table = np.zeros((len(vocab), dim), dtype=np.float32)
    table[1, 7] = 0.05                                   # unknown words: near nothing
    for token_id, concept in axis.items():
        table[token_id, concept] = 1.0
    graph = helper.make_graph(
        [helper.make_node("Gather", ["table", "input_ids"], ["last_hidden_state"])],
        "toy",
        [helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["b", "t"]),
         helper.make_tensor_value_info("attention_mask", TensorProto.INT64, ["b", "t"])],
        [helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT,
                                       ["b", "t", dim])],
        initializer=[numpy_helper.from_array(table, "table")])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    model_dir.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(model_dir / "model.onnx"))
    tok = tokenizers.Tokenizer(tokenizers.models.WordLevel(vocab, unk_token="[UNK]"))
    tok.normalizer = tokenizers.normalizers.Lowercase()
    tok.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    tok.save(str(model_dir / "tokenizer.json"))
    (model_dir / "atom-embed.json").write_text(
        json.dumps({"query_prefix": "", "passage_prefix": ""}), encoding="utf-8")
    return model_dir


def _master(root: Path) -> Path:
    build_master("Robots", SOURCES, root)
    return root / "robots"


# --- pure Python ---------------------------------------------------------

def test_hybrid_keeps_every_candidate_and_its_lexical_score():
    lexical = [{"note_id": f"n{i}", "score": float(60 - i)} for i in range(60)]
    sem = [{"note_id": "n59", "cosine": 0.95}, {"note_id": "n3", "cosine": 0.5}]
    fused = fuse_hybrid(lexical, sem)
    assert len(fused) == 60
    by_id = {e["note_id"]: e for e in fused}
    assert by_id["n59"]["score"] == 1.0 and by_id["n59"]["cosine"] == 0.95
    assert by_id["n0"]["cosine"] is None
    assert [e["note_id"] for e in fused].index("n59") < 50   # pulled up by rank


def test_semantic_hit_passes_on_cosine_alone():
    hits = [{"score": 2.0, "cosine": 0.9}, {"score": 9.0, "cosine": None},
            {"score": 2.0, "cosine": 0.5}]
    assert cli._confident(hits, 8.0, 0.8) == hits[:2]
    assert cli._confident(hits, 8.0) == hits[1:2]


def test_no_index_means_no_semantic_and_no_noise(tmp_path, capsys):
    _master(tmp_path)
    cli.main(["--masters-root", str(tmp_path), "--question", "What is loop closure?"])
    captured = capsys.readouterr()
    assert "[semantic]" not in captured.err and "cosine" not in captured.out


# --- toy ONNX model ------------------------------------------------------

def test_embedder_pools_over_real_tokens_only(tmp_path):
    np = pytest.importorskip("numpy")
    embedder = semantic.Embedder(_toy_model(tmp_path / "model"))
    alone = embedder.embed(["đóng vòng"])[0]
    batched = embedder.embed(["đóng vòng", "motion planning path route collision"])[0]
    assert np.allclose(alone, batched, atol=1e-6)            # padding ignored
    assert abs(float(np.linalg.norm(alone)) - 1.0) < 1e-5


def test_index_reuses_unchanged_rows(tmp_path):
    master_dir = _master(tmp_path / "root")
    embedder = semantic.Embedder(_toy_model(tmp_path / "model"))
    notes = load_master(master_dir)["notes"]
    assert semantic.build_index(master_dir, notes, embedder)["embedded"] == len(notes)
    assert semantic.build_index(master_dir, notes, embedder)["reused"] == len(notes)
    meta, _matrix = semantic.load_index(master_dir)
    notes[0]["body"] += " Edited."
    assert semantic.stale_notes(meta, notes) == [notes[0]["id"]]
    assert semantic.build_index(master_dir, notes, embedder)["embedded"] == 1


def test_question_with_no_shared_word_is_answered_semantically(tmp_path, capsys):
    root = tmp_path / "root"
    _master(root)
    model = _toy_model(tmp_path / "model")
    question = "Robot quay lại chỗ cũ thì xử lý trôi thế nào?"
    cli.main(["--masters-root", str(root), "--question", question])
    assert "does not hold an answer" in capsys.readouterr().out   # lexical alone
    assert cli.main(["--masters-root", str(root), "--field", "Robots",
                     "--embed", "--embed-model", str(model)]) == 0
    assert "embedding index: 5 notes (5 embedded, 0 reused)" in capsys.readouterr().out
    cli.main(["--masters-root", str(root), "--question", question,
              "--embed-model", str(model), "--top", "1"])
    out = capsys.readouterr().out
    assert "- Loop closure" in out
    assert float(re.search(r"cosine ([0-9.]+)", out).group(1)) >= 0.9


def test_wrong_model_or_missing_model_falls_back_to_lexical(tmp_path, capsys,
                                                             monkeypatch):
    root = tmp_path / "root"
    _master(root)
    model = _toy_model(tmp_path / "model")
    cli.main(["--masters-root", str(root), "--field", "Robots", "--embed",
              "--embed-model", str(model)])
    capsys.readouterr()
    monkeypatch.delenv(semantic.MODEL_ENV, raising=False)
    cli.main(["--masters-root", str(root), "--question", "What is loop closure?"])
    assert "no model is set" in capsys.readouterr().err
    other = tmp_path / "other"
    shutil.copytree(model, other)
    (other / "atom-embed.json").write_text(json.dumps({"query_prefix": "q "}))
    cli.main(["--masters-root", str(root), "--question", "What is loop closure?",
              "--embed-model", str(other)])
    captured = capsys.readouterr()
    assert "built with another model" in captured.err
    assert "- Loop closure" in captured.out                          # lexical still answers


def test_rebuild_keeps_the_index_and_says_it_may_be_stale(tmp_path, capsys):
    root = tmp_path / "root"
    master_dir = _master(root)
    model = _toy_model(tmp_path / "model")
    cli.main(["--masters-root", str(root), "--field", "Robots", "--embed",
              "--embed-model", str(model)])
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"field": "Robots", "sources": SOURCES}))
    capsys.readouterr()
    assert cli.main(["--masters-root", str(root), "--skills-json", str(manifest)]) == 0
    assert "semantic index may be stale" in capsys.readouterr().out
    assert (master_dir / semantic.INDEX_DATA).exists()


def test_compact_dir_uses_the_copied_index(tmp_path, capsys):
    root = tmp_path / "root"
    web = [dict(src, kind="web", url=f"https://example.org/{i}/",
                fetched_at="2026-01-02T00:00:00+00:00") for i, src in enumerate(SOURCES)]
    build_master("Robots", web, root)
    model = _toy_model(tmp_path / "model")
    cli.main(["--masters-root", str(root), "--field", "Robots", "--embed",
              "--embed-model", str(model)])
    out = tmp_path / "compact"
    subprocess.run([sys.executable, str(ROOT_DIR / "tools" / "build_compact.py"),
                    str(root / "robots"), str(out)], check=True, capture_output=True)
    capsys.readouterr()
    cli.main(["--compact-dir", str(out), "--question",
              "Robot quay lại chỗ cũ thì xử lý trôi thế nào?",
              "--embed-model", str(model), "--top", "1"])
    assert "- Loop closure" in capsys.readouterr().out
