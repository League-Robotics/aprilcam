"""DaemonService lens provenance (sprint 044 ticket 010), hardware-free."""
from __future__ import annotations

from dataclasses import replace

from aprilcam.calibration.lens import LensModel
from aprilcam.config.interfaces import CameraConfig
from tests.aprilcam.calibration.test_calibration import (
    MAIN_PLAYFIELD_ARUCO_POSITIONS,
    main_playfield_definition,
    render_playfield_frame,
)
from tests.aprilcam.daemon.test_service import (
    _calibration_for_main_playfield,
    _open_camera_with_frames,
    _service_with_camera,
    directories,  # noqa: F401  (fixture)
    registry,  # noqa: F401  (fixture)
)

_K1 = ((800.0, 0.0, 320.0), (0.0, 800.0, 240.0), (0.0, 0.0, 1.0))
_K2 = ((820.0, 0.0, 320.0), (0.0, 820.0, 240.0), (0.0, 0.0, 1.0))
_D = (-0.1, 0.01, 0.0, 0.0, 0.0)


def _lens(k=_K1, size=(10, 10), source="measured") -> LensModel:
    return LensModel(
        camera_matrix=k, dist_coeffs=_D, image_size=size, source=source,
        views=12, rms_px=0.4, residual_px=0.6,
    )


def _service(directories, registry):  # noqa: F811
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, backend = _open_camera_with_frames(registry, [frame])
    backend.loop = True
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    return service, slug, frame


def _state(service, slug):
    return next(s for s in service.list_cameras() if s.name == slug)


def _install_calibration(service, slug, **lens):
    cal = replace(_calibration_for_main_playfield(slug), **lens)
    with service._lock:  # noqa: SLF001
        service._calibrations[slug] = cal
    return cal


def test_no_lens_data_is_absent_and_not_pending(directories, registry):  # noqa: F811
    service, slug, _ = _service(directories, registry)
    state = _state(service, slug)
    assert (state.lens_model, state.lens_pending) == ("absent", False)
    _install_calibration(service, slug)
    state = _state(service, slug)
    assert (state.lens_model, state.lens_pending) == ("absent", False)


def test_legacy_embedded_lens_reports_legacy(directories, registry):  # noqa: F811
    service, slug, _ = _service(directories, registry)
    _install_calibration(service, slug, camera_matrix=_K1, dist_coeffs=_D)
    state = _state(service, slug)
    assert (state.lens_model, state.lens_pending) == ("legacy", False)


def test_store_model_reports_measured_and_never_calibrated_stays_none(directories, registry):  # noqa: F811
    service, slug, _ = _service(directories, registry)
    service._lens_store.put(slug, _lens())  # noqa: SLF001
    state = _state(service, slug)
    # No calibration yet -> nothing to be pending against.
    assert (state.lens_model, state.lens_pending) == ("measured", False)
    assert service.get_calibration(slug) is None


def test_pending_when_store_differs_from_the_embedded_lens(directories, registry):  # noqa: F811
    service, slug, _ = _service(directories, registry)
    _install_calibration(service, slug, camera_matrix=_K1, dist_coeffs=_D, lens_source="estimated")
    service._lens_store.put(slug, _lens(k=_K2))  # noqa: SLF001
    state = _state(service, slug)
    assert (state.lens_model, state.lens_pending) == ("measured", True)


def test_not_pending_when_store_equals_the_embedded_lens(directories, registry):  # noqa: F811
    service, slug, _ = _service(directories, registry)
    service._lens_store.put(slug, _lens())  # noqa: SLF001
    _install_calibration(service, slug, camera_matrix=_K1, dist_coeffs=_D, lens_source="measured")
    state = _state(service, slug)
    assert (state.lens_model, state.lens_pending) == ("measured", False)


def test_pending_when_calibration_has_no_lens_but_store_does(directories, registry):  # noqa: F811
    service, slug, _ = _service(directories, registry)
    _install_calibration(service, slug)
    service._lens_store.put(slug, _lens())  # noqa: SLF001
    assert _state(service, slug).lens_pending is True


def test_get_calibration_summarises_a_measured_lens_only_while_it_matches(directories, registry):  # noqa: F811
    service, slug, _ = _service(directories, registry)
    service._lens_store.put(slug, _lens())  # noqa: SLF001
    _install_calibration(service, slug, camera_matrix=_K1, dist_coeffs=_D, lens_source="measured")
    cal = service.get_calibration(slug)
    assert (cal.lens_source, cal.lens_views, cal.lens_rms_px, cal.lens_residual_px) == (
        "measured", 12, 0.4, 0.6,
    )
    assert cal.lens_installed_at is not None
    service._lens_store.put(slug, _lens(k=_K2))  # noqa: SLF001
    assert service.get_calibration(slug).lens_views is None


def test_calibrate_reports_measured_source_and_a_resolution_mismatch_warning(directories, registry):  # noqa: F811
    service, slug, frame = _service(directories, registry)
    height, width = frame.shape[:2]
    service._lens_store.put(slug, _lens(size=(width, height)))  # noqa: SLF001
    result = service.calibrate(slug)
    assert result.succeeded is True
    assert result.lens_model_source == "measured" and result.lens_warning is None

    service._lens_store.put(slug, _lens(size=(width + 1, height)))  # noqa: SLF001
    result = service.calibrate(slug)
    assert result.lens_warning is not None and "not applying it" in result.lens_warning
    # The store was ignored; the first calibrate's embedded (measured) lens
    # carries forward, so provenance stays "measured".
    assert result.lens_model_source == "measured"
