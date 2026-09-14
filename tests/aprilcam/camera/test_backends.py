"""Tests for the pure, hardware-independent logic in ``camera/backends/``
(ticket 020-003) -- not named in the ticket's Testing section directly,
but promised by ``v4l2_loopback.py``'s and ``libcamera.py``'s own module
docstrings ("unit-tested headlessly against a faked subprocess" /
"headlessly testable"). Real device I/O for both is exercised, where it
can be at all, only through ``OpenCVBackend`` on real hardware
(``test_camera.py``'s ``@pytest.mark.camera`` tests) -- neither
``v4l2-ctl`` nor a ``/sys/class/video4linux`` tree exist on this Mac.

**Ticket 040-003 real-hardware validation.** Every function in this file
was additionally exercised live against ``vidar.local`` (two imx296 CSI
cameras behind v4l2loopback ``/dev/video70``/``/dev/video71``, card
labels ``aprilcam-imx296-88000``/``aprilcam-imx296-80000``) -- see that
ticket's file for the full session record. The real hardware confirmed
every function in this module already behaved correctly: no source
change was needed in ``libcamera.py``, ``v4l2_loopback.py``, or
``__init__.py``. The tests below marked "040-003" freeze that
now-*proven* (not merely assumed) behaviour with literal captured
evidence (the real ``v4l2-ctl --get-fmt-video`` output, the real
"format not negotiated" error text, the real zero-byte "unsupported
stream type" stream failure) so a future change cannot silently regress
a real-hardware-verified path back to only-ever-tested-against-a-guess.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np
import pytest

from aprilcam.camera.backends import (
    CAPTURE_BACKEND_ENV,
    default_capture_backend_factory,
    libcamera,
    resolve_capture_backend,
    v4l2_loopback,
)
from aprilcam.camera.backends.libcamera import LibcameraDevice
from aprilcam.camera.backends.opencv_backend import OpenCVBackend, default_backend_priority, property_map
from aprilcam.camera.backends.v4l2_loopback import V4l2LoopbackBackend
from aprilcam.camera.backends.worker_backend import WorkerBackend


# ---------------------------------------------------------------------------
# opencv_backend -- trivial, platform-independent shape checks
# ---------------------------------------------------------------------------


def test_default_backend_priority_ends_in_cap_any():
    priority = default_backend_priority()
    assert priority[-1] == 0 or priority[-1] == pytest.importorskip("cv2").CAP_ANY


def test_property_map_has_expected_controls():
    m = property_map()
    assert {"exposure", "auto_exposure", "gain", "white_balance_temperature"} <= set(m)


# ---------------------------------------------------------------------------
# v4l2_loopback -- format parsing (faked subprocess.run)
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def test_query_format_parses_width_height_and_pixfmt(monkeypatch):
    sample = (
        "Format Video Capture:\n"
        "\tWidth/Height      : 1280/720\n"
        "\tPixel Format      : 'YUYV' (YUYV 4:2:2)\n"
    )
    monkeypatch.setattr(
        v4l2_loopback.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(sample)
    )
    width, height, pixfmt = v4l2_loopback._query_format("/dev/video70")
    assert (width, height, pixfmt) == (1280, 720, "YUYV")


def test_query_format_degrades_to_defaults_on_subprocess_failure(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("v4l2-ctl not found")

    monkeypatch.setattr(v4l2_loopback.subprocess, "run", boom)
    width, height, pixfmt = v4l2_loopback._query_format("/dev/video70")
    assert (width, height, pixfmt) == (
        v4l2_loopback._DEFAULT_WIDTH,
        v4l2_loopback._DEFAULT_HEIGHT,
        v4l2_loopback._DEFAULT_PIXFMT,
    )


# ---------------------------------------------------------------------------
# v4l2_loopback -- open/read/close against a faked subprocess.Popen
# ---------------------------------------------------------------------------


class _FakeStdout:
    """A readable stream that yields *frames* worth of bytes, then EOF."""

    def __init__(self, chunks: List[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, n: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        return chunk[:n]


class _FakePopen:
    def __init__(self, stdout_chunks: List[bytes]) -> None:
        self.stdout = _FakeStdout(stdout_chunks)
        self.terminated = False
        self.killed = False
        self._alive = True

    def poll(self) -> Optional[int]:
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:  # pragma: no cover - only on terminate() failure
        self.killed = True
        self._alive = False


def _patch_query_format(monkeypatch, width=2, height=2, pixfmt="YUYV"):
    monkeypatch.setattr(v4l2_loopback, "_query_format", lambda device: (width, height, pixfmt))


def test_open_read_close_roundtrip_with_faked_subprocess(monkeypatch):
    _patch_query_format(monkeypatch, width=2, height=2, pixfmt="YUYV")
    frame_bytes = 2 * 2 * 2  # width*height*2 (YUYV)
    fake_frame = bytes(range(frame_bytes))
    monkeypatch.setattr(
        v4l2_loopback.subprocess, "Popen", lambda *a, **k: _FakePopen([fake_frame])
    )

    backend = v4l2_loopback.V4l2LoopbackBackend("/dev/video70")
    backend.open()
    assert backend.is_open is True
    assert backend.frame_size() == (2, 2)

    frame = backend.read()
    assert isinstance(frame, np.ndarray)
    assert frame.shape == (2, 2, 3)
    assert frame.dtype == np.uint8

    backend.close()
    assert backend.is_open is False


def test_read_returns_none_on_short_stream_eof(monkeypatch):
    _patch_query_format(monkeypatch, width=4, height=4, pixfmt="YUYV")
    # Fewer bytes than one full frame -> _read_exact hits EOF -> None.
    monkeypatch.setattr(
        v4l2_loopback.subprocess, "Popen", lambda *a, **k: _FakePopen([b"\x00" * 4])
    )
    backend = v4l2_loopback.V4l2LoopbackBackend("/dev/video70")
    backend.open()
    assert backend.read() is None


def test_open_raises_on_unsupported_pixel_format(monkeypatch):
    _patch_query_format(monkeypatch, pixfmt="MJPG")
    backend = v4l2_loopback.V4l2LoopbackBackend("/dev/video70")
    with pytest.raises(RuntimeError):
        backend.open()
    assert backend.is_open is False


def test_close_terminates_the_subprocess(monkeypatch):
    _patch_query_format(monkeypatch)
    fake_proc = _FakePopen([b"\x00" * 8])
    monkeypatch.setattr(v4l2_loopback.subprocess, "Popen", lambda *a, **k: fake_proc)
    backend = v4l2_loopback.V4l2LoopbackBackend("/dev/video70")
    backend.open()
    backend.close()
    assert fake_proc.terminated is True


# ---------------------------------------------------------------------------
# libcamera -- /sys enumeration (injectable glob root, no real /sys needed)
# ---------------------------------------------------------------------------


def _write_sys_video_name(root, index: int, name: str) -> None:
    d = root / f"video{index}" / "name"
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text(name)


def test_list_loopback_cameras_reads_labelled_devices(tmp_path, monkeypatch):
    monkeypatch.delenv("APRILCAM_LIBCAM_LOOPBACK_LABEL", raising=False)
    _write_sys_video_name(tmp_path, 70, "aprilcam-imx296-88000")
    _write_sys_video_name(tmp_path, 71, "aprilcam-imx477-abcd")
    _write_sys_video_name(tmp_path, 72, "uvcvideo-something")  # not our prefix

    devices = libcamera.list_loopback_cameras(sys_glob=str(tmp_path / "video*" / "name"))

    assert [d.device_index for d in devices] == [70, 71]
    assert [d.position for d in devices] == [0, 1]
    assert devices[0].slug == "imx296-88000"
    assert devices[0].model == "imx296"
    assert devices[0].camera_id == "aprilcam-imx296-88000"
    assert devices[1].slug == "imx477-abcd"


def test_list_loopback_cameras_empty_when_glob_matches_nothing(tmp_path):
    devices = libcamera.list_loopback_cameras(sys_glob=str(tmp_path / "video*" / "name"))
    assert devices == []


def test_list_loopback_cameras_honours_custom_label_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("APRILCAM_LIBCAM_LOOPBACK_LABEL", "field-")
    _write_sys_video_name(tmp_path, 5, "field-imx219-1234")
    _write_sys_video_name(tmp_path, 6, "aprilcam-imx296-88000")  # wrong prefix now

    devices = libcamera.list_loopback_cameras(sys_glob=str(tmp_path / "video*" / "name"))
    assert len(devices) == 1
    assert devices[0].slug == "imx219-1234"


def test_this_macs_probe_libcamera_stays_empty_via_real_wiring():
    """Confirms identity._probe_libcamera's ticket-003 wiring (real
    enumeration, not the old stub) is still empty on this Mac -- no
    /sys/class/video4linux tree here, so the glob genuinely misses."""
    from aprilcam.camera import identity

    assert identity._probe_libcamera() == {}


# ---------------------------------------------------------------------------
# resolve_capture_backend -- backend selection (ticket 031-006, SUC-006)
# ---------------------------------------------------------------------------


class _FakeOpenCVBackend:
    """A minimal stand-in the ``backend_factory`` override returns, so
    these tests never need real ``cv2`` hardware to prove *which* class
    got constructed."""

    def __init__(self, index: int) -> None:
        self.index = index


def _loopback_device(
    *, position: int = 0, slug: str = "imx296-88000", model: str = "imx296",
    device_index: int = 70, camera_id: str = "aprilcam-imx296-88000",
) -> LibcameraDevice:
    return LibcameraDevice(
        position=position, slug=slug, model=model, device_index=device_index, camera_id=camera_id,
    )


def test_resolve_capture_backend_uses_v4l2loopback_for_libcamera_hardware_key():
    """AC1 (SUC-006): a libcamera-identified camera constructs
    V4l2LoopbackBackend at the real /dev/videoN device index -- not the
    enumeration "position" index that flows through identity_source."""
    device = _loopback_device(position=3, device_index=70, camera_id="aprilcam-imx296-88000")
    calls: List[int] = []

    backend = resolve_capture_backend(
        "libcamera:aprilcam-imx296-88000",
        3,  # the position, deliberately different from device_index=70
        backend_factory=lambda index: calls.append(index) or _FakeOpenCVBackend(index),
        list_loopback_cameras=lambda: [device],
    )

    assert isinstance(backend, V4l2LoopbackBackend)
    assert backend._device == "/dev/video70"
    assert calls == []  # backend_factory (OpenCVBackend stand-in) never invoked


def test_resolve_capture_backend_matches_by_camera_id_not_position():
    """Two loopback devices enumerated; the correct one is picked by
    camera_id, not by list order or position."""
    devices = [
        _loopback_device(position=0, device_index=70, camera_id="aprilcam-imx296-88000"),
        _loopback_device(position=1, device_index=71, camera_id="aprilcam-imx477-abcd"),
    ]

    backend = resolve_capture_backend(
        "libcamera:aprilcam-imx477-abcd",
        1,
        backend_factory=lambda index: _FakeOpenCVBackend(index),
        list_loopback_cameras=lambda: devices,
    )

    assert isinstance(backend, V4l2LoopbackBackend)
    assert backend._device == "/dev/video71"


def test_resolve_capture_backend_falls_through_to_backend_factory_for_other_sources():
    """AC2 (SUC-006): regression -- a non-libcamera hardware_key is an
    unchanged pass-through to backend_factory(index), exactly today's
    OpenCVBackend(index) behaviour."""
    calls: List[int] = []

    backend = resolve_capture_backend(
        "vidpid:1234:5678@usb-1",
        2,
        backend_factory=lambda index: calls.append(index) or _FakeOpenCVBackend(index),
        list_loopback_cameras=lambda: [_loopback_device()],  # present but must be ignored
    )

    assert isinstance(backend, _FakeOpenCVBackend)
    assert backend.index == 2
    assert calls == [2]


def test_resolve_capture_backend_default_list_loopback_cameras_is_the_real_sys_scan():
    """No override given -- falls back to the real, subprocess-free /sys
    scan (empty on this Mac), which then degrades to backend_factory
    exactly like the "no matching device" case below."""
    backend = resolve_capture_backend(
        "libcamera:aprilcam-imx296-88000",
        0,
        backend_factory=lambda index: _FakeOpenCVBackend(index),
    )
    assert isinstance(backend, _FakeOpenCVBackend)


def test_resolve_capture_backend_falls_back_when_no_matching_loopback_device(caplog):
    """A libcamera-prefixed hardware_key with no matching enumerated
    device (stale registry entry / enumeration race) logs a warning and
    degrades to backend_factory rather than raising."""
    caplog.set_level(logging.WARNING)
    backend = resolve_capture_backend(
        "libcamera:aprilcam-gone",
        0,
        backend_factory=lambda index: _FakeOpenCVBackend(index),
        list_loopback_cameras=lambda: [_loopback_device(camera_id="aprilcam-imx296-88000")],
    )

    assert isinstance(backend, _FakeOpenCVBackend)
    assert any("no matching loopback device" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# default_capture_backend_factory -- config/env-gated selection (sprint 038
# ticket 005). Every case is headless: OpenCVBackend/WorkerBackend
# construction alone never touches hardware or spawns a subprocess (that
# happens in .open(), never called here).
# ---------------------------------------------------------------------------


def test_default_capture_backend_factory_env_worker_forces_workerbackend(monkeypatch):
    """APRILCAM_CAPTURE_BACKEND=worker forces WorkerBackend regardless of
    platform -- e.g. exercising the worker path from a non-macOS CI
    runner, or a test on this Mac that wants it explicitly."""
    monkeypatch.setenv(CAPTURE_BACKEND_ENV, "worker")
    monkeypatch.setattr("sys.platform", "linux")

    backend = default_capture_backend_factory(3)

    assert isinstance(backend, WorkerBackend)


def test_default_capture_backend_factory_env_opencv_forces_opencvbackend(monkeypatch):
    """APRILCAM_CAPTURE_BACKEND=opencv forces OpenCVBackend regardless of
    platform -- e.g. an operator opting a wedge-prone Mac back out of the
    worker-process path."""
    monkeypatch.setenv(CAPTURE_BACKEND_ENV, "opencv")
    monkeypatch.setattr("sys.platform", "darwin")

    backend = default_capture_backend_factory(3)

    assert isinstance(backend, OpenCVBackend)


def test_default_capture_backend_factory_env_is_case_insensitive(monkeypatch):
    monkeypatch.setenv(CAPTURE_BACKEND_ENV, "WORKER")
    monkeypatch.setattr("sys.platform", "linux")

    assert isinstance(default_capture_backend_factory(0), WorkerBackend)


def test_default_capture_backend_factory_unset_defaults_to_worker_on_darwin(monkeypatch):
    """Team-lead override of this ticket's original 'always OpenCVBackend'
    default (recorded on the stakeholder_approval gate): with no env
    override, darwin -- where the AVFoundation capture-session wedge
    occurs -- gets WorkerBackend by default."""
    monkeypatch.delenv(CAPTURE_BACKEND_ENV, raising=False)
    monkeypatch.setattr("sys.platform", "darwin")

    backend = default_capture_backend_factory(0)

    assert isinstance(backend, WorkerBackend)


def test_default_capture_backend_factory_unset_defaults_to_opencv_off_darwin(monkeypatch):
    """No env override, non-macOS platform -- OpenCVBackend, unchanged
    from every platform's behaviour before this ticket."""
    monkeypatch.delenv(CAPTURE_BACKEND_ENV, raising=False)
    monkeypatch.setattr("sys.platform", "linux")

    backend = default_capture_backend_factory(0)

    assert isinstance(backend, OpenCVBackend)


