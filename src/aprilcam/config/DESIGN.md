# config/

**Owner:** Eric Busboom · **Last reviewed:** 2026-08-21 · **Status:** stable

---

## 1. Purpose

`config/` owns two related things, both load-bearing for every other
subsystem: **where AprilCam's files live** (`directories.py`) and **what a
configuration document looks like and how it is read** (`interfaces.py` +
`loader.py`). It is deliberately narrow — it does not open sockets, does
not talk to a daemon, and (see §3) does not write anything to disk. It is
the schema-plus-directory-resolution layer that `daemon/`, `camera/`,
`calibration/`, `client/`, `cli/`, `mcp/`, and `playfield/` all sit on top
of. `config/__init__.py` is empty (0 bytes) — there is no package-level
re-export surface; every consumer imports directly from
`aprilcam.config.directories`, `aprilcam.config.interfaces`, or
`aprilcam.config.loader`.

Two ideas from `docs/design/configuration.md` anchor the module (per its
own module docstrings):

- **Self-naming documents.** Every configuration document carries its own
  `name`; the target is never inferred from a filename.
- **File-backed vs. in-memory.** Disk is the source of record for
  `config_dir`; a document pushed to the daemon over RPC is a live
  override, discarded on restart. This directly implements design.md
  §2's "whole-document configuration" principle.

## 2. Orientation

