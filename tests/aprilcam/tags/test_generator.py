"""Tests for ``aprilcam.tags.generator.TagGenerator`` (ticket 028-001).

Fixture/headless only -- ``tags/`` is a standalone, purely-offline
utility (design.md §4.10); nothing here needs a daemon, camera, or
network. Dictionary sizes are asserted against the real installed
OpenCV programmatically, never as hardcoded literals, so this suite
stays correct if the installed OpenCV version changes a dictionary.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import cv2 as cv
import pytest

from aprilcam.errors import InvalidTagId, UnwritableOutputPath
from aprilcam.tags.generator import TagGenerator
from aprilcam.types import TagFamily


@pytest.fixture
def generator() -> TagGenerator:
    return TagGenerator()


# ---------------------------------------------------------------------------
# valid_ids -- programmatic dictionary-size assertions
# ---------------------------------------------------------------------------


def test_valid_ids_apriltag_matches_real_dictionary_size(generator: TagGenerator) -> None:
    dictionary = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_APRILTAG_36h11)
    expected = dictionary.bytesList.shape[0]

    result = generator.valid_ids(TagFamily.APRILTAG)

    assert result == range(0, expected)


def test_valid_ids_aruco_matches_real_dictionary_size(generator: TagGenerator) -> None:
    dictionary = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
    expected = dictionary.bytesList.shape[0]

    result = generator.valid_ids(TagFamily.ARUCO)

    assert result == range(0, expected)


# ---------------------------------------------------------------------------
# SUC-001: successful generation
# ---------------------------------------------------------------------------


def test_generate_writes_one_png_per_id_at_correct_size(
    generator: TagGenerator, tmp_path: Path
) -> None:
    ids = tuple(range(1, 11))
    size_cm = 8.0
    dpi = 300
    expected_px = round(size_cm / 2.54 * dpi)

    written = generator.generate(
        TagFamily.ARUCO, ids, size_cm=size_cm, out_dir=tmp_path, dpi=dpi
    )

    assert len(written) == 10
    assert len(set(written)) == 10, "paths must be distinct, one per id"
    for path in written:
        assert path.is_file()
        image = cv.imread(str(path), cv.IMREAD_GRAYSCALE)
        assert image is not None
        assert image.shape == (expected_px, expected_px)


def test_generate_touches_nothing_else_in_out_dir(
    generator: TagGenerator, tmp_path: Path
) -> None:
    preexisting = tmp_path / "keep-me.txt"
    preexisting.write_text("do not touch")

    written = generator.generate(
        TagFamily.ARUCO, (1, 2, 3), size_cm=8.0, out_dir=tmp_path, dpi=300
    )

    assert preexisting.read_text() == "do not touch"
    on_disk = set(tmp_path.iterdir())
    assert on_disk == set(written) | {preexisting}


def test_generate_creates_out_dir_when_missing_and_ancestor_writable(
    generator: TagGenerator, tmp_path: Path
) -> None:
    out_dir = tmp_path / "does" / "not" / "exist-yet"

    written = generator.generate(
        TagFamily.ARUCO, (1,), size_cm=8.0, out_dir=out_dir, dpi=300
    )

    assert out_dir.is_dir()
    assert written[0].parent == out_dir


# ---------------------------------------------------------------------------
# SUC-002: an out-of-range id rejects the whole batch
# ---------------------------------------------------------------------------


def test_generate_rejects_out_of_range_id_and_writes_nothing(
    generator: TagGenerator, tmp_path: Path
) -> None:
    valid = generator.valid_ids(TagFamily.ARUCO)
    assert 55 not in valid, "test assumes 55 is out of range for DICT_4X4_50"

    with pytest.raises(InvalidTagId) as excinfo:
        generator.generate(
            TagFamily.ARUCO, (1, 2, 3, 55), size_cm=8.0, out_dir=tmp_path, dpi=300
        )

    assert excinfo.value.tag_id == 55
    assert excinfo.value.valid == valid
    assert f"{valid.start}..{valid.stop - 1}" in str(excinfo.value)
    # The whole batch fails together -- ids 1-3 are valid but nothing for
    # any of them was written either.
    assert list(tmp_path.iterdir()) == []


def test_generate_validates_before_touching_filesystem(
    generator: TagGenerator, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    render = MagicMock(wraps=cv.aruco.generateImageMarker)
    monkeypatch.setattr(cv.aruco, "generateImageMarker", render)

    with pytest.raises(InvalidTagId):
        generator.generate(
            TagFamily.ARUCO, (1, 55), size_cm=8.0, out_dir=tmp_path, dpi=300
        )

    render.assert_not_called()


# ---------------------------------------------------------------------------
# SUC-003: unwritable out_dir fails before any encode call
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits are not meaningful for root or on non-POSIX systems",
)
def test_generate_unwritable_out_dir_raises_before_any_encode_call(
    generator: TagGenerator, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o444)

    render = MagicMock(wraps=cv.aruco.generateImageMarker)
    write = MagicMock(wraps=cv.imwrite)
    monkeypatch.setattr(cv.aruco, "generateImageMarker", render)
    monkeypatch.setattr(cv, "imwrite", write)

    try:
        with pytest.raises(UnwritableOutputPath) as excinfo:
            generator.generate(
                TagFamily.ARUCO, (1,), size_cm=8.0, out_dir=blocked, dpi=300
            )
    finally:
        blocked.chmod(0o755)

    assert excinfo.value.path == blocked
    render.assert_not_called()
    write.assert_not_called()


def test_unwritable_output_path_usable_with_report_error() -> None:
    from aprilcam.cli._common import report_error

    exc = UnwritableOutputPath(Path("/no/such/writable/dir"))
    # report_error only needs a clean str(); must not raise.
    assert report_error(exc) != 0
    assert "/no/such/writable/dir" in str(exc)


# ---------------------------------------------------------------------------
# Size/DPI legibility warning -- warns, does not raise, still generates
# ---------------------------------------------------------------------------


def test_generate_warns_on_unachievably_small_size_but_still_writes(
    generator: TagGenerator, tmp_path: Path
) -> None:
    with pytest.warns(UserWarning):
        written = generator.generate(
            TagFamily.ARUCO, (1,), size_cm=0.5, out_dir=tmp_path, dpi=72
        )

    assert len(written) == 1
    assert written[0].is_file()


def test_generate_does_not_warn_for_a_normal_size(
    generator: TagGenerator, tmp_path: Path
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        generator.generate(
            TagFamily.ARUCO, (1,), size_cm=8.0, out_dir=tmp_path, dpi=300
        )
