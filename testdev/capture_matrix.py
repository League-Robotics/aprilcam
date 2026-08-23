#!/usr/bin/env python3
"""Capture a per-camera image variation matrix for offline pipeline test fixtures.

See docs/test-plans/camera-image-matrix.md (§3/§4) for the full design.

For a given camera this tool:
  1. Requires a reachable ``aprilcam`` daemon (fails with a clear message if
     none answers -- see the "Daemon-driven, not direct-open" section below).
  2. Fetches the camera's own live config from the daemon and treats it as
     the baseline -- NEVER a hardcoded baseline.
  3. Selects a variation matrix from ``capture_matrix.yaml`` by camera-slug
     pattern (or falls back to a generic exposure/gain/WB sweep built from
     this tool's own documented control ranges -- see "No live range query"
     below).
  4. For each variation: overlays the varied settings onto the camera's own
     baseline settings and pushes the WHOLE merged document through the
     daemon's ``SetCameraConfig`` RPC, settles, and captures one frame via
     ``GetFrame``.
  5. ALWAYS re-pushes the exact pre-sweep baseline document in a ``finally``
     block. Because ``Daemon.set_camera_config`` -> ``Camera.apply_config``
     verifies every write against a real hardware readback before returning
     (``camera/backends/uvc_util.py``'s "never trusts its own write"), a
     restore push that returns without raising **is** a hardware-verified
     restore -- there is no separate readback step to run from here.
  6. Writes ``tests/images/<camera-slug>/manifest.json`` recording the full
     effective settings, camera identity, resolution, playfield, capture
     timestamp, package version, and field-state id for every image.

**Daemon-driven, not direct-open (reworked sprint 035, ticket 004).**
Earlier revisions of this tool (sprint 031 ticket 007, sprint 035 ticket
003) opened cameras directly via ``OpenCVBackend`` and drove hardware
controls directly via raw ``uvc-util`` subprocess calls, refusing to run
whenever a daemon held the machine-global lock (design.md §8's offline-tool
exception). That design is now inverted: **a reachable daemon is a
precondition, not a blocker.** Two things forced the change:

* Direct ``cv2.VideoCapture``/``OpenCVBackend.open()`` fails outright under
  Claude Code -- macOS TCC denies camera access to the responsible bundle
  (``docs/knowledge/claude-code-camera-permission-run-via-terminal.md``).
  The daemon already holds the camera open in a process that macOS *did*
  grant access to; routing through it sidesteps the whole TCC problem.
* This project's own architecture (``docs/design/design.md``, the "Vision
  Boundary" section of ``project-overview.md``) makes **the daemon the sole
  vision authority** and everything else a thin client. This tool is not
  one of the three named thin clients (MCP server / web hub / view client),
  but the same principle applies once a daemon exists: the daemon already
  exposes every operation this sweep needs (``GetCameraConfig``/
  ``SetCameraConfig``/``ResetCameraConfig``/``GetFrame``/``GetTags``),
  hand-verified against real hardware (camera 5: pushed ``exposure=120`` ->
  mean luminance 205.9; ``--reset`` -> back to ``exposure=20``, mean 115.3).

This tool now talks **only** to ``aprilcam.client.discovery.Discovery`` /
``aprilcam.daemon.interfaces.Daemon`` -- no ``cv2``, no ``OpenCVBackend``,
no ``camera.identity``/``CameraRegistry`` live-hardware enumeration, no
``uvc-util`` subprocess. Target cameras are selected from
``Daemon.list_cameras()`` (present, playfield-linked, matched against the
daemon's own ``CameraState.name``/``.number`` -- the same slug/number every
other ``aprilcam camera ...`` command addresses a camera by) rather than
matched against live AVFoundation enumeration.

**Whole-document push, not restore-then-overlay.** The old direct-open tool
pushed a variation in two separate ``uvc-util`` calls per capture: the full
baseline, then the variation's own controls. ``Daemon.set_camera_config``
takes one whole ``CameraConfig`` document per call (UC-03: "applies... in
memory... unsupported settings fail the ENTIRE push"), so this tool now
computes ``{**baseline.settings, **variation.controls}`` client-side and
pushes that ONE merged document per variation -- baseline restoration
between variations is implicit in recomputing from the untouched baseline
snapshot every time, not a separate call.

**Gate-override injection still needed; gate-ordering is not.**
``camera.camera.Camera.apply_config`` already pushes ``_GATE_KEYS``
(``auto_exposure``, ``auto_white_balance``) first, unconditionally, for
every push it receives (``camera/camera.py``'s own ``_GATE_KEYS`` constant
and its use in ``apply_config``) -- so the old ``_gate_first_order``
re-ordering helper is genuinely dead code once pushes go through the
daemon and has been deleted, not ported. What survives is
``_ensure_gate_overrides``: ``apply_config`` only *reorders* a gate key
that is already present in the pushed document, it never *injects* one --
so a variation that sets ``exposure`` without also naming ``auto_exposure``
still needs that gate forced to manual client-side, exactly as the July
HD-cam bug (rejected every ``exposure``/``white_balance_temperature``
variation while its baseline's own gate was in auto mode) required before.
That function is kept, translated from raw ``uvc-util`` control names to
``CameraConfig.settings``'s own five-key abstracted vocabulary
(``camera/backends/uvc_util.py``'s ``_CONTROLS``: ``auto_exposure``,
``exposure``, ``gain``, ``auto_white_balance``, ``white_balance_temperature``)
-- the only vocabulary ``Daemon.set_camera_config`` accepts.

**No live range query for the generic fallback sweep.** The old fallback
path (any camera with no dedicated ``capture_matrix.yaml`` entry, e.g. the
HD USB camera) queried ``uvc-util -S <control>`` live for each control's
``[minimum, maximum]`` before computing fractional sweep values. The daemon
has no RPC exposing hardware control ranges (``GetCameraConfig`` reports
*values*, not ranges), and re-adding a direct ``uvc-util`` shell here for
range-only queries would need the OS device name back, which
``Daemon.list_cameras()``'s ``CameraState`` deliberately does not carry
(cameras are daemon-addressed by slug/number only). Rather than
reintroduce a live-hardware dependency for this one piece, the fallback
sweep now uses the SAME fixed ranges ``camera/backends/uvc_util.py``'s own
module docstring already documents and relies on for every macOS camera's
control surface (``exposure``: 1..5000 in 100µs ticks; ``gain``: 0..100;
``white_balance_temperature``: 2800..6500 Kelvin) -- see
``capture_matrix.yaml``'s ``fallback:`` section.

**No cv2.** Frames arrive from ``Daemon.get_frame`` already JPEG-encoded
(the daemon does the encoding); this tool writes those bytes straight to
disk. The one piece of local pixel work this tool still does -- computing
each capture's mean luminance to flag ``dark``/``washed`` images in the
manifest -- decodes the JPEG with Pillow + numpy (both base installs,
matching the "Vision Boundary" rule: a thin client that needs pixel work
fetches a JPEG via ``get_frame`` and processes it with its own libraries,
never OpenCV). Baseline detection counts come from ``Daemon.get_tags``
instead of a local ``TagDetector`` pass.

Usage:
    python testdev/capture_matrix.py                 # every configured camera
    python testdev/capture_matrix.py --all
    python testdev/capture_matrix.py arducam          # one camera, by slug pattern
    python testdev/capture_matrix.py hd-usb --field-state-id fs-20260823
        # tag manifests with an explicit field-state id and write fixtures to
        # tests/images/fs-20260823/<slug>/ instead of tests/images/<slug>/,
        # so this generation coexists with an older one (sprint 035 ticket 003).
    python testdev/capture_matrix.py hd-usb -d some-host
        # target a non-local daemon (default: the local one).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import yaml
from PIL import Image

from aprilcam.client.discovery import Discovery
from aprilcam.config.directories import Directories
from aprilcam.daemon.interfaces import Daemon
from aprilcam.errors import AprilCamError
from aprilcam.types import CameraState, ImageFrame

log = logging.getLogger("capture_matrix")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MATRIX_SPEC = Path(__file__).resolve().parent / "capture_matrix.yaml"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tests" / "images"

DARK_LUMINANCE_FLOOR = 10.0
WASHED_LUMINANCE_CEIL = 245.0


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("aprilcam")
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Daemon precondition -- require one, rather than refuse against one (the
# module docstring's "Daemon-driven, not direct-open" section).
# ---------------------------------------------------------------------------


class DaemonUnavailable(RuntimeError):
    """Raised when no daemon answers -- this tool cannot do anything
    without one now (every camera operation is a daemon RPC)."""


def require_daemon(discovery: Discovery, host: Optional[str] = None) -> Daemon:
    """Connect to *host* (or the local daemon when ``None``), or raise
    :class:`DaemonUnavailable` with a message pointing at ``aprilcam
    daemon start`` -- the precondition check every other function here
    assumes has already passed."""
    try:
        return discovery.connect(host)
    except AprilCamError as exc:
        raise DaemonUnavailable(
            "no aprilcam daemon is reachable -- this tool drives camera "
            "hardware exclusively through a running daemon's RPCs now, "
            f"never by opening a device directly. Start one first: "
            f"'aprilcam daemon start' ({exc})"
        ) from exc


# ---------------------------------------------------------------------------
# Gate-override injection -- CameraConfig.settings vocabulary (module
# docstring's "Gate-override injection still needed" section).
# ---------------------------------------------------------------------------

#: ``CameraConfig.settings`` key -> (gate key, the value that puts that gate
#: into manual mode) -- mirrors ``camera/backends/uvc_util.py``'s own
#: ``_GATE_FOR``/``_GATE_MANUAL_VALUE`` (diagnostic-only there; load-bearing
#: here, since this tool -- unlike ``Camera.apply_config`` -- must not just
#: reorder an already-present gate key but actually inject one that is
#: missing from a variation's own controls).
_GATE_FOR_VALUE_KEY: Dict[str, Tuple[str, Any]] = {
    "exposure": ("auto_exposure", 1),
    "white_balance_temperature": ("auto_white_balance", False),
}


def _ensure_gate_overrides(controls: Dict[str, Any]) -> Dict[str, Any]:
    """Force a value key's gate to manual when the value is present but
    the gate is not already explicit in *controls*.

    Applied to each *variation*'s own control set, never to the baseline
    (which always reflects the camera's own live config -- the module
    docstring's "NEVER a hardcoded baseline" contract; only what this tool
    actively varies gets this override). A variation that already names the
    gate explicitly (e.g. the auto-exposure toggle test itself) is left
    untouched -- this fills a gap, it never overrides stated intent.
    """
    out = dict(controls)
    for value_key, (gate_key, manual_value) in _GATE_FOR_VALUE_KEY.items():
        if value_key in out and gate_key not in out:
            out[gate_key] = manual_value
    return out


def _drop_settings_with_inactive_gate(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Never attempt to push a gated value key while its own gate (read
    from the SAME settings dict) is not that gate's manual value.

    A value key held while its gate is auto is a normal, coherent camera
    state (e.g. a camera actively in auto-exposure mode, whose baseline
    document -- fetched straight from the daemon -- legitimately carries
    both ``auto_exposure: 8`` and a last-observed ``exposure`` reading).
    Attempting to *push* the value key anyway is physically meaningless --
    the hardware ignores or rejects a manual value while its auto mode is
    active, the same constraint July's HD-cam manifest recorded.

    The pre-035-004 direct-uvc-util tool tolerated exactly this: it pushed
    each control with a separate, discardable per-key ``uvc-util -s`` call
    and never even checked whether the baseline-establish/restore steps'
    per-key failures succeeded (``capture_matrix.py``'s own July-era test
    suite documented this as an accepted limitation: "a byte-exact
    readback of those two values specifically is not achievable... while
    its gate is auto"). ``Daemon.set_camera_config`` has no such per-key
    tolerance -- it is one atomic whole-document push (UC-03), so ONE
    physically-guaranteed-to-fail key would reject baseline establishment
    and restoration entirely, not just itself. Dropping the key here
    avoids ever attempting a push that cannot succeed, rather than
    reacting to the failure after the fact -- the gate key itself is
    always left in place (it is not gated by anything), only the value
    key it gates is omitted.
    """
    out = dict(settings)
    for value_key, (gate_key, manual_value) in _GATE_FOR_VALUE_KEY.items():
        if value_key not in out:
            continue
        if out.get(gate_key) != manual_value:
            del out[value_key]
    return out