def test_default_capture_backend_factory_unrecognised_env_value_falls_back_and_warns(
    monkeypatch, caplog
):
    """An unrecognised APRILCAM_CAPTURE_BACKEND value is logged and
    ignored -- falls through to the platform default -- rather than
    raising and taking a working daemon down over a typo."""
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv(CAPTURE_BACKEND_ENV, "bogus")
    monkeypatch.setattr("sys.platform", "linux")

    backend = default_capture_backend_factory(0)

    assert isinstance(backend, OpenCVBackend)
    assert any("unrecognised" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# 040-003 real-hardware evidence -- literal captures from vidar.local,
# frozen as offline fixtures so the now-verified-correct behaviour cannot
# silently regress. See this module's docstring and ticket 040-003's file
# for the live session that produced these exact byte strings.
# ---------------------------------------------------------------------------


def test_query_format_parses_the_real_vidar_get_fmt_video_output(monkeypatch):
    """The literal ``v4l2-ctl -d /dev/video71 --get-fmt-video`` stdout
    captured live from vidar's working camera (i2c@80000, card label
    ``aprilcam-imx296-80000``) -- richer than the hand-written two-line
    fixture above (real ``Field``/``Bytes per Line``/``Colorspace``/etc.
    lines interleaved), confirming the parser's line-prefix matching
    still finds the two lines it wants and ignores the rest."""
    real_output = (
        "Format Video Capture:\n"
        "\tWidth/Height      : 1280/720\n"
        "\tPixel Format      : 'YUYV' (YUYV 4:2:2)\n"
        "\tField             : None\n"
        "\tBytes per Line    : 2560\n"
        "\tSize Image        : 1843200\n"
        "\tColorspace        : sRGB\n"
        "\tTransfer Function : sRGB\n"
        "\tYCbCr/HSV Encoding: ITU-R 601\n"
        "\tQuantization      : Limited Range\n"
        "\tFlags             : \n"
    )
    monkeypatch.setattr(
        v4l2_loopback.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(real_output)
    )
    assert v4l2_loopback._query_format("/dev/video71") == (1280, 720, "YUYV")


def test_query_format_degrades_when_device_has_no_negotiated_format(monkeypatch):
    """Real finding: vidar's *other* loopback (video70, camera i2c@88000)
    has a genuine hardware fault (confirmed independently with the
    ``cam`` tool directly, bypassing AprilCam entirely -- "Camera
    frontend has timed out") and so never has a bridge-negotiated
    format. ``v4l2-ctl --get-fmt-video`` against it does not raise or
    time out -- it runs, exits non-zero, and prints this exact one-line
    error to stdout. ``_query_format`` has no exception to catch here
    (``subprocess.run`` succeeded), so it falls through its line-prefix
    scan, matches neither "Width/Height" nor "Pixel Format", and
    degrades to the module defaults -- exactly as the docstring's
    "any subprocess failure or unparseable output" promises, verified
    against the literal real error text rather than a guessed one."""
    monkeypatch.setattr(
        v4l2_loopback.subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess("VIDIOC_G_FMT: failed: Invalid argument\n"),
    )
    width, height, pixfmt = v4l2_loopback._query_format("/dev/video70")
    assert (width, height, pixfmt) == (
        v4l2_loopback._DEFAULT_WIDTH,
        v4l2_loopback._DEFAULT_HEIGHT,
        v4l2_loopback._DEFAULT_PIXFMT,
    )


def test_read_returns_none_when_stream_is_zero_bytes(monkeypatch):
    """Real finding: ``v4l2-ctl --stream-mmap`` against a loopback device
    with no negotiated format (no bridge writer attached) prints
    "unsupported stream type" to stderr, writes **zero bytes** to
    stdout, and exits **0** -- not a non-zero exit, and not a hang. A
    watchdog that only checks the exit code (as
    ``deploy/aprilcam-camera-bridge``'s does -- out of this ticket's
    file scope, flagged separately) would misread this as success, but
    ``V4l2LoopbackBackend`` never looks at the exit code: it only reads
    bytes from stdout, so the first ``read()`` on an empty pipe returns
    ``b""`` (EOF) immediately and ``read()`` correctly reports failure
    (``None``) rather than hanging -- confirmed live against vidar's
    video70 (0.05 ms per the live session log, not a multi-second
    stall)."""
    _patch_query_format(monkeypatch, width=2, height=2, pixfmt="YUYV")
    monkeypatch.setattr(
        v4l2_loopback.subprocess, "Popen", lambda *a, **k: _FakePopen([b""])
    )
    backend = v4l2_loopback.V4l2LoopbackBackend("/dev/video70")
    backend.open()
    assert backend.read() is None


def test_reopen_after_stream_failure_recovers_once_the_writer_returns(monkeypatch):
    """Real finding (self-heal, AC5): once a bridge dies mid-stream, the
    live ``v4l2-ctl`` subprocess a ``V4l2LoopbackBackend`` is already
    holding open never recovers on its own -- ``read()`` keeps
    returning ``None`` forever (confirmed live: 400 consecutive
    ``None`` reads over 40s while vidar's bridge was down). Recovery is
    a fresh ``close()``/``open()`` cycle, exactly what
    ``camera.camera.Camera._attempt_self_heal`` performs after
    ``FAILURE_THRESHOLD`` consecutive failures -- confirmed live end to
    end through the real daemon (``imx296-2: read failing, reopen
    attempt 1/10`` while the bridge was still down, then ``attempt
    2/10`` succeeding once it was back, with frame capture resuming
    immediately). This test freezes the backend-level half of that:
    a backend whose subprocess has already gone quiet produces a fresh,
    working stream after ``close()`` + ``open()``, without needing a
    new ``V4l2LoopbackBackend`` instance."""
    _patch_query_format(monkeypatch, width=2, height=2, pixfmt="YUYV")
    frame_bytes = 2 * 2 * 2
    dead_proc = _FakePopen([b""])  # writer gone: immediate EOF, like the real stall
    live_proc = _FakePopen([bytes(range(frame_bytes))])  # writer back: real frame

    procs = [dead_proc, live_proc]
    monkeypatch.setattr(v4l2_loopback.subprocess, "Popen", lambda *a, **k: procs.pop(0))

    backend = v4l2_loopback.V4l2LoopbackBackend("/dev/video71")
    backend.open()
    assert backend.read() is None  # bridge still down

    backend.close()
    backend.open()  # the self-heal reopen Camera._attempt_self_heal performs
    frame = backend.read()
    assert isinstance(frame, np.ndarray)
    assert frame.shape == (2, 2, 3)


def test_list_loopback_cameras_matches_vidars_exact_real_card_labels(tmp_path):
    """AC1, literal: the exact two real ``/sys`` card labels vidar's
    v4l2loopback module was loaded with (``video_nr=70,71
    card_label=aprilcam-imx296-88000,aprilcam-imx296-80000``, confirmed
    live via ``cat /sys/module/v4l2loopback/parameters/*`` and
    ``/sys/class/video4linux/video7{0,1}/name``), enumerated together in
    one pass -- not the look-alike synthetic labels the earlier tests in
    this file use."""
    _write_sys_video_name(tmp_path, 70, "aprilcam-imx296-88000")
    _write_sys_video_name(tmp_path, 71, "aprilcam-imx296-80000")

    devices = libcamera.list_loopback_cameras(sys_glob=str(tmp_path / "video*" / "name"))

    assert [(d.device_index, d.camera_id) for d in devices] == [
        (70, "aprilcam-imx296-88000"),
        (71, "aprilcam-imx296-80000"),
    ]
    assert [d.position for d in devices] == [0, 1]


def test_resolve_capture_backend_matches_vidars_exact_real_hardware_keys():
    """AC2, literal: ``resolve_capture_backend`` given each real
    ``hardware_key`` observed live on vidar
    (``libcamera:aprilcam-imx296-88000`` /
    ``libcamera:aprilcam-imx296-80000``) constructs a
    ``V4l2LoopbackBackend`` at the matching real ``/dev/videoN`` --
    confirmed live via a throwaway venv's
    ``resolve_capture_backend("libcamera:aprilcam-imx296-88000", 0,
    backend_factory=...)`` returning ``V4l2LoopbackBackend(/dev/video70)``
    (and the ``-80000``/``video71`` pair symmetrically), reproduced here
    offline."""
    devices = [
        _loopback_device(position=0, device_index=70, camera_id="aprilcam-imx296-88000"),
        _loopback_device(position=1, device_index=71, camera_id="aprilcam-imx296-80000"),
    ]

    for hardware_key, expected_device in (
        ("libcamera:aprilcam-imx296-88000", "/dev/video70"),
        ("libcamera:aprilcam-imx296-80000", "/dev/video71"),
    ):
        backend = resolve_capture_backend(
            hardware_key,
            0,
            backend_factory=lambda index: _FakeOpenCVBackend(index),
            list_loopback_cameras=lambda: devices,
        )
        assert isinstance(backend, V4l2LoopbackBackend)
        assert backend._device == expected_device
