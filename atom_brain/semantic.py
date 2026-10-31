"""Optional semantic retrieval with a local multilingual embedding model.

Experimental and off by default. A question worded differently from the
notes, or in another language, can still find them when notes and
question are compared as embeddings instead of words. It needs:

- the optional dependencies: ``pip install "atom-brain[semantic]"``
  (onnxruntime, tokenizers, numpy), and
- a model directory on disk with an ONNX export of a sentence-embedding
  model (``model.onnx``, ``model_quantized.onnx``, or the same under
  ``onnx/``) and its ``tokenizer.json``. ATOM never downloads a model;
  the user puts it there (see docs/master-skill.md). An optional
  ``atom-embed.json`` in that directory sets ``query_prefix``,
  ``passage_prefix`` and ``max_length``; the defaults follow the E5
  convention ("query: " / "passage: ", 512 tokens).

The index lives in the master directory as ``embeddings.json`` (model
identity, note ids, a hash of each note's text) plus ``embeddings.f16``
(one float16 row per note). The builder's directory swap copies both, so
a rebuild keeps them; rows for notes whose text changed are recomputed
by the next ``--embed`` run, and the query warns while the index is
stale. The core package never imports this module's dependencies unless
semantic retrieval is asked for.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

INDEX_META = "embeddings.json"
INDEX_DATA = "embeddings.f16"
MODEL_ENV = "ATOM_EMBED_MODEL"
CONFIG_NAME = "atom-embed.json"
DEFAULT_CONFIG = {"query_prefix": "query: ", "passage_prefix": "passage: ",
                  "max_length": 512}
# A semantic hit with no lexical support is shown only at or above this
# cosine similarity. NOT calibrated: cosine scales differ between models.
# Measure with tools/eval_multilingual.py --embed-model and set
# "min_cosine" in the index (or pass --min-cosine) before relying on it.
DEFAULT_MIN_COSINE = 0.80
NOTE_TEXT_CHARS = 2000
_ONNX_CANDIDATES = ("model.onnx", "onnx/model.onnx",
                    "model_quantized.onnx", "onnx/model_quantized.onnx")


class SemanticUnavailable(RuntimeError):
    """Semantic retrieval was asked for but cannot run here."""


def missing_dependencies() -> list[str]:
    missing = []
    for module in ("onnxruntime", "tokenizers", "numpy"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    return missing


def model_dir_from(value: str | os.PathLike | None) -> Path | None:
    """The model directory from an argument, else $ATOM_EMBED_MODEL."""
    raw = str(value) if value else os.environ.get(MODEL_ENV, "").strip()
    return Path(os.path.expanduser(raw)) if raw else None


def _onnx_path(model_dir: Path) -> Path:
    for name in _ONNX_CANDIDATES:
        path = model_dir / name
        if path.is_file():
            return path
    raise SemanticUnavailable(
        f"no ONNX model in {model_dir} (looked for {', '.join(_ONNX_CANDIDATES)})")


def _config(model_dir: Path) -> dict:
    config = dict(DEFAULT_CONFIG)
    path = model_dir / CONFIG_NAME
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SemanticUnavailable(f"cannot read {path}: {exc}") from exc
        if isinstance(data, dict):
            config.update({k: data[k] for k in DEFAULT_CONFIG if k in data})
    return config


def model_identity(model_dir: Path) -> str:
    """A stable id for a model directory without hashing the whole model:
    tokenizer, config, ONNX size, and its first and last MiB."""
    model_dir = Path(model_dir)
    onnx_path = _onnx_path(model_dir)
    digest = hashlib.sha256()
    for name in ("tokenizer.json", CONFIG_NAME):
        path = model_dir / name
        if path.is_file():
            digest.update(path.read_bytes())
    size = onnx_path.stat().st_size
    digest.update(str(size).encode("ascii"))
    with onnx_path.open("rb") as handle:
        digest.update(handle.read(1 << 20))
        if size > (1 << 20):
            handle.seek(max(0, size - (1 << 20)))
            digest.update(handle.read(1 << 20))
    return digest.hexdigest()[:24]


class Embedder:
    """Mean-pooled, L2-normalized sentence embeddings from an ONNX model."""

    def __init__(self, model_dir: str | os.PathLike):
        missing = missing_dependencies()
        if missing:
            raise SemanticUnavailable(
                "semantic retrieval needs " + ", ".join(missing)
                + ': pip install "atom-brain[semantic]"')
        import onnxruntime
        from tokenizers import Tokenizer

        self.model_dir = Path(model_dir)
        tokenizer_path = self.model_dir / "tokenizer.json"
        if not tokenizer_path.is_file():
            raise SemanticUnavailable(f"no tokenizer.json in {self.model_dir}")
        self.config = _config(self.model_dir)
        self.identity = model_identity(self.model_dir)
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=int(self.config["max_length"]))
        pad_token = next((t for t in ("<pad>", "[PAD]")
                          if self.tokenizer.token_to_id(t) is not None), None)
        pad_id = self.tokenizer.token_to_id(pad_token) if pad_token else 0
        self.tokenizer.enable_padding(pad_id=pad_id, pad_token=pad_token or "[PAD]")
        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        self.session = onnxruntime.InferenceSession(
            str(_onnx_path(self.model_dir)), sess_options=options,
            providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def embed(self, texts: list[str], kind: str = "passage", batch: int = 16):
        import numpy as np

        prefix = self.config["query_prefix" if kind == "query" else "passage_prefix"]
        rows = []
        for start in range(0, len(texts), batch):
            chunk = [prefix + (t or "") for t in texts[start:start + batch]]
            encoded = self.tokenizer.encode_batch(chunk)
            ids = np.array([e.ids for e in encoded], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            feed = {k: v for k, v in feed.items() if k in self.inputs}
            output = self.session.run(None, feed)[0]
            if output.ndim == 3:                     # token states: mean-pool
                weights = mask[:, :, None].astype(np.float32)
                output = (output * weights).sum(axis=1) / np.clip(
                    weights.sum(axis=1), 1e-9, None)
            rows.append(output.astype(np.float32))
        if not rows:
            return np.zeros((0, 0), dtype=np.float32)
        matrix = np.concatenate(rows, axis=0)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.clip(norms, 1e-12, None)


def note_text(note: dict) -> str:
    """What is embedded for a note: title, other names, then the body."""
    parts = [note.get("title", "")] + list(note.get("aliases", []))
    head = ". ".join(p for p in parts if p)
    return f"{head}. {note.get('body', '')}"[:NOTE_TEXT_CHARS]


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_index(master_dir: Path):
    """(meta, float32 matrix) of a master's index, or None if it has none."""
    import numpy as np

    master_dir = Path(master_dir)
    meta_path, data_path = master_dir / INDEX_META, master_dir / INDEX_DATA
    if not meta_path.is_file() or not data_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        flat = np.frombuffer(data_path.read_bytes(), dtype="<f2")
    except (OSError, ValueError):
        return None
    dim, count = int(meta.get("dim", 0)), len(meta.get("ids", []))
    if dim <= 0 or flat.size != dim * count:
        return None
    return meta, flat.reshape(count, dim).astype(np.float32)


