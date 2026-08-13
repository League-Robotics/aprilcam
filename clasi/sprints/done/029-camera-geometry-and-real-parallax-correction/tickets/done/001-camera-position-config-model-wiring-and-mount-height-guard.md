---
id: '001'
title: camera_position config model, wiring, and mount-height guard
status: done
use-cases:
- SUC-001
- SUC-002
depends-on: []
github-issue: ''
issue: camera-geometry-for-real-parallax-correction.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# camera_position config model, wiring, and mount-height guard

## Description

Add `camera_position` as a real, first-class `CameraConfig` field — a
camera's ground nadir plus its optical height above the field plane —
with the full proto + config-loader + CLI-push plumbing sprints
020-023 established for every other camera setting, and wire
`DaemonService._camera_geometry_for` to read it instead of returning
the hardcoded `(WorldPoint(0, 0), 0.0)` stopgap it has returned since
sprint 025. This ticket is the foundation ticket 003 (`camera locate`)
writes into; it does not itself solve anything.

**No `typing.Protocol`, ever** — `config/interfaces.py`'s new
`CameraPosition` is a frozen `@dataclass(frozen=True, kw_only=True)`
declaring fields, exactly like the existing `Feature`/`ConfigDocument`
in that same module (see `daemon/interfaces.py`'s module docstring for
the full rule).

**`config/interfaces.py`**: new value type, next to `CameraConfig`:

```python
@dataclass(frozen=True, kw_only=True)
class CameraPosition:
    """The ground point directly below a camera's optical centre
    (nadir) and its optical height above the field plane -- the two
    inputs calibration.geometry.project_tag_to_field_plane needs."""
    nadir: WorldPoint
    height_cm: float
```

`CameraConfig` gains `camera_position: Optional[CameraPosition] = None`.
`None` means "not configured" — the exact pre-029 state. Do **not**
put this inside `settings: dict[str, Any]`: `settings` is specifically
hardware controls pushed through `Camera.apply_config` and validated
against a device's control surface (`UnsupportedSetting` on an
unrecognized key); `camera_position` is operator-asserted metadata
that never touches hardware. Confirm `Camera.apply_config`
(`camera/camera.py`) needs **no change** — it iterates `config.settings`
only, so the new sibling field is invisible to it by construction; add
a regression test proving a config push carrying `camera_position` and
no `settings` changes still applies cleanly to a held-open camera.

**`config/loader.py`**: `parse_camera_config`/`to_dict` gain a
`camera_position` block. On-disk shape (a fresh v2 shape — see
sprint.md's Design Rationale for why it diverges from v1's
`{height, x_offset, y_offset}`):

```json
"camera_position": {
  "nadir_x_cm": 3.05,
  "nadir_y_cm": -2.75,
  "height_cm": 128.25
}
```

- Key absent -> `camera_position = None` (a pre-029 file parses
  unchanged — write a regression test asserting this explicitly).
- Key present, `height_cm <= 0` -> whole-document `MalformedDocument`,
  mirroring `_require_geometry`'s existing positivity check for
  playfield `width_cm`/`height_cm` (same shape, same error type, named
  clearly which value was the problem).
- `nadir_x_cm`/`nadir_y_cm` are plain floats, no bounds check — unlike
  a playfield feature, a camera's nadir has no reason to be constrained
  to the field's own rectangle.
- `to_dict()` emits the `camera_position` key **only when set** — never
  a `null` placeholder — so a config document that never configured
  position round-trips byte-for-byte identical to before this ticket.

**`proto/aprilcam_v2.proto`**: new message, and one new field on the
existing `CameraConfig` message (field 4 — `name`/`playfield`/
`settings_json` already occupy 1-3, verified against the current file):

```protobuf
message CameraPosition {
  WorldPoint nadir = 1;
  double height_cm = 2;
}

message CameraConfig {
  string name = 1;
  optional string playfield = 2;
  string settings_json = 3;
  optional CameraPosition camera_position = 4;
}
```

`optional` on the field, `HasField`-guarded on both marshalling sides —
mirror `GetCalibrationResponse.calibration`'s existing "absent means
not yet configured, never a fabricated default" presence discipline,
not `settings_json`'s always-present-string convention. Add a header
comment above this section naming this the tenth extension of this
file, following the file's own numbering convention in its header
comment block.

**`daemon/rpc.py`, `client/models.py`**: extend the existing
`CameraConfig` marshalling (the code around
`aprilcam_v2_pb2.CameraConfig(name=..., settings_json=...)` in
`rpc.py` and its mirror in `client/models.py`) to carry
`camera_position` both directions, `HasField`-guarded.

**`daemon/service.py`**: `_camera_geometry_for` (~line 1273) becomes:

```python
def _camera_geometry_for(self, slug: str) -> Tuple[WorldPoint, float]:
    with self._lock:
        config = self._camera_configs.get(slug)
    if config is None or config.camera_position is None:
        return WorldPoint(x=0.0, y=0.0), 0.0
    return config.camera_position.nadir, config.camera_position.height_cm
```

