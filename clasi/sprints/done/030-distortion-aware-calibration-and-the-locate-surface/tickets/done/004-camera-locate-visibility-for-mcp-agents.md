---
id: '004'
title: camera locate visibility for MCP agents
status: done
use-cases:
- SUC-004
depends-on: []
github-issue: ''
issue: camera-locate-missing-from-mcp-surface-and-agent-guide.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# camera locate visibility for MCP agents

## Description

Sprint 029 made `camera locate` a precondition for a registered
`mount_z` to have any effect, but that precondition is documented only
in the CLI/`robot` guide direction: `AGENT_GUIDE.md` (the one document
an MCP-only agent is told to read first) never mentions `locate`, and
nothing on the MCP tool surface reveals that a calibrated-but-unlocated
camera silently serves the raw tag position instead of the
parallax-corrected one. This ticket closes the trap the smallest way
that actually closes it: documentation plus an observable flag, not a
new MCP tool and not a stricter guard (see sprint.md's Architecture
section, Design Rationale Decisions 4 and 5, for why both of those
alternatives were considered and rejected).

This ticket also has no dependency on tickets 001-003 — it needs no
distortion math, only a read of state that already exists
(`_camera_geometry_for`'s existing fallback check). It is grouped into
this sprint rather than deferred because this sprint's own
re-verification work (ticket 003) will re-run `camera locate`, and
shipping a "you'll need to re-locate" requirement without also making
the precondition visible to MCP agents would make the underlying
problem worse, not better.

**Stakeholder amendment**: `list_tag_parameters` carrying
`mount_z_applied` is discoverable *if you look* — but the defect being
fixed is silence at the point of the actual mistake. The warning must
also appear on `register_tag`'s own response, at the moment a nonzero
`mount_z` is registered while no camera anywhere holds a
`camera_position` — in addition to, not instead of, the
`list_tag_parameters` flag. Same non-blocking posture as everywhere
else in this ticket: `register_tag` still succeeds, still does not
raise; it just no longer answers with silence when it already knows
the registration is currently inert.

## Acceptance Criteria

- [x] `AGENT_GUIDE.md` §7's command table gains a `camera locate` row (CLI-only), with a one-line description matching the existing table's style and column shape.
- [x] `AGENT_GUIDE.md` §6 states the located-camera precondition for a nonzero `mount_z` (calibrated is necessary but not sufficient; the observing camera must also be located) at the point where an agent about to register a mount will actually read it, and explicitly states there is no MCP tool for locating — "ask your operator to run `aprilcam camera locate`" — per Decision 5.
- [x] The daemon computes, per mount registration, whether its `mount_z` is currently being applied: `False` whenever `mount_z > 0` and the observing camera's geometry lookup falls back to the pre-029 `(0, 0)`/`0.0` stopgap (reuses the existing `_camera_geometry_for` check — no new guard logic, no change to `register_tag`'s existing `NotCalibrated` gate).
- [x] `mcp/tools/mobile_tags.py`'s `TagMountEntry` gains `mount_z_applied: bool`; `list_tag_parameters` populates it per entry.
- [x] The wire shape backing `list_tag_parameters` (proto `TagMountEntry` message, `daemon/rpc.py`, `client/models.py`) carries the new field additively — no existing field renumbered or removed.
- [x] `mcp/tools/mobile_tags.py`'s `register_tag` docstring is corrected: it currently says camera geometry is modelled "nowhere in this codebase... yet" — stale since sprint 029. Update it to state that geometry modelling exists (sprint 029) and the remaining gap is *locating* a specific camera, matching `AGENT_GUIDE.md`'s new language, and pointing an agent at `list_tag_parameters`'s new flag.
- [x] Unit test confirms `register_tag`'s existing behaviour is unchanged: registering a nonzero `mount_z` against a calibrated-but-unlocated camera still succeeds with no error (no regression from tightening anything), and a subsequent `list_tag_parameters` call reports `mount_z_applied=False` for that registration.
- [x] Unit test confirms `mount_z_applied` flips to `True` once the observing camera's `camera_position` is set (mirrors `test_mount_z_is_a_noop_until_camera_position_is_pushed_then_engages_with_no_restart`'s existing live-mechanism-proof shape, at the unit/headless level — no new hardware test required for this ticket).
- [x] `ROBOT_API_GUIDE.md` is checked against the issue's own claim that it "states the constraint correctly" — confirmed correct as-is, or corrected if drift is found; not touched otherwise.

**Warn at the moment of registration (stakeholder amendment)**

