"""Tests for ``aprilcam.mcp.images`` (ticket 026-001).

Headless, no daemon or camera anywhere: every test hands
``image_response`` already-encoded bytes directly (standing in for
``Daemon.get_frame``'s output) and checks the shaped result, never
decoding or re-interpreting the bytes as an actual image -- this module
does not either.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import pytest
from mcp.types import ImageContent, TextContent

from aprilcam.mcp.images import create_temp_dir, image_response

# ---------------------------------------------------------------------------
# create_temp_dir -- one grouped subdirectory, not the bare system root
# ---------------------------------------------------------------------------


def test_create_temp_dir_creates_a_directory_with_the_expected_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    path = create_temp_dir()

    assert path.is_dir()
    assert path.name.startswith("aprilcam-mcp-")
    assert path.parent == tmp_path


def test_create_temp_dir_returns_a_fresh_directory_each_call(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    first = create_temp_dir()
    second = create_temp_dir()

    assert first != second
    assert first.is_dir() and second.is_dir()


# ---------------------------------------------------------------------------
# format="base64" -- inline content, nothing written to disk
# ---------------------------------------------------------------------------


def test_base64_format_returns_inline_image_content_with_bytes_unchanged(tmp_path):
    data = b"\xff\xd8\xff not really a jpeg but bytes are bytes"

    result = image_response(data, encoding="jpeg", format="base64", temp_dir=tmp_path)

    assert isinstance(result, ImageContent)
    assert base64.b64decode(result.data) == data
    assert list(tmp_path.iterdir()) == []


def test_base64_format_maps_jpeg_encoding_to_the_jpeg_mime_type(tmp_path):
    result = image_response(b"data", encoding="jpeg", format="base64", temp_dir=tmp_path)
    assert result.mimeType == "image/jpeg"


def test_base64_format_maps_png_encoding_to_the_png_mime_type(tmp_path):
    result = image_response(b"data", encoding="png", format="base64", temp_dir=tmp_path)
    assert result.mimeType == "image/png"


def test_base64_format_falls_back_to_a_generic_mime_type_for_an_unlisted_encoding(tmp_path):
    result = image_response(b"data", encoding="bmp", format="base64", temp_dir=tmp_path)
    assert result.mimeType == "image/bmp"


# ---------------------------------------------------------------------------
# format="file" -- only the path returned, bytes written unchanged
# ---------------------------------------------------------------------------


def test_file_format_writes_bytes_unchanged_under_the_given_temp_dir(tmp_path):
    data = b"raw-jpeg-bytes-unchanged"

    result = image_response(data, encoding="jpeg", format="file", temp_dir=tmp_path)

    assert isinstance(result, TextContent)
    written = Path(result.text)
    assert written.parent == tmp_path
    assert written.read_bytes() == data


def test_file_format_never_returns_inline_image_content(tmp_path):
    result = image_response(b"data", encoding="jpeg", format="file", temp_dir=tmp_path)

    assert not isinstance(result, ImageContent)
    assert isinstance(result, TextContent)


def test_file_format_two_calls_do_not_collide(tmp_path):
    first = image_response(b"one", encoding="jpeg", format="file", temp_dir=tmp_path)
    second = image_response(b"two", encoding="jpeg", format="file", temp_dir=tmp_path)

    assert first.text != second.text
    assert Path(first.text).read_bytes() == b"one"
    assert Path(second.text).read_bytes() == b"two"
    assert len(list(tmp_path.iterdir())) == 2


# ---------------------------------------------------------------------------
# argument validation
# ---------------------------------------------------------------------------


def test_unknown_format_raises_value_error_naming_the_valid_choices(tmp_path):
    with pytest.raises(ValueError, match='"base64" or "file"'):
        image_response(b"data", encoding="jpeg", format="tiff", temp_dir=tmp_path)
