"""Tests for the release version script."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

_path = Path(__file__).parents[1] / ".github" / "scripts" / "next_version.py"
_spec = importlib.util.spec_from_file_location("next_version", _path)
nv = importlib.util.module_from_spec(_spec)
sys.modules["next_version"] = nv
_spec.loader.exec_module(nv)


@pytest.mark.parametrize(
    ("labels", "title", "manual", "kind"),
    [
        ([], "Fix rounding", None, "patch"),
        ([], "feat: monthly sensor", None, "minor"),
        ([], "feat(sensor): monthly sensor", None, "minor"),
        ([], "refactor!: rename entities", None, "major"),
        (["minor"], "fix: typo", None, "minor"),
        (["major", "patch"], "x", None, "major"),
        (["patch"], "feat: x", "major", "major"),
    ],
)
def test_bump_kind(labels, title, manual, kind):
    assert nv.bump_kind(labels, title, manual) == kind


def test_next_version():
    assert nv.next_version(None, "0.1.0", "minor") == "0.1.0"  # first release
    assert nv.next_version("v0.1.0", "0.1.0", "patch") == "0.1.1"
    assert nv.next_version("v0.1.9", "0.1.0", "minor") == "0.2.0"
    assert nv.next_version("v1.4.2", "0.1.0", "major") == "2.0.0"
    with pytest.raises(ValueError):
        nv.next_version("latest", "0.1.0", "patch")


def test_cli(tmp_path, capsys):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"version": "0.1.0"}')
    assert nv.main(["--latest-tag", "v0.3.1", "--manifest", str(manifest), "--labels", '["minor"]']) == 0
    assert capsys.readouterr().out.strip() == "0.4.0"