Rewrite the method's docstring: it is no longer "explicitly
provisional" — state plainly that it now reads real configured
geometry when present, falling back to the historical stopgap
(named as such) when absent. Do the same for `daemon/detection.py`'s
`_default_camera_geometry_lookup` docstring and the "Camera-geometry
caveat" section of that module's own docstring, and
`calibration/geometry.py`'s module-docstring section by the same
name — all three currently describe this as an open gap "a follow-up
ticket must" close; this is that ticket. Update them to describe the
real source, not remove the historical context (the worked example and
reasoning in `project_tag_to_field_plane`'s docstring stay — the math
does not change, only where its inputs come from).

**`daemon/detection.py` — the mount-height guard**: in
`_apply_mount_correction`, before calling `correct_tag_pose`, add a
check: when the looked-up `camera_height_cm > 0` (a real value is
configured — previously this branch was unreachable, since height was
always `0.0`) and `mount.mount_z >= camera_height_cm`, skip the
correction entirely and return the record unchanged, exactly like the
existing "no registration" no-op path just above it. Without this
guard, `project_tag_to_field_plane`'s `ratio = mount_z_cm /
camera_height_cm` would be `>= 1`, extrapolating the corrected position
*past* the camera's own nadir — a plausible-looking number that is
physically nonsensical, exactly the "silently wrong" failure mode this
codebase treats as worse than an explicit failure. This is a
**per-burst, per-observing-camera** check (not a `register_tag`-time
rejection) — see sprint.md's Design Rationale for why: a registered
tag is not tied to one camera, so there is no single "the camera" to
validate against at registration time. `register_tag` itself is
unchanged by this ticket.

## Acceptance Criteria

- [x] `CameraPosition` is a frozen `@dataclass(frozen=True,
      kw_only=True)` in `config/interfaces.py`; `CameraConfig.
      camera_position: Optional[CameraPosition] = None`. No
      `typing.Protocol` anywhere in the diff.
- [x] `parse_camera_config`/`to_dict` round-trip `camera_position`
      (`load(x.to_dict()) == x`), omit the key entirely when unset, and
      reject `height_cm <= 0` with `MalformedDocument` naming the
      problem, before any RPC.
- [x] A pre-029 config file (no `camera_position` key) parses with
      `camera_position is None` — explicit regression test, not just
      "no test failed."
- [x] `SetCameraConfig`/`GetCameraConfig` round-trip `camera_position`
      over a real gRPC channel; `HasField("camera_position")` is false
      on the wire when unset, never a zero-valued message.
- [x] `Camera.apply_config` requires no code change — a config push
      carrying `camera_position` (with or without `settings` changes)
      applies cleanly to a held-open camera (regression test).
- [x] `_camera_geometry_for` returns a configured camera's real
      `(nadir, height_cm)`; returns the exact pre-sprint stopgap
      `(WorldPoint(0,0), 0.0)` for an unconfigured camera (unit tests,
      both branches).
- [x] Existing `mount_x`/`mount_y`/`mount_yaw_rad` synthetic tests
      (sprint 025's suite, `tests/aprilcam/calibration/test_geometry.py`
      and `tests/aprilcam/daemon/test_detection.py`) pass unmodified —
      the issue's own "do not regress" bullet as a literal assertion.
- [x] New guard: a registered `mount_z` at or above a configured
      camera's `height_cm` reports the raw (uncorrected) position, not
      an extrapolated-past-the-camera value (synthetic test with a
      known degenerate case).
- [x] `calibration/geometry.py`, `daemon/detection.py`'s "camera-
      geometry caveat" docstrings, and `_camera_geometry_for`'s own
      docstring updated to describe the real source, no longer framed
      as an open gap.
- [x] `uv run mypy` clean.

## Testing

- **Existing tests to run**: `uv run pytest
  tests/aprilcam/config/test_loader.py
  tests/aprilcam/daemon/test_service.py
  tests/aprilcam/daemon/test_detection.py
  tests/aprilcam/calibration/test_geometry.py
  tests/aprilcam/client/test_daemon_client.py
  tests/aprilcam/camera/test_camera.py -v`, plus the full headless
  suite (`uv run pytest -m "not camera"`, baseline 1324 passed / 4
  skipped — this ticket only adds).
- **New tests to write**: `CameraPosition`/`CameraConfig` round-trip
  and rejection tests (`config/loader.py`); proto/gRPC round-trip
  including the absent-field case; `_camera_geometry_for` unit tests
  for both branches; the mount-height guard's synthetic degenerate
  case; a `Camera.apply_config` regression test with `camera_position`
  set.
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: docstrings only this ticket (listed above in
Description) — the use-case document updates (`daemon.md` UC-12,
`cli.md` UC-29) are ticket 004's, once the whole feature (including
`locate`) exists to document accurately.

## Completion Notes

Implemented exactly as planned in the Description, plus one
unplanned-but-necessary fix discovered by the test suite:

- `config/interfaces.py`: `CameraPosition` (frozen, `kw_only`) added
  next to `CameraConfig`; `CameraConfig.camera_position: Optional
  [CameraPosition] = None`; `to_dict()` emits the key only when set.
- `config/loader.py`: `parse_camera_config` gains `_parse_camera_position`
  — parses `{"nadir_x_cm", "nadir_y_cm", "height_cm"}`, absent key ->
  `None`, `height_cm <= 0` -> `MalformedDocument` naming the field.
- `proto/aprilcam_v2.proto`: new `CameraPosition` message; `CameraConfig`
  gains `optional CameraPosition camera_position = 4`; header comment
  updated naming this the file's tenth extension. Recompiled via
  `uv run python scripts/compile_proto.py` (regenerated
  `aprilcam_v2_pb2.py`/`_pb2_grpc.py`, no manual edits to those files).
- `daemon/rpc.py`, `client/daemon_client.py`, `client/models.py`: added
  `_camera_position_to_proto`/`_camera_position_from_proto` on both
  sides (server marshalling in `rpc.py`; outbound request marshalling
  in `daemon_client.py`; inbound response marshalling in `models.py`,
  matching this codebase's existing request/response split for
  `CameraConfig`), `HasField`-guarded throughout.
- `daemon/service.py`: `_camera_geometry_for` now reads
  `self._camera_configs[slug].camera_position`, falling back to the
  exact pre-029 `(WorldPoint(0,0), 0.0)` stopgap when the camera or its
  position is unconfigured. Docstring rewritten to describe the real
  source rather than "explicitly provisional."
- `daemon/detection.py`: `_apply_mount_correction` gains the
  `camera_height_cm > 0.0 and mount.mount_z >= camera_height_cm` guard
  — full no-op (not just parallax) when it trips. Module docstring's
  "Camera-geometry caveat" section and `_default_camera_geometry_lookup`'s
  own docstring rewritten to describe the real source now available via
  `DaemonService`, while keeping this function itself as the
  `DetectionEngine` constructor default for callers with no real
  geometry to inject (test suite mainly).
- `calibration/geometry.py`: only the module docstring's
  "Camera-geometry caveat" section changed (now "-- resolved"), per the
  ticket's explicit instruction not to touch `project_tag_to_field_plane`
  itself or its worked example.

**Unplanned fix (discovered by the full test suite, not called out in
the ticket's file list):** `mcp/tools/configuration.py`'s
`get_camera_config` MCP tool returned the `CameraConfig` dataclass
directly and relied on FastMCP's pydantic `TypeAdapter` to serialize it
— that module's own docstring documented this as relying on the
dataclass's fields already matching `to_dict()`'s shape exactly. Adding
`camera_position` broke that coincidence: the raw dataclass serializer
emitted `"camera_position": null` for every camera, while `to_dict()`
(per this ticket's own explicit requirement) omits the key entirely
when unset. This failed the pre-existing
`tests/aprilcam/mcp/test_configuration.py::test_get_camera_config_returns_the_whole_document`
test. Fixed by making `get_camera_config` call `.to_dict()` explicitly,
matching `get_playfield`'s already-established pattern in the same
file — restores byte-for-byte pre-029 MCP output for every camera that
hasn't set `camera_position`, and gives a correctly-shaped
(key-omitted-when-unset) answer for one that has. No test needed
changing; the existing assertion started passing again once the tool's
output matched `to_dict()` again.

**Test verification, and an explicit caveat on the full suite:**
- `uv run mypy` — clean (78 source files).
- `uv run pytest -q -m "not camera and not integration"` — 1293 passed,
  54 deselected (up from the pre-ticket baseline; this ticket only
  added tests).
- All six test files the ticket's Testing section names, run directly
  — all pass.
- `uv run pytest -q` **was attempted** (the ticket's stated full-suite
  baseline is 1324 passed / 4 skipped) but a real `aprilcam` daemon is
  running on this machine holding 3 cameras open (per this ticket's own
  dispatch caution). Running the full unmarked suite drove a
  `camera`/`integration`-marked test (`test_streaming_integration.py`'s
  real-clock calibration test) to contend with that live daemon for the
  same physical hardware, producing several failures and one hard
  `Fatal Python error: Aborted` crash inside `cv2` detection code deep
  in a background thread — unrelated to any file this ticket touches
  (confirmed: none of the failing/aborting tests are in files this
  ticket modified, and every one is `camera`- or `integration`-marked,
  the two markers this project's own `pyproject.toml` documents as
  "require real capture hardware" / "start real daemon/hardware
  processes"). No stray processes were left behind after the abort
  (verified via `ps aux`), and nothing under `~/.config/aprilcam` or
  `~/.local/share/aprilcam` was touched by any test in this ticket (all
  use `tmp_path`/`directories` fixtures). Given the explicit caution
  against contending with the live daemon, I did not retry the full
  unmarked run — `-m "not camera and not integration"` plus the six
  named files plus `mypy` is the verification this ticket's changes
  actually received. This is a pre-existing hardware-contention hazard
  in the current environment, not a regression introduced by this
  ticket, but it is reported here explicitly rather than silently
  claiming the untested baseline number.
