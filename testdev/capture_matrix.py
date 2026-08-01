#!/usr/bin/env python3
"""Capture a per-camera image variation matrix for offline pipeline test fixtures.

See docs/test-plans/camera-image-matrix.md (§3/§4) for the full design.

For a given camera this tool:
  1. Verifies the daemon is not holding the device (refuses on contention).
  2. Identifies the camera (registry lookup, config.json baseline) and reads
     its own baseline hardware controls -- NEVER a hardcoded baseline.
  3. Selects a variation matrix from ``capture_matrix.yaml`` by device-name
     pattern (or falls back to a generic exposure/gain/WB sweep built from
     whatever controls ``uvc-util -c`` reports live).
  4. For each variation: restores the full baseline, overlays the varied
     controls directly on the HARDWARE (config.json is never touched),
     settles, and captures one frame.
  5. ALWAYS restores the baseline controls in a ``finally`` block, verified
     by reading them back where uvc-util supports it.
  6. Writes ``tests/images/<camera-slug>/manifest.json`` recording the full
     effective controls, camera identity, resolution, playfield, capture
     timestamp, package version, and field-state id for every image.

This tool deliberately opens the camera directly (bypassing the
daemon-sole-owner rule) -- it is offline test tooling, not a runtime
client, and predates the finished v2 daemon (plan §4).

Usage:
    python testdev/capture_matrix.py                 # every configured camera
    python testdev/capture_matrix.py --all
    python testdev/capture_matrix.py arducam          # one camera, by pattern
    python testdev/capture_matrix.py --dry-run        # print the plan only
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2 as cv
import numpy as np
import yaml

from aprilcam.camera import camutil
from aprilcam.camera import identity as camera_identity
from aprilcam.camera.camera_config import load_camera_config
from aprilcam.camera.registry import CameraRegistry, dir_slug
from aprilcam.config import Config
from aprilcam.core.detector import DetectorConfig, TagDetector

log = logging.getLogger("capture_matrix")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MATRIX_SPEC = Path(__file__).resolve().parent / "capture_matrix.yaml"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tests" / "images"

DARK_LUMINANCE_FLOOR = 10.0
WASHED_LUMINANCE_CEIL = 245.0

_CURRENT_VALUE_RE = re.compile(r"current-value:\s*(.+)")
_MIN_VALUE_RE = re.compile(r"minimum:\s*(-?\d+)")
_MAX_VALUE_RE = re.compile(r"maximum:\s*(-?\d+)")


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("aprilcam")
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Contention checks (plan §4.1) -- refuse rather than fight the daemon.
# ---------------------------------------------------------------------------


class ContentionError(RuntimeError):
    """Raised when the daemon or another process is holding a camera."""


def check_daemon_not_running(config: Config) -> None:
    """Refuse to proceed if the aprilcam daemon holds its machine-global lock.

    Uses the same ``config.daemon_lock_path`` flock the daemon itself takes
    (sprint 018 singleton guard) -- a non-blocking exclusive-lock attempt
    that fails means a daemon is alive right now.
    """
    lock_path = config.daemon_lock_path
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        log.warning("could not open daemon lock path %s: %s -- proceeding without this check", lock_path, exc)
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    except BlockingIOError:
        pid = None
        try:
            pid = int(config.daemon_pidfile.read_text().strip())
        except Exception:
            pass
        pid_str = f" (pid {pid})" if pid else ""
        raise ContentionError(
            f"the aprilcam daemon appears to be running{pid_str} (lock held at "
            f"{lock_path}). Stop it first: aprilcam daemon stop"
        )
    finally:
        os.close(fd)


def check_camera_not_busy(index: int, device_name: str) -> None:
    """Refuse to open *index* if another process is already holding it."""
    diag = camutil.diagnose_camera_failure(index)
    if diag.get("exists") is False:
        raise ContentionError(f"camera index {index} ('{device_name}') is not present")
    blocking = diag.get("blocking_processes") or []
    if blocking:
        procs = ", ".join(f"{p.get('name', '?')}(pid {p.get('pid', '?')})" for p in blocking)
        raise ContentionError(
            f"'{device_name}' (index {index}) appears busy -- {procs}. "
            "Close whatever holds it (e.g. 'aprilcam daemon stop') and retry."
        )


# ---------------------------------------------------------------------------
# uvc-util helpers -- reuses the daemon's shell-out contract
# (daemon.camera_pipeline._apply_camera_settings), factored so each control
# apply/read reports success independently (needed to skip a variation on a
# partial failure rather than aborting the whole run).
# ---------------------------------------------------------------------------


def locate_uvc_util(config: Config) -> Path:
    """Find the ``uvc-util`` binary: ``<env_dir>/bin/uvc-util`` first, then PATH."""
    if config.env_dir:
        candidate = config.env_dir / "bin" / "uvc-util"
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("uvc-util")
    if found:
        return Path(found)
    raise RuntimeError("uvc-util not found (checked <env_dir>/bin and PATH)")


def _run_uvc(uvc_bin: Path, device_name: str, *args: str, timeout: float = 5.0):
    cmd = [str(uvc_bin), "-N", device_name, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def apply_control(uvc_bin: Path, device_name: str, name: str, value: str) -> bool:
    try:
        result = _run_uvc(uvc_bin, device_name, "-s", f"{name}={value}")
    except Exception as exc:
        log.warning("uvc-util error setting %s=%s on %s: %s", name, value, device_name, exc)
        return False
    if result.returncode != 0:
        log.warning(
            "uvc-util -s %s=%s on %s returned %d: %s",
            name, value, device_name, result.returncode, result.stderr.strip(),
        )
        return False
    return True


def apply_controls(uvc_bin: Path, device_name: str, controls: Dict[str, str]) -> List[str]:
    """Apply every control in *controls*. Returns the names that failed to apply."""
    failed: List[str] = []
    for name, value in controls.items():
        if not apply_control(uvc_bin, device_name, name, str(value)):
            failed.append(name)
    return failed


def read_control(uvc_bin: Path, device_name: str, name: str) -> Optional[str]:
    """Read a control's current value via ``-o`` (value only, no label)."""
    try:
        result = _run_uvc(uvc_bin, device_name, "-o", name)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    val = result.stdout.strip()
    return val or None


