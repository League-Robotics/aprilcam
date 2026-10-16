# mcp/

**Owner:** Eric Busboom · **Last reviewed:** 2026-10-16 · **Status:** stable

---

## 1. Purpose

`mcp/` is the AI-agent-facing surface of AprilCam: a FastMCP stdio
server that exposes perception, configuration, mobile-tag, and
annotation capabilities as MCP tool calls. It is a pure daemon client —
per the vision-boundary rule (`docs/design/design.md` §2 principle 1)
and the general adapter discipline (`docs/design/adapters.md`), it does
no OpenCV or pixel work of its own. Image-returning tools pass
already-encoded bytes straight through; nothing here decodes,
re-encodes, or inspects a pixel. This is mechanically enforced by
`tests/test_no_cv2_in_client_modules.py`'s scan of the package.

Every tool this package registers calls an RPC that already exists on
`daemon.interfaces.Daemon` (`adapters.md` rule 5) — a new tool must
never require a new daemon capability, and that discipline is the
signal that the boundary between `mcp/` and the daemon is drawn
correctly.

## 2. Orientation

| File | Role |
|---|---|
| `server.py` | Assembles the `FastMCP` instance via `build_server()`; registers each tool group; owns the server's `instructions` text and the one daemon-free tool, `get_guide` |
| `connection.py` | `ConnectionManager` — resolves a `Daemon` per tool call from a small cache keyed by target host; cache-and-evict-on-failure |
| `errors.py` | `handle_tool_errors` — maps `aprilcam.errors.AprilCamError` and any other exception to a clean MCP `ToolError` |
| `images.py` | `create_temp_dir` / `image_response` — shapes already-encoded image bytes into `format="base64"` (inline) or `format="file"` (temp path) MCP content |
| `tools/perception.py` | Read-only queries: cameras, frames, tags, tag history, `where` |
| `tools/configuration.py` | Whole-document camera/playfield config, calibration trigger, and (sprint 037) flat-field gain-map capture/inspect/clear |
| `tools/mobile_tags.py` | Mount-registry: `register_tag` / `unregister_tag` / `list_tag_parameters` |
| `tools/annotations.py` | World-coordinate drawing/annotation-store surface: `draw_marker`/`draw_path`/`draw_polygon`/`draw_circle`/`draw_text`/`remove_annotation`/`clear_layer`/`list_annotations` |

Every tool group module exposes exactly one `register(mcp, connection,
temp_dir)` entry point, called once from `server.py`'s `build_server`.
`server.py`'s own docstring frames ticket 001 (`get_guide`) as "the
pattern-setter proving the whole stack end to end with no daemon
anywhere," with tickets 002-005 (perception, configuration,
mobile_tags, annotations) each extending `build_server`'s body with
their own `register` call against the same `connection`/`temp_dir`
locals.

## 3. Constraints and Invariants

- **Zero daemon RPCs during construction.** Neither
  `ConnectionManager.__init__` nor `create_temp_dir` touches a daemon —
  `__init__` only stores its arguments, and `discovery_factory` is not
  called until the first `resolve`/`call`. This means `build_server()`
  always succeeds, with or without any daemon running anywhere. The one
  deliberate exception, `get_guide`, reads `client.guides.Guides`
  directly with no `Discovery`/`Daemon` involved at all — the one
  capability that must work before any daemon exists, mirroring
  `cli/guide.py`'s identical exception.
- **Cache-and-evict-on-failure, not reconnect-per-call or
  preflight-probe.** `ConnectionManager` resolves a target once and
  reuses it across calls until an actual RPC raises
  `ConnectionFailed`, at which point only that target's cache entry is
  evicted — the next call for that target re-resolves; every other
  cached target is untouched.