# ---------------------------------------------------------------------------
# Camera selection -- via the daemon's own registry, never live hardware
# enumeration.
# ---------------------------------------------------------------------------


def select_target_cameras(daemon: Daemon, pattern: Optional[str]) -> List[CameraState]:
    """Every daemon-known camera that is present and playfield-linked,
    optionally narrowed by *pattern* (a case-insensitive substring against
    the camera's slug, or an exact match against its persistent number).

    Mirrors the old direct-open tool's "every configured, currently-
    connected camera with a playfield link" contract, but resolved entirely
    from ``Daemon.list_cameras()`` -- the daemon already did the identity
    resolution/enumeration this tool used to do itself against live
    hardware (``camera.identity.resolve_all`` + ``CameraRegistry``).
    """
    cameras = daemon.list_cameras()
    linked = [c for c in cameras if c.present and c.playfield is not None]
    if not pattern:
        return linked
    needle = pattern.strip().lower()
    return [
        c
        for c in linked
        if needle in c.name.lower() or (c.number is not None and needle == str(c.number))
    ]


# ---------------------------------------------------------------------------
# Variation matrix
# ---------------------------------------------------------------------------


@dataclass
class Variation:
    name: str
    controls: Dict[str, Any]
    settle_s: Optional[float] = None


def load_matrix_spec(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def select_matrix_entry(matrix_spec: dict, camera_slug: str) -> Optional[dict]:
    """The ``cameras:`` entry whose ``match`` substring is in *camera_slug*
    (case-insensitive), or ``None`` for "use the fallback sweep".

    Matched against the daemon's own slug (e.g.
    ``arducam-ov9281-usb-camera``) rather than a live-enumerated OS device
    name -- the slug already contains the same distinguishing substring
    (``ov9281``, ``ov9782``) every ``capture_matrix.yaml`` entry's ``match``
    field names, so this loses no matching power versus the old
    device-name-based lookup.
    """
    name_lower = camera_slug.lower()
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
                controls=dict(v.get("controls", {})),
                settle_s=v.get("settle_s"),
            )
        )
    return out


