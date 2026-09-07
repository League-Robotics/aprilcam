"""Tests for ``aprilcam.camera.backends.opencv_backend``'s identity-safe
reopen (sprint 039 ticket 003) -- see ``camera-DESIGN.md``'s "Identity-safe
reopen" section and this ticket's own file for the full contract.

Headless: ``cv2.VideoCapture`` itself is monkeypatched with a small fake
(``_FakeCV2Capture``) so these tests exercise ``OpenCVBackend.open()``'s new
``index_resolver``/``expected_hardware_key`` logic without any real camera
hardware. ``@pytest.mark.camera`` tests elsewhere in this suite continue to
exercise the class against this Mac's real fixture cameras.
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np
import pytest

import aprilcam.camera.backends.opencv_backend as ocv
from aprilcam.camera.backends import CameraNotEnumeratedError
from aprilcam.camera.backends.opencv_backend import OpenCVBackend
from aprilcam.camera.camera import FAILURE_THRESHOLD, Camera
from aprilcam.camera.interfaces import CameraIdentity


class _FakeCV2Capture:
    """Fakes ``cv2.VideoCapture`` well enough to exercise
    ``OpenCVBackend.open()``: opens successfully only for an index
    currently in ``open_indices`` (a class attribute the test mutates to
    simulate enumeration changing between an initial open and a
    reopen), and every constructed instance is recorded in
    ``instances`` so a test can tell exactly which index was opened and
    whether a given instance was released.
    """

    open_indices: set = set()
    instances: List["_FakeCV2Capture"] = []

    def __init__(self, index: int, backend: int) -> None:
        self.index = index
        self.backend = backend
        self._opened = index in type(self).open_indices
        self._props: dict = {}
        self.released = False
        #: Per-instance override -- a test can flip this on an
        #: already-open instance to simulate that specific device going
        #: bad (stalls/disconnects) without touching every other
        #: instance's behaviour via the class-level ``open_indices``.
        self.reads_ok = True
        type(self).instances.append(self)

    def isOpened(self) -> bool:
        return self._opened

    def release(self) -> None:
        self.released = True
        self._opened = False

    def set(self, prop, value) -> bool:
        self._props[prop] = value
        return True

    def get(self, prop):
        return self._props.get(prop, 0)

    def read(self):
        if not self._opened or not self.reads_ok:
            return False, None
        return True, np.zeros((2, 2, 3), dtype=np.uint8)


@pytest.fixture(autouse=True)
def _fake_cv2_capture(monkeypatch):
    """Every test in this file gets a clean ``_FakeCV2Capture`` in place
    of ``cv2.VideoCapture`` -- reset class state per test so instance
    lists/open-index sets from one test never leak into the next."""
    _FakeCV2Capture.open_indices = set()
    _FakeCV2Capture.instances = []
    monkeypatch.setattr(ocv.cv, "VideoCapture", _FakeCV2Capture)
    yield


def _identity(hardware_key: str) -> CameraIdentity:
    return CameraIdentity(
        slug="camera",
        device_name="Camera",
        hardware_key=hardware_key,
        source="avfoundation_unique_id",
        is_fallback=False,
    )


# ---------------------------------------------------------------------------
# AC1: index_resolver is called fresh on every open(), not just the first.
# ---------------------------------------------------------------------------


def test_open_calls_index_resolver_fresh_every_time():
    _FakeCV2Capture.open_indices = {0, 5}
    calls: List[int] = []
    resolved = iter([0, 5])

    def resolver() -> Optional[int]:
        idx = next(resolved)
        calls.append(idx)
        return idx

    backend = OpenCVBackend(0, index_resolver=resolver)
    backend.open()
    assert backend.index == 0
    backend.close()

    backend.open()
    assert backend.index == 5  # the *new* index, not the one from first open
    assert calls == [0, 5]


def test_index_resolver_absent_preserves_todays_fixed_index_behaviour():
    """No index_resolver (every existing direct OpenCVBackend(index)
    construction, AC5) -- index never changes across close()/open()."""
    _FakeCV2Capture.open_indices = {3}
    backend = OpenCVBackend(3)
    backend.open()
    assert backend.index == 3
    backend.close()
    backend.open()
    assert backend.index == 3


# ---------------------------------------------------------------------------
# AC2 (core regression): the exact source incident -- a camera's hardware
# key shifts to a new index between an initial open and a triggered reopen;
# the reopen must open the *new* index, never blindly reuse the old one
# (which may now be a completely different physical camera).
# ---------------------------------------------------------------------------


def test_self_heal_reopen_through_camera_uses_freshly_resolved_index_not_stale_one():
    _FakeCV2Capture.open_indices = {0}
    current_index = {"value": 0}

    backend = OpenCVBackend(0, index_resolver=lambda: current_index["value"])
    cam = Camera("cam-a", backend)
    cam.open()

    first_cap = _FakeCV2Capture.instances[-1]
    assert first_cap.index == 0

    # The incident: this camera's hardware key leaves index 0 (a sibling
    # camera's USB churn shifts enumeration) and re-enumerates at index 2.
    # A *different* camera may now sit at index 0 -- the fix must never
    # try to open that.
    first_cap.reads_ok = False
    _FakeCV2Capture.open_indices = {2}
    current_index["value"] = 2

    for _ in range(FAILURE_THRESHOLD):
        assert cam.read() is None  # drives _attempt_self_heal on the 5th failure

    second_cap = _FakeCV2Capture.instances[-1]
    assert second_cap is not first_cap
    assert second_cap.index == 2  # the fresh index -- never the stale 0
    assert backend.index == 2
    assert cam.state.present is True
    assert cam.state.usable is True
    assert cam.read() is not None  # the new device actually serves frames


def test_reopen_reports_not_enumerated_rather_than_a_retried_stale_index_when_camera_vanishes():
    """The other half of the core regression: the hardware key does not
    reappear anywhere (fully unplugged, not merely shifted). The reopen
    must report present=False/usable=False, never retry the stale index
    that a different camera might now occupy."""
    _FakeCV2Capture.open_indices = {0}
    enumerated = {"value": True}

    backend = OpenCVBackend(0, index_resolver=lambda: 0 if enumerated["value"] else None)
    cam = Camera("cam-a", backend)
    cam.open()
    first_cap = _FakeCV2Capture.instances[-1]

    first_cap.reads_ok = False
    enumerated["value"] = False  # hardware key no longer enumerated anywhere
    _FakeCV2Capture.open_indices = set()  # nothing to (mis)open at index 0 either

    for _ in range(FAILURE_THRESHOLD):
        assert cam.read() is None

    assert cam.state.present is False
    assert cam.state.usable is False
    # Only the one instance ever existed -- no attempt to open anything.
    assert len(_FakeCV2Capture.instances) == 1


# ---------------------------------------------------------------------------
# AC4 (None-resolver): OpenCVBackend.open() itself, no Camera involved.
# ---------------------------------------------------------------------------


def test_open_raises_camera_not_enumerated_when_resolver_returns_none():
    backend = OpenCVBackend(0, index_resolver=lambda: None)
    with pytest.raises(CameraNotEnumeratedError):
        backend.open()
    assert backend.is_open is False
    assert _FakeCV2Capture.instances == []  # never even tried cv2.VideoCapture


# ---------------------------------------------------------------------------
# AC3: post-open AVFoundation identity verification.
# ---------------------------------------------------------------------------


def test_post_open_identity_mismatch_is_refused_and_closes_the_session(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    _FakeCV2Capture.open_indices = {0}
    monkeypatch.setattr(ocv, "resolve_identity", lambda index, *a, **k: _identity("avf:OBSERVED"))

    backend = OpenCVBackend(0, expected_hardware_key="avf:EXPECTED")
    with pytest.raises(RuntimeError):
        backend.open()

    assert backend.is_open is False
    cap = _FakeCV2Capture.instances[-1]
    assert cap.released is True  # the just-opened session was closed, not left dangling


def test_post_open_identity_match_completes_the_open(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    _FakeCV2Capture.open_indices = {0}
    monkeypatch.setattr(ocv, "resolve_identity", lambda index, *a, **k: _identity("avf:EXPECTED"))

    backend = OpenCVBackend(0, expected_hardware_key="avf:EXPECTED")
    backend.open()

    assert backend.is_open is True


def test_identity_check_skipped_off_darwin(monkeypatch):
    """Every other platform: no reliable per-index identity read after
    open exists, so the check is skipped entirely -- a mismatched fake
    identity here must not raise."""
    monkeypatch.setattr("sys.platform", "linux")
    _FakeCV2Capture.open_indices = {0}
    monkeypatch.setattr(ocv, "resolve_identity", lambda index, *a, **k: _identity("avf:OBSERVED"))

    backend = OpenCVBackend(0, expected_hardware_key="avf:EXPECTED")
    backend.open()  # does not raise

    assert backend.is_open is True


def test_identity_check_skipped_for_a_non_avf_expected_hardware_key(monkeypatch):
    """A fallback-tier hardware_key (vidpid/loc/name) is inherently
    sensitive to a USB re-plug moving *this* camera's own location --
    re-resolving it post-open is not a reliable mismatch signal, so the
    check degrades to "not verified" rather than raising."""
    monkeypatch.setattr("sys.platform", "darwin")
    _FakeCV2Capture.open_indices = {0}
    monkeypatch.setattr(ocv, "resolve_identity", lambda index, *a, **k: _identity("avf:OBSERVED"))

    backend = OpenCVBackend(0, expected_hardware_key="vidpid:1234:5678@loc-1")
    backend.open()  # does not raise -- the vidpid tier is never checked post-open

    assert backend.is_open is True


def test_identity_check_skipped_when_no_expected_hardware_key_given():
    """AC5/AC6: every existing direct OpenCVBackend(index) construction
    (no expected_hardware_key at all) is completely unaffected."""
    monkeypatch_free_backend = OpenCVBackend(0)
    _FakeCV2Capture.open_indices = {0}
    monkeypatch_free_backend.open()
    assert monkeypatch_free_backend.is_open is True
