"""Tests for ``testdev/capture_matrix.py`` (sprint 035 ticket 003).

``testdev/`` is deliberately excluded from pytest collection
(``testdev/conftest.py``) and is not an importable package (no
``__init__.py``) -- it holds standalone developer tooling, run directly as
``python testdev/capture_matrix.py``. This test module loads it explicitly
via ``importlib.util`` (see :func:`_load_capture_matrix`) rather than
relying on any package machinery, and lives under ``tests/`` (which *is*
collected) rather than inside ``testdev/`` itself.

Ticket 003's own constraint: the stakeholder cannot grant this session
camera permission reliably, so every test here exercises the tool's logic
against fakes/recorded call order -- never real hardware, never a real
``uvc-util`` subprocess, never an opened camera. See each test's docstring
for exactly what it fakes and why.

Covers the ticket's four changes:
  1. ``--field-state-id`` / :func:`resolve_fixture_dir` / the
     :func:`ensure_field_state_stub` extension.
  2. :func:`pin_calibration_snapshot` + the manifest's
     ``calibration_snapshot_sha256`` field.
  3. The new ``ov9281`` entry in ``capture_matrix.yaml``.
  4. The HD-cam control-ordering fix (``_gate_first_order`` /
     ``_ensure_gate_overrides``), reproducing the July run's exact
     rejection scenario against a fake hardware simulator and proving it
     no longer occurs.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional, Tuple

import pytest

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


# ---------------------------------------------------------------------------
# 4. HD-cam control-ordering fix
# ---------------------------------------------------------------------------


class TestGateFirstOrder:
    def test_gate_key_moved_first_regardless_of_input_order(self) -> None:
        """A dict built with the value control *before* its gate (exactly
        what a ``set``-derived baseline dict can produce nondeterministically
        -- see ``_gate_first_order``'s own docstring) is reordered so the
        gate comes first."""
        controls = {"exposure-time-abs": "20", "gain": "4", "auto-exposure-mode": "1"}
        ordered = cm._gate_first_order(controls)
        assert list(ordered.keys())[0] == "auto-exposure-mode"
        assert ordered == {"auto-exposure-mode": "1", "exposure-time-abs": "20", "gain": "4"}

    def test_both_gate_keys_come_before_any_value_key(self) -> None:
        controls = {
            "white-balance-temp": "3000",
            "exposure-time-abs": "20",
            "auto-white-balance-temp": "false",
            "auto-exposure-mode": "1",
        }
        ordered = list(cm._gate_first_order(controls).keys())
        gate_positions = [ordered.index(k) for k in ("auto-exposure-mode", "auto-white-balance-temp")]
        value_positions = [ordered.index(k) for k in ("exposure-time-abs", "white-balance-temp")]
        assert max(gate_positions) < min(value_positions)

    def test_no_gate_keys_present_leaves_order_unchanged(self) -> None:
        controls = {"gain": "4"}
        assert cm._gate_first_order(controls) == {"gain": "4"}


class TestEnsureGateOverrides:
    def test_injects_manual_exposure_gate_when_missing(self) -> None:
        out = cm._ensure_gate_overrides({"exposure-time-abs": "20"})
        assert out == {"auto-exposure-mode": "1", "exposure-time-abs": "20"}

    def test_injects_manual_wb_gate_when_missing(self) -> None:
        out = cm._ensure_gate_overrides({"white-balance-temp": "3000"})
        assert out == {"auto-white-balance-temp": "false", "white-balance-temp": "3000"}

    def test_does_not_override_an_explicit_gate_value(self) -> None:
        """The auto-exposure toggle variation itself (``{"auto-exposure-mode":
        "8"}`` alone, or combined with an explicit choice) must never be
        silently flipped back to manual -- injection only fills a gap, it
        never overrides stated intent."""
        out = cm._ensure_gate_overrides({"exposure-time-abs": "20", "auto-exposure-mode": "8"})
        assert out["auto-exposure-mode"] == "8"

    def test_auto_mode_toggle_alone_is_untouched(self) -> None:
        out = cm._ensure_gate_overrides({"auto-exposure-mode": "8"})
        assert out == {"auto-exposure-mode": "8"}

    def test_gain_only_variation_is_untouched(self) -> None:
        """Gain is not gated by auto-exposure-mode (confirmed live: every
        July ``gain-*`` variation succeeded with no gate involved) -- a
        gain-only variation must not gain a spurious auto-exposure-mode
        override."""
        out = cm._ensure_gate_overrides({"gain": "16"})
        assert out == {"gain": "16"}

    def test_stress_combo_gets_exposure_gate_injected(self) -> None:
        out = cm._ensure_gate_overrides({"exposure-time-abs": "3", "gain": "16"})
        assert out["auto-exposure-mode"] == "1"
        assert out["exposure-time-abs"] == "3"
        assert out["gain"] == "16"

    def test_respects_supported_filter(self) -> None:
        """A camera that does not expose the gate control at all must never
        have it injected -- pushing an unsupported control would fail and
        wrongly mark an otherwise-fine variation as apply-failed."""
        out = cm._ensure_gate_overrides({"exposure-time-abs": "20"}, supported={"exposure-time-abs"})
        assert out == {"exposure-time-abs": "20"}

    def test_ov9782_regression_effective_controls_unchanged(self) -> None:
        """AC: existing ov9782 behavior is unchanged. Its live baseline is
        already manual (auto-exposure-mode='1', auto-white-balance-temp=
        'false'), so injecting the (already-agreeing) gate into an exposure
        variation must not change the EFFECTIVE (baseline-merged) controls
        actually pushed/recorded -- only the intermediate dict gains an
        explicit key that was already implied by baseline."""
        baseline_controls = {
            "auto-exposure-mode": "1",
            "auto-white-balance-temp": "false",
            "exposure-time-abs": "6",
            "gain": "2",
            "white-balance-temp": "4600",
        }
        raw_variation_controls = {"exposure-time-abs": "3"}
        effective_before = {**baseline_controls, **raw_variation_controls}
        injected = cm._ensure_gate_overrides(raw_variation_controls)
        effective_after = {**baseline_controls, **injected}
        assert effective_after == effective_before


class TestApplyControlsAgainstFakeHardware:
    """Reproduces the exact July HD-cam scenario
    (``tests/images/hd-usb-camera/manifest.json``'s recorded baseline and
    ``skipped_variations.apply_failed``) against a fake uvc-util
    stand-in -- no subprocess, no hardware -- and proves the fix.
    """

    @staticmethod
    def _fake_hardware(initial: Dict[str, str]):
        state = dict(initial)
        calls: List[Tuple[str, str]] = []

        def fake_apply_control(uvc_bin, device_name, name, value) -> bool:
            calls.append((name, value))
            if name == "exposure-time-abs" and state.get("auto-exposure-mode") != "1":
                return False
            if name == "white-balance-temp" and state.get("auto-white-balance-temp") != "false":
                return False
            state[name] = value
            return True

        return state, calls, fake_apply_control

    def test_baseline_only_auto_exposure_active_rejects_exposure_push_pre_fix_shape(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanity check that the fake hardware genuinely reproduces the July
        bug shape: pushing exposure-time-abs alone, while auto-exposure-mode
        is "8" (auto), is rejected -- exactly what July's manifest recorded."""
        state, calls, fake_apply_control = self._fake_hardware(
            {"auto-exposure-mode": "8", "exposure-time-abs": "128"}
        )
        monkeypatch.setattr(cm, "apply_control", fake_apply_control)
        failed = cm.apply_controls(Path("/usr/bin/uvc-util"), "HD USB CAMERA", {"exposure-time-abs": "20"})
        assert failed == ["exposure-time-abs"]
        assert state["exposure-time-abs"] == "128"  # unchanged -- rejected, not silently accepted

    def test_gate_injected_variation_succeeds_against_same_hardware(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The fix: run the SAME push through ``_ensure_gate_overrides``
        first (what ``run_camera`` now does to every variation) -- the gate
        goes first (``apply_controls``' own ordering) and the exposure value
        sticks."""
        state, calls, fake_apply_control = self._fake_hardware(
            {"auto-exposure-mode": "8", "exposure-time-abs": "128"}
        )
        monkeypatch.setattr(cm, "apply_control", fake_apply_control)
        controls = cm._ensure_gate_overrides({"exposure-time-abs": "20"})
        failed = cm.apply_controls(Path("/usr/bin/uvc-util"), "HD USB CAMERA", controls)
        assert failed == []
        assert state["auto-exposure-mode"] == "1"
        assert state["exposure-time-abs"] == "20"
        assert calls[0][0] == "auto-exposure-mode"  # gate pushed first


# ---------------------------------------------------------------------------
# 1. --field-state-id / fixture nesting
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
        """The file lives directly at *output_root* (the fixture root
        passed in), never nested under a per-camera or per-generation
        subdirectory -- so every camera's manifest in a run reads the SAME
        file."""
        cm.ensure_field_state_stub(tmp_path, "fs-20260823")
        assert (tmp_path / "field-state.json").is_file()
        assert not (tmp_path / "fs-20260823" / "field-state.json").exists()

    def test_explicit_id_never_clobbers_an_existing_confirmed_entry(self, tmp_path: Path) -> None:
        """A stakeholder-confirmed entry for an id must never be silently
        reset to an unverified stub by a later run that happens to pass the
        same id again."""
        cm.ensure_field_state_stub(tmp_path, "fs-20260823")
        path = tmp_path / "field-state.json"
        data = _read_json(path)
        data["states"]["fs-20260823"]["status"] = "confirmed"
        data["states"]["fs-20260823"]["visible_april_tags"] = [1, 10, 11, 15, 100]
        _write_json(path, data)

        cm.ensure_field_state_stub(tmp_path, "fs-20260823")

        after = _read_json(path)
        assert after["states"]["fs-20260823"]["status"] == "confirmed"
        assert after["states"]["fs-20260823"]["visible_april_tags"] == [1, 10, 11, 15, 100]

    def test_explicit_id_preserves_other_generations(self, tmp_path: Path) -> None:
        """Adding a new generation's id must not disturb an older
        generation's own entry (the July ``unverified-20260729`` corpus
        coexisting with a later ``fs-20260823`` one)."""
        cm.ensure_field_state_stub(tmp_path, "unverified-20260729")
        cm.ensure_field_state_stub(tmp_path, "fs-20260823")
        data = _read_json(tmp_path / "field-state.json")
        assert "unverified-20260729" in data["states"]
        assert "fs-20260823" in data["states"]
        assert data["current"] == "fs-20260823"


# ---------------------------------------------------------------------------
# 2. Calibration snapshot pinning
# ---------------------------------------------------------------------------


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
# 3. ov9281 yaml entry
# ---------------------------------------------------------------------------


class TestOv9281MatrixEntry:
    @pytest.fixture()
    def matrix_spec(self) -> dict:
        return cm.load_matrix_spec(cm.DEFAULT_MATRIX_SPEC)

    def test_selected_for_the_real_device_name_pattern(self, matrix_spec: dict) -> None:
        entry = cm.select_matrix_entry(matrix_spec, "Arducam OV9281 USB Camera")
        assert entry is not None
        assert entry["match"] == "ov9281"

    def test_not_selected_for_ov9782_or_hd_cam(self, matrix_spec: dict) -> None:
        ov9782 = cm.select_matrix_entry(matrix_spec, "Arducam OV9782 USB Camera")
        hd = cm.select_matrix_entry(matrix_spec, "HD USB CAMERA")
        assert ov9782["match"] == "ov9782"
        assert hd is None  # HD cam still has no dedicated entry -- fallback applies

    def test_exposure_sweep_spans_5_to_300(self, matrix_spec: dict) -> None:
        entry = cm.select_matrix_entry(matrix_spec, "Arducam OV9281 USB Camera")
        exposures = sorted(
            int(v["controls"]["exposure-time-abs"])
            for v in entry["variations"]
            if "exposure-time-abs" in v["controls"]
        )
        assert exposures[0] == 5
        assert exposures[-1] == 300

    def test_has_a_gain_axis(self, matrix_spec: dict) -> None:
        entry = cm.select_matrix_entry(matrix_spec, "Arducam OV9281 USB Camera")
        gains = [v for v in entry["variations"] if "gain" in v["controls"]]
        assert len(gains) >= 2

    def test_no_white_balance_axis(self, matrix_spec: dict) -> None:
        entry = cm.select_matrix_entry(matrix_spec, "Arducam OV9281 USB Camera")
        for v in entry["variations"]:
            assert "white-balance-temp" not in v["controls"]
            assert "auto-white-balance-temp" not in v["controls"]


# ---------------------------------------------------------------------------
# End-to-end: run_camera against a fully faked backend/hardware (no camera,
# no subprocess) -- exercises all four changes together the way a real
# capture run would.
# ---------------------------------------------------------------------------


class _FakeCaptureBackend:
    """A minimal ``CaptureBackend`` double -- open/read/close/frame_size,
    matching ``OpenCVBackend``'s surface, no real capture device involved.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        self._open = False

    def open(self) -> None:
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    def read(self):
        import numpy as np

        return np.zeros((48, 64, 3), dtype="uint8")

    def close(self) -> None:
        self._open = False

    def frame_size(self) -> Tuple[int, int]:
        return (64, 48)


class _FakeDetector:
    def detect(self, gray):  # noqa: ANN001 -- duck-typed, matches TagDetector.detect's call shape
        return []


def _read_json(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    import json

    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class TestRunCameraEndToEnd:
    """HD-cam (camera 2), matching July's exact recorded baseline
    (``tests/images/hd-usb-camera/manifest.json``), driven through
    ``run_camera`` with every hardware touchpoint faked."""

    def _make_cam(self, tmp_path: Path):
        config = cm.CameraConfig(name="hd-usb-camera", playfield="main-playfield", settings={})
        return cm.ConfiguredCamera(
            slug="hd-usb-camera",
            config_dir=tmp_path / "config" / "cameras" / "hd-usb-camera",
            config=config,
            device_name="HD USB CAMERA",
            live_index=2,
            hardware_key="vidpid:1234:5678",
            registry_number=2,
        )

    def _install_fakes(self, monkeypatch: pytest.MonkeyPatch, hw_state: Dict[str, str]) -> List[Tuple[str, str]]:
        calls: List[Tuple[str, str]] = []

        def fake_list_supported_controls(uvc_bin, device_name) -> List[str]:
            return list(hw_state.keys())

        def fake_read_control(uvc_bin, device_name, name) -> Optional[str]:
            return hw_state.get(name)

        def fake_read_control_range(uvc_bin, device_name, name) -> Optional[Tuple[int, int]]:
            return {
                "exposure-time-abs": (5, 10000),
                "gain": (0, 255),
                "white-balance-temp": (2800, 6500),
            }.get(name)

        def fake_apply_control(uvc_bin, device_name, name, value) -> bool:
            calls.append((name, value))
            if name == "exposure-time-abs" and hw_state.get("auto-exposure-mode") != "1":
                return False
            if name == "white-balance-temp" and hw_state.get("auto-white-balance-temp") != "false":
                return False
            hw_state[name] = value
            return True

        monkeypatch.setattr(cm, "OpenCVBackend", _FakeCaptureBackend)
        monkeypatch.setattr(cm, "list_supported_controls", fake_list_supported_controls)
        monkeypatch.setattr(cm, "read_control", fake_read_control)
        monkeypatch.setattr(cm, "read_control_range", fake_read_control_range)
        monkeypatch.setattr(cm, "apply_control", fake_apply_control)
        return calls

    def test_full_matrix_applies_with_calibration_and_field_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # -- July's exact recorded baseline (both auto modes active) --
        hw_state = {
            "auto-exposure-mode": "8",
            "auto-white-balance-temp": "true",
            "exposure-time-abs": "128",
            "gain": "64",
            "white-balance-temp": "4826",
        }
        self._install_fakes(monkeypatch, hw_state)

        directories = cm.Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
        cal_dir = directories.state_dir / "cameras" / "hd-usb-camera"
        cal_dir.mkdir(parents=True)
        calibration_payload = b'{"homography": [[1,0,0],[0,1,0],[0,0,1]]}'
        (cal_dir / "calibration.json").write_bytes(calibration_payload)

        cam = self._make_cam(tmp_path)
        matrix_spec = cm.load_matrix_spec(cm.DEFAULT_MATRIX_SPEC)
        output_root = tmp_path / "images"
        output_root.mkdir()
        # main() calls ensure_field_state_stub before run_camera -- do the
        # same here since this test drives run_camera directly.
        field_state_id = cm.ensure_field_state_stub(output_root, "fs-20260823")

        result = cm.run_camera(
            cam,
            Path("/usr/bin/uvc-util"),
            matrix_spec,
            output_root,
            field_state_id,
            0.0,  # settle_override -- instant, no real waiting
            _FakeDetector(),
            directories,
            "fs-20260823",
        )

        assert result.error is None
        # The fix: none of July's twelve apply-failed variations recur.
        assert result.skipped_apply_failed == []
        # AC: the finally: block still always runs and restores baseline
        # (regression-checked, not just assumed). This fake baseline's own
        # auto-exposure-mode/auto-white-balance-temp are themselves "auto"
        # (matching July's real recorded baseline exactly) -- on real
        # hardware, a manual exposure/WB *value* is inherently unobservable
        # while its gate is auto (the camera ignores it), so a byte-exact
        # readback of those two values specifically is not achievable here
        # or on the real July hardware; that pre-existing limitation is not
        # this ticket's HD-cam fix (which is about variations applying, not
        # about restoring a value the auto mode was already overriding).
        # The two gate controls -- never subject to that ambiguity -- must
        # still restore exactly.
        assert result.restored is True
        assert result.restore_mismatches == [
            m for m in result.restore_mismatches
            if m.startswith("exposure-time-abs:") or m.startswith("white-balance-temp:")
        ]
        assert not any(m.startswith("auto-exposure-mode:") for m in result.restore_mismatches)
        assert not any(m.startswith("auto-white-balance-temp:") for m in result.restore_mismatches)

        fixture_dir = output_root / "fs-20260823" / "hd-usb-camera"
        assert fixture_dir.is_dir()
        manifest = _read_json(fixture_dir / "manifest.json")
        assert manifest["field_state_id"] == "fs-20260823"
        assert manifest["calibration_snapshot_sha256"] == hashlib.sha256(calibration_payload).hexdigest()
        assert (fixture_dir / "calibration.json").read_bytes() == calibration_payload

        field_state = _read_json(output_root / "field-state.json")
        assert field_state["current"] == "fs-20260823"

    def test_default_no_field_state_id_stays_unnested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC: existing camera entries unchanged with no new flags -- same
        flat ``tests/images/<slug>/`` layout as before."""
        hw_state = {
            "auto-exposure-mode": "1",
            "auto-white-balance-temp": "false",
            "exposure-time-abs": "6",
            "gain": "2",
            "white-balance-temp": "4600",
        }
        self._install_fakes(monkeypatch, hw_state)
        directories = cm.Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")

        config = cm.CameraConfig(name="arducam-ov9782-usb-camera", playfield="main-playfield", settings={})
        cam = cm.ConfiguredCamera(
            slug="arducam-ov9782-usb-camera",
            config_dir=tmp_path / "config" / "cameras" / "arducam-ov9782-usb-camera",
            config=config,
            device_name="Arducam OV9782 USB Camera",
            live_index=3,
            hardware_key="vidpid:aaaa:bbbb",
            registry_number=3,
        )
        matrix_spec = cm.load_matrix_spec(cm.DEFAULT_MATRIX_SPEC)
        output_root = tmp_path / "images"
        output_root.mkdir()

        field_state_id = cm.ensure_field_state_stub(output_root, None)
        result = cm.run_camera(
            cam,
            Path("/usr/bin/uvc-util"),
            matrix_spec,
            output_root,
            field_state_id,
            0.0,
            _FakeDetector(),
            directories,
            None,
        )

        assert result.error is None
        assert result.skipped_apply_failed == []
        # Fully-manual baseline (matching ov9782's real config) -- restore
        # is byte-exact achievable here, unlike the auto-baseline HD-cam
        # scenario above.
        assert result.restored is True
        assert result.restore_mismatches == []
        assert result.fixture_dir == "arducam-ov9782-usb-camera"
        assert (output_root / "arducam-ov9782-usb-camera" / "manifest.json").is_file()
        # Not nested under the auto-generated field-state id -- nesting only
        # happens when --field-state-id was explicitly given.
        assert not (output_root / field_state_id).is_dir()