def _fraction_value(rng: Tuple[float, float], fraction: float) -> int:
    lo, hi = rng
    return int(round(lo + fraction * (hi - lo)))


def build_fallback_variations(
    fb_spec: dict, baseline_settings: Dict[str, Any],
) -> List[Variation]:
    """Generic exposure/gain/white-balance sweep for a camera with no
    dedicated ``capture_matrix.yaml`` entry, built from the FIXED ranges in
    *fb_spec* -- see the module docstring's "No live range query" section
    for why these are no longer queried live."""
    variations: List[Variation] = []

    exp_key = fb_spec.get("exposure_control", "exposure")
    gain_key = fb_spec.get("gain_control", "gain")
    ae_key = fb_spec.get("auto_exposure_control", "auto_exposure")
    wb_key = fb_spec.get("white_balance_control", "white_balance_temperature")
    awb_key = fb_spec.get("auto_white_balance_control", "auto_white_balance")

    exp_range = fb_spec.get("exposure_range")
    gain_range = fb_spec.get("gain_range")
    wb_range = fb_spec.get("white_balance_range")

    if exp_range:
        for frac in fb_spec.get("exposure_fractions", []):
            val = _fraction_value(tuple(exp_range), frac)
            variations.append(Variation(name=f"exp-{val}", controls={exp_key: val}))

    if gain_range:
        for frac in fb_spec.get("gain_fractions", []):
            val = _fraction_value(tuple(gain_range), frac)
            variations.append(Variation(name=f"gain-{val}", controls={gain_key: val}))

    baseline_ae = baseline_settings.get(ae_key)
    for cand in fb_spec.get("auto_exposure_candidate_values", []):
        if cand != baseline_ae:
            variations.append(
                Variation(
                    name=f"auto-exposure-{cand}",
                    controls={ae_key: cand},
                    settle_s=fb_spec.get("auto_exposure_settle_s"),
                )
            )
            break

    if wb_range:
        for frac in fb_spec.get("white_balance_fractions", []):
            val = _fraction_value(tuple(wb_range), frac)
            variations.append(Variation(name=f"wb-{val}", controls={wb_key: val}))

    baseline_awb = baseline_settings.get(awb_key)
    candidate = baseline_awb is not True
    variations.append(
        Variation(
            name=f"auto-wb-{str(candidate).lower()}",
            controls={awb_key: candidate},
            settle_s=fb_spec.get("auto_white_balance_settle_s"),
        )
    )

    if exp_range and gain_range:
        for combo in fb_spec.get("stress_combos", []):
            ev = _fraction_value(tuple(exp_range), combo["exposure_fraction"])
            gv = _fraction_value(tuple(gain_range), combo["gain_fraction"])
            variations.append(Variation(name=combo["name"], controls={exp_key: ev, gain_key: gv}))

    return variations


