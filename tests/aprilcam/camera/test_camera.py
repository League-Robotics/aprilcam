"""Tests for ``aprilcam.camera.camera`` (ticket 020-003).

Headless tests use :class:`FakeBackend`, a fully-controllable
``CaptureBackend`` test double — no hardware, no ``cv2`` behaviour
depended on. ``@pytest.mark.camera`` tests exercise the real
``OpenCVBackend`` against this Mac's two fixture cameras
(``arducam-ov9782-usb-camera``, ``hd-usb-camera``), located by name via
``cv2_enumerate_cameras`` at test-collection time and skipped cleanly
if either is not attached.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import pytest

from aprilcam.camera.backends.opencv_backend import property_map
from aprilcam.camera.camera import FAILURE_THRESHOLD, MAX_REOPENS, Camera
from aprilcam.camera.interfaces import Camera as CameraProtocol
from aprilcam.config.interfaces import CameraConfig
from aprilcam.errors import UnsupportedSetting


# ---------------------------------------------------------------------------
# Fake backend
# ---------------------------------------------------------------------------


class FakeBackend:
    """A ``CaptureBackend`` test double with fully controllable behaviour.

    ``read_queue`` items are popped one per ``read()`` call; once
    exhausted, ``read()`` returns ``None`` (a failure) as long as the
    backend is open. ``fail_open`` makes ``open()`` raise instead of
    succeeding — for AC "present but unopenable" style scenarios.
    """

    def __init__(
        self,
        *,
        read_queue: Optional[List[Optional[np.ndarray]]] = None,
        fail_open: bool = False,
        size: Tuple[int, int] = (640, 480),
    ) -> None:
        self._is_open = False
        self._queue: List[Optional[np.ndarray]] = list(read_queue) if read_queue else []
        self.fail_open = fail_open
        self._size = size
        self.open_calls = 0
        self.close_calls = 0
        self.read_calls = 0
        self._properties: Dict[str, float] = {}

    @property
    def is_open(self) -> bool:
        return self._is_open

    def open(self) -> None:
        self.open_calls += 1
        if self.fail_open:
            raise RuntimeError("simulated open failure")
        self._is_open = True

    def read(self) -> Optional[np.ndarray]:
        self.read_calls += 1
        if not self._is_open:
            return None
        if self._queue:
            return self._queue.pop(0)
        return None

    def close(self) -> None:
        self.close_calls += 1
        self._is_open = False

    def frame_size(self) -> Optional[Tuple[int, int]]:
        return self._size if self._is_open else None

    # -- best-effort settings surface (mirrors OpenCVBackend) -----------

    def get_property(self, name: str) -> Optional[float]:
        return self._properties.get(name)

    def set_property(self, name: str, value: float) -> bool:
        self._properties[name] = value
        return True

    def settable_names(self):
        # Mirrors OpenCVBackend's real, generic key set -- so
        # "unknown key" tests stay meaningful against a realistic
        # boundary rather than an artificially permissive fake.
        return set(property_map())


def _frame() -> np.ndarray:
    return np.zeros((4, 4, 3), dtype=np.uint8)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_camera_protocol():
    cam = Camera("cam-a", FakeBackend())
    assert isinstance(cam, CameraProtocol)


# ---------------------------------------------------------------------------
# open / close / state
# ---------------------------------------------------------------------------


def test_open_marks_present_and_usable():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    assert backend.open_calls == 1
    assert cam.state.present is True
    assert cam.state.usable is True


def test_open_propagates_backend_exception():
    backend = FakeBackend(fail_open=True)
    cam = Camera("cam-a", backend)
    with pytest.raises(RuntimeError):
        cam.open()
    assert cam.state.present is False
    assert cam.state.usable is False


def test_close_resets_present_and_usable():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    cam.close()
    assert backend.close_calls == 1
    assert cam.state.present is False
    assert cam.state.usable is False


def test_state_carries_number_and_slug():
    cam = Camera("cam-a", FakeBackend(), number=3)
    assert cam.slug == "cam-a"
    assert cam.state.name == "cam-a"
    assert cam.state.number == 3
    assert cam.state.playfield is None
    assert cam.state.calibrated is False


# ---------------------------------------------------------------------------
# read() — never raises, returns None on failure (AC2)
# ---------------------------------------------------------------------------


def test_read_returns_frame_on_success():
    frame = _frame()
    backend = FakeBackend(read_queue=[frame])
    cam = Camera("cam-a", backend)
    cam.open()
    assert cam.read() is frame


def test_read_returns_none_on_failure_without_raising():
    backend = FakeBackend(read_queue=[])
    cam = Camera("cam-a", backend)
    cam.open()
    assert cam.read() is None


def test_read_before_open_returns_none():
    cam = Camera("cam-a", FakeBackend())
    assert cam.read() is None


# ---------------------------------------------------------------------------
# Self-heal reopen (AC2): 5 consecutive failures -> reopen, capped at 10
# ---------------------------------------------------------------------------


def test_reopen_triggers_after_five_consecutive_failures():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    assert backend.open_calls == 1

    for _ in range(FAILURE_THRESHOLD - 1):
        assert cam.read() is None
    assert backend.open_calls == 1  # not yet -- one short of the threshold

    assert cam.read() is None  # 5th consecutive failure
    assert backend.open_calls == 2  # self-heal reopened once
    assert backend.close_calls == 1


def test_successful_read_resets_failure_and_reopen_counters():
    backend = FakeBackend(read_queue=[None, None, None, None, _frame()])
    cam = Camera("cam-a", backend)
    cam.open()
    for _ in range(4):
        cam.read()
    assert cam.read() is not None  # the 5th read succeeds, before threshold trips
    assert backend.open_calls == 1  # no reopen -- the healthy frame arrived first

    # A fresh run of failures now needs its own 5 in a row to trigger.
    for _ in range(FAILURE_THRESHOLD - 1):
        cam.read()
    assert backend.open_calls == 1
    cam.read()
    assert backend.open_calls == 2


def test_reopen_budget_capped_at_ten_then_gives_up():
    backend = FakeBackend()  # open() always trivially succeeds; never yields frames
    cam = Camera("cam-a", backend)
    cam.open()

    # Drive FAILURE_THRESHOLD * MAX_REOPENS failed reads: exactly enough
    # to trigger MAX_REOPENS reopen attempts (all "successful" opens
    # that still produce no frames).
    for _ in range(FAILURE_THRESHOLD * MAX_REOPENS):
        cam.read()
    assert backend.open_calls == 1 + MAX_REOPENS
    assert cam.state.usable is True  # budget not spent yet on this exact boundary

    # One more full failure run trips the exhausted path: no further
    # reopen attempt, and usable now reflects "gave up" even though
    # the fake backend's open() would have happily succeeded again.
    for _ in range(FAILURE_THRESHOLD):
        cam.read()
    assert backend.open_calls == 1 + MAX_REOPENS  # unchanged -- budget spent
    assert cam.state.present is True  # "plugged in and broken", not "gone"
    assert cam.state.usable is False


def test_reopen_failure_leaves_camera_unusable_but_present():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()

    backend.fail_open = True  # the reopen attempt itself will now fail
    for _ in range(FAILURE_THRESHOLD):
        cam.read()
    assert backend.close_calls == 1
    assert backend.open_calls == 2  # the failed reopen attempt
    assert cam.state.present is True
    assert cam.state.usable is False  # backend.is_open is False after the failed reopen


# ---------------------------------------------------------------------------
# effective_config() — introspected facts (AC3)
# ---------------------------------------------------------------------------


def test_effective_config_reports_device_name_and_frame_dims_when_open():
    backend = FakeBackend(size=(1920, 1080))
    cam = Camera("cam-a", backend, device_name="Arducam OV9782 USB Camera")
    cam.open()
    cfg = cam.effective_config()
    assert cfg.name == "cam-a"
    assert cfg.settings["device_name"] == "Arducam OV9782 USB Camera"
    assert cfg.settings["frame_width"] == 1920
    assert cfg.settings["frame_height"] == 1080


def test_effective_config_reports_none_dims_when_not_open():
    cam = Camera("cam-a", FakeBackend(), device_name="Cam A")
    cfg = cam.effective_config()
    assert cfg.settings["frame_width"] is None
    assert cfg.settings["frame_height"] is None


def test_camera_config_round_trips_to_dict():
    cfg = CameraConfig(name="cam-a", playfield=None, settings={"k": 1})
    d = cfg.to_dict()
    assert d == {"name": "cam-a", "playfield": None, "settings": {"k": 1}}


# ---------------------------------------------------------------------------
# apply_config() — hard errors, ordering, atomic rollback
# ---------------------------------------------------------------------------


def test_apply_config_requires_open_camera():
    cam = Camera("cam-a", FakeBackend())
    cfg = CameraConfig(name="cam-a", playfield=None, settings={})
    with pytest.raises(RuntimeError):
        cam.apply_config(cfg)


def test_apply_config_unknown_key_raises_unsupported_setting():
    cam = Camera("cam-a", FakeBackend())
    cam.open()
    cfg = CameraConfig(name="cam-a", playfield=None, settings={"not_a_real_key": 1})
    with pytest.raises(UnsupportedSetting) as exc_info:
        cam.apply_config(cfg)
    assert exc_info.value.key == "not_a_real_key"


def test_apply_config_applies_gate_keys_before_dependents():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    order: List[str] = []
    original_set = backend.set_property

    def spy_set(name, value):
        order.append(name)
        return original_set(name, value)

    backend.set_property = spy_set  # type: ignore[method-assign]
    cfg = CameraConfig(
        name="cam-a",
        playfield=None,
        settings={"exposure": 100, "auto_exposure": 1},
    )
    cam.apply_config(cfg)
    assert order == ["auto_exposure", "exposure"]


def test_apply_config_blank_settings_are_skipped():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    cfg = CameraConfig(name="cam-a", playfield=None, settings={"gain": None})
    cam.apply_config(cfg)  # no error, nothing pushed
    assert backend.get_property("gain") is None


def test_apply_config_rolls_back_on_mid_push_failure():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    backend.set_property("gain", 5.0)  # pre-existing value to roll back to

    def failing_set(name, value):
        if name == "exposure":
            return False
        backend._properties[name] = value
        return True

    backend.set_property = failing_set  # type: ignore[method-assign]
    cfg = CameraConfig(
        name="cam-a", playfield=None, settings={"gain": 9.0, "exposure": 100}
    )
    with pytest.raises(UnsupportedSetting):
        cam.apply_config(cfg)
    assert backend.get_property("gain") == 5.0  # rolled back


def test_apply_config_backend_without_settings_surface_rejects_any_push():
    class NoSettingsBackend:
        """A minimal CaptureBackend with no get_property/set_property at
        all -- mirrors V4l2LoopbackBackend, whose stream format is fixed
        by its writer (v1's comment on V4l2CtlCapture.set())."""

        def __init__(self) -> None:
            self._is_open = False

        @property
        def is_open(self) -> bool:
            return self._is_open

        def open(self) -> None:
            self._is_open = True

        def read(self):
            return None

        def close(self) -> None:
            self._is_open = False

        def frame_size(self):
            return None

    backend = NoSettingsBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    cfg = CameraConfig(name="cam-a", playfield=None, settings={"gain": 1})
    with pytest.raises(UnsupportedSetting):
        cam.apply_config(cfg)
    # An empty push against such a backend is fine -- nothing to reject.
    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={}))