def read_control_range(uvc_bin: Path, device_name: str, name: str) -> Optional[Tuple[int, int]]:
    """(minimum, maximum) for a scalar integer control, parsed from ``-S``."""
    try:
        result = _run_uvc(uvc_bin, device_name, "-S", name)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout
    mn = _MIN_VALUE_RE.search(text)
    mx = _MAX_VALUE_RE.search(text)
    if not mn or not mx:
        return None
    return int(mn.group(1)), int(mx.group(1))


def list_supported_controls(uvc_bin: Path, device_name: str) -> List[str]:
    """Control names reported by ``uvc-util -N <name> -c`` for the live device."""
    try:
        result = _run_uvc(uvc_bin, device_name, "-c")
    except Exception:
        return []
    if result.returncode != 0:
        return []
    names: List[str] = []
    started = False
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not started:
            if line.lower().startswith("uvc controls"):
                started = True
            continue
        names.append(line)
    return names


# ---------------------------------------------------------------------------
# Camera discovery -- config.json (baseline source of truth) matched against
# the LIVE device_name (never a hardcoded camera list, plan §7).
# ---------------------------------------------------------------------------


@dataclass
class ConfiguredCamera:
    config_dir: Path
    dir_name: str
    config: dict
    device_name: str
    live_index: int