def dedupe_against_baseline(variations: List[Variation], baseline: Dict[str, Any]) -> List[Variation]:
    """Drop a variation whose every overridden value already equals baseline."""
    out = []
    for v in variations:
        if v.controls and all(baseline.get(k) == val for k, val in v.controls.items()):
            log.info("skipping %s: identical to baseline, would duplicate baseline.jpg", v.name)
            continue
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# Field state stub (plan §5) -- unchanged from sprint 035 ticket 003. Pure
# file logic; no camera/daemon interaction, so this ticket's daemon-driven
# rework does not touch it.
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

    Unchanged from sprint 035 ticket 003. Reads directly from
    ``directories.state_dir`` rather than through a daemon RPC: this is a
    plain filesystem read of a file the daemon itself only ever writes (a
    calibration result), not camera hardware, so it carries none of the
    TCC/direct-open problems this ticket's rework is about, and ticket 003
    explicitly lands it -- see this ticket's own notes on what to keep.

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


def _decode_mean_luminance(jpeg_bytes: bytes) -> float:
    """Mean grayscale luminance of an encoded frame -- the one piece of
    local pixel work this tool still does (dark/washed image flagging),
    done with Pillow + numpy rather than OpenCV, per the module docstring's
    "No cv2" section (the Vision Boundary "thin client fetches a JPEG and
    processes it with its own libraries" pattern)."""
    with Image.open(io.BytesIO(jpeg_bytes)) as img:
        gray = img.convert("L")
        return float(np.asarray(gray).mean())


