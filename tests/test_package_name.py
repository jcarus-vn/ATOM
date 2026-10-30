"""The pip distribution and import package must not reuse PyPI's `atom`.

`atom` on PyPI is nucleic/atom (used by Enaml). Shipping a distribution or
a top-level module with that name made the two packages overwrite each
other. The command users type stays `atom`.
"""

import importlib
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

PYPROJECT = (ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8")


def _project_value(key):
    match = re.search(rf'^{key}\s*=\s*"([^"]+)"', PYPROJECT, re.M)
    assert match, key
    return match.group(1)


def test_distribution_is_atom_brain():
    assert _project_value("name") == "atom-brain"


def test_atom_command_points_at_atom_brain():
    scripts = PYPROJECT.split("[project.scripts]", 1)[1].split("[", 1)[0]
    target = re.search(r'^atom\s*=\s*"([^"]+)"', scripts, re.M).group(1)
    module, func = target.split(":")
    assert module == "atom_brain.cli"
    assert callable(getattr(importlib.import_module(module), func))


def test_no_top_level_atom_package_is_shipped():
    assert not (ROOT_DIR / "atom" / "__init__.py").exists()
    assert (ROOT_DIR / "atom_brain" / "__init__.py").exists()