- [x] `RegisterTagResponse` (`proto/aprilcam_v2.proto`, currently `message RegisterTagResponse {}`) gains an additive optional field (e.g. `optional string warning = 1`) — no existing field affected, since none exist yet on this message.
- [x] `daemon/service.py`'s `register_tag` computes the same "no camera anywhere holds a `camera_position`" condition used for `mount_z_applied` (mirrors `NotCalibrated`'s existing "any camera" reading, per Decision 4 — a mobile tag is not bound to one specific camera) and, when true for a nonzero `mount_z`, returns a warning describing the registration is currently inert and naming `aprilcam camera locate` as the fix — computed and returned, never raised, never blocking the registration itself.
- [x] `daemon/interfaces.py`'s `Daemon.register_tag` signature is updated to return the optional warning (e.g. `Optional[str]`) instead of `None`; `client/daemon_client.py`'s `register_tag` and every existing caller are updated to accept the new return shape.
- [x] `mcp/tools/mobile_tags.py`'s `register_tag` tool surfaces the warning in its response (rather than returning bare `None` as today) when present.
- [x] `cli/tags mount register` prints the warning to stderr (non-fatal — the registration still succeeds and the command still exits 0) when present.
- [x] Unit test: registering a nonzero `mount_z` while no camera holds a `camera_position` returns/prints the warning; registering the same while at least one camera holds a `camera_position` (even a different, unrelated camera — matching the existing "any camera" scope) returns no warning, mirroring `NotCalibrated`'s existing precedent exactly.
- [x] Unit test: registering a zero `mount_z` (the default) never produces a warning, regardless of any camera's located state — there is nothing for parallax to project in that case (mirrors `register_tag`'s existing `NotCalibrated` scoping).

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/mcp/test_mobile_tags.py tests/aprilcam/daemon/test_service.py tests/aprilcam/daemon/test_rpc.py tests/aprilcam/daemon/test_detection.py tests/aprilcam/cli/ -v` — confirm `register_tag`'s guard and `mount_z`'s existing no-op-until-located behaviour are unchanged, and that every existing caller of `Daemon.register_tag`/`client/daemon_client.py`'s `register_tag` is updated for its new return shape.
- **New tests to write**: `mount_z_applied` computation and reporting (headless, hand-picked geometry, mirroring `test_detection.py`'s existing style for this exact correction path); the `register_tag` response warning (present/absent cases above), including RPC/proto round-trip of the new additive `RegisterTagResponse` field.
- **Verification command**: `uv run pytest tests/aprilcam/mcp/ tests/aprilcam/daemon/ tests/aprilcam/cli/ -v`

## Completion Notes

**Design decision not spelled out in the ticket text: `Daemon.list_tag_parameters`'s
return shape had to widen too.** The ticket names `mcp/tools/mobile_tags.py`'s
own `TagMountEntry` and "the wire shape" (proto `TagMountEntry`,
`daemon/rpc.py`, `client/models.py`) as needing the new field, but for
`mount_z_applied` to actually reach the MCP tool from the daemon at
all, `Daemon.list_tag_parameters()` itself has to return something
richer than `Dict[TagId, MountParameters]` — that dict is the *only*
channel between `DaemonService` (which alone can compute the flag,
reusing `_camera_geometry_for`) and every caller (MCP tool, CLI,
`GrpcDaemon`). Introduced `daemon.interfaces.MountRegistration`
(`params: MountParameters, mount_z_applied: bool`), a frozen dataclass
alongside `DaemonStatus` in `daemon/interfaces.py` per this codebase's
value-shaped-interface rule. `Daemon.list_tag_parameters` now returns
`Dict[TagId, MountRegistration]`. This rippled to every existing
caller, matching the same "find them all" standard the ticket set
explicitly for `register_tag`'s own return-type change:
`daemon/service.py` (computes it), `daemon/rpc.py` (marshals
`.mount_z_applied` onto the wire `TagMountEntry`, no business logic
added — `_Servicer`'s own "marshalling only" rule stays intact since
the flag is computed daemon-side, not by the RPC layer),
`client/models.py`'s `tag_parameters_from_proto`,
`client/daemon_client.py`'s `list_tag_parameters`,
`mcp/tools/mobile_tags.py`'s `list_tag_parameters` tool, `cli/tags.py`'s
`_print_registrations`/`cmd_list` (unpacks `.params`, does not print
the new flag — not asked for, and `AGENT_GUIDE.md`'s new text
deliberately points agents at the MCP tool's flag, not a CLI one),
`cli/locate.py`'s reference-contamination guard (unpacks `.params`),
and every test double/fixture across `tests/aprilcam/{daemon,mcp,cli,client}/`
that stood in for a `Daemon`.

**Every caller of `Daemon.register_tag` updated for its new `Optional[str]`
return** (the ticket's own explicit ask): `daemon/service.py`
(computes the warning via the same `_any_camera_located()` helper
`mount_z_applied` uses), `daemon/rpc.py`'s `RegisterTag` handler
(`HasField`-guards `RegisterTagResponse.warning`, mirroring
`CalibrationResult.reason`'s existing precedent exactly),
`client/daemon_client.py`'s `register_tag`, `mcp/tools/mobile_tags.py`'s
`register_tag` tool (now returns `Optional[str]` instead of bare
`None`), `cli/tags.py`'s `cmd_register` (prints `warning: {warning}` to
stderr, non-fatal, exit code unchanged), and every fake `Daemon` in the
test suite.

**Shared logic, one helper, reused everywhere it's needed.**
`DaemonService._any_camera_located()` (new) reuses the existing
`_camera_geometry_for` per configured camera — "known vs. the pre-029
`(0,0)`/`0.0` stopgap" is the exact same read `_camera_geometry_for`'s
own docstring already documented, just checked across every camera
instead of one. `_mount_z_applied(mount_z)` (new) is `True` for
`mount_z <= 0` (vacuous — nothing for parallax to withhold) and
otherwise `_any_camera_located()`. Both `register_tag`'s warning and
`list_tag_parameters`'s `mount_z_applied` call these same two methods
— no duplicated condition, no risk of the two drifting apart.

**No change to `register_tag`'s existing `NotCalibrated` guard** — its
`params.mount_z != 0.0 and not self._calibrations` check is untouched,
still runs first, still inside the lock, still raises before the new
warning logic (which runs after the lock is released, purely
informational) is ever reached. Decision 4 stays intact: registering a
mount before running `camera locate` on the observing camera keeps
working.

**`ROBOT_API_GUIDE.md`**: read in full (`src/aprilcam/ROBOT_API_GUIDE.md`
lines ~55-108, "Is Your Tag Mounted on a Robot?"). It already states
the located-vs-calibrated distinction correctly ("Calibration alone is
not enough for the correction to actually move the reported position
... run `aprilcam camera locate` once per camera ... A camera nobody
has `locate`d accepts a nonzero `mount_z` registration without error
but reports the tag's raw, uncorrected position"). Confirmed correct
as-is per the issue's own claim; not touched, per the ticket's explicit
instruction.

**`AGENT_GUIDE.md` wording is deliberately consistent with ticket 002's
real-hardware finding** (camera 3's near-exact-nadir mount makes the
`fx`/depth degeneracy hard to resolve from a single coplanar view) —
§6's new text says locating "is also not guaranteed to succeed on the
first try: a camera mounted very close to straight-down (near-nadir)
can make the solve numerically ill-conditioned," rather than implying
`camera locate` always just works.

**Testing performed.** `uv run pytest tests/aprilcam/mcp/test_mobile_tags.py
tests/aprilcam/daemon/test_service.py tests/aprilcam/daemon/test_rpc.py
tests/aprilcam/daemon/test_detection.py tests/aprilcam/cli/
tests/aprilcam/client/test_daemon_client.py -q` — 673 passed, all
green. Broader `tests/aprilcam/mcp/ tests/aprilcam/daemon/
tests/aprilcam/cli/ -m "not camera"` — 822 passed, 40 deselected
(hardware-marked). Two `@pytest.mark.camera` hardware tests were also
run live against the real arducam attached to the implementation
environment: `tests/aprilcam/mcp/test_integration.py::test_camera_golden_path_against_real_daemon_and_camera_3`
and `tests/aprilcam/daemon/test_mobile_tags_hardware.py::test_register_mount_offset_via_cli_moves_reported_position_clear_reverts_it`
both fail — both failing identically (`git stash` to the pre-ticket
commit and re-run, confirmed byte-for-byte the same failure reason) on
a pre-existing, unrelated cause: the physical arducam currently cannot
see the full `main-playfield` (missing several/all ArUco ids at
calibration time) in this environment right now. Neither failure
touches this ticket's code path — the second test never reaches
mount-registration logic at all (it fails at the `camera calibrate`
step, before `tags mount register` is ever invoked), and this ticket's
own new code (the `mount_z` warning/flag) is never exercised by that
module in the first place, since it deliberately registers only
`mount_x`/`mount_y`/`mount_yaw_rad`, never a nonzero `mount_z` (see
that module's own docstring). Both are pre-existing, environment-level
gaps this sprint's ticket 005 owns; not chased here, per this ticket's
explicit instruction.

**Every caller of `Daemon.register_tag`, for the report:**
`client/daemon_client.py` (`GrpcDaemon.register_tag`), `mcp/tools/mobile_tags.py`
(`register_tag` tool), `cli/tags.py` (`cmd_register`), `daemon/rpc.py`
(`_Servicer.RegisterTag`) — all four updated for the new
`Optional[str]` return.