@dataclass
class CameraRunResult:
    camera: str
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
    restore_error: Optional[str] = None
    fixture_dir: Optional[str] = None
    wall_time_s: float = 0.0


def run_camera(
    daemon: Daemon,
    cam: CameraState,
    matrix_spec: dict,
    output_root: Path,
    field_state_id: str,
    settle_override: Optional[float],
    directories: Directories,
    explicit_field_state_id: Optional[str] = None,
) -> CameraRunResult:
    result = CameraRunResult(camera=cam.name, dir_name=cam.name)
    t0 = time.monotonic()

    fixture_dir = resolve_fixture_dir(output_root, explicit_field_state_id, cam.name)
    result.fixture_dir = str(fixture_dir.relative_to(output_root))

    if not cam.usable:
        result.error = (
            f"{cam.name!r} is present but the daemon's last probe found it "
            "unusable -- see 'aprilcam camera probe' for detail."
        )
        result.wall_time_s = time.monotonic() - t0
        return result

    # A warm-up capture -- guarantees the daemon actually holds this camera
    # open before anything below relies on a config push being applied to
    # hardware. ``Daemon.set_camera_config`` only applies to hardware "when
    # the camera is held open" (daemon/service.py's ``DaemonService.
    # set_camera_config`` looks the camera up in its own open-handle dict
    # and never opens one itself); ``get_frame`` is one of the RPCs that
    # does trigger an on-demand open (``_camera_for``), so calling it first
    # is what makes every subsequent push in this function hardware-
    # verified rather than merely stored.
    try:
        daemon.get_frame(cam.name, deskewed=False, encoding="jpeg")
    except AprilCamError as exc:
        result.error = f"could not reach {cam.name!r} via the daemon: {exc}"
        result.wall_time_s = time.monotonic() - t0
        return result

    try:
        baseline_config = daemon.get_camera_config(cam.name)
    except AprilCamError as exc:
        result.error = f"could not read {cam.name!r}'s live config from the daemon: {exc}"
        result.wall_time_s = time.monotonic() - t0
        return result

    # Only create the fixture directory once the camera is confirmed
    # reachable, so a daemon/camera failure never leaves an empty
    # directory behind.
    fixture_dir.mkdir(parents=True, exist_ok=True)

    default_settle = matrix_spec.get("settle_s", 1.5)
    settle_auto = matrix_spec.get("settle_s_auto", 3.0)
    manifest_images: List[dict] = []
    baseline_frame_info: Dict[str, Any] = {}

    def _capture_one(name: str, effective_settings: Dict[str, Any], settle_s: float) -> bool:
        try:
            frame: ImageFrame = daemon.get_frame(cam.name, deskewed=False, encoding="jpeg")
        except AprilCamError as exc:
            log.warning("no frame for %s/%s: %s", cam.name, name, exc)
            result.skipped_no_frame.append(name)
            return False
        lum = _decode_mean_luminance(frame.data)
        dark = lum < DARK_LUMINANCE_FLOOR
        washed = lum > WASHED_LUMINANCE_CEIL
        if dark:
            result.dark_images.append(name)
        if washed:
            result.washed_images.append(name)
        out_path = fixture_dir / f"{name}.jpg"
        out_path.write_bytes(frame.data)
        manifest_images.append(
            {
                "name": name,
                "file": out_path.name,
                "controls": dict(effective_settings),
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
            baseline_frame_info["resolution"] = [frame.width, frame.height]
            try:
                tag_frame = daemon.get_tags(cam.name)
            except AprilCamError as exc:
                log.warning("could not fetch baseline detections for %s: %s", cam.name, exc)
            else:
                by_family: Dict[str, int] = {}
                for record in tag_frame.tags:
                    by_family[record.tag.family.value] = by_family.get(record.tag.family.value, 0) + 1
                result.baseline_detections = by_family
        return True

    baseline_pushable = _drop_settings_with_inactive_gate(baseline_config.settings)

    try:
        try:
            # Establish a known-good starting point before doing anything
            # else -- re-pushing what we just fetched is a no-op on
            # hardware unless something drifted between the fetch and now,
            # but it is also this function's only chance to confirm the
            # baseline itself is genuinely applicable (Camera.apply_config
            # hardware-verifies every push) before any variation is tried.
            # Only the settings whose own gate is currently active are
            # pushed -- see _drop_settings_with_inactive_gate's docstring
            # for why a gated value (e.g. an "exposure" reading while
            # auto_exposure is not manual) is never attempted at all.
            daemon.set_camera_config(replace(baseline_config, settings=baseline_pushable))
        except AprilCamError as exc:
            result.error = (
                f"{cam.name!r}'s own live config was rejected when re-applied "
                f"as the sweep's starting point: {exc}"
            )
            return result
        time.sleep(settle_override if settle_override is not None else default_settle)

        _capture_one("baseline", dict(baseline_pushable), settle_override or default_settle)

        matrix_entry = select_matrix_entry(matrix_spec, cam.name)
        fb_spec = matrix_spec.get("fallback", {})
        if matrix_entry is not None:
            log.info("%s: using matrix entry match=%r", cam.name, matrix_entry.get("match"))
            variations = _variations_from_matrix_entry(matrix_entry)
        else:
            log.info("%s: no matrix entry matched -- using generic fallback sweep", cam.name)
            variations = build_fallback_variations(fb_spec, baseline_config.settings)

        # HD-cam control-ordering fix, translated: force each variation's
        # own gated value key's gate to manual, when the variation touches
        # the value but doesn't already say what the gate should be -- see
        # _ensure_gate_overrides/_GATE_FOR_VALUE_KEY above. Re-ordering
        # within the merged document is no longer this tool's job --
        # Camera.apply_config already does that (module docstring).
        variations = [
            Variation(name=v.name, controls=_ensure_gate_overrides(v.controls), settle_s=v.settle_s)
            for v in variations
        ]
        variations = dedupe_against_baseline(variations, baseline_config.settings)

        for var in variations:
            # Recomputed from the untouched baseline snapshot every time,
            # so a previous variation's settings never bleed into the next
            # one -- the whole-document push replaces the old two-call
            # "restore baseline, then apply variation" sequence (module
            # docstring's "Whole-document push" section).
            effective_settings = _drop_settings_with_inactive_gate(
                {**baseline_config.settings, **var.controls}
            )
            push_config = replace(baseline_config, settings=effective_settings)
            try:
                daemon.set_camera_config(push_config)
            except AprilCamError as exc:
                log.warning("skipping variation %s on %s: %s", var.name, cam.name, exc)
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
            time.sleep(settle_s)
            _capture_one(var.name, effective_settings, settle_s)

        # -- calibration snapshot pin (§6.2: L0's world-accuracy tests
        # must not depend on live machine state) --
        calibration_sha256 = pin_calibration_snapshot(directories, cam.name, fixture_dir)

        # -- manifest --
        manifest = {
            "camera": {
                "config_dir": cam.name,
                "registry_enum": cam.number,
                "resolution": baseline_frame_info.get("resolution"),
            },
            "playfield": cam.playfield,
            "package_version": _package_version(),
            "daemon_version": _package_version(),
            "field_state_id": field_state_id,
            "calibration_snapshot_sha256": calibration_sha256,
            "matrix_source": "matrix" if matrix_entry is not None else "fallback",
            "baseline_controls": dict(baseline_config.settings),
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
        # camera misconfigured (plan §4.5). Re-pushing the exact pre-sweep
        # document IS the hardware-verified restore: Camera.apply_config
        # (invoked by DaemonService.set_camera_config) reads every write
        # back and raises UnsupportedSetting rather than return
        # successfully for a value that did not take (camera/backends/
        # uvc_util.py's "never trusts its own write") -- so there is no
        # separate readback/mismatch step to run from this side of the
        # RPC boundary; a push that returns without raising already proved
        # the hardware matches. This is honestly weaker in one respect
        # than the old direct-open tool's own readback: Daemon.
        # get_camera_config answers from "whatever set_camera_config last
        # held" (daemon/service.py's own docstring), never a live hardware
        # poll, so re-fetching after this push and diffing it against
        # baseline_config would only prove the daemon echoed back what it
        # was just given, not that the hardware still agrees a moment
        # later -- that check is deliberately not done here because it
        # would be theater, not verification.
        try:
            daemon.set_camera_config(replace(baseline_config, settings=baseline_pushable))
            result.restored = True
            result.restore_error = None
            log.info("restore verified for %s (push accepted)", cam.name)
        except AprilCamError as exc:
            result.restored = False
            result.restore_error = str(exc)
            log.critical(
                "FAILED to restore baseline config for %s after the sweep -- "
                "camera may be left misconfigured: %s", cam.name, exc,
            )
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
        help="Camera slug / persistent-number substring pattern to select one camera "
        "(matched against the daemon's own Daemon.list_cameras() -- the same slug/number "
        "every other 'aprilcam camera ...' command uses). Omit (or pass --all) to capture "
        "every present, playfield-linked camera the daemon knows about.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Capture every present, playfield-linked camera the daemon knows about "
        "(default when no pattern is given).",
    )
    p.add_argument(
        "-d", "--daemon", metavar="HOST", default=None,
        help="target daemon (default: the local daemon)",
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
        print(f"\n{r.camera}")
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
        print(f"  baseline restored: {r.restored} error={r.restore_error or 'none'}")
        print(f"  wall time:         {r.wall_time_s:.1f}s")


def main(argv: Optional[List[str]] = None, *, discovery_factory: Callable[[], Discovery] = Discovery) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    discovery = discovery_factory()
    try:
        daemon = require_daemon(discovery, args.daemon)
    except DaemonUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2

    directories = Directories()

    pattern = None if args.all else args.camera
    cams = select_target_cameras(daemon, pattern)
    if not cams:
        print(
            "No present, playfield-linked camera matched "
            f"(pattern={pattern!r}) on this daemon.",
            file=sys.stderr,
        )
        return 1

    output_root = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT
    output_root.mkdir(parents=True, exist_ok=True)
    field_state_id = ensure_field_state_stub(output_root, args.field_state_id)

    matrix_spec_path = Path(args.matrix_spec) if args.matrix_spec else DEFAULT_MATRIX_SPEC
    matrix_spec = load_matrix_spec(matrix_spec_path)

    print(f"Targeting {len(cams)} camera(s):")
    for cam in cams:
        number = f"[{cam.number}]" if cam.number is not None else "[-]"
        print(f"  - {cam.name}  {number}  playfield={cam.playfield}")

    results: List[CameraRunResult] = []
    for cam in cams:
        print(f"\n=== capturing {cam.name} ===")
        result = run_camera(
            daemon, cam, matrix_spec, output_root, field_state_id, args.settle,
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
