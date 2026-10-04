"""The scanner does not walk through Windows junctions (Documents\My Pictures and the like)."""
from __future__ import annotations

import sys
from datetime import datetime

import pytest

from photointel.pipeline.scanner import iter_files
from tests.conftest import make_image

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")


def test_a_junction_inside_a_root_is_not_followed(tmp_path):
    import _winapi

    root = tmp_path / "root"
    elsewhere = tmp_path / "Pictures"
    make_image(root / "own.jpg", colour=(10, 20, 30), taken=datetime(2024, 1, 1, 9, 0))
    make_image(elsewhere / "theirs.jpg", colour=(200, 20, 30), taken=datetime(2024, 1, 2, 9, 0))
    _winapi.CreateJunction(str(elsewhere), str(root / "My Pictures"))
    assert (root / "My Pictures" / "theirs.jpg").exists(), "the junction should work for everyone else"
    found = [rel for rel, *_ in iter_files(root, [])]
    assert found == ["own.jpg"], f"walked through the junction: {found}"


def test_a_junction_pointing_back_up_does_not_loop(tmp_path):
    import _winapi

    root = tmp_path / "root"
    make_image(root / "a" / "one.jpg", colour=(10, 20, 30), taken=datetime(2024, 1, 1, 9, 0))
    _winapi.CreateJunction(str(root), str(root / "a" / "back"))
    found = [rel for rel, *_ in iter_files(root, [])]
    assert found == ["a/one.jpg"]
