"""The external OCR hook: a page the built-in OCR still reads poorly is
sent to a user-configured command (e.g. a handwriting model). ATOM bundles
no model -- this only tests the seam. Fixtures are synthetic.
"""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from atom_brain import ocr  # noqa: E402

HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def _tsv(conf):
    return HEADER + f"\n5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t{conf}\tword"


def _echo_hook(tmp_path, text, exit_code=0):
    script = tmp_path / "hook.py"
    script.write_text(
        "import sys\n"
        f"sys.stderr.write('note')\n"
        f"print({text!r})\n"
        f"sys.exit({exit_code})\n")
    return f"{sys.executable} {script}"


def test_external_ocr_runs_with_the_image_path(tmp_path, monkeypatch):
    monkeypatch.setenv(ocr.OCR_HOOK_ENV_VAR, _echo_hook(tmp_path, "chữ viết tay"))
    assert ocr.external_ocr(b"fake-png") == "chữ viết tay"


def test_no_hook_env_means_no_call(monkeypatch):
    monkeypatch.delenv(ocr.OCR_HOOK_ENV_VAR, raising=False)
    assert ocr.external_ocr(b"fake-png") is None


def test_hook_failure_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv(ocr.OCR_HOOK_ENV_VAR, _echo_hook(tmp_path, "x", exit_code=3))
    assert ocr.external_ocr(b"fake-png") is None


def _wire(monkeypatch, confidence, blank=False):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ocr, "_split_into_false_columns", lambda tsv: False)
    monkeypatch.setattr(ocr, "is_blank_image", lambda data: blank)
    monkeypatch.setattr(ocr, "_ocr_with_layout",
                        lambda *a, **k: ("built-in read", _tsv(confidence)))
    monkeypatch.setattr(ocr, "_recover_low_confidence", lambda *a, **k: [])
    monkeypatch.setattr(ocr, "_deskew_candidate", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(ocr, "external_ocr",
                        lambda data, timeout=ocr.OCR_HOOK_TIMEOUT: calls.append(1) or "HOOK TEXT")
    return calls


def test_hook_used_only_when_the_built_in_read_is_poor(monkeypatch):
    monkeypatch.setenv(ocr.OCR_HOOK_ENV_VAR, "some-cmd")
    calls = _wire(monkeypatch, confidence=20.0)
    assert ocr.ocr_page_bytes(b"png", lang="eng") == "HOOK TEXT"
    assert calls == [1]


def test_hook_not_used_on_a_confident_page(monkeypatch):
    monkeypatch.setenv(ocr.OCR_HOOK_ENV_VAR, "some-cmd")
    calls = _wire(monkeypatch, confidence=90.0)
    assert ocr.ocr_page_bytes(b"png", lang="eng") == "built-in read"
    assert calls == []


def test_hook_not_used_when_unset(monkeypatch):
    monkeypatch.delenv(ocr.OCR_HOOK_ENV_VAR, raising=False)
    calls = _wire(monkeypatch, confidence=20.0)
    assert ocr.ocr_page_bytes(b"png", lang="eng") == "built-in read"
    assert calls == []


def test_hook_not_used_on_a_blank_page(monkeypatch):
    monkeypatch.setenv(ocr.OCR_HOOK_ENV_VAR, "some-cmd")
    calls = _wire(monkeypatch, confidence=-1.0, blank=True)
    assert ocr.ocr_page_bytes(b"png", lang="eng") == "built-in read"
    assert calls == []


def test_empty_hook_output_falls_back_to_built_in(monkeypatch):
    monkeypatch.setenv(ocr.OCR_HOOK_ENV_VAR, "some-cmd")
    _wire(monkeypatch, confidence=20.0)
    monkeypatch.setattr(ocr, "external_ocr", lambda data, timeout=ocr.OCR_HOOK_TIMEOUT: None)
    assert ocr.ocr_page_bytes(b"png", lang="eng") == "built-in read"
