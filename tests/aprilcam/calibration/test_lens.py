"""Tests for ``aprilcam.calibration.lens`` (sprint 044 ticket 001)."""

from __future__ import annotations

import json
import os
import sys

import pytest

from aprilcam.calibration.lens import INTRINSICS_FILENAME, LensModel, LensStore

_MATRIX = ((400.0, 0.0, 450.0), (0.0, 400.0, 350.0), (0.0, 0.0, 1.0))
_DIST = (-0.3, 0.1, 0.0, 0.0, 0.0)


def _model(size=(900, 700), **kw) -> LensModel:
    defaults = dict(
        camera_matrix=_MATRIX,
        dist_coeffs=_DIST,
        image_size=size,
        board_spec={"squares": [6, 8]},
        views=12,
        rms_px=0.4,
        residual_px=0.5,
        correction_px=3.2,
        installed_at=1000.0,
    )
    defaults.update(kw)
    return LensModel(**defaults)


def test_lens_module_does_not_import_cv2():
    import aprilcam.calibration.lens as lens

    src = open(lens.__file__, encoding="utf-8").read()
    assert "import cv2" not in src


def test_round_trip(tmp_path):
    store = LensStore(tmp_path)
    store.put("cam", _model())
    assert store.path("cam") == tmp_path / "cameras" / "cam" / INTRINSICS_FILENAME
    loaded, reason = store.get("cam", (900, 700))
    assert reason is None
    assert loaded == _model()


def test_missing_file_is_absent_without_reason(tmp_path):
    assert LensStore(tmp_path).get("cam", (900, 700)) == (None, None)


def test_size_mismatch_returns_none_with_reason(tmp_path):
    store = LensStore(tmp_path)
    store.put("cam", _model())
    model, reason = store.get("cam", (1280, 720))
    assert model is None
    assert "900x700" in reason and "1280x720" in reason
    assert store.get_lens_model("cam", (1280, 720)) is None


def test_corrupt_file_is_tolerated_with_warning(tmp_path, caplog):
    store = LensStore(tmp_path)
    path = store.path("cam")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert store.get("cam", (900, 700)) == (None, None)
    assert "corrupt" in caplog.text

    path.write_text(json.dumps({"camera_matrix": [[1]]}), encoding="utf-8")
    assert store.load("cam") is None


def test_put_is_atomic_failed_replace_keeps_previous_file(tmp_path, monkeypatch):
    store = LensStore(tmp_path)
    store.put("cam", _model(views=1))

    def boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.put("cam", _model(views=99))
    monkeypatch.undo()

    assert store.load("cam").views == 1
    assert not list(store.path("cam").parent.glob("*.tmp"))


def test_put_stamps_installed_at_when_unset(tmp_path):
    store = LensStore(tmp_path)
    store.put("cam", _model(installed_at=0.0))
    assert store.load("cam").installed_at > 0
