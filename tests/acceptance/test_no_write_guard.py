"""Unit tests for the no-write guard itself (``conftest.py``'s
:func:`hash_tree` / :func:`assert_no_writes`), independent of any real
daemon.

Ticket 014's own acceptance criterion: "The no-write guard is itself
unit-tested independent of any real daemon: construct a fake tree,
touch a file inside it mid-'session', confirm the guard raises naming
that exact file." Everything below operates on a fake tree under
``tmp_path`` -- never the real ``~/.config/aprilcam`` or
``~/.local/share/aprilcam`` -- so this file carries zero hardware risk
and zero risk to the operator's real, persistent state even though it
lives inside the marker-audited ``tests/acceptance/`` directory.

These tests still carry all three of
``acceptance``+``camera``+``integration`` (via the module-level
``pytestmark`` below) per sprint.md Decision 4 and this directory's own
``pytest_collection_modifyitems`` audit -- every test under
``tests/acceptance/`` carries the full marker set uniformly, regardless
of whether that individual test happens to need real hardware, so
directory membership alone is what keeps the whole layer out of the
fast gate rather than a per-test judgment call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import pytest

from tests.acceptance.conftest import NoWriteViolation, assert_no_writes, hash_tree

pytestmark = [pytest.mark.acceptance, pytest.mark.camera, pytest.mark.integration]


def _make_fake_tree(root: Path) -> Tuple[Path, Path]:
    """A small fake config_dir/state_dir pair standing in for the real
    ones -- same shape (a top-level file, a nested cameras/<slug>
    directory), never the real thing."""
    config_dir = root / "config_dir"
    state_dir = root / "state_dir"
    (config_dir / "cameras" / "arducam-ov9782-usb-camera").mkdir(parents=True)
    (state_dir / "cameras" / "arducam-ov9782-usb-camera").mkdir(parents=True)

    (config_dir / "config.json").write_text('{"schema_version": 1}')
    (config_dir / "cameras" / "arducam-ov9782-usb-camera" / "config.json").write_text(
        '{"camera_position": {"height_cm": 128.22990496814816}}'
    )
    (state_dir / "tags.json").write_text("{}")
    (state_dir / "cameras" / "arducam-ov9782-usb-camera" / "info.json").write_text(
        "{}"
    )
    return config_dir, state_dir


def _snapshot(config_dir: Path, state_dir: Path) -> dict:
    return {**hash_tree(config_dir), **hash_tree(state_dir)}


def test_hash_tree_is_deterministic_and_content_keyed(tmp_path: Path) -> None:
    config_dir, _state_dir = _make_fake_tree(tmp_path)

    first = hash_tree(config_dir)
    second = hash_tree(config_dir)

    assert first == second
    cam_config = config_dir / "cameras" / "arducam-ov9782-usb-camera" / "config.json"
    assert cam_config in first
    assert first[cam_config] != ""


def test_hash_tree_of_missing_root_is_empty(tmp_path: Path) -> None:
    assert hash_tree(tmp_path / "does-not-exist-yet") == {}


def test_assert_no_writes_passes_when_tree_is_unchanged(tmp_path: Path) -> None:
    config_dir, state_dir = _make_fake_tree(tmp_path)
    baseline = _snapshot(config_dir, state_dir)

    current = _snapshot(config_dir, state_dir)

    assert_no_writes(baseline, current, label="after fake session")  # must not raise


def test_guard_raises_naming_the_touched_file(tmp_path: Path) -> None:
    config_dir, state_dir = _make_fake_tree(tmp_path)
    baseline = _snapshot(config_dir, state_dir)

    touched = config_dir / "cameras" / "arducam-ov9782-usb-camera" / "config.json"
    touched.write_text('{"camera_position": {"height_cm": 999.0}}')  # simulated write

    current = _snapshot(config_dir, state_dir)

    with pytest.raises(NoWriteViolation) as excinfo:
        assert_no_writes(baseline, current, label="after fake session")

    message = str(excinfo.value)
    assert str(touched) in message
    assert "MODIFIED" in message
    # And only the touched file is named -- every untouched file agreed.
    untouched = config_dir / "config.json"
    assert f"MODIFIED {untouched}" not in message


def test_guard_raises_naming_an_added_file(tmp_path: Path) -> None:
    config_dir, state_dir = _make_fake_tree(tmp_path)
    baseline = _snapshot(config_dir, state_dir)

    new_file = state_dir / "unexpected.json"
    new_file.write_text("{}")

    current = _snapshot(config_dir, state_dir)

    with pytest.raises(NoWriteViolation) as excinfo:
        assert_no_writes(baseline, current, label="after fake session")

    message = str(excinfo.value)
    assert str(new_file) in message
    assert "ADDED" in message


def test_guard_raises_naming_a_removed_file(tmp_path: Path) -> None:
    config_dir, state_dir = _make_fake_tree(tmp_path)
    baseline = _snapshot(config_dir, state_dir)

    removed = config_dir / "config.json"
    removed.unlink()

    current = _snapshot(config_dir, state_dir)

    with pytest.raises(NoWriteViolation) as excinfo:
        assert_no_writes(baseline, current, label="after fake session")

    message = str(excinfo.value)
    assert str(removed) in message
    assert "REMOVED" in message


def test_guard_is_silent_when_an_unrelated_file_is_added_outside_the_roots(
    tmp_path: Path,
) -> None:
    """Nothing outside config_dir/state_dir is this guard's concern --
    confirms hash_tree really is scoped to the root it is given, not the
    whole tmp_path sandbox."""
    config_dir, state_dir = _make_fake_tree(tmp_path)
    baseline = _snapshot(config_dir, state_dir)

    (tmp_path / "unrelated-scratch-file.txt").write_text("not under either root")

    current = _snapshot(config_dir, state_dir)

    assert_no_writes(baseline, current, label="after fake session")  # must not raise