def has_index(master_dir: Path) -> bool:
    return (Path(master_dir) / INDEX_META).is_file()


def build_index(master_dir: Path, notes: list[dict], embedder: Embedder) -> dict:
    """Embed the notes into the master directory, reusing unchanged rows.

    A row is reused when the note id, its text hash and the model are the
    same as in the existing index. Returns counts for the report.
    """
    import numpy as np

    master_dir = Path(master_dir)
    texts = [note_text(n) for n in notes]
    hashes = [_text_hash(t) for t in texts]
    ids = [n.get("id", "") for n in notes]
    reuse: dict[tuple[str, str], object] = {}
    old = load_index(master_dir)
    if old is not None and old[0].get("model_id") == embedder.identity:
        for i, (old_id, old_hash) in enumerate(zip(old[0]["ids"], old[0]["hashes"])):
            reuse[(old_id, old_hash)] = old[1][i]
    todo = [i for i, key in enumerate(zip(ids, hashes)) if key not in reuse]
    fresh = embedder.embed([texts[i] for i in todo], kind="passage") if todo else None
    rows = []
    fresh_rows = dict(zip(todo, list(fresh) if fresh is not None else []))
    for i, key in enumerate(zip(ids, hashes)):
        rows.append(fresh_rows[i] if i in fresh_rows else reuse[key])
    matrix = np.stack(rows).astype("<f2") if rows else np.zeros((0, 0), dtype="<f2")
    keep_min = old[0].get("min_cosine") if old is not None and \
        old[0].get("model_id") == embedder.identity else None
    meta = {
        "format": "atom-embeddings", "version": 1,
        "model_id": embedder.identity, "model_name": embedder.model_dir.name,
        "dim": int(matrix.shape[1]) if rows else 0, "dtype": "float16",
        "ids": ids, "hashes": hashes,
        "query_prefix": embedder.config["query_prefix"],
        "passage_prefix": embedder.config["passage_prefix"],
        "min_cosine": keep_min,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    data_tmp = master_dir / f".{INDEX_DATA}.tmp-{os.getpid()}"
    meta_tmp = master_dir / f".{INDEX_META}.tmp-{os.getpid()}"
    data_tmp.write_bytes(matrix.tobytes())
    meta_tmp.write_text(json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(data_tmp, master_dir / INDEX_DATA)
    os.replace(meta_tmp, master_dir / INDEX_META)
    return {"notes": len(ids), "embedded": len(todo), "reused": len(ids) - len(todo),
            "dim": meta["dim"], "model_id": embedder.identity}


def stale_notes(meta: dict, notes: list[dict]) -> list[str]:
    """Ids of notes missing from the index or changed since it was built."""
    indexed = dict(zip(meta.get("ids", []), meta.get("hashes", [])))
    return [n.get("id", "") for n in notes
            if indexed.get(n.get("id", "")) != _text_hash(note_text(n))]


def rank_semantic(meta: dict, matrix, query_vectors,
                  allowed: set[str] | None = None) -> list[dict]:
    """Notes by cosine similarity to the closest of the query vectors.

    Entries: ``note_id`` and ``cosine`` (both vectors are normalized, so
    the dot product is the cosine). ``allowed`` restricts the candidates
    (a ``--source`` bound). Ties break by id.
    """
    import numpy as np

    if matrix.size == 0 or len(query_vectors) == 0:
        return []
    sims = np.asarray(matrix @ np.asarray(query_vectors, dtype=np.float32).T)
    best = sims.max(axis=1)
    entries = [{"note_id": note_id, "cosine": float(best[i])}
               for i, note_id in enumerate(meta["ids"])
               if allowed is None or note_id in allowed]
    entries.sort(key=lambda e: (-e["cosine"], e["note_id"]))
    return entries


def min_cosine_for(meta: dict, override: float | None = None) -> float:
    if override is not None:
        return float(override)
    value = meta.get("min_cosine")
    return float(value) if isinstance(value, (int, float)) else DEFAULT_MIN_COSINE
