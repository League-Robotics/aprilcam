"""Tests for ``testdev/capture_matrix.py`` (sprint 035 ticket 004).

``testdev/`` is deliberately excluded from pytest collection
(``testdev/conftest.py``) and is not an importable package (no
``__init__.py``) -- it holds standalone developer tooling, run directly as
``python testdev/capture_matrix.py``. This test module loads it explicitly
via ``importlib.util`` (see :func:`_load_capture_matrix`) rather than
relying on any package machinery, and lives under ``tests/`` (which *is*
collected) rather than inside ``testdev/`` itself.

Ticket 004 reworked this tool from direct camera/uvc-util access to
exclusively driving a daemon's RPCs (``Daemon.get_camera_config`` /
``set_camera_config`` / ``get_frame`` / ``get_tags``) -- see
``testdev/capture_matrix.py``'s module docstring, "Daemon-driven, not
direct-open". Every test here exercises that against a small in-memory
:class:`FakeDaemon` (a real ``aprilcam.daemon.interfaces.Daemon``
subclass) -- never a real daemon process, never real hardware, never a
``uvc-util`` subprocess. The stakeholder is actively using the real rig; no
test in this module may touch it.

Covers:
  1. The daemon-precondition inversion (``require_daemon``/
     ``DaemonUnavailable``) -- a reachable daemon is now required, not
     refused against.
  2. ``select_target_cameras`` -- camera selection via
     ``Daemon.list_cameras()`` instead of live hardware enumeration.
  3. ``_ensure_gate_overrides``, translated to ``CameraConfig.settings``'s
     five-key vocabulary -- reproducing the July HD-cam gate-rejection bug
     against a fake daemon that models ``Camera.apply_config``'s real
     gate-first/whole-push-atomic contract, and proving the fix.
  4. ``build_fallback_variations`` against the new fixed-range fallback
     spec (no live uvc-util range query).
  5. ``resolve_fixture_dir`` / ``ensure_field_state_stub`` /
     ``pin_calibration_snapshot`` -- unchanged by this ticket (pure file
     logic / a read-only disk read), carried over verbatim from ticket 003.
  6. ``run_camera`` end-to-end against :class:`FakeDaemon`: whole-document
     pushes, the "always restore" ``finally`` guarantee, and the ov9281
     ``capture_matrix.yaml`` entry's daemon-vocabulary controls.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_capture_matrix() -> ModuleType:
    """Load ``testdev/capture_matrix.py`` as a real module.

    Registered in ``sys.modules`` *before* ``exec_module`` -- required
    because the module defines ``@dataclass`` classes, and the dataclass
    machinery looks its own defining module up via
    ``sys.modules[cls.__module__]`` while the class body is still being
    processed; skipping this raises ``AttributeError`` on import.
    """
    path = REPO_ROOT / "testdev" / "capture_matrix.py"
    spec = importlib.util.spec_from_file_location("capture_matrix", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["capture_matrix"] = module
    spec.loader.exec_module(module)
    return module


cm = _load_capture_matrix()

from aprilcam.config.interfaces import CameraConfig  # noqa: E402
from aprilcam.daemon.interfaces import Daemon  # noqa: E402
from aprilcam.errors import ConnectionFailed, UnsupportedSetting  # noqa: E402
from aprilcam.types import CameraState, ImageFrame, TagFrame, TagId, TagFamily, TagRecord  # noqa: E402


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _tiny_jpeg(luminance: int = 128, size: Tuple[int, int] = (8, 6)) -> bytes:
    """A tiny real JPEG, flat grey at *luminance* -- decodable by
    :func:`capture_matrix._decode_mean_luminance` the same way a daemon's
    ``GetFrame`` response would be."""
    buf = io.BytesIO()
    Image.new("L", size, color=luminance).save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# A real Daemon subclass, entirely in-memory -- models the one piece of
# real-hardware behaviour these tests need to reproduce: Camera.
# apply_config's gate-first, whole-push-atomic contract (camera/camera.py).
# ---------------------------------------------------------------------------

_GATE_KEYS = ("auto_exposure", "auto_white_balance")


class FakeDaemon(Daemon):
    def __init__(
        self,
        *,
        name: str = "hd-usb-camera",
        playfield: Optional[str] = "main-playfield",
        hw_settings: Optional[Dict[str, Any]] = None,
        present: bool = True,
        usable: bool = True,
        number: Optional[int] = None,
        jpeg_luminance: int = 128,
        tags: Tuple[TagRecord, ...] = (),
    ) -> None:
        self._name = name
        self._playfield = playfield
        self._hw: Dict[str, Any] = dict(hw_settings or {})
        self._present = present
        self._usable = usable
        self._number = number
        self._jpeg = _tiny_jpeg(jpeg_luminance)
        self._tags = tags
        self.set_calls: List[Dict[str, Any]] = []
        self.get_frame_calls = 0
        self.get_tags_calls = 0

    # -- Daemon --------------------------------------------------------

    def list_cameras(self) -> Tuple[CameraState, ...]:
        return (
            CameraState(
                name=self._name,
                present=self._present,
                usable=self._usable,
                playfield=self._playfield,
                calibrated=False,
                number=self._number,
            ),
        )

    def get_camera_config(self, camera: str):
        assert camera == self._name, f"unexpected camera {camera!r}"
        return CameraConfig(name=self._name, playfield=self._playfield, settings=dict(self._hw))

    def set_camera_config(self, config) -> None:
        """Models ``Camera.apply_config``'s real contract: gate keys
        (``auto_exposure``/``auto_white_balance``) are applied first,
        unconditionally, regardless of the pushed document's own key
        order; a value key gated by one of them (``exposure`` needs
        ``auto_exposure == 1``, ``white_balance_temperature`` needs
        ``auto_white_balance is False``) is rejected -- raising
        :class:`UnsupportedSetting` and leaving the whole push unapplied
        -- unless its gate is already manual by the time it is reached
        (either already manual on hardware, or set manual earlier in this
        SAME push, exactly the ordering camera/camera.py's ``_GATE_KEYS``
        guarantees)."""
        assert config.name == self._name, f"unexpected camera {config.name!r}"
        self.set_calls.append(dict(config.settings))

        ordered = [k for k in _GATE_KEYS if k in config.settings]
        ordered += [k for k in config.settings if k not in _GATE_KEYS]

        trial = dict(self._hw)
        for key in ordered:
            value = config.settings[key]
            if value is None:
                continue
            if key == "exposure" and trial.get("auto_exposure") != 1:
                raise UnsupportedSetting(key, reason="auto_exposure gate is not manual")
            if key == "white_balance_temperature" and trial.get("auto_white_balance") is not False:
                raise UnsupportedSetting(key, reason="auto_white_balance gate is not manual")
            trial[key] = value
        self._hw = trial

    def reset_camera_config(self, camera: str) -> None:
        raise NotImplementedError

    def get_frame(self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg") -> ImageFrame:
        assert camera == self._name, f"unexpected camera {camera!r}"
        self.get_frame_calls += 1
        return ImageFrame(data=self._jpeg, width=8, height=6, encoding=encoding, timestamp=0.0)

    def get_tags(self, camera: str) -> TagFrame:
        assert camera == self._name, f"unexpected camera {camera!r}"
        self.get_tags_calls += 1
        return TagFrame(tags=self._tags, timestamp=0.0, frame_index=0)


class FakeUnreachableDaemonBackend:
    """Just enough of a ``Discovery`` to prove ``require_daemon``'s
    precondition-inversion: ``connect`` always raises, exactly like a real
    ``Discovery.connect`` would when nothing answers."""

    def connect(self, host: Optional[str] = None):
        raise ConnectionFailed(host or "localhost", reason="no local daemon answered")


class FakeReachableDiscovery:
    def __init__(self, daemon: Daemon) -> None:
        self._daemon = daemon
        self.connected_hosts: List[Optional[str]] = []

    def connect(self, host: Optional[str] = None):
        self.connected_hosts.append(host)
        return self._daemon


# ---------------------------------------------------------------------------
# 1. Daemon precondition inversion
# ---------------------------------------------------------------------------


class TestRequireDaemon:
    def test_raises_daemon_unavailable_when_nothing_answers(self) -> None:
        with pytest.raises(cm.DaemonUnavailable) as excinfo:
            cm.require_daemon(FakeUnreachableDaemonBackend())
        # Actionable message -- names the fix, not just the failure.
        assert "aprilcam daemon start" in str(excinfo.value)

    def test_returns_the_connected_daemon_when_reachable(self) -> None:
        daemon = FakeDaemon()
        discovery = FakeReachableDiscovery(daemon)
        result = cm.require_daemon(discovery, "some-host")
        assert result is daemon
        assert discovery.connected_hosts == ["some-host"]


# ---------------------------------------------------------------------------
# 2. Camera selection via Daemon.list_cameras()
# ---------------------------------------------------------------------------


class TestSelectTargetCameras:
    def _daemon_with(self, *cameras: CameraState) -> Daemon:
        class _D(Daemon):
            def list_cameras(self) -> Tuple[CameraState, ...]:
                return cameras

        return _D()

    def test_filters_to_present_and_playfield_linked(self) -> None:
        daemon = self._daemon_with(
            CameraState(name="a", present=True, usable=True, playfield="main-playfield", calibrated=False),
            CameraState(name="b", present=False, usable=True, playfield="main-playfield", calibrated=False),
            CameraState(name="c", present=True, usable=True, playfield=None, calibrated=False),
        )
        result = cm.select_target_cameras(daemon, None)
        assert [c.name for c in result] == ["a"]

    def test_pattern_matches_slug_substring_case_insensitively(self) -> None:
        daemon = self._daemon_with(
            CameraState(name="arducam-ov9281-usb-camera", present=True, usable=True, playfield="main-playfield", calibrated=False),
            CameraState(name="arducam-ov9782-usb-camera", present=True, usable=True, playfield="main-playfield", calibrated=False),
        )
        result = cm.select_target_cameras(daemon, "OV9281")
        assert [c.name for c in result] == ["arducam-ov9281-usb-camera"]

    def test_pattern_matches_exact_registry_number(self) -> None:
        # Neither slug contains "5" as a substring, so a "5" pattern can
        # only match via the exact-number path -- unlike "2", which would
        # also substring-match "arducam-ov9782-usb-camera" (it contains
        # "9782") and so cannot disambiguate the two match strategies.
        daemon = self._daemon_with(
            CameraState(name="arducam-ov9281-usb-camera", present=True, usable=True, playfield="main-playfield", calibrated=False, number=5),
            CameraState(name="arducam-ov9782-usb-camera", present=True, usable=True, playfield="main-playfield", calibrated=False, number=3),
        )
        result = cm.select_target_cameras(daemon, "5")
        assert [c.name for c in result] == ["arducam-ov9281-usb-camera"]


# ---------------------------------------------------------------------------
# 3. Gate-override injection (daemon vocabulary)
# ---------------------------------------------------------------------------


class TestEnsureGateOverrides:
    def test_injects_manual_exposure_gate_when_missing(self) -> None:
        out = cm._ensure_gate_overrides({"exposure": 20})
        assert out == {"auto_exposure": 1, "exposure": 20}

    def test_injects_manual_wb_gate_when_missing(self) -> None:
        out = cm._ensure_gate_overrides({"white_balance_temperature": 3000})
        assert out == {"auto_white_balance": False, "white_balance_temperature": 3000}

    def test_does_not_override_an_explicit_gate_value(self) -> None:
        """The auto-exposure toggle variation itself must never be silently
        flipped back to manual -- injection only fills a gap, it never
        overrides stated intent."""
        out = cm._ensure_gate_overrides({"exposure": 20, "auto_exposure": 8})
        assert out["auto_exposure"] == 8

    def test_auto_mode_toggle_alone_is_untouched(self) -> None:
        out = cm._ensure_gate_overrides({"auto_exposure": 8})
        assert out == {"auto_exposure": 8}

    def test_gain_only_variation_is_untouched(self) -> None:
        """Gain is not gated by auto_exposure -- a gain-only variation must
        not gain a spurious override."""
        out = cm._ensure_gate_overrides({"gain": 16})
        assert out == {"gain": 16}

    def test_stress_combo_gets_exposure_gate_injected(self) -> None:
        out = cm._ensure_gate_overrides({"exposure": 3, "gain": 16})
        assert out["auto_exposure"] == 1
        assert out["exposure"] == 3
        assert out["gain"] == 16

    def test_ov9782_regression_effective_controls_unchanged(self) -> None:
        """Existing ov9782 behavior is unchanged. Its live baseline is
        already manual (auto_exposure=1, auto_white_balance=False), so
        injecting the (already-agreeing) gate into an exposure variation
        must not change the EFFECTIVE (baseline-merged) settings actually
        pushed/recorded -- only the intermediate dict gains an explicit
        key that was already implied by baseline."""
        baseline = {
            "auto_exposure": 1,
            "auto_white_balance": False,
            "exposure": 6,
            "gain": 2,
            "white_balance_temperature": 4600,
        }
        raw_variation = {"exposure": 3}
        effective_before = {**baseline, **raw_variation}
        injected = cm._ensure_gate_overrides(raw_variation)
        effective_after = {**baseline, **injected}
        assert effective_after == effective_before


# ---------------------------------------------------------------------------
# 4. Whole-push gate simulation against FakeDaemon -- reproduces the July
# HD-cam bug and proves the fix, one level down from run_camera.
# ---------------------------------------------------------------------------


class TestFakeDaemonReproducesJulyGateBug:
    def test_exposure_alone_rejected_while_baseline_gate_is_auto(self) -> None:
        """Sanity check that FakeDaemon genuinely reproduces the July bug
        shape: pushing `exposure` alone, while `auto_exposure` is still 8
        (aperture-priority/auto) on the simulated hardware, is rejected --
        exactly what July's manifest recorded for the direct-uvc-util
        tool, now reproduced one layer up at the daemon-push boundary."""
        daemon = FakeDaemon(hw_settings={"auto_exposure": 8, "exposure": 128})
        with pytest.raises(UnsupportedSetting):
            daemon.set_camera_config(
                CameraConfig(name="hd-usb-camera", playfield="main-playfield", settings={"exposure": 20})
            )
        assert daemon.get_camera_config("hd-usb-camera").settings["exposure"] == 128  # unchanged, rejected not silently accepted

    def test_gate_injected_push_succeeds_against_same_hardware(self) -> None:
        """The fix: run the SAME push through ``_ensure_gate_overrides``
        first (what ``run_camera`` now does to every variation) -- the gate
        is applied first (FakeDaemon's own gate-first ordering, mirroring
        Camera.apply_config) and the exposure value sticks."""
        daemon = FakeDaemon(hw_settings={"auto_exposure": 8, "exposure": 128})
        controls = cm._ensure_gate_overrides({"exposure": 20})
        daemon.set_camera_config(
            CameraConfig(name="hd-usb-camera", playfield="main-playfield", settings=controls)
        )
        settings = daemon.get_camera_config("hd-usb-camera").settings
        assert settings["auto_exposure"] == 1
        assert settings["exposure"] == 20


# ---------------------------------------------------------------------------
# 5. Fallback sweep (fixed ranges, no live uvc-util query)
# ---------------------------------------------------------------------------


class TestBuildFallbackVariations:
    @pytest.fixture()
    def fb_spec(self) -> dict:
        spec = cm.load_matrix_spec(cm.DEFAULT_MATRIX_SPEC)
        return spec["fallback"]

    def test_exposure_sweep_uses_fixed_range(self, fb_spec: dict) -> None:
        variations = cm.build_fallback_variations(fb_spec, {})
        # Only the dedicated "exp-*" sweep -- not the stress combos, which
        # also set "exposure" but are a separate axis (checked below).
        exposures = sorted(v.controls["exposure"] for v in variations if v.name.startswith("exp-"))
        lo, hi = fb_spec["exposure_range"]
        assert all(lo <= e <= hi for e in exposures)
        assert len(exposures) == len(fb_spec["exposure_fractions"])

    def test_auto_exposure_toggle_skips_a_value_matching_baseline(self, fb_spec: dict) -> None:
        """Baseline auto_exposure already equals the first candidate (1) --
        the toggle variation must try the SECOND candidate (8) instead of
        proposing a no-op."""
        variations = cm.build_fallback_variations(fb_spec, {"auto_exposure": 1})
        toggles = [v for v in variations if v.name.startswith("auto-exposure-")]
        assert len(toggles) == 1
        assert toggles[0].controls["auto_exposure"] == 8

    def test_auto_white_balance_toggle_flips_from_baseline(self, fb_spec: dict) -> None:
        variations = cm.build_fallback_variations(fb_spec, {"auto_white_balance": True})
        toggle = next(v for v in variations if v.name.startswith("auto-wb-"))
        assert toggle.controls["auto_white_balance"] is False

    def test_stress_combos_present(self, fb_spec: dict) -> None:
        variations = cm.build_fallback_variations(fb_spec, {})
        names = {v.name for v in variations}
        assert "stress-lowexp-higain" in names
        assert "stress-highexp-logain" in names


# ---------------------------------------------------------------------------
# 6. --field-state-id / fixture nesting -- unchanged by this ticket
# ---------------------------------------------------------------------------


class TestResolveFixtureDir:
    def test_default_no_flag_is_unnested(self) -> None:
        root = Path("/tmp/tests/images")
        assert cm.resolve_fixture_dir(root, None, "hd-usb-camera") == root / "hd-usb-camera"

    def test_explicit_field_state_id_nests_under_it(self) -> None:
        root = Path("/tmp/tests/images")
        result = cm.resolve_fixture_dir(root, "fs-20260823", "hd-usb-camera")
        assert result == root / "fs-20260823" / "hd-usb-camera"


class TestEnsureFieldStateStub:
    def test_legacy_no_id_generates_unverified_stub(self, tmp_path: Path) -> None:
        state_id = cm.ensure_field_state_stub(tmp_path)
        assert state_id.startswith("unverified-")
        data = _read_json(tmp_path / "field-state.json")
        assert data["current"] == state_id
        assert state_id in data["states"]

    def test_legacy_no_id_is_idempotent_returns_existing_current(self, tmp_path: Path) -> None:
        first = cm.ensure_field_state_stub(tmp_path)
        second = cm.ensure_field_state_stub(tmp_path)
        assert first == second

    def test_explicit_id_sets_current_and_creates_stub_entry(self, tmp_path: Path) -> None:
        state_id = cm.ensure_field_state_stub(tmp_path, "fs-20260823")
        assert state_id == "fs-20260823"
        data = _read_json(tmp_path / "field-state.json")
        assert data["current"] == "fs-20260823"
        assert data["states"]["fs-20260823"]["status"] == "unverified"
        assert data["states"]["fs-20260823"]["visible_april_tags"] == []

    def test_explicit_id_written_at_fixture_root_not_per_camera(self, tmp_path: Path) -> None:
        cm.ensure_field_state_stub(tmp_path, "fs-20260823")
        assert (tmp_path / "field-state.json").is_file()
        assert not (tmp_path / "fs-20260823" / "field-state.json").exists()

    def test_explicit_id_never_clobbers_an_existing_confirmed_entry(self, tmp_path: Path) -> None:
        cm.ensure_field_state_stub(tmp_path, "fs-20260823")
        path = tmp_path / "field-state.json"
        data = _read_json(path)
        data["states"]["fs-20260823"]["status"] = "confirmed"
        data["states"]["fs-20260823"]["visible_april_tags"] = [1]
        _write_json(path, data)

        cm.ensure_field_state_stub(tmp_path, "fs-20260823")

        after = _read_json(path)
        assert after["states"]["fs-20260823"]["status"] == "confirmed"
        assert after["states"]["fs-20260823"]["visible_april_tags"] == [1]

    def test_explicit_id_preserves_other_generations(self, tmp_path: Path) -> None:
        cm.ensure_field_state_stub(tmp_path, "unverified-20260729")
        cm.ensure_field_state_stub(tmp_path, "fs-20260823")
        data = _read_json(tmp_path / "field-state.json")
        assert "unverified-20260729" in data["states"]
        assert "fs-20260823" in data["states"]
        assert data["current"] == "fs-20260823"


class TestPinCalibrationSnapshot:
    def _directories(self, tmp_path: Path):
        return cm.Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")

    def test_copies_and_returns_matching_sha256(self, tmp_path: Path) -> None:
        directories = self._directories(tmp_path)
        cam_dir = directories.state_dir / "cameras" / "hd-usb-camera"
        cam_dir.mkdir(parents=True)
        payload = b'{"calibrated_at": "2026-08-23T00:00:00", "homography": []}'
        (cam_dir / "calibration.json").write_bytes(payload)

        fixture_dir = tmp_path / "fixtures" / "hd-usb-camera"
        fixture_dir.mkdir(parents=True)

        digest = cm.pin_calibration_snapshot(directories, "hd-usb-camera", fixture_dir)

        assert digest == hashlib.sha256(payload).hexdigest()
        pinned = fixture_dir / "calibration.json"
        assert pinned.is_file()
        assert pinned.read_bytes() == payload

    def test_never_writes_the_source_file(self, tmp_path: Path) -> None:
        directories = self._directories(tmp_path)
        cam_dir = directories.state_dir / "cameras" / "arducam-ov9782-usb-camera"
        cam_dir.mkdir(parents=True)
        src = cam_dir / "calibration.json"
        payload = b'{"homography": [[1,0,0],[0,1,0],[0,0,1]]}'
        src.write_bytes(payload)
        mtime_before = src.stat().st_mtime_ns

        fixture_dir = tmp_path / "fixtures" / "arducam-ov9782-usb-camera"
        fixture_dir.mkdir(parents=True)
        cm.pin_calibration_snapshot(directories, "arducam-ov9782-usb-camera", fixture_dir)

        assert src.read_bytes() == payload
        assert src.stat().st_mtime_ns == mtime_before

    def test_returns_none_when_camera_has_no_calibration(self, tmp_path: Path) -> None:
        directories = self._directories(tmp_path)
        fixture_dir = tmp_path / "fixtures" / "hd-usb-camera"
        fixture_dir.mkdir(parents=True)

        digest = cm.pin_calibration_snapshot(directories, "hd-usb-camera", fixture_dir)

        assert digest is None
        assert not (fixture_dir / "calibration.json").exists()


# ---------------------------------------------------------------------------
# 7. ov9281 yaml entry -- matched by slug, daemon vocabulary
# ---------------------------------------------------------------------------


class TestOv9281MatrixEntry:
    @pytest.fixture()
    def matrix_spec(self) -> dict:
        return cm.load_matrix_spec(cm.DEFAULT_MATRIX_SPEC)

    def test_selected_for_the_real_slug(self, matrix_spec: dict) -> None:
        entry = cm.select_matrix_entry(matrix_spec, "arducam-ov9281-usb-camera")
        assert entry is not None
        assert entry["match"] == "ov9281"

    def test_not_selected_for_ov9782_or_hd_cam(self, matrix_spec: dict) -> None:
        ov9782 = cm.select_matrix_entry(matrix_spec, "arducam-ov9782-usb-camera")
        hd = cm.select_matrix_entry(matrix_spec, "hd-usb-camera")
        assert ov9782["match"] == "ov9782"
        assert hd is None  # HD cam still has no dedicated entry -- fallback applies

    def test_exposure_sweep_spans_5_to_300(self, matrix_spec: dict) -> None:
        entry = cm.select_matrix_entry(matrix_spec, "arducam-ov9281-usb-camera")
        exposures = sorted(
            v["controls"]["exposure"] for v in entry["variations"] if "exposure" in v["controls"]
        )
        assert exposures[0] == 5
        assert exposures[-1] == 300

    def test_has_a_gain_axis(self, matrix_spec: dict) -> None:
        entry = cm.select_matrix_entry(matrix_spec, "arducam-ov9281-usb-camera")
        gains = [v for v in entry["variations"] if "gain" in v["controls"]]
        assert len(gains) >= 2

    def test_no_white_balance_axis(self, matrix_spec: dict) -> None:
        entry = cm.select_matrix_entry(matrix_spec, "arducam-ov9281-usb-camera")
        for v in entry["variations"]:
            assert "white_balance_temperature" not in v["controls"]
            assert "auto_white_balance" not in v["controls"]


# ---------------------------------------------------------------------------
# 8. run_camera end-to-end against FakeDaemon
# ---------------------------------------------------------------------------


class TestRunCameraEndToEnd:
    def test_full_matrix_applies_with_calibration_and_field_state(
        self, tmp_path: Path,
    ) -> None:
        """HD-cam (camera 2, fallback sweep), starting from July's exact
        recorded baseline (both gates in auto mode, translated to the
        daemon vocabulary) -- proves none of July's apply-failed
        variations recur, the manifest/calibration/field-state wiring
        from ticket 003 still works, and the finally: restore runs."""
        daemon = FakeDaemon(
            name="hd-usb-camera",
            playfield="main-playfield",
            hw_settings={
                "auto_exposure": 8,
                "auto_white_balance": True,
                "exposure": 128,
                "gain": 64,
                "white_balance_temperature": 4826,
            },
            number=2,
        )

        directories = cm.Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
        cal_dir = directories.state_dir / "cameras" / "hd-usb-camera"
        cal_dir.mkdir(parents=True)
        calibration_payload = b'{"homography": [[1,0,0],[0,1,0],[0,0,1]]}'
        (cal_dir / "calibration.json").write_bytes(calibration_payload)

        cam = daemon.list_cameras()[0]
        matrix_spec = cm.load_matrix_spec(cm.DEFAULT_MATRIX_SPEC)
        output_root = tmp_path / "images"
        output_root.mkdir()
        field_state_id = cm.ensure_field_state_stub(output_root, "fs-20260823")

        result = cm.run_camera(
            daemon,
            cam,
            matrix_spec,
            output_root,
            field_state_id,
            0.0,  # settle_override -- instant, no real waiting
            directories,
            "fs-20260823",
        )

        assert result.error is None
        # The fix: none of July's apply-failed variations recur.
        assert result.skipped_apply_failed == []
        assert result.restored is True
        assert result.restore_error is None
        # Last push in the finally: block put hardware back to the exact
        # pre-sweep document -- minus `exposure`/`white_balance_temperature`,
        # which are never pushed at all while their own gate is auto
        # (_drop_settings_with_inactive_gate): this baseline's own
        # auto_exposure/auto_white_balance are both non-manual, so those two
        # gated values were physically unpushable from the very first
        # establish-baseline call onward, exactly like the July HD-cam
        # baseline itself.
        assert daemon.set_calls[-1] == {
            "auto_exposure": 8,
            "auto_white_balance": True,
            "gain": 64,
        }

        fixture_dir = output_root / "fs-20260823" / "hd-usb-camera"
        assert fixture_dir.is_dir()
        manifest = _read_json(fixture_dir / "manifest.json")
        assert manifest["field_state_id"] == "fs-20260823"
        assert manifest["matrix_source"] == "fallback"
        assert manifest["calibration_snapshot_sha256"] == hashlib.sha256(calibration_payload).hexdigest()
        assert (fixture_dir / "calibration.json").read_bytes() == calibration_payload
        assert manifest["camera"]["registry_enum"] == 2
        assert len(manifest["images"]) == result.captured

        field_state = _read_json(output_root / "field-state.json")
        assert field_state["current"] == "fs-20260823"

    def test_default_no_field_state_id_stays_unnested(self, tmp_path: Path) -> None:
        """Existing camera entries unchanged with no new flags -- same flat
        ``tests/images/<slug>/`` layout as before, matched entry (ov9782,
        not the fallback), fully-manual baseline restores byte-exact."""
        daemon = FakeDaemon(
            name="arducam-ov9782-usb-camera",
            playfield="main-playfield",
            hw_settings={
                "auto_exposure": 1,
                "auto_white_balance": False,
                "exposure": 6,
                "gain": 2,
                "white_balance_temperature": 4600,
            },
            number=3,
        )
        directories = cm.Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
        cam = daemon.list_cameras()[0]
        matrix_spec = cm.load_matrix_spec(cm.DEFAULT_MATRIX_SPEC)
        output_root = tmp_path / "images"
        output_root.mkdir()

        field_state_id = cm.ensure_field_state_stub(output_root, None)
        result = cm.run_camera(
            daemon, cam, matrix_spec, output_root, field_state_id, 0.0, directories, None,
        )

        assert result.error is None
        assert result.skipped_apply_failed == []
        assert result.restored is True
        assert result.restore_error is None
        assert result.fixture_dir == "arducam-ov9782-usb-camera"
        manifest_path = output_root / "arducam-ov9782-usb-camera" / "manifest.json"
        assert manifest_path.is_file()
        assert _read_json(manifest_path)["matrix_source"] == "matrix"
        # Not nested under the auto-generated field-state id -- nesting only
        # happens when --field-state-id was explicitly given.
        assert not (output_root / field_state_id).is_dir()

    def test_camera_marked_unusable_by_daemon_probe_is_reported_and_never_touched(
        self, tmp_path: Path,
    ) -> None:
        daemon = FakeDaemon(name="hd-usb-camera", usable=False)
        cam = daemon.list_cameras()[0]
        directories = cm.Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
        matrix_spec = cm.load_matrix_spec(cm.DEFAULT_MATRIX_SPEC)
        output_root = tmp_path / "images"
        output_root.mkdir()

        result = cm.run_camera(
            daemon, cam, matrix_spec, output_root, "fs-x", 0.0, directories, None,
        )

        assert result.error is not None
        assert "unusable" in result.error
        assert daemon.set_calls == []
        assert daemon.get_frame_calls == 0

    def test_restore_is_still_attempted_when_the_sweep_fails_partway(
        self, tmp_path: Path,
    ) -> None:
        """A mid-sweep AprilCamError from a specific variation's push must
        not abort the whole camera without at least attempting the
        finally: restore -- 'always restore' (module docstring point 5)
        is unconditional."""

        class _FlakyDaemon(FakeDaemon):
            def get_frame(self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg"):
                self.get_frame_calls += 1
                if self.get_frame_calls > 2:
                    raise ConnectionFailed("localhost", reason="simulated wedge mid-sweep")
                return super().get_frame(camera, deskewed=deskewed, encoding=encoding)

        original_baseline = {
            "auto_exposure": 1,
            "auto_white_balance": False,
            "exposure": 6,
            "gain": 2,
            "white_balance_temperature": 4600,
        }
        daemon = _FlakyDaemon(name="arducam-ov9782-usb-camera", hw_settings=dict(original_baseline))
        cam = daemon.list_cameras()[0]
        directories = cm.Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
        matrix_spec = cm.load_matrix_spec(cm.DEFAULT_MATRIX_SPEC)
        output_root = tmp_path / "images"
        output_root.mkdir()

        result = cm.run_camera(
            daemon, cam, matrix_spec, output_root, "fs-x", 0.0, directories, None,
        )

        # Some captures failed with "no frame" once the simulated wedge
        # kicked in, but the camera-level run still completed and restored.
        assert result.skipped_no_frame != []
        assert result.restored is True
        # The finally: block's own push -- the very last one recorded --
        # is exactly the untouched pre-sweep baseline, regardless of how
        # far the sweep got before the wedge hit.
        assert daemon.set_calls[-1] == original_baseline

    def test_baseline_detections_recorded_from_daemon_get_tags(self, tmp_path: Path) -> None:
        """Baseline detection counts come from ``Daemon.get_tags`` now,
        not a local ``TagDetector`` pass (module docstring's "No cv2"
        section)."""
        daemon = FakeDaemon(
            name="arducam-ov9782-usb-camera",
            hw_settings={
                "auto_exposure": 1,
                "auto_white_balance": False,
                "exposure": 6,
                "gain": 2,
                "white_balance_temperature": 4600,
            },
            tags=(
                TagRecord(
                    tag=TagId(family=TagFamily.APRILTAG, number=1),
                    world=None,
                    heading_rad=None,
                    world_velocity=None,
                    speed=None,
                    timestamp=0.0,
                ),
                TagRecord(
                    tag=TagId(family=TagFamily.ARUCO, number=2),
                    world=None,
                    heading_rad=None,
                    world_velocity=None,
                    speed=None,
                    timestamp=0.0,
                ),
                TagRecord(
                    tag=TagId(family=TagFamily.ARUCO, number=3),
                    world=None,
                    heading_rad=None,
                    world_velocity=None,
                    speed=None,
                    timestamp=0.0,
                ),
            ),
        )
        cam = daemon.list_cameras()[0]
        directories = cm.Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
        matrix_spec = cm.load_matrix_spec(cm.DEFAULT_MATRIX_SPEC)
        output_root = tmp_path / "images"
        output_root.mkdir()

        result = cm.run_camera(
            daemon, cam, matrix_spec, output_root, "fs-x", 0.0, directories, None,
        )

        assert result.error is None
        assert daemon.get_tags_calls >= 1
        assert result.baseline_detections == {"apriltag": 1, "aruco": 2}


# ---------------------------------------------------------------------------
# 9. main() -- the daemon precondition inversion wired end-to-end through
# argv parsing, via the discovery_factory DI seam.
# ---------------------------------------------------------------------------


class TestMainDaemonPrecondition:
    def test_refuses_with_a_clear_message_when_no_daemon_answers(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        """The core inversion this ticket makes: a daemon is now a
        precondition, not something this tool refuses to run against."""
        exit_code = cm.main([], discovery_factory=lambda: FakeUnreachableDaemonBackend())
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "REFUSING" in err
        assert "aprilcam daemon start" in err

    def test_reports_no_match_when_daemon_is_reachable_but_no_camera_matches(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daemon = FakeDaemon(name="hd-usb-camera")
        # Keep this test's Directories() resolution off the real machine's
        # config/state roots -- main() constructs a bare Directories()
        # itself (no override seam), so point the env vars it reads at a
        # throwaway tmp_path rather than touching real state.
        monkeypatch.setenv("APRILCAM_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("APRILCAM_STATE_DIR", str(tmp_path / "state"))
        exit_code = cm.main(
            ["totally-unmatched-pattern"],
            discovery_factory=lambda: FakeReachableDiscovery(daemon),
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "No present, playfield-linked camera matched" in err