def discover_target_cameras(config: Config, pattern: Optional[str]) -> List[ConfiguredCamera]:
    """Every camera in the config dir that has a ``playfield`` link AND is live.

    A config.json entry is only included when its ``device_name`` matches a
    currently-connected camera's live device_name -- this naturally skips
    stale/duplicate registry-adopted config dirs (e.g. a
    ``<slug>-<enum>`` dir left behind by a re-plug) without needing to
    special-case them, since their ``device_name`` field won't match
    anything currently enumerated.
    """
    live_cams = camutil.list_cameras(detailed_names=True)
    by_name: Dict[str, camutil.CameraInfo] = {}
    for c in live_cams:
        key = (c.device_name or c.name or "").strip().lower()
        if key and key not in by_name:
            by_name[key] = c

    cameras_root = config.effective_config_dir / "cameras"
    results: List[ConfiguredCamera] = []
    claimed_indices: set = set()
    if not cameras_root.is_dir():
        return results

    for cam_dir in sorted(p for p in cameras_root.iterdir() if p.is_dir()):
        cfg = load_camera_config(cam_dir)
        if not cfg or not cfg.get("playfield"):
            continue
        cfg_device_name = cfg.get("device_name") or cam_dir.name
        live = by_name.get(cfg_device_name.strip().lower())
        if live is None:
            log.info(
                "skipping %s: device_name %r is not currently connected",
                cam_dir.name, cfg_device_name,
            )
            continue
        if live.index in claimed_indices:
            log.warning(
                "skipping %s: live index %d already claimed by another config dir",
                cam_dir.name, live.index,
            )
            continue
        live_device_name = live.device_name or cfg_device_name
        if pattern:
            haystack = f"{live_device_name} {cam_dir.name}".lower()
            if pattern.strip().lower() not in haystack:
                continue
        claimed_indices.add(live.index)
        results.append(
            ConfiguredCamera(
                config_dir=cam_dir,
                dir_name=cam_dir.name,
                config=cfg,
                device_name=live_device_name,
                live_index=live.index,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Variation matrix
# ---------------------------------------------------------------------------


@dataclass
class Variation:
    name: str
    controls: Dict[str, str]
    settle_s: Optional[float] = None


def load_matrix_spec(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def select_matrix_entry(matrix_spec: dict, device_name: str) -> Optional[dict]:
    name_lower = device_name.lower()
    for entry in matrix_spec.get("cameras", []):
        if entry.get("match", "").lower() in name_lower:
            return entry
    return None


def _variations_from_matrix_entry(entry: dict) -> List[Variation]:
    out = []
    for v in entry.get("variations", []):
        out.append(
            Variation(
                name=v["name"],
                controls={k: str(val) for k, val in v.get("controls", {}).items()},
                settle_s=v.get("settle_s"),
            )
        )
    return out


def _fraction_value(rng: Tuple[int, int], fraction: float) -> str:
    lo, hi = rng
    return str(int(round(lo + fraction * (hi - lo))))


def build_fallback_variations(
    uvc_bin: Path, device_name: str, fb_spec: dict, supported: set,
) -> List[Variation]:
    """Generic exposure/gain/WB sweep built from live ``uvc-util -S`` ranges."""
    variations: List[Variation] = []

    exp_ctrl = fb_spec.get("exposure_control", "exposure-time-abs")
    gain_ctrl = fb_spec.get("gain_control", "gain")
    ae_ctrl = fb_spec.get("auto_exposure_control", "auto-exposure-mode")
    wb_ctrl = fb_spec.get("white_balance_control", "white-balance-temp")
    awb_ctrl = fb_spec.get("auto_white_balance_control", "auto-white-balance-temp")

    exp_range = read_control_range(uvc_bin, device_name, exp_ctrl) if exp_ctrl in supported else None
    gain_range = read_control_range(uvc_bin, device_name, gain_ctrl) if gain_ctrl in supported else None
    wb_range = read_control_range(uvc_bin, device_name, wb_ctrl) if wb_ctrl in supported else None

    if exp_range:
        for frac in fb_spec.get("exposure_fractions", []):
            val = _fraction_value(exp_range, frac)
            variations.append(Variation(name=f"exp-{val}", controls={exp_ctrl: val}))
    else:
        log.info("fallback: %s not supported/queryable on %s -- skipping exposure sweep", exp_ctrl, device_name)

    if gain_range:
        for frac in fb_spec.get("gain_fractions", []):
            val = _fraction_value(gain_range, frac)
            variations.append(Variation(name=f"gain-{val}", controls={gain_ctrl: val}))
    else:
        log.info("fallback: %s not supported/queryable on %s -- skipping gain sweep", gain_ctrl, device_name)

    if ae_ctrl in supported:
        baseline_ae = read_control(uvc_bin, device_name, ae_ctrl)
        for cand in fb_spec.get("auto_exposure_candidate_values", []):
            if cand != baseline_ae:
                variations.append(
                    Variation(
                        name=f"auto-exposure-{cand}",
                        controls={ae_ctrl: cand},
                        settle_s=fb_spec.get("auto_exposure_settle_s"),
                    )
                )
                break

    if wb_range:
        for frac in fb_spec.get("white_balance_fractions", []):
            val = _fraction_value(wb_range, frac)
            variations.append(Variation(name=f"wb-{val}", controls={wb_ctrl: val}))

    if awb_ctrl in supported:
        baseline_awb = read_control(uvc_bin, device_name, awb_ctrl)
        candidate = "true" if baseline_awb != "true" else "false"
        variations.append(
            Variation(
                name=f"auto-wb-{candidate}",
                controls={awb_ctrl: candidate},
                settle_s=fb_spec.get("auto_white_balance_settle_s"),
            )
        )

    if exp_range and gain_range:
        for combo in fb_spec.get("stress_combos", []):
            ev = _fraction_value(exp_range, combo["exposure_fraction"])
            gv = _fraction_value(gain_range, combo["gain_fraction"])
            variations.append(Variation(name=combo["name"], controls={exp_ctrl: ev, gain_ctrl: gv}))

    return variations


def snapshot_fallback_baseline(uvc_bin: Path, device_name: str, fb_spec: dict, supported: set) -> Dict[str, str]:
    """Snapshot the live value of every control the fallback sweep will touch."""
    ctrls = [
        fb_spec.get("exposure_control", "exposure-time-abs"),
        fb_spec.get("gain_control", "gain"),
        fb_spec.get("auto_exposure_control", "auto-exposure-mode"),
        fb_spec.get("white_balance_control", "white-balance-temp"),
        fb_spec.get("auto_white_balance_control", "auto-white-balance-temp"),
    ]
    baseline: Dict[str, str] = {}
    for ctrl in ctrls:
        if ctrl not in supported:
            continue
        val = read_control(uvc_bin, device_name, ctrl)
        if val is not None:
            baseline[ctrl] = val
    return baseline


def dedupe_against_baseline(variations: List[Variation], baseline: Dict[str, str]) -> List[Variation]:
    """Drop a variation whose every overridden value already equals baseline."""
    out = []
    for v in variations:
        if v.controls and all(baseline.get(k) == val for k, val in v.controls.items()):
            log.info("skipping %s: identical to baseline, would duplicate baseline.jpg", v.name)
            continue
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# Field state stub (plan §5)
# ---------------------------------------------------------------------------


def ensure_field_state_stub(output_root: Path) -> str:
    """Create ``tests/images/field-state.json`` if absent; return the current id."""
    path = output_root / "field-state.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            current = data.get("current")
            if current:
                return current
        except Exception:
            log.warning("field-state.json exists but could not be parsed; leaving it as-is")
    state_id = "unverified-" + datetime.now(timezone.utc).strftime("%Y%m%d")
    stub = {
        "current": state_id,
        "states": {
            state_id: {
                "status": "unverified",
                "description": (
                    "Stub generated by testdev/capture_matrix.py. The set of tags/"
                    "objects actually visible on the field at capture time has not "
                    "been confirmed by the stakeholder yet -- populate "
                    "visible_april_tags / visible_aruco_tags / visible_rectangles / "
                    "visible_dots (IDs from main-playfield.json) and flip status to "
                    "\"confirmed\" before wiring tests/test_image_matrix.py "
                    "assertions to this state."
                ),
                "visible_april_tags": [],
                "visible_aruco_tags": [],
                "visible_rectangles": [],
                "visible_dots": [],
            }
        },
    }
    if not path.exists():
        output_root.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stub, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        log.info("wrote stub field-state.json (id=%s)", state_id)
    return state_id


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _settle(cap: "cv.VideoCapture", seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        cap.read()


def _grab_frame(cap: "cv.VideoCapture", attempts: int = 5) -> Optional[np.ndarray]:
    frame = None
    for _ in range(attempts):
        ret, f = cap.read()
        if ret and f is not None:
            frame = f
    return frame


def _mean_luminance(frame_bgr: np.ndarray) -> float:
    gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
    return float(np.mean(gray))


@dataclass
class CameraRunResult:
    device_name: str
    dir_name: str
    error: Optional[str] = None
    captured: int = 0
    skipped_apply_failed: List[str] = field(default_factory=list)
    skipped_no_frame: List[str] = field(default_factory=list)
    dark_images: List[str] = field(default_factory=list)
    washed_images: List[str] = field(default_factory=list)
    baseline_detections: Optional[Dict[str, int]] = None
    baseline_luminance: Optional[float] = None
    restored: bool = False
    restore_mismatches: List[str] = field(default_factory=list)
    fixture_dir: Optional[str] = None
    wall_time_s: float = 0.0


def run_camera(
    cam: ConfiguredCamera,
    config: Config,
    uvc_bin: Path,
    matrix_spec: dict,
    output_root: Path,
    field_state_id: str,
    settle_override: Optional[float],
    detector: TagDetector,
    registry: CameraRegistry,
) -> CameraRunResult:
    result = CameraRunResult(device_name=cam.device_name, dir_name=cam.dir_name)
    t0 = time.monotonic()

    try:
        check_camera_not_busy(cam.live_index, cam.device_name)
    except ContentionError as exc:
        result.error = str(exc)
        return result

    # -- identify (plan §4.2): registry lookup is best-effort/informational
    # only -- this tool never calls registry.resolve()/upsert(), so it never
    # mutates the live registry.json as a side effect of an offline capture.
    unique_id = None
    try:
        w_cfg, h_cfg = (cam.config.get("resolution") or [None, None])[:2]
        ident = camera_identity.resolve_identity(
            cam.live_index, name=cam.device_name, width=w_cfg, height=h_cfg
        )
        unique_id = ident.unique_id
    except Exception:
        log.debug("identity resolution failed for %s", cam.device_name, exc_info=True)
    registry_record = registry.get(unique_id) if unique_id else None

    fixture_slug = dir_slug(cam.device_name)
    fixture_dir = output_root / fixture_slug
    result.fixture_dir = fixture_slug

    settings_block = cam.config.get("settings") or {}

    cap = cv.VideoCapture(cam.live_index)
    if not cap.isOpened():
        cap.release()
        result.error = (
            f"cv2.VideoCapture({cam.live_index}) could not open '{cam.device_name}'. "
            "If this reports 'not authorized to capture video', it is a macOS TCC "
            "camera-permission denial for the process running this script (check "
            "System Settings > Privacy & Security > Camera), not a device-busy or "
            "code problem -- 'uvc-util -d' enumeration succeeding does not imply "
            "capture access is granted."
        )
        return result

    # Only create the fixture directory once the camera is actually open, so
    # a permission/open failure never leaves an empty directory behind.
    fixture_dir.mkdir(parents=True, exist_ok=True)

    try:
        for _ in range(5):  # let AVFoundation finish initialising, per daemon convention
            cap.read()

        if settings_block.get("controls"):
            baseline_controls: Dict[str, str] = {
                k: str(v) for k, v in settings_block["controls"].items()
            }
            supported = set(list_supported_controls(uvc_bin, cam.device_name))
        else:
            supported = set(list_supported_controls(uvc_bin, cam.device_name))
            fb_spec = matrix_spec.get("fallback", {})
            baseline_controls = snapshot_fallback_baseline(uvc_bin, cam.device_name, fb_spec, supported)

        default_settle = matrix_spec.get("settle_s", 1.5)

        try:
            # Establish a known-good starting point before doing anything else.
            apply_controls(uvc_bin, cam.device_name, baseline_controls)
            _settle(cap, settle_override if settle_override is not None else default_settle)

            manifest_images = []

            def _capture_one(name: str, effective_controls: Dict[str, str], settle_s: float) -> bool:
                frame = _grab_frame(cap)
                if frame is None:
                    log.warning("no frame available for %s/%s", cam.device_name, name)
                    result.skipped_no_frame.append(name)
                    return False
                lum = _mean_luminance(frame)
                dark = lum < DARK_LUMINANCE_FLOOR
                washed = lum > WASHED_LUMINANCE_CEIL
                if dark:
                    result.dark_images.append(name)
                if washed:
                    result.washed_images.append(name)
                out_path = fixture_dir / f"{name}.jpg"
                cv.imwrite(str(out_path), frame, [cv.IMWRITE_JPEG_QUALITY, 90])
                manifest_images.append(
                    {
                        "name": name,
                        "file": out_path.name,
                        "controls": effective_controls,
                        "settle_s": settle_s,
                        "mean_luminance": round(lum, 2),
                        "dark": dark,
                        "washed": washed,
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                result.captured += 1
                if name == "baseline":
                    result.baseline_luminance = lum
                    dets = detector.detect(frame)
                    by_family: Dict[str, int] = {}
                    for d in dets:
                        by_family[d.family] = by_family.get(d.family, 0) + 1
                    result.baseline_detections = by_family
                return True

            # -- baseline image --
            _capture_one("baseline", dict(baseline_controls), settle_override or default_settle)

            # -- select + build variations --
            matrix_entry = select_matrix_entry(matrix_spec, cam.device_name)
            if matrix_entry is not None:
                log.info("%s: using matrix entry match=%r", cam.device_name, matrix_entry.get("match"))
                variations = _variations_from_matrix_entry(matrix_entry)
            else:
                log.info("%s: no matrix entry matched -- using generic fallback sweep", cam.device_name)
                fb_spec = matrix_spec.get("fallback", {})
                variations = build_fallback_variations(uvc_bin, cam.device_name, fb_spec, supported)
            variations = dedupe_against_baseline(variations, baseline_controls)

            settle_auto = matrix_spec.get("settle_s_auto", 3.0)

            for var in variations:
                # Restore full baseline before every variation so dimensions
                # never bleed into each other (plan §4.4a).
                apply_controls(uvc_bin, cam.device_name, baseline_controls)
                failed = apply_controls(uvc_bin, cam.device_name, var.controls)
                if failed:
                    log.warning(
                        "skipping variation %s on %s: control(s) failed to apply: %s",
                        var.name, cam.device_name, ", ".join(failed),
                    )
                    result.skipped_apply_failed.append(var.name)
                    continue
                settle_s = (
                    settle_override
                    if settle_override is not None
                    else (var.settle_s if var.settle_s is not None else default_settle)
                )
                # If this variation touches an "auto" control, give it longer
                # to converge unless the caller/YAML already specified a settle.
                if var.settle_s is None and settle_override is None:
                    if any("auto" in k for k in var.controls):
                        settle_s = settle_auto
                _settle(cap, settle_s)
                effective = {**baseline_controls, **var.controls}
                _capture_one(var.name, effective, settle_s)

            # -- manifest --
            manifest = {
                "camera": {
                    "device_name": cam.device_name,
                    "config_dir": cam.dir_name,
                    "unique_id": unique_id,
                    "registry_enum": registry_record.enum if registry_record else None,
                    "resolution": cam.config.get("resolution"),
                },
                "playfield": cam.config.get("playfield"),
                "package_version": _package_version(),
                "daemon_version": _package_version(),
                "field_state_id": field_state_id,
                "matrix_source": "matrix" if matrix_entry is not None else "fallback",
                "baseline_controls": baseline_controls,
                "capture_started_at": manifest_images[0]["captured_at"] if manifest_images else None,
                "capture_finished_at": datetime.now(timezone.utc).isoformat(),
                "images": manifest_images,
                "skipped_variations": {
                    "apply_failed": result.skipped_apply_failed,
                    "no_frame": result.skipped_no_frame,
                },
            }
            (fixture_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

        finally:
            # ALWAYS restore -- crashed run must not leave the playfield
            # camera misconfigured (plan §4.5).
            apply_controls(uvc_bin, cam.device_name, baseline_controls)
            mismatches = []
            for ctrl, expected in baseline_controls.items():
                actual = read_control(uvc_bin, cam.device_name, ctrl)
                if actual is not None and actual != expected:
                    mismatches.append(f"{ctrl}: expected {expected!r}, read back {actual!r}")
            result.restored = True
            result.restore_mismatches = mismatches
            if mismatches:
                log.warning("restore verification mismatches for %s: %s", cam.device_name, mismatches)
            else:
                log.info("restore verified for %s", cam.device_name)
    finally:
        cap.release()
        result.wall_time_s = time.monotonic() - t0

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="capture_matrix",
        description="Capture a camera image variation matrix for offline pipeline test fixtures.",
    )
    p.add_argument(
        "camera",
        nargs="?",
        default=None,
        help="Device-name / config-dir substring pattern to select one camera. "
        "Omit (or pass --all) to capture every configured camera with a playfield link.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Capture every configured camera with a playfield link (default when no pattern is given).",
    )
    p.add_argument("--output-dir", default=None, help="Fixture output root (default: tests/images).")
    p.add_argument("--matrix-spec", default=None, help="Path to capture_matrix.yaml (default: alongside this script).")
    p.add_argument("--settle", type=float, default=None, help="Override the per-variation settle time (seconds).")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return p.parse_args(argv)


def _print_summary(results: List[CameraRunResult]) -> None:
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for r in results:
        print(f"\n{r.device_name} (config dir: {r.dir_name})")
        if r.error:
            print(f"  ERROR: {r.error}")
            continue
        print(f"  fixture dir:       tests/images/{r.fixture_dir}/")
        print(f"  images captured:   {r.captured}")
        print(f"  skipped (apply):   {r.skipped_apply_failed or 'none'}")
        print(f"  skipped (no frame):{r.skipped_no_frame or ' none'}")
        print(f"  dark images:       {r.dark_images or 'none'}")
        print(f"  washed images:     {r.washed_images or 'none'}")
        if r.baseline_detections is not None:
            total = sum(r.baseline_detections.values())
            print(f"  baseline detections: {total} total {dict(r.baseline_detections)} (luminance={r.baseline_luminance:.1f})")
        print(f"  baseline restored: {r.restored} mismatches={r.restore_mismatches or 'none'}")
        print(f"  wall time:         {r.wall_time_s:.1f}s")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    config = Config.load()

    try:
        check_daemon_not_running(config)
    except ContentionError as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2

    pattern = None if args.all else args.camera
    cams = discover_target_cameras(config, pattern)
    if not cams:
        print(
            "No configured, currently-connected camera with a playfield link "
            f"matched (pattern={pattern!r}).",
            file=sys.stderr,
        )
        return 1

    output_root = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT
    output_root.mkdir(parents=True, exist_ok=True)
    field_state_id = ensure_field_state_stub(output_root)

    matrix_spec_path = Path(args.matrix_spec) if args.matrix_spec else DEFAULT_MATRIX_SPEC
    matrix_spec = load_matrix_spec(matrix_spec_path)
    uvc_bin = locate_uvc_util(config)
    registry = CameraRegistry(config.cameras_dir)
    detector = TagDetector(DetectorConfig(family="all", detect_aruco_4x4=True))

    print(f"Targeting {len(cams)} camera(s):")
    for cam in cams:
        print(f"  - {cam.device_name}  (config dir: {cam.dir_name}, live index {cam.live_index})")

    results: List[CameraRunResult] = []
    for cam in cams:
        print(f"\n=== capturing {cam.device_name} ===")
        result = run_camera(
            cam, config, uvc_bin, matrix_spec, output_root, field_state_id,
            args.settle, detector, registry,
        )
        results.append(result)
        if result.error:
            print(f"  ERROR: {result.error}")
        else:
            print(f"  captured {result.captured} image(s) -> tests/images/{result.fixture_dir}/")

    _print_summary(results)
    return 0 if all(r.error is None for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