- **The `from __future__ import annotations` / `mcp.tools.annotations`
  name-collision trap**, documented in `server.py`'s own import comment
  and `mcp/tools/__init__.py`'s module docstring: `from __future__
  import annotations` is the one statement in the language that also
  *binds a name* — it sets `annotations` in the importing module's
  namespace to a `__future__._Feature` object, not just a compiler
  flag. Because `mcp/tools/__init__.py` has that import, `from
  aprilcam.mcp.tools import annotations` in `server.py` would resolve
  the wrong object via a successful `getattr` on the package and never
  trigger the `mcp/tools/annotations.py` submodule import at all
  (`AttributeError: '_Feature' object has no attribute 'register'`,
  not an import error — non-obvious). Mitigations: `server.py` imports
  it aliased (`from aprilcam.mcp.tools import annotations as
  annotation_tools`), and `mcp/tools/__init__.py` deliberately omits
  `from __future__ import annotations` (it has no annotations of its
  own to postpone) for as long as the same-named submodule exists.
- **Image-return-format convention.** `format="base64"` returns an
  inline `ImageContent` and writes nothing to disk; `format="file"`
  writes to disk under a per-process temp directory
  (`tempfile.mkdtemp(prefix="aprilcam-mcp-")`, created once at server
  startup) and returns only that path — never both, and never a path
  the caller did not ask for (spec principle 7, "clients see handles
  and services, never file paths," applied doubly since an MCP agent
  typically has no filesystem of its own). No TTL/cleanup policy exists
  this sprint; files accumulate for the life of the process (see Open
  Questions).
- **No decode, no re-encode, no cv2 in `images.py`.** Base64-encoding
  bytes for `format="base64"` is wire transport (MCP's `ImageContent`
  shape requires base64 text), not a pixel operation.
- **Push tools take a JSON object, never a file path.** `set_camera_config`
  / `set_playfield` accept the whole document as a `dict` tool
  parameter, validated client-side by `config.loader.ConfigLoader`'s
  dict-based `parse_camera_config`/`parse_playfield` entry points
  *before* any daemon connection is attempted — a malformed document
  raises `MalformedDocument` with zero RPCs, mirroring the file-based
  CLI's "a bad file never reaches the daemon" guarantee.
- **Fetch tools return `to_dict()`-equivalent shapes, not raw
  dataclasses**, for any type whose wire shape has since diverged from
  its dataclass shape (e.g. `camera_position`, omitted when unset
  rather than serialized as `null`) — `get_playfield`/`list_playfields`/
  `get_camera_config` all call `.to_dict()` explicitly rather than
  relying on FastMCP's raw pydantic introspection to coincidentally
  still match.
- **`calibrate`/`get_calibration` use the interface-declared,
  cv2-free types** (`calibration.interfaces.Calibration`/
  `CalibrationResult`), never the concrete cv2-backed
  `calibration.calibration.Calibration` — importing the concrete type
  would pull `cv2` into `mcp/` transitively, violating the "cv2 absent
  from sys.modules after importing every mcp/ module" success
  criterion even though no tool here ever touches a camera.
- **Pose provenance in the MCP results (sprint 045).** `calibrate` and `get_calibration` return `method`, `method_reason`, `confidence`, `corner_count`, `reprojection_error_px`, `scale_agreement`, `angular_spread_deg`, camera height/nadir/tilt, `notes`, `helpful_missing_ids` and the worst leave-one-out id/value, plus per-camera `calibration_method`/`calibration_confidence` in the camera listing, all as plain JSON from the cv2-free interface types. `with_intrinsics` no longer exists. An agent should treat `reduced`/`low` as a prompt to add or uncover markers.
- **`capture_flatfield`/`get_flatfield`/`clear_flatfield` (sprint 037)
  follow the identical rule**, using `calibration.interfaces.FlatFieldResult`/
  `FlatFieldInfo` — never `calibration.flatfield.FlatFieldCapturer`'s
  own module (which imports `cv2` at module scope, same as
  `calibration.calibration`) — for exactly the same reason.
- **`list_tag_parameters` and `list_annotations` differ in whether they
  need a flattening shim.** `Daemon.list_tag_parameters` returns a
  dataclass-keyed `dict`, which does not survive the MCP/JSON boundary
  (JSON object keys must be strings; pydantic would stringify via
  `repr()`), so `mobile_tags.py` defines `TagMountEntry` — a flat
  tuple of `(tag, params, mount_z_applied)` pairs — as its JSON-safe
  shape. `list_annotations` needs no equivalent: `AnnotationSet.annotations`
  is already a plain tuple, and `Annotation.shape`'s `Union` field
  serializes cleanly via each member's own concrete runtime type.

## 4. Design

**Tool-registration extension pattern.** Every tool-group module
follows the identical shape: define `register(mcp, connection,
temp_dir)`, decorate each tool function with `@mcp.tool()` then
`@handle_tool_errors` (in that order, so `functools.wraps` preserves
the signature FastMCP's schema introspection needs), and inside the
body call `connection.call(lambda d: d.<method>(...), host=daemon)`.
No tool group holds a `Daemon` of its own or re-derives business logic
the daemon already owns (upsert semantics, no-op-on-absent behavior,
precondition ordering) — each docstring is explicit about which
concern belongs to the daemon and is "not reimplemented here."

**Connection management.** `ConnectionManager.call(fn, host=...)`
resolves a `Daemon` for the target (explicit per-call `host` argument
wins, falling back to the server's session default —
`aprilcam mcp -d HOST`, or local), invokes `fn` against it, and on a
`ConnectionFailed` evicts only that target's cache entry before
re-raising. This is the one place a failed RPC triggers self-healing;
every tool function routes through it rather than holding a connection
itself.

**Error mapping.** `handle_tool_errors` wraps a tool function so any
`aprilcam.errors.AprilCamError` becomes a `ToolError(str(exc))` —
passing through that exception's own already agent-legible message
verbatim — and any other exception becomes a generic
`"internal error: {type name}"` `ToolError`, with the original
exception and traceback logged server-side via `logger.exception` and
never echoed to the agent. This was verified directly against the
pinned `mcp==1.26.0` SDK: raising from inside an `@FastMCP.tool()`
function produces a clean `CallToolResult(isError=True, ...)`, never a
crashed transport. Every tool function in every tool-group module
applies this decorator; `server.py`'s `get_guide` does too, exercising
the mechanism end to end with no daemon involved.

**Image output shaping.** `images.image_response(data, encoding=,
format=, temp_dir=)` is the sole place output-format policy lives,
factored out because it is a distinct concern from which bytes to
fetch. `get_frame` (`tools/perception.py`) is the only tool that calls
it — every other tool either returns no image or hands a structured
dataclass straight back for FastMCP's pydantic `TypeAdapter` to
serialize natively.

### Intrinsics tools (sprint 044, extends the ticket 044-009 entry in §5)

The four tools mirror the CLI's daemon commands one for one; an agent starts a session, polls `get_intrinsics_status` about once per second (an unpolled session auto-aborts after 120 s), follows `next_hint`, and calls `finish_intrinsics(install=True)` only when `good_enough`. Board resolution is the same chain as the CLI. `get_calibration` and `list_cameras` surface `lens_source`/`lens_pending`.

## 5. Interfaces

### Exposes

MCP tool groups, registered in this order by `server.py`'s
`build_server`:

- **`get_guide`** (server.py) — the packaged usage guide; the one tool
  that works with no daemon running anywhere.
- **Perception** (`tools/perception.py`) — `list_cameras`,
  `probe_cameras`, `get_frame` (raw or deskewed, base64/file),
  `get_tags`, `get_tag`, `get_tag_history`, `where`. Read-only; never
  mutates daemon state. **Sprint 039**: `get_tags`/`get_tag`/
  `get_tag_history` carry `TagRecord`'s new `image_yaw_deg` field with
  no code change in this file — none of the three needs a `.to_dict()`
  shim (§3's "fetch tools return `to_dict()`-equivalent shapes" rule
  only applies where the wire shape has *diverged* from the dataclass
  shape; `TagRecord` has not), so FastMCP's pydantic introspection
  picks up the new field automatically, the same way it already picks
  up every other `TagRecord` field.
- **Configuration** (`tools/configuration.py`) — `get_camera_config`,
  `set_camera_config`, `reset_camera_config`, `get_playfield`,
  `set_playfield`, `list_playfields`, `calibrate`, `get_calibration`,
  and (sprint 037) `capture_flatfield` (`exposure`/`frames`/`force`/
  `dry_run` optional parameters), `get_flatfield`, `clear_flatfield`.
  Whole-document push/fetch plus the calibration and flat-field-capture
  triggers.
- **Intrinsics** (`tools/intrinsics.py`, sprint 044) — `start_intrinsics`
  (`camera`, optional `board`), `get_intrinsics_status`,
  `finish_intrinsics` (`install`, `force`), `abort_intrinsics`. Return
  plain JSON dicts built from the cv2-free `intrinsics.interfaces` types;
  an agent polls status, relays `next_hint` to the human holding the
  board, finishes with install at `good_enough`, and always aborts when
  giving up (the daemon self-aborts after 120 s without polling).
- **Mobile tags** (`tools/mobile_tags.py`) — `register_tag`,
  `unregister_tag`, `list_tag_parameters`. In-memory mount-parameter
  registrations that do not survive a daemon restart.
- **Annotations** (`tools/annotations.py`) — `draw_marker`,
  `draw_path`, `draw_polygon`, `draw_circle`, `draw_text`,
  `remove_annotation`, `clear_layer`, `list_annotations`.
  World-coordinate authoring only; no rendering happens here.

Every daemon-backed tool accepts an optional `daemon` parameter for a
per-call target host, overriding the server's session default.

### Consumes

- `aprilcam.client.discovery.Discovery` / `aprilcam.client.interfaces.Discovery`
  — resolves a `Daemon` for a target host (`ConnectionManager`'s
  `discovery_factory`).
- `aprilcam.daemon.interfaces.Daemon` — the sole sanctioned way any
  adapter talks to a daemon; every tool's actual RPC.
- `aprilcam.client.guides.Guides` / `aprilcam.client.interfaces.Guides`
  — `get_guide`'s direct, no-daemon dependency.
- `aprilcam.config.loader.ConfigLoader` — `parse_camera_config`/
  `parse_playfield` for client-side document validation before any RPC.
- `aprilcam.calibration.interfaces.Calibration` / `CalibrationResult` /
  `FlatFieldResult` / `FlatFieldInfo` (sprint 037) — the cv2-free
  calibration and flat-field types `configuration.py` returns.
- `aprilcam.types` — the dataclass vocabulary (`CameraState`,
  `TagFrame`, `TagId`, `TagRecord`, `MountParameters`, `Annotation`,
  `AnnotationSet`, shape types) every tool builds or returns.
- `aprilcam.errors.AprilCamError` and subclasses — the taxonomy
  `errors.py` maps to tool-level failures.
- `mcp` (the FastMCP SDK, pinned `1.26.0`) — `FastMCP`, `mcp.types`,
  `mcp.server.fastmcp.exceptions.ToolError`.

## 6. Open Questions / Known Limitations

- **No temp-file cleanup/TTL policy.** `format="file"` output
  accumulates under the per-process temp directory for the life of the
  server process; this matches v1's behavior and is an explicit
  deferred item rather than an oversight, but a long-running agent
  session with heavy image traffic will grow that directory unbounded.
- **The `from __future__ import annotations` collision is a structural
  trap, not a one-time fix.** It will resurface if a future tool-group
  submodule is ever named the same as a stdlib/`__future__` name, or if
  someone "cleans up" `mcp/tools/__init__.py` by adding the omitted
  future-import back. The constraint is documented in three places
  (this doc, `server.py`'s import comment, `mcp/tools/__init__.py`'s
  docstring) precisely because it is easy to reintroduce.
- **`list_tag_parameters`'s `TagMountEntry` and `list_annotations`'s
  direct dataclass return are two different fixes to the same
  underlying "does this shape cross the JSON tool boundary cleanly"
  question**, decided per-case rather than by a single documented rule
  for future tool authors to follow — a future tool returning a
  dataclass-keyed structure would need to rediscover this reasoning.
