---
id: '001'
title: Playfield definitions and camera-playfield link
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: calibration-deskew-and-the-live-view.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Playfield definitions and camera-playfield link

## Description

Implements SUC-001 (parent UC-02, UC-03 link slice): playfield
definitions held by the daemon, plus a minimal camera→playfield link,
per `sprint.md` Architecture Decision 1 (config-service scope — full
hardware-settings push stays deferred to sprint 023). This is the
foundation every later ticket in this sprint depends on: calibration
(ticket 003) cannot proceed without a `PlayfieldDefinition` to match
detected ArUco ids against.

Builds:

- `config/loader.py` (new) — concrete `ConfigLoader`: `load_playfield`/
  `load_camera_config` from `Path`, JSON → typed documents, whole-
  document validation (`MalformedDocument` on: no `name`, malformed
  JSON, duplicate ArUco id at different coordinates, field-size/corner
  inconsistency). A concrete `PlayfieldDefinition` subclass implementing
  `features()`/`required_aruco_ids()`.
- `daemon/service.py` — `set_playfield`/`get_playfield`/`list_playfields`:
  the daemon holds zero-or-more definitions in memory, keyed by name
  (Architecture Decision 4 — no single-slot cardinality limit).
  `set_camera_config`/`get_camera_config` implemented as the minimal
  link slice: accepts/echoes `name` + `playfield` + `settings`
  (`settings` is stored and returned but **never applied to hardware**
  — Decision 1's stated, temporary limitation). `reset_camera_config`
  stays `NotImplementedError` — do not stub it as a no-op.
- `playfield/playfield.py` (new) — concrete `Playfield` with
  `linked_cameras()` derived at serve time from the daemon's held
  camera configs.
- `proto/aprilcam_v2.proto` — additive `SetPlayfield`/`GetPlayfield`/
  `ListPlayfields`/`SetCameraConfig`/`GetCameraConfig` RPCs + messages;
  `daemon/rpc.py`, `client/models.py`, `client/daemon_client.py` wired
  to match.
- `cli/playfield.py` (new) — `aprilcam playfield config <file>` (push).
  `cli/camera.py` extended — `aprilcam camera config <file>` (push,
  playfield-link only).

`MalformedDocument`/`UnknownPlayfield` already exist in `errors.py` —
reuse them, do not redeclare.

**Archive pointers**: none directly reusable — this is new v2
document-model code written fresh against `config/interfaces.py`'s
already-declared contract, not a port of v1's `camera_config.py`
load/save (different shape, different lifecycle).

## Acceptance Criteria

- [x] Pushing `config/aprilcam-old/playfields/main-playfield.json` and
      fetching it back returns all 25 features with identical
      coordinates — ArUco 1 at (−67, 44.65), ArUco 5 at (67, −44.65)
      (UC-02.S-1).
- [x] A definition with ArUco id 3 listed twice at different
      coordinates is rejected whole, naming the duplicate id; a
      previously-held definition is returned unchanged on a subsequent
      fetch (UC-02.S-2).
- [x] A camera-config or playfield document with no `name` field raises
      `MalformedDocument` client-side, before any RPC is sent (UC-23.S-4
      pattern) — verify via `ConfigLoader` directly, not only through
      the daemon.
- [x] Pushing a `CameraConfig` naming `arducam-ov9782-usb-camera` with
      `playfield: "main-playfield"` and re-fetching it via
      `get_camera_config` returns the same `playfield` value;
      `list_playfields()`/`get_playfield()` on `main-playfield` reports
      that camera in its `linked_cameras()`.
- [x] `get_playfield` on a name the daemon does not hold raises
      `UnknownPlayfield`, listing the playfields that do exist.
- [x] A pushed `CameraConfig.settings` dict round-trips through
      `get_camera_config` unchanged but has no observable effect on the
      camera — assert via a mock/spy backend that zero setting-
      application calls occur.
- [x] `reset_camera_config` raises `NotImplementedError` — explicitly
      not built this sprint, never silently a no-op.
- [x] The daemon holds two differently-named playfield definitions
      simultaneously without either affecting the other (Decision 4).
- [x] After a simulated daemon restart (fresh `DaemonService`
      instance), pushed playfields and camera links are gone (UC-02's
      ephemeral rule).

## Testing

- **Existing tests to run**: `uv run pytest
  tests/aprilcam/daemon/test_service.py tests/aprilcam/config/
  tests/aprilcam/cli/` — confirm no regression to sprint 020/021
  behavior (`get_frame`, `get_tags`, camera listing all unaffected).
- **New tests to write**: `tests/aprilcam/config/test_loader.py`
  (round-trip `load(x.to_dict()) == x` for both document types,
  malformed-document rejection, duplicate-id rejection);
  `tests/aprilcam/playfield/test_playfield.py` (`linked_cameras()`
  against a fake camera-config store); `tests/aprilcam/daemon/
  test_service.py` additions for the five new RPC method bodies
  (headless, `FakeBackend`-driven daemon per the existing pattern); a
  wire round-trip test using the existing `stub`/`grpc.Server`-over-
  `tmp_path`-socket fixture pattern.
- **Verification command**: `uv run pytest && uv run mypy` — must stay
  clean; zero `typing.Protocol` (all new classes derive from
  `config/interfaces.py`'s/`playfield/interfaces.py`'s already-declared
  base classes, or are frozen `@dataclass(kw_only=True)`); baseline is
  388 passing — this ticket only adds tests, never removes or skips an
  existing one.
