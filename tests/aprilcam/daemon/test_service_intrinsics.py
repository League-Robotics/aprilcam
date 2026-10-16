"""DaemonService intrinsics integration (sprint 044 ticket 005), hardware-free."""
from __future__ import annotations

import time

import pytest

from aprilcam.config.interfaces import CameraConfig
from aprilcam.errors import UnknownCamera
from aprilcam.intrinsics.interfaces import (
    STATE_ABORTED,
    STATE_COLLECTING,
    STATE_FINISHED,
    IntrinsicsSessionActive,
)
from tests.aprilcam.calibration.test_calibration import (
    MAIN_PLAYFIELD_ARUCO_POSITIONS,
    main_playfield_definition,
    render_playfield_frame,
)
from tests.aprilcam.daemon.test_service import (
    _open_camera_with_frames,
    _service_with_camera,
    directories,  # noqa: F401  (fixture)
    registry,  # noqa: F401  (fixture)
)
from tests.aprilcam.intrinsics.boards import spread_frames

# A geometry-preserving window the same size as the playfield canvas so a
# lens measured here matches the frames a later calibrate() reads.
CROP = (190, 10, 900, 700)


@pytest.fixture(scope="module")
def board_frames():
    return spread_frames(12, seed=11, crop=CROP, dist_range=(0.6, 0.75),
                         x_range=(380, 900), y_range=(300, 420))


@pytest.fixture
def svc(directories, registry, board_frames):  # noqa: F811
    identity, slug, cam, backend = _open_camera_with_frames(registry, board_frames)
    backend.loop = True
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service._intrinsics.frame_interval = 0.0
    yield service, slug, cam, backend
    service.close()


def wait_for(service, slug, pred, timeout=60.0):
    deadline = time.monotonic() + timeout
    st = service.get_intrinsics_status(slug)
    while time.monotonic() < deadline:
        st = service.get_intrinsics_status(slug)
        if pred(st):
            return st
        time.sleep(0.05)
    raise AssertionError(f"timed out; last status {st}")


def test_start_poll_abort_installs_nothing(svc):
    service, slug, _, _ = svc
    st = service.start_intrinsics(slug)
    assert st.state == STATE_COLLECTING and st.view_count == 0
    wait_for(service, slug, lambda s: s.view_count >= 3)
    done = service.abort_intrinsics(slug)
    assert done.state == STATE_ABORTED
    assert not service._lens_store.path(slug).exists()
    assert service.start_intrinsics(slug).view_count == 0  # fresh session


def test_double_start_refused_and_number_token_resolves(svc, registry):  # noqa: F811
    service, slug, cam, _ = svc
    number = str(registry.get(slug).number)
    service.start_intrinsics(number)
    with pytest.raises(IntrinsicsSessionActive):
        service.start_intrinsics(slug)
    assert service.get_intrinsics_status(number).camera == slug


def test_unknown_camera_raises(svc):
    service, *_ = svc
    with pytest.raises(UnknownCamera):
        service.start_intrinsics("no-such-camera")


def test_bad_board_spec_rejected(svc):
    service, slug, _, _ = svc
    with pytest.raises(ValueError):
        service.start_intrinsics(slug, board="nonsense")
    assert service._intrinsics.active_slugs() == []


def test_close_aborts_sessions(svc):
    service, slug, _, _ = svc
    service.start_intrinsics(slug)
    service.close()
    assert service._intrinsics.active_slugs() == []
    assert service.get_intrinsics_status(slug).state == STATE_ABORTED


def test_finish_install_then_calibrate_uses_new_lens_without_restart(svc):
    service, slug, cam, backend = svc
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    service.start_intrinsics(slug)
    st = wait_for(service, slug, lambda s: s.view_count >= 8)
    assert st.image_size == (900, 700)

    refused = service.finish_intrinsics(slug, install=True)
    if not st.good:
        assert not refused.installed
        assert not service._lens_store.path(slug).exists()
    result = service.finish_intrinsics(slug, install=True, force=True)
    assert result.installed, result.reason
    assert "calibrate" in result.reason
    assert result.status.state == STATE_FINISHED
    stored = service._lens_store.load(slug)
    assert stored is not None and stored.source == "measured"
    assert stored.image_size == (900, 700)
    assert stored.views >= 6

    # The same service instance now calibrates against playfield frames.
    pf = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    backend.loop = False
    backend._queue = [pf] * 100
    calibrated = service.calibrate(slug)
    assert calibrated.succeeded, calibrated.reason
    assert calibrated.lens_model_source == "measured"
    assert service.get_calibration(slug).lens_source == "measured"