- **`directories.py`** — concrete `Directories`, resolving `config_dir`,
  `state_dir`, and `socket_dir` once, eagerly, at construction (cached for
  the object's lifetime). Also defines `DEFAULT_DAEMON_PORT = 5280`, the
  one piece of daemon-wide-but-neither-daemon-nor-client-owned state that
  lives here to keep the dependency direction clean (`daemon/rpc.py` binds
  it; `client/discovery.py` re-exports it rather than redefining it).
- **`interfaces.py`** — the value-shaped documents (`ConfigDocument`,
  `CameraConfig`, `CameraPosition`, `PlayfieldDefinition`, `Feature`), all
  frozen `@dataclass(frozen=True, kw_only=True)` per CLAUDE.md's interface
  rule, plus two behavioral base classes (`ConfigLoader`, `Directories`)
  with `NotImplementedError` bodies. `PlayfieldDefinition` here is
  abstract — `features()`/`required_aruco_ids()` raise — because the base
  class has no data to answer them with.
- **`loader.py`** (~620 lines) — the concrete `ConfigLoader` and the
  concrete `PlayfieldDefinition` (adds the per-section `Feature` tuples
  the base class lacks). This is a **read/validate-only** module: it turns
  JSON (file or already-parsed dict) into typed, validated documents. It
  has no `save`/`dump`/`write` method of any kind.

Note: `directories.py`/`interfaces.py` name the regenerable root
`state_dir`; design.md §7's storage-layout diagram calls the same
concept `data_dir`. See §6.

`log_dir` (present in design.md §7's diagram) is not part of this
module — `configuration.md` §1 states logs go to stderr only, and
`Directories` has no `log_dir` property.

## 3. Constraints and Invariants

- **`config_dir` / `state_dir` split — daemon reads config, never writes
  it.** `interfaces.py`'s module docstring is explicit: "The daemon never
  writes config: the config root is human-owned and not writable by it,
  which makes the ephemeral rule structural rather than a policy the
  daemon has to remember." `config/` itself contains **no write path**
  anywhere — confirmed by grep: no `fsync`/tmp-file/atomic-write code
  exists in `config/*.py`. Every actual write to a config-shaped file in
  this codebase lives in a *consumer*: `cli/locate.py`'s
  `_write_camera_config_atomic` (writes `config_dir/cameras/<name>/
  config.json` — a human-facing CLI tool, the sanctioned writer of
  `config_dir`), `camera/registry.py`'s `_save` (registry, `state_dir`),
  and `daemon/service.py`'s calibration-result writer (`calibration.json`,
  `state_dir`). This is a deliberate absence, not an oversight — treat any
  future write path added directly inside `config/` as suspicious unless
  it is documented as an intentional boundary change.
- **Files never migrate implicitly** (design.md §2 point 8). `directories.py`'s
  `_resolve_root`/`_resolve_socket_dir` re-run the flag > env > default >
  fallback cascade fresh on every construction; there is no code path that
  moves or copies a file from one resolved root to another when the
  cascade's answer changes between runs.
- **Self-naming, enforced whole-document.** `_require_name` rejects any
  document with no (or blank) `name` — `MalformedDocument`
  (`aprilcam.errors`) — regardless of entry point (file path or dict).
  `load_playfield`/`load_camera_config`/`parse_playfield`/
  `parse_camera_config` either return a fully valid document or raise;
  they never return a partially-built one (UC-23.S-4). A bad file/dict
  never reaches an RPC.
- **Round-trip contract.** `load(x.to_dict()) == x` for both document
  types. `PlayfieldDefinition.to_dict()` is lossless for the fields this
  schema models (id/slug/x/y/colour/height_cm) but does not carry forward
  `cardinal`/per-entry `"type"`/`display_name`/rectangle-dot sizing —
  fields nothing currently reads back.
- **Feature validation specifics** (all in `loader.py`, all raising
  `MalformedDocument`): a feature positioned outside the declared field's
  half-extents plus a `1e-6` cm epsilon (`_BOUNDS_EPSILON_CM`, sized to
  absorb float round-off, not to mask a real data-entry mistake — the
  fixture's corner ArUcos sit exactly on the boundary); a duplicate
  AprilTag or ArUco id *within its own family* at two different
  coordinates (same id at the same coordinates is redundant, not
  rejected); an id-section (`april_tags`/`aruco_tags`) entry's
  `height_cm` that is negative (sprint 030 ticket 002 — a feature cannot
  sit below the field plane); a `camera_position.height_cm` that is not
  strictly positive (sprint 029 ticket 001 — non-positive is
  `project_tag_to_field_plane`'s own "nothing to correct" sentinel, so
  silently accepting one would silently configure a camera that can never
  parallax-correct).
- **`rectangles`/`dots` never gain elevation.** Their own pre-existing
  `"height_cm"` key already means the shape's own physical dimension
  (e.g. `main-playfield.json`'s `rect-northwest-purple` has
  `"width_cm": 5, "height_cm": 4`); `_parse_feature_height_cm` is
  guarded to run only for the two id-sections so it can never collide
  with that older meaning.
- **Atomic writes (tmp+fsync+rename) and "calibration.json before
  config.json" write ordering** are global conventions from design.md §6.
  Grounding note for whoever implements a future combined-write path:
  today no single code path in the repository writes both
  `calibration.json` and `config.json` in one operation — `cli/locate.py`
  writes only `config.json` (via its own atomic-write helper, not one
  provided by `config/`); `daemon/service.py` writes only
  `calibration.json`. See §6.
- **Socket dir is never part of the flag/env cascade** — `configuration.md`
  §1: "sockets are runtime plumbing nobody needs to choose the location
  of." No flag, no `APRILCAM_*` env var reaches `_resolve_socket_dir`.

## 4. Design

**Directory resolution algorithm** (`directories.py`). For `config_dir`
and `state_dir`, `_resolve_root` runs, in order: explicit constructor
argument ("flag" tier) -> `APRILCAM_CONFIG_DIR`/`APRILCAM_STATE_DIR` env
var -> the hard-coded system default (`/etc/aprilcam`, `/var/lib/aprilcam`)
if it passes an availability check -> an unprivileged user-home fallback
(`~/.config/aprilcam`, `~/.local/share/aprilcam`). The availability check
differs by root's semantics: `config_dir` must already exist and be
readable (`_config_dir_available` — nothing ever creates it, it is
human-owned); `state_dir` must be writable, or not exist yet with a
writable nearest existing ancestor (`_writable_or_creatable` — the daemon
may need to create it on first run). `socket_dir` is resolved separately
by `_resolve_socket_dir`, outside the flag/env cascade: `/run/aprilcam` if
writable/creatable, else `$XDG_RUNTIME_DIR/aprilcam`, else a per-uid
directory under the OS temp root (the fallback exercised on macOS and
headless-Linux-without-systemd-logind dev boxes). Every resolved root is
logged at INFO on construction and its cascade tier
(`"flag"`/`"env"`/`"default"`/`"fallback"`/`"xdg-runtime"`) is exposed via
`config_dir_source`/`state_dir_source`/`socket_dir_source` properties and
rendered as one line by `describe()`, for a startup banner — the concrete
answer to UC-01's "which roots were chosen is never a mystery."

**Load/validate mechanics** (`loader.py`). `parse_camera_config`/
`parse_playfield` are the canonical validation path, run against an
already-parsed `dict`; `load_camera_config`/`load_playfield` are thin
file-reading wrappers that delegate to them (`_read_json_object` handles
unreadable-path/invalid-JSON/non-object errors, then hands off). The dict
entry points exist because an MCP client has no filesystem the daemon can
reach (`mcp/tools/configuration.py`'s `set_camera_config`/`set_playfield`
tools call `parse_*` directly) — spec principle 7 ("clients see handles
and services, never file paths"). `_parse_section` builds one
`PlayfieldFeatureEntry` tuple per JSON section (`april_tags`, `aruco_tags`,
`rectangles`, `dots`), tagging each with a `kind` taken from *which
section it was found in* (never a per-entry `"type"` field, more robust
against a malformed one), applying bounds/id/height checks along the way;
`_reject_duplicate_ids` runs once per family afterward. `load_config_dir`
is the boot-time scan: `directories.config_dir/playfields/*.json` then
`cameras/*/config.json`, sorted-path order, playfields before camera
configs. It never raises for one malformed file among good ones — each
failure is caught and recorded in the returned `ScanResult.errors` (named
by path); a missing subdirectory, or a camera directory with no
`config.json`, is silently skipped, not an error. This is the
skip-and-warn boot posture `daemon/__main__.py build()` relies on.

**The whole-document push flow** ties `config/` to the wire contract
described in `proto/`'s DESIGN.md and design.md §5. `cli/playfield.py`'s
`cmd_config` is the concrete example: it constructs a `ConfigLoader`,
calls `loader.load_playfield(Path(args.file))` — full client-side
validation, per the module docstring's "**before** `loader.load_playfield`
ever touches the filesystem [of the daemon]" — and only if that succeeds
does it call `discovery_factory().connect(args.daemon).set_playfield(
definition)`. A bad file is rejected before any RPC is constructed.
`daemon/rpc.py` reconstructs the daemon-side `PlayfieldDefinition`/
`CameraConfig` directly from `config.loader`'s concrete classes when
handling `SetPlayfieldRequest`/`SetCameraConfigRequest` (imported
directly, not duck-typed).

**`ResetCameraConfig`'s fixed re-read target.** `daemon/service.py`'s
`_config_path_for(slug)` always computes
`config_dir/cameras/<slug>/config.json` — sprint 023 ticket 004's
"re-reading exactly this location, never whatever was last pushed." This
is the one place the daemon itself calls into `config/` against
`config_dir` — still a read (via `ConfigLoader.load_camera_config`), never
a write, preserving the human-owned invariant.

**`camera locate`'s config.json write.** `cli/locate.py` is the one
sanctioned writer of a `config_dir` file outside a human directly editing
it: after solving camera geometry from references, it reads the
on-disk `CameraConfig` (`loader.load_camera_config`), replaces its
`camera_position` field (`dataclasses.replace`), writes the updated
`.to_dict()` back atomically (`_write_camera_config_atomic` — tmp+fsync+
`os.replace`, mirroring `camera/registry.py`'s `_save` and
`daemon/service.py`'s calibration write, but implemented locally rather
than provided by `config/`), then pushes the same document live via
`daemon.set_camera_config(updated)` so no daemon restart is required.

## 5. Interfaces

### Exposes

- `Directories` (`directories.py`, concrete) / `Directories`
  (`interfaces.py`, base) — `config_dir`, `state_dir`, `socket_dir`
  properties, `*_source` provenance, `describe()`.
- `DEFAULT_DAEMON_PORT` (`directories.py`) — the shared TCP port constant
  both `daemon/rpc.py` and `client/discovery.py` need without importing
  each other.
- `ConfigDocument`, `CameraConfig`, `CameraPosition`,
  `PlayfieldDefinition` (abstract, `interfaces.py`), `Feature`
  (`interfaces.py`) — the value-shaped document types.
- `PlayfieldDefinition` (concrete), `PlayfieldFeatureEntry`, `ConfigLoader`
  (concrete), `ScanResult`, `ConfigDirError` (`loader.py`).

### Consumes

- `aprilcam.errors.MalformedDocument` — the one exception type
  `loader.py` raises for every validation failure.
- `aprilcam.types.WorldPoint` — used to build `CameraPosition.nadir` and
  `Feature.position`/`PlayfieldDefinition.aruco_position()`.

That is the module's entire external dependency surface — `config/` sits
near the bottom of the dependency graph. Its actual consumers span nearly
every other subsystem: `daemon/service.py`, `daemon/rpc.py`,
`daemon/__main__.py`, `daemon/detection.py`, `daemon/lifecycle.py`,
`daemon/streaming.py`, `daemon/interfaces.py` (camera lifecycle,
config-service RPC bodies, boot-time scan, directory access,
tag-detection playfield lookups); `camera/camera.py` and
`camera/interfaces.py` (`CameraConfig` as the `apply_config` argument
type); `calibration/calibration.py` (`CameraConfig`/`PlayfieldDefinition`
as calibration inputs); `client/models.py`, `client/discovery.py`,
`client/daemon_client.py`, `client/lifecycle.py` (wire round-trip types,
`Directories`, `DEFAULT_DAEMON_PORT`); `cli/camera.py`, `cli/locate.py`,
`cli/playfield.py` (load-then-push flows); `mcp/tools/configuration.py`,
`mcp/tools/perception.py` (dict-based parse entry points, `Feature`); and
`playfield/interfaces.py`, `playfield/playfield.py`, `playfield/query.py`
(the v2 playfield subsystem, already building on
`CameraConfig`/`PlayfieldDefinition`/`Feature` today).

## 6. Open Questions / Known Limitations

- **`data_dir` vs. `state_dir` naming mismatch.** design.md §7's
  storage-layout diagram calls the regenerable root `data_dir`; every
  piece of actual code and `docs/design/configuration.md` call it
  `state_dir` throughout (`Directories.state_dir`,
  `APRILCAM_STATE_DIR`). One of the two documents is stale; worth
  reconciling so a reader does not go looking for a `data_dir` property
  that does not exist.
- **"Write ordering: calibration.json before config.json" (design.md
  §6) has no grounded combined-write code path today.** No single
  operation in this codebase currently writes both files, so this
  convention is not yet exercised — only asserted. Whoever adds the first
  combined write should confirm the ordering explicitly rather than
  assume it is already tested.
- **No shared atomic-write helper lives in `config/`.** `cli/locate.py`
  implements its own `_write_camera_config_atomic`; `camera/registry.py`
  and `daemon/service.py` each implement their own equivalent for their
  own files. All three now independently mirror the same tmp+fsync+
  `os.replace` pattern (each docstring cross-references the others by
  name) — whether that belongs as one shared helper in `config/` instead
  of three hand-synchronized copies is open.
- **`configuration.md` §6's own carried-forward open questions**: whether
  a single `APRILCAM_DIR` override (setting both roots under one parent)
  is worth the fourth cascade tier it would add; whether any v1-layout
  migration path is needed or v2 starts clean. Neither is resolved in
  code as of this review.