def test_reset_is_a_documented_noop():
    cam = Camera("cam-a", FakeBackend())
    cam.open()
    assert cam.reset() is None


# ---------------------------------------------------------------------------
# @pytest.mark.camera — real hardware (AC1, AC3)
# ---------------------------------------------------------------------------


def _real_camera_index(name_substring: str) -> Optional[int]:
    """OpenCV index for the first enumerated camera whose name contains
    *name_substring* (case-insensitive), or ``None`` if not attached."""
    try:
        import cv2 as cv
        from cv2_enumerate_cameras import enumerate_cameras
    except ImportError:
        return None
    avf_offset = getattr(cv, "CAP_AVFOUNDATION", 1200)
    for cam in enumerate_cameras():
        name = getattr(cam, "name", "") or ""
        if name_substring.lower() in name.lower():
            raw = cam.index
            return raw - avf_offset if raw >= avf_offset else raw
    return None


@pytest.mark.camera
@pytest.mark.parametrize(
    "name_substring,expected_slug",
    [
        ("Arducam OV9782", "arducam-ov9782-usb-camera"),
        ("HD USB", "hd-usb-camera"),
    ],
)
def test_real_camera_open_read_close(name_substring, expected_slug):
    from aprilcam.camera.backends.opencv_backend import OpenCVBackend

    index = _real_camera_index(name_substring)
    if index is None:
        pytest.skip(f"no attached camera matching {name_substring!r}")

    cam = Camera(expected_slug, OpenCVBackend(index), device_name=name_substring)
    cam.open()
    try:
        assert cam.state.present is True
        assert cam.state.usable is True

        frame = None
        for _ in range(10):  # a real device may need a few reads to warm up
            frame = cam.read()
            if frame is not None:
                break
        assert frame is not None
        assert isinstance(frame, np.ndarray)
        assert frame.ndim == 3

        cfg = cam.effective_config()
        assert cfg.settings["device_name"] == name_substring
        assert cfg.settings["frame_width"] > 0
        assert cfg.settings["frame_height"] > 0
    finally:
        cam.close()
    assert cam.state.usable is False
