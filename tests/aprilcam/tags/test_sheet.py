"""Tests for ``TagGenerator.generate_sheet`` -- the printable PDF (UC-21,
sprint 043 ticket 002). Hardware-free: the PDF is inspected structurally
(page count, MediaBox, caption text from the decompressed content
streams), with no PDF-reader dependency."""

from __future__ import annotations

import re
import zlib
from pathlib import Path

import pytest

from aprilcam.errors import InvalidTagId, UnwritableOutputPath
from aprilcam.tags.generator import TagGenerator
from aprilcam.types import TagFamily

_PT_PER_MM = 72 / 25.4


def _pdf_text(path: Path) -> str:
    """All decompressed stream content plus the raw bytes, as latin-1."""
    raw = path.read_bytes()
    parts = [raw.decode("latin-1")]
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        try:
            parts.append(zlib.decompress(m.group(1)).decode("latin-1"))
        except zlib.error:
            pass
    return "\n".join(parts)


def _page_texts(path: Path) -> list[str]:
    """Caption strings drawn (``(text) Tj``), in document order."""
    return re.findall(r"\(([^)]*)\)\s*Tj", _pdf_text(path))


@pytest.fixture
def generator() -> TagGenerator:
    return TagGenerator()


def test_sheet_has_one_59_by_102_mm_page_per_id(generator: TagGenerator, tmp_path: Path) -> None:
    out = tmp_path / "tags.pdf"

    returned = generator.generate_sheet(TagFamily.ARUCO, tuple(range(1, 9)), out_path=out)

    assert returned == out
    text = _pdf_text(out)
    assert len(re.findall(r"/Type\s*/Page\b", text)) == 8
    boxes = re.findall(r"/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]", text)
    assert boxes
    for x0, y0, w, h in boxes:
        assert float(w) / _PT_PER_MM == pytest.approx(59.0, abs=0.05)
        assert float(h) / _PT_PER_MM == pytest.approx(102.0, abs=0.05)


def test_sheet_captions_name_family_and_id_and_nothing_positional(
    generator: TagGenerator, tmp_path: Path
) -> None:
    aruco = tmp_path / "aruco.pdf"
    generator.generate_sheet(TagFamily.ARUCO, (1, 2, 3), out_path=aruco)
    assert _page_texts(aruco) == ["ArUco 4x4 ID: 1", "ArUco 4x4 ID: 2", "ArUco 4x4 ID: 3"]

    april = tmp_path / "april.pdf"
    generator.generate_sheet(TagFamily.APRILTAG, (7,), out_path=april)
    assert _page_texts(april) == ["AprilTag 36h11 ID: 7"]

    lowered = _pdf_text(aruco).lower()
    for word in ("upper", "lower", "left", "right", "northwest", "southeast"):
        assert word not in lowered


def test_sheet_embeds_one_tag_image_per_page(generator: TagGenerator, tmp_path: Path) -> None:
    out = tmp_path / "t.pdf"
    generator.generate_sheet(TagFamily.APRILTAG, (0, 1), out_path=out)
    assert len(re.findall(r"/Subtype\s*/Image", _pdf_text(out))) == 2


def test_sheet_rejects_out_of_range_id_naming_range_and_writes_nothing(
    generator: TagGenerator, tmp_path: Path
) -> None:
    out = tmp_path / "tags.pdf"
    with pytest.raises(InvalidTagId) as excinfo:
        generator.generate_sheet(TagFamily.ARUCO, (1, 2, 5000), out_path=out)
    assert "5000" in str(excinfo.value)
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []


def test_sheet_unwritable_path_fails_before_rendering(
    generator: TagGenerator, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cv2 as cv

    def boom(*a, **k):
        raise AssertionError("rendered before the writability check")

    monkeypatch.setattr(cv.aruco, "generateImageMarker", boom)

    with pytest.raises(UnwritableOutputPath):
        generator.generate_sheet(TagFamily.ARUCO, (1,), out_path=tmp_path)  # a directory
    with pytest.raises(UnwritableOutputPath):
        generator.generate_sheet(
            TagFamily.ARUCO, (1,), out_path=Path("/proc/no-such-dir/tags.pdf")
        )


def test_sheet_creates_missing_parent_directory(generator: TagGenerator, tmp_path: Path) -> None:
    out = tmp_path / "new" / "dir" / "tags.pdf"
    generator.generate_sheet(TagFamily.ARUCO, (1,), out_path=out)
    assert out.is_file()
