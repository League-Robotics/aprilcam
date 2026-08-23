#!/usr/bin/env python3
"""Capture a per-camera image variation matrix for offline pipeline test fixtures.

See docs/test-plans/camera-image-matrix.md (§3/§4) for the full design.

For a given camera this tool:
  1. Verifies the daemon is not holding its machine-global lock (refuses on
     contention).
  2. Identifies the camera (registry lookup against the LIVE hardware) and
     reads its own baseline hardware controls -- NEVER a hardcoded baseline.
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

Ported onto the v2 surface (sprint 031 ticket 007). Five v1-era imports were
missing/renamed after the v2 rebuild (``aprilcam.camera.camutil``,
``aprilcam.camera.camera_config``, ``aprilcam.camera.registry.dir_slug``,
``aprilcam.config.Config``, ``aprilcam.core.detector``); this tool's contract
is unchanged, but it is now wired to their v2 replacements:
``camera/identity.py`` (``resolve_all`` -- live enumeration) +
``camera/registry.py`` (``CameraRegistry`` -- persisted identity -> slug,
read-only from this tool's side) for identity/slugs,
``camera/backends/opencv_backend.py`` (``OpenCVBackend``) for capture,
``config/directories.py`` (``Directories``) for roots, and
``vision/detector.py`` (``TagDetector``/``DetectorConfig``) for the baseline
image's informational detection summary.

**Design decision -- direct-open, not daemon RPC** (recorded in sprint 031's
``sprint.md`` Architecture section; this module implements it). This tool
opens cameras directly rather than routing through the daemon's gRPC
control plane. ``SetCameraConfig``/``GetFrame`` are whole-document
push/single-frame-read RPCs with no shape for this tool's actual operation
-- "apply one raw UVC control in isolation, capture, restore baseline,
repeat per matrix row." Building that RPC shape into the daemon's config
service would add an RPC surface serving exactly one caller (this offline
tool) and would also make the tool depend on a running daemon, breaking its
documented ability to run standalone -- before a daemon exists, or while one
is down for maintenance -- which is exactly the sanctioned exception
``docs/design/design.md`` §8 carves out for offline test tooling ("it opens
cameras directly with the daemon's own ``camera/`` code... it refuses to run
when the daemon holds a device"). Direct-open is not a stopgap here; it is
the right shape for what this tool does. The daemon-contention refusal below
(``check_daemon_not_running``) is what keeps this compatible with that
exception rather than fighting a live daemon for the hardware.

**Config-schema note (v1 -> v2).** v1's on-disk camera config stored a raw
``settings.controls`` block keyed by literal ``uvc-util`` control names
(``exposure-time-abs``, ...), which this tool used to trust as the baseline
when present. v2's self-naming ``CameraConfig`` (``config/interfaces.py``)
has no such block at all -- ``settings`` is keyed by ``Camera.apply_config``'s
small abstracted vocabulary (``exposure``, ``gain``, ...) instead, a
different vocabulary this tool's raw-per-control sweep cannot use directly.
So this tool now *always* reads the true, current hardware state as its
baseline rather than ever consulting the on-disk document for it -- a
strictly stronger reading of its own pre-existing "read its own baseline
hardware controls -- NEVER a hardcoded baseline" contract, not a weaker one.

**No v2 counterpart for v1's per-camera busy diagnosis.** v1's
``camutil.diagnose_camera_failure`` (an OS-level "who is holding this
device" probe) has no v2 replacement -- no module in ``camera/`` does that
diagnosis; even ``camera/discovery.py``'s own ``probe()`` only learns
"unopenable" from a raised exception, the same way the rest of v2 does. This
tool now relies on the same signal: an ``OpenCVBackend.open()`` failure
(after the daemon-contention check above has already ruled out the one
cause this tool can name with confidence) is reported with the honest
diagnosis available -- a macOS TCC permission denial, an absent device, or
some other non-daemon process holding it -- rather than a precise
process-table answer v2 has nothing to offer.

Usage:
    python testdev/capture_matrix.py                 # every configured camera
    python testdev/capture_matrix.py --all
    python testdev/capture_matrix.py arducam          # one camera, by pattern
    python testdev/capture_matrix.py --dry-run        # print the plan only
    python testdev/capture_matrix.py hd-usb --field-state-id fs-20260823
        # tag manifests with an explicit field-state id and write fixtures to
        # tests/images/fs-20260823/<slug>/ instead of tests/images/<slug>/,
        # so this generation coexists with an older one (sprint 035 ticket 003).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

import cv2 as cv
import numpy as np
import yaml

from aprilcam.camera.backends.opencv_backend import OpenCVBackend
from aprilcam.camera.identity import CameraIdentity, resolve_all
from aprilcam.camera.registry import CameraRegistry
from aprilcam.client.discovery import probe_lock_holder
from aprilcam.config.directories import Directories
from aprilcam.config.interfaces import CameraConfig
from aprilcam.config.loader import ConfigLoader
from aprilcam.daemon.lifecycle import LOCK_FILENAME
from aprilcam.errors import MalformedDocument
from aprilcam.vision.detector import DetectorConfig, TagDetector

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
# Contention check -- refuse rather than fight the daemon (design.md §8's
# sanctioned direct-open exception for offline test tooling).
# ---------------------------------------------------------------------------


class ContentionError(RuntimeError):
    """Raised when the daemon is holding its machine-global lock."""


def check_daemon_not_running(directories: Directories) -> None:
    """Refuse to proceed if the aprilcam daemon holds its machine-global lock.

    Uses ``client.discovery.probe_lock_holder`` -- the same non-blocking
    flock read ``client/lifecycle.py``'s own pre-flight ``start()`` probe
    uses -- against ``directories.socket_dir / daemon.lifecycle.LOCK_FILENAME``,
    exactly the lock file ``daemon/lifecycle.py``'s ``DaemonLifecycle.
    _acquire_lock`` takes. A non-``None`` result means a v2 daemon is alive
    right now; ``probe_lock_holder`` itself already reports "nothing running"
    for a missing/never-locked lock file, so no separate existence check is
    needed here.
    """
    lock_path = directories.socket_dir / LOCK_FILENAME
    try:
        holder = probe_lock_holder(lock_path)
    except OSError as exc:
        log.warning(
            "could not probe daemon lock at %s: %s -- proceeding without this check",
            lock_path, exc,
        )
        return
    if holder is not None:
        raise ContentionError(
            f"the aprilcam daemon appears to be running ({holder}, lock held at "
            f"{lock_path}). Stop it first: aprilcam daemon stop"
        )


# ---------------------------------------------------------------------------
# uvc-util helpers -- raw per-control shelling. Mirrors the subprocess
# conventions ``camera/backends/uvc_util.py``'s ``UvcUtilBackend`` uses
# (always an argument list, never a shell string; a bounded timeout; never
# raises for an ordinary failure) -- but that class only exposes a fixed
# five-key vocabulary (``exposure``, ``gain``, ...) via ``get_property``/
# ``set_property``, not arbitrary raw ``uvc-util`` control names. This
# tool's fallback sweep needs whatever control names ``uvc-util -c`` reports
# live for a given camera, so it keeps its own thin, raw-control-name
# shelling here rather than routing through that class.
# ---------------------------------------------------------------------------


def locate_uvc_util() -> Path:
    """Find the ``uvc-util`` binary on ``PATH``.

    Mirrors ``camera.backends.uvc_util.UvcUtilBackend``'s own resolution
    convention exactly (``shutil.which("uvc-util")``, never a hardcoded
    path). v2 dropped v1's project-local ``<env_dir>/bin`` search tier along
    with the rest of the old ``aprilcam.config.Config`` object, so this tool
    now resolves the binary the same way the daemon's own control backend
    does.
    """
    found = shutil.which("uvc-util")
    if found:
        return Path(found)
    raise RuntimeError(
        "uvc-util not found on PATH (required for exposure/gain/white-balance "
        "control -- camera.backends.uvc_util.UvcUtilBackend needs the same "
        "binary). Install uvc-util and ensure it is on PATH."
    )


def _run_uvc(uvc_bin: Path, device_name: str, *args: str, timeout: float = 5.0):
    cmd = [str(uvc_bin), "-N", device_name, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


#: Raw uvc-util control names this tool's own vocabulary gates on, mapped
#: to the (gate control name, manual value) that must be pushed before a
#: value on the left will reliably stick. Mirrors ``camera.camera``'s
#: ``_GATE_KEYS``-first convention (``camera/DESIGN.md`` §3: "Auto-exposure
#: must be pushed manual before an exposure value sticks; auto-white-balance
#: off before a WB temperature") -- but that module's ``_GATE_KEYS`` is
#: keyed by ``Camera.apply_config``'s abstracted vocabulary (``exposure``,
#: ``auto_exposure``, ...), not the raw uvc-util control names this tool
#: drives hardware with directly (module docstring's "Config-schema note").
#: Confirmed live: HD USB CAMERA's July run rejected every
#: ``exposure-time-abs``/``white-balance-temp`` variation because its own
#: LIVE baseline had ``auto-exposure-mode``/``auto-white-balance-temp``
#: themselves set to "auto" -- uvc-util rejects a manual value while the
#: gating auto mode is active. See ``tests/images/hd-usb-camera/manifest.json``'s
#: ``skipped_variations.apply_failed`` for the recorded evidence (every one
#: of ``exp-*``, ``wb-*``, and the two ``stress-*`` combos -- all of which
#: touch exposure and/or white balance -- failed; every ``gain-*`` variation,
#: ungated, succeeded).
_GATE_FOR_VALUE_CONTROL: Dict[str, Tuple[str, str]] = {
    "exposure-time-abs": ("auto-exposure-mode", "1"),
    "white-balance-temp": ("auto-white-balance-temp", "false"),
}

#: Every gate control name from the map above, for ordering purposes.
_GATE_CONTROLS: FrozenSet[str] = frozenset(gate for gate, _ in _GATE_FOR_VALUE_CONTROL.values())


def _gate_first_order(controls: Dict[str, str]) -> Dict[str, str]:
    """Reorder *controls* so any ``_GATE_CONTROLS`` member is pushed first,
    deterministically -- mirrors ``camera.camera.Camera.apply_config``'s own
    ``_GATE_KEYS``-first ordering, which this tool does not reuse (it drives
    hardware directly) but should mirror per that module's docstring.

    Without this, ``controls`` dicts built from a ``set`` union (e.g.
    ``snapshot_baseline``'s ``default_names | variation_names``) iterate in
    Python's hash-randomized order -- nondeterministic per-process -- so a
    gate control could easily land *after* the value control it gates,
    silently reproducing the HD-cam July bug on an unlucky run. Relative
    order among non-gate keys is preserved (stable sort).
    """
    gate_items = [(k, v) for k, v in controls.items() if k in _GATE_CONTROLS]
    rest_items = [(k, v) for k, v in controls.items() if k not in _GATE_CONTROLS]
    return dict(gate_items + rest_items)


def _ensure_gate_overrides(
    controls: Dict[str, str], supported: Optional[Set[str]] = None,
) -> Dict[str, str]:
    """Force a value control's gate to manual when the value is present but
    the gate is not already explicit in *controls*.

    Applied to each *variation*'s own control set, never to the baseline
    (which always reflects the true live hardware state -- the module
    docstring's "NEVER a hardcoded baseline" contract; only what this tool
    actively varies gets this override). A variation that already names the
    gate explicitly (e.g. the auto-exposure toggle test itself) is left
    untouched -- this fills a gap, it never overrides stated intent.

    *supported* (a camera's live ``uvc-util -c`` control names, when known)
    guards against injecting a control name a given camera does not even
    expose -- pushing an unsupported control would fail and wrongly mark an
    otherwise-fine variation as ``skipped_apply_failed``.
    """
    out = dict(controls)
    for value_ctrl, (gate_ctrl, manual_value) in _GATE_FOR_VALUE_CONTROL.items():
        if value_ctrl not in out or gate_ctrl in out:
            continue
        if supported is not None and gate_ctrl not in supported:
            continue
        out[gate_ctrl] = manual_value
    return _gate_first_order(out)


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
    """Apply every control in *controls*, gate controls first. Returns the
    names that failed to apply.

    Ordering is unconditional and applies to every caller -- baseline
    restore/apply as well as a single variation's overlay -- via
    :func:`_gate_first_order`, mirroring ``camera.camera.Camera.
    apply_config``'s own ``_GATE_KEYS``-first convention (``camera/
    DESIGN.md`` §3).
    """
    failed: List[str] = []
    for name, value in _gate_first_order(controls).items():
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
# Camera discovery -- registry-mediated identity match against the LIVE
# hardware (never a hardcoded camera list).
# ---------------------------------------------------------------------------


@dataclass
class ConfiguredCamera:
    slug: str
    config_dir: Path
    config: CameraConfig
    device_name: str
    live_index: int
    hardware_key: str
    registry_number: Optional[int]


def discover_target_cameras(
    directories: Directories, registry: CameraRegistry, pattern: Optional[str]
) -> List[ConfiguredCamera]:
    """Every camera dir under ``config_dir/cameras/`` that (a) has a
    ``playfield`` link and (b) is currently connected.

    v1 matched a config's on-disk ``device_name`` field against a live
    enumeration's device name directly. v2's self-naming ``CameraConfig``
    (``config/interfaces.py``) carries no ``device_name`` field at all --
    the config directory name *is* the slug, and a bare slug is not itself a
    stable hardware match key (two identical camera models can propose the
    same slug prefix and get disambiguated by the registry). So this now
    goes through the registry instead, mirroring ``camera/discovery.py``'s
    own two-step shape: resolve each live identity, then reconcile against
    what is configured.

    For each ``cameras/<slug>/config.json`` with a ``playfield`` link: look
    up the *registry* record for that slug -- **read-only**, this function
    never calls ``registry.resolve()``/mutates ``registry.json`` as a side
    effect of an offline capture, matching the tool's pre-existing
    contract -- then check whether that record's ``hardware_key`` is among
    the identities enumerated *right now*. A slug with no registry record
    (never seen by ``camera/discovery.py``'s ``probe()``/``watch()``) or
    whose hardware is not currently connected is skipped, with a log line
    naming why.
    """
    identities = resolve_all()
    by_hardware_key: Dict[str, Tuple[int, CameraIdentity]] = {
        identity.hardware_key: (index, identity) for index, identity in identities.items()
    }
    by_slug = {record.slug: record for record in registry.records()}

    cameras_root = directories.config_dir / "cameras"
    results: List[ConfiguredCamera] = []
    claimed_indices: set = set()
    if not cameras_root.is_dir():
        return results

    loader = ConfigLoader()
    for cam_dir in sorted(p for p in cameras_root.iterdir() if p.is_dir()):
        config_path = cam_dir / "config.json"
        if not config_path.exists():
            continue
        try:
            cfg = loader.load_camera_config(config_path)
        except MalformedDocument as exc:
            log.warning("skipping %s: %s", cam_dir.name, exc)
            continue
        if not cfg.playfield:
            continue

        slug = cam_dir.name
        record = by_slug.get(slug)
        if record is None:
            log.info(
                "skipping %s: no registry record for this slug -- this camera "
                "has never been seen by camera discovery (probe/watch)",
                slug,
            )
            continue
        live = by_hardware_key.get(record.hardware_key)
        if live is None:
            log.info(
                "skipping %s: not currently connected (hardware_key %s not enumerated)",
                slug, record.hardware_key,
            )
            continue
        live_index, identity = live
        if live_index in claimed_indices:
            log.warning(
                "skipping %s: live index %d already claimed by another config dir",
                slug, live_index,
            )
            continue
        if pattern:
            haystack = f"{identity.device_name} {slug}".lower()
            if pattern.strip().lower() not in haystack:
                continue
        claimed_indices.add(live_index)
        results.append(
            ConfiguredCamera(
                slug=slug,
                config_dir=cam_dir,
                config=cfg,
                device_name=identity.device_name,
                live_index=live_index,
                hardware_key=record.hardware_key,
                registry_number=record.number,
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


def snapshot_baseline(
    uvc_bin: Path, device_name: str, control_names: Set[str], supported: Set[str],
) -> Dict[str, str]:
    """Snapshot the live value of every name in *control_names* that
    *supported* confirms this device actually exposes.

    v2's on-disk ``CameraConfig.settings`` carries no raw-uvc-util-keyed
    baseline block at all (see the module docstring's "Config-schema note")
    -- this tool therefore always reads the true, current hardware state as
    its baseline, never the on-disk document, for every camera regardless of
    whether a ``capture_matrix.yaml`` entry matched it or the generic
    fallback sweep applies.
    """
    baseline: Dict[str, str] = {}
    for ctrl in control_names:
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


def ensure_field_state_stub(output_root: Path, field_state_id: Optional[str] = None) -> str:
    """Create/update ``tests/images/field-state.json`` -- always at the
    fixture root, never per-camera, so every camera's manifest in a run can
    reference the SAME field-state id. Returns the id to stamp into
    manifests.

    With an explicit *field_state_id* (``--field-state-id``): ensures a
    ``"states"`` entry exists for it (a stub, if none is already there -- an
    operator session fills in the real ``visible_*``/``status`` afterward,
    per ``docs/test-plans/camera-image-matrix.md`` §5) and sets it as
    ``"current"``. An existing entry for that id is left completely
    untouched -- this never clobbers a stakeholder-confirmed entry.

    With no *field_state_id*: unchanged legacy behavior -- returns the
    existing ``"current"`` id if the file already exists and parses,
    otherwise generates a fresh ``"unverified-<date>"`` stub id and writes
    it.
    """
    path = output_root / "field-state.json"
    if field_state_id:
        data: dict = {"current": None, "states": {}}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                log.warning(
                    "field-state.json exists but could not be parsed; "
                    "rewriting its top-level structure (existing 'states' "
                    "entries for other ids are lost in this case)"
                )
                data = {"current": None, "states": {}}
        data.setdefault("states", {})
        if field_state_id not in data["states"]:
            data["states"][field_state_id] = {
                "status": "unverified",
                "description": (
                    "Stub generated by testdev/capture_matrix.py "
                    f"--field-state-id {field_state_id}. The set of tags/"
                    "objects actually visible on the field at capture time "
                    "has not been confirmed by the stakeholder yet -- "
                    "populate visible_april_tags / visible_aruco_tags / "
                    "visible_rectangles / visible_dots (IDs from "
                    "main-playfield.json) and flip status to \"confirmed\" "
                    "before wiring image-matrix test assertions to this "
                    "state."
                ),
                "visible_april_tags": [],
                "visible_aruco_tags": [],
                "visible_rectangles": [],
                "visible_dots": [],
            }
        else:
            log.info(
                "field-state.json already has a '%s' entry -- leaving it untouched",
                field_state_id,
            )
        data["current"] = field_state_id
        output_root.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        log.info("field-state.json: current=%s (%s)", field_state_id, path)
        return field_state_id

    # -- legacy path: no --field-state-id given, behavior unchanged --
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


def resolve_fixture_dir(output_root: Path, explicit_field_state_id: Optional[str], slug: str) -> Path:
    """Where one camera's fixtures for this run are written.

    With an explicit ``--field-state-id``, fixtures nest under a
    per-generation directory (``tests/images/<field-state-id>/<slug>/``) so
    a new generation coexists with an older one instead of overwriting it
    in place (the July corpus stays put when a later ``fs-20260823`` run
    happens). With no ``--field-state-id`` (the default/legacy path), the
    fixture directory is unchanged: ``tests/images/<slug>/`` directly under
    the fixture root -- existing camera entries keep their existing layout
    with no flags.
    """
    if explicit_field_state_id:
        return output_root / explicit_field_state_id / slug
    return output_root / slug


def pin_calibration_snapshot(directories: Directories, cam_slug: str, fixture_dir: Path) -> Optional[str]:
    """Copy the camera's active ``calibration.json`` (read-only -- copy
    only, the source is never touched) beside its fixtures and return its
    sha256 hex digest, or ``None`` if this camera has no calibration yet.

    ``docs/test-plans/camera-image-matrix.md`` §6.2: L0's world-accuracy
    tests must not depend on live machine state, so whatever calibration a
    camera held at capture time is pinned alongside its images and its
    hash recorded in the manifest -- a later drift/recalibration on the
    real machine can never silently invalidate what a committed fixture
    generation's tests assert against.

    Source path mirrors ``daemon/service.py``'s own convention exactly:
    ``<state_dir>/cameras/<slug>/calibration.json``.
    """
    src = directories.state_dir / "cameras" / cam_slug / "calibration.json"
    if not src.is_file():
        log.warning(
            "%s: no calibration.json at %s -- capturing without a pinned "
            "calibration snapshot (this camera has not been calibrated yet; "
            "world-accuracy tests against this fixture generation will skip "
            "until it is calibrated and recaptured)",
            cam_slug, src,
        )
        return None
    data = src.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    dest = fixture_dir / "calibration.json"
    dest.write_bytes(data)
    log.info("%s: pinned calibration.json snapshot (sha256=%s) -> %s", cam_slug, digest, dest)
    return digest


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _settle(backend: OpenCVBackend, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        backend.read()


def _grab_frame(backend: OpenCVBackend, attempts: int = 5) -> Optional[np.ndarray]:
    frame = None
    for _ in range(attempts):
        f = backend.read()
        if f is not None:
            frame = f
    return frame


def _mean_luminance(gray: np.ndarray) -> float:
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
    uvc_bin: Path,
    matrix_spec: dict,
    output_root: Path,
    field_state_id: str,
    settle_override: Optional[float],
    detector: TagDetector,
    directories: Directories,
    explicit_field_state_id: Optional[str] = None,
) -> CameraRunResult:
    result = CameraRunResult(device_name=cam.device_name, dir_name=cam.slug)
    t0 = time.monotonic()

    fixture_dir = resolve_fixture_dir(output_root, explicit_field_state_id, cam.slug)
    result.fixture_dir = str(fixture_dir.relative_to(output_root))

    backend = OpenCVBackend(cam.live_index)
    try:
        backend.open()
    except Exception as exc:
        result.error = (
            f"OpenCVBackend(index={cam.live_index}) could not open '{cam.device_name}': {exc}. "
            "If this reports 'not authorized to capture video', it is a macOS TCC "
            "camera-permission denial for the process running this script (check "
            "System Settings > Privacy & Security > Camera). It can also mean the "
            "device is physically absent, or held by some other, non-daemon process "
            "-- this tool already refused above if the aprilcam daemon itself holds "
            "it, and v2 has no per-process busy diagnosis to say more than that."
        )
        return result

    # Only create the fixture directory once the camera is actually open, so
    # a permission/open failure never leaves an empty directory behind.
    fixture_dir.mkdir(parents=True, exist_ok=True)

    try:
        for _ in range(5):  # let AVFoundation finish initialising, per daemon convention
            backend.read()

        resolution = backend.frame_size()

        supported = set(list_supported_controls(uvc_bin, cam.device_name))
        matrix_entry = select_matrix_entry(matrix_spec, cam.device_name)
        fb_spec = matrix_spec.get("fallback", {})

        # Baseline must cover every control name any variation this run
        # might touch, whether that variation comes from a matched
        # `cameras:` entry or the generic fallback sweep -- see
        # snapshot_baseline's docstring for why this is always a live
        # hardware read now, never the on-disk config.
        default_names = {
            fb_spec.get("exposure_control", "exposure-time-abs"),
            fb_spec.get("gain_control", "gain"),
            fb_spec.get("auto_exposure_control", "auto-exposure-mode"),
            fb_spec.get("white_balance_control", "white-balance-temp"),
            fb_spec.get("auto_white_balance_control", "auto-white-balance-temp"),
        }
        variation_names: Set[str] = set()
        if matrix_entry is not None:
            for v in matrix_entry.get("variations", []):
                variation_names.update(v.get("controls", {}).keys())
        baseline_controls = snapshot_baseline(
            uvc_bin, cam.device_name, default_names | variation_names, supported
        )

        default_settle = matrix_spec.get("settle_s", 1.5)

        try:
            # Establish a known-good starting point before doing anything else.
            apply_controls(uvc_bin, cam.device_name, baseline_controls)
            _settle(backend, settle_override if settle_override is not None else default_settle)

            manifest_images = []

            def _capture_one(name: str, effective_controls: Dict[str, str], settle_s: float) -> bool:
                frame = _grab_frame(backend)
                if frame is None:
                    log.warning("no frame available for %s/%s", cam.device_name, name)
                    result.skipped_no_frame.append(name)
                    return False
                gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
                lum = _mean_luminance(gray)
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
                    dets = detector.detect(gray)
                    by_family: Dict[str, int] = {}
                    for d in dets:
                        by_family[d.family.value] = by_family.get(d.family.value, 0) + 1
                    result.baseline_detections = by_family
                return True

            # -- baseline image --
            _capture_one("baseline", dict(baseline_controls), settle_override or default_settle)

            # -- select + build variations --
            if matrix_entry is not None:
                log.info("%s: using matrix entry match=%r", cam.device_name, matrix_entry.get("match"))
                variations = _variations_from_matrix_entry(matrix_entry)
            else:
                log.info("%s: no matrix entry matched -- using generic fallback sweep", cam.device_name)
                variations = build_fallback_variations(uvc_bin, cam.device_name, fb_spec, supported)
            # HD-cam control-ordering fix: force each variation's own gated
            # value control's gate to manual, when the variation touches the
            # value but doesn't already say what the gate should be -- see
            # _ensure_gate_overrides/_GATE_FOR_VALUE_CONTROL above.
            variations = [
                Variation(name=v.name, controls=_ensure_gate_overrides(v.controls, supported), settle_s=v.settle_s)
                for v in variations
            ]
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
                _settle(backend, settle_s)
                effective = {**baseline_controls, **var.controls}
                _capture_one(var.name, effective, settle_s)

            # -- calibration snapshot pin (§6.2: L0's world-accuracy tests
            # must not depend on live machine state) --
            calibration_sha256 = pin_calibration_snapshot(directories, cam.slug, fixture_dir)

            # -- manifest --
            manifest = {
                "camera": {
                    "device_name": cam.device_name,
                    "config_dir": cam.slug,
                    "unique_id": cam.hardware_key,
                    "registry_enum": cam.registry_number,
                    "resolution": list(resolution) if resolution else None,
                },
                "playfield": cam.config.playfield,
                "package_version": _package_version(),
                "daemon_version": _package_version(),
                "field_state_id": field_state_id,
                "calibration_snapshot_sha256": calibration_sha256,
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
        backend.close()
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
    p.add_argument(
        "--field-state-id",
        default=None,
        help="Tag this run's manifests with an explicit field-state id (e.g. "
        "fs-20260823) instead of the default auto-generated/legacy "
        "'unverified-<date>' stub, and nest this generation's fixtures under "
        "tests/images/<field-state-id>/<slug>/ so it coexists with an older "
        "generation instead of overwriting it. tests/images/field-state.json "
        "(always at the fixture root, never per-camera) gains/keeps a stub "
        "entry for this id and is set as current. Omit to keep the default "
        "unnested output and legacy field-state.json behavior.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return p.parse_args(argv)


def _print_summary(results: List[CameraRunResult]) -> None:
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for r in results:
        print(f"\n{r.device_name} (slug: {r.dir_name})")
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

    directories = Directories()
    log.info("directories: %s", directories.describe())

    try:
        check_daemon_not_running(directories)
    except ContentionError as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2

    registry = CameraRegistry(directories.state_dir / "cameras")
    pattern = None if args.all else args.camera
    cams = discover_target_cameras(directories, registry, pattern)
    if not cams:
        print(
            "No configured, currently-connected camera with a playfield link "
            f"matched (pattern={pattern!r}).",
            file=sys.stderr,
        )
        return 1

    output_root = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT
    output_root.mkdir(parents=True, exist_ok=True)
    field_state_id = ensure_field_state_stub(output_root, args.field_state_id)

    matrix_spec_path = Path(args.matrix_spec) if args.matrix_spec else DEFAULT_MATRIX_SPEC
    matrix_spec = load_matrix_spec(matrix_spec_path)
    uvc_bin = locate_uvc_util()
    detector = TagDetector(DetectorConfig())

    print(f"Targeting {len(cams)} camera(s):")
    for cam in cams:
        print(f"  - {cam.device_name}  (slug: {cam.slug}, live index {cam.live_index})")

    results: List[CameraRunResult] = []
    for cam in cams:
        print(f"\n=== capturing {cam.device_name} ===")
        result = run_camera(
            cam, uvc_bin, matrix_spec, output_root, field_state_id, args.settle, detector,
            directories, args.field_state_id,
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
