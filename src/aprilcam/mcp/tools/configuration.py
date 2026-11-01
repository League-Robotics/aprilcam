"""Configuration/calibration tools: whole-document camera config and
playfield push/fetch, plus calibration trigger (ticket 026-003;
sprint.md's Architecture Step 3 ``mcp/tools/configuration.py`` row:
"Exposes whole-document camera/playfield config and calibration as
tools. Calls Daemon config/calibration methods; owns dict<->document
parsing via extended config/loader.py.").

Every tool here is a thin wrapper over ``Daemon``, following ticket
002's ``mcp/tools/perception.py`` precedent exactly: resolve via
``connection.call`` (ticket 001's
:class:`~aprilcam.mcp.connection.ConnectionManager`), call the matching
``Daemon`` method, hand the result back. No tool catches anything
itself -- every one is wrapped by ticket 001's
:func:`~aprilcam.mcp.errors.handle_tool_errors`, registered *after*
``@mcp.tool()`` so ``functools.wraps`` preserves the signature
FastMCP's schema introspection needs.

**Push tools take a JSON object, never a file path** (spec principle
7 -- "clients see handles and services, never file paths"; sprint.md's
Design Rationale, "configuration tools take structured JSON content,
never a file path"). An MCP agent has no filesystem the daemon can
reach in the first place, so ``set_camera_config``/``set_playfield``
accept the document as a ``dict`` tool parameter and hand it to
:class:`~aprilcam.config.loader.ConfigLoader`'s new dict-based parsing
entry points, ``parse_camera_config``/``parse_playfield`` (ticket
026-003) -- the exact same whole-document validation
(self-naming, geometry, per-section entry checks, duplicate-id
rejection) the file-based CLI push path (``cli/camera.py``/
``cli/playfield.py``) already enforces via
``load_camera_config``/``load_playfield``, now factored out and shared
rather than re-implemented here. A malformed document raises
``MalformedDocument`` from the loader *before* ``connection.call`` is
ever reached -- no daemon resolution and no RPC is attempted for a
document that was never going to be valid (UC-23.S-4), exactly mirroring
the file-based CLI's "a bad file never reaches the daemon" guarantee.

**Fetch tools return the ``to_dict()``-equivalent shape, not the raw
dataclass.** ``get_playfield``/``list_playfields`` return
``PlayfieldDefinition.to_dict()`` (a plain ``dict``), not the
dataclass itself -- the concrete ``config.loader.PlayfieldDefinition``
renames fields on the way out (``tag_id`` -> ``id``, ``colour`` ->
``color``) and nests geometry under a ``"playfield"`` key --
``to_dict()`` is the one canonical, loadable shape
(``ConfigLoader.parse_playfield(get_playfield(name)) ==`` what the
daemon holds), matching the round-trip contract ``config/loader.py``
already documents and the same shape ``cli/playfield.py``'s own
``--dump`` flag prints. ``get_camera_config`` follows the same
explicit-``to_dict()`` shape (sprint 029 ticket 001): before
``CameraConfig`` gained the optional ``camera_position`` field, its
fields already matched ``to_dict()``'s shape field-for-field, so
returning the dataclass directly and letting FastMCP's pydantic
``TypeAdapter`` serialize it natively (ticket 002's original precedent)
happened to coincide with ``to_dict()``'s output. That coincidence
broke the moment ``camera_position`` became a field that ``to_dict()``
*omits* when unset (``config/interfaces.py``'s own "never a ``null``
placeholder" rule) but a naive dataclass serializer would still emit as
``null`` -- so this tool now calls ``.to_dict()`` explicitly, exactly
like ``get_playfield``, rather than relying on raw dataclass
introspection to keep matching a shape it no longer automatically does.

**``calibrate``/``get_calibration`` use the interface-declared types,
never the concrete cv2-backed ones.** ``calibration.interfaces.
Calibration``/``CalibrationResult`` (imported here) are what
``daemon.interfaces.Daemon``'s own method signatures declare and are
themselves ``cv2``-free (only ``numpy``, which -- unlike ``cv2`` -- is a
base-install dependency, per ``.claude/rules/project-overview.md``).
The concrete ``calibration.calibration.Calibration`` (extra
``homography``/``matched_ids``/``reprojection_error_cm`` fields) lives
in a module that imports ``cv2`` at module scope; importing it here
would pull ``cv2`` into ``mcp/`` transitively, violating
``adapters.md`` rule 2 ("adapters do no vision") and sprint.md's
Success Criteria ("cv2 is absent from sys.modules after importing every
mcp/ module") even though this file never touches a camera. This
module never imports it -- ``get_calibration`` reports exactly the
fields the ``Daemon`` interface promises (``stale``, ``calibrated_at``,
``playfield``), the ones an agent actually needs to reason about
whether a calibration is trustworthy, not the internal homography
matrix.

**``capture_flatfield``/``get_flatfield``/``clear_flatfield`` (sprint 037
ticket 005) follow the identical rule, with one further wrinkle for
``capture_flatfield``.** ``calibration.interfaces.FlatFieldResult``/
``FlatFieldInfo`` (imported here) are what ``daemon.interfaces.
Daemon.capture_flat_field``/``get_flatfield`` declare and are
themselves ``cv2``-free -- never ``calibration.flatfield.
FlatFieldCapturer``'s own module, which imports ``cv2`` at module scope
for the identical reason ``calibration.calibration`` is avoided above.
``get_flatfield`` returns ``FlatFieldInfo`` verbatim (no ``gain``-shaped
field, so FastMCP's own pydantic ``TypeAdapter`` introspects it
cleanly, same as ``Calibration`` above). ``capture_flatfield`` cannot:
``FlatFieldResult.gain`` is typed ``Optional[numpy.ndarray]`` --
*always* ``None`` on this client-library path (``client/daemon_client.
py``'s ``GrpcDaemon.capture_flat_field`` never serializes a gain array
across the wire, matching this ticket's "never expose gain" contract),
but pydantic's schema generation runs over the *type annotation*, not
the runtime value, and raises ``PydanticSchemaGenerationError`` for a
bare ``numpy.ndarray`` field with no ``arbitrary_types_allowed``
opt-in -- confirmed directly: returning ``FlatFieldResult`` verbatim
here fails ``build_server()`` itself, for every tool, not just this
one, since FastMCP introspects every registered tool's return
annotation up front. So ``capture_flatfield`` uses this module's other
established convention instead -- the explicit-dict shape
``get_camera_config``/``get_playfield`` already use for a type whose
wire shape has diverged -- via :func:`_flatfield_result_to_dict`,
which copies every field except ``gain``.

``capture_flatfield``'s ``exposure``/``frames``/``force``/``dry_run``
parameters are
optional overrides forwarded straight to ``Daemon.capture_flat_field``
with no client-side interpretation -- a failing quality-gate check is
``FlatFieldResult(succeeded=False, reason=...)``, explicit failure as
data, not a tool error (mirrors ``CalibrationResult``'s identical
contract); only a precondition failure (``NoFieldMask`` -- no
derivable field-interior mask) or a transport/addressing problem comes
back as a tool error, via the same generic ``handle_tool_errors``
mapping every other tool here uses.

Every tool takes an optional ``daemon`` parameter, matching tickets
001/002's per-call target-host override.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from aprilcam.calibration.interfaces import Calibration, CalibrationResult, FlatFieldInfo, FlatFieldResult
from aprilcam.config.loader import ConfigLoader
from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.errors import handle_tool_errors


def _flatfield_result_to_dict(result: FlatFieldResult) -> dict[str, Any]:
    """``FlatFieldResult`` minus ``gain`` -- the explicit-dict shape
    ``capture_flatfield`` returns instead of the dataclass itself. See
    this module's docstring for why: ``gain`` is typed
    ``Optional[numpy.ndarray]``, and FastMCP's pydantic-based return-type
    introspection cannot build a schema for a bare ``numpy.ndarray``
    field, even though this client-library path always leaves it
    ``None`` at runtime. Every other field copies straight across.
    """
    return {
        "succeeded": result.succeeded,
        "spread_before": result.spread_before,
        "spread_after": result.spread_after,
        "reason": result.reason,
        "forced_checks": result.forced_checks,
    }

__all__ = ["register"]


def register(mcp: FastMCP, connection: ConnectionManager, temp_dir: Path) -> None:
    """Register every configuration/calibration tool on *mcp*, resolving
    daemons through *connection* -- the same locals ``mcp/server.py``'s
    ``build_server`` constructs once per server process and hands to
    every tool-group module (ticket 001's handoff). *temp_dir* is
    accepted only to match that uniform
    ``mcp.tools.*.register(mcp, connection, temp_dir)`` shape; this
    group returns no images, so it never writes under it.
    """
    loader = ConfigLoader()

    # -- camera config -----------------------------------------------------

    @mcp.tool()
    @handle_tool_errors
    def get_camera_config(camera: str, daemon: Optional[str] = None) -> dict[str, Any]:
        """The whole live camera config document for *camera* (UC-03,
        UC-24), in its ``to_dict()``-equivalent shape (sprint 029
        ticket 001: explicit now, matching :func:`get_playfield`'s own
        precedent below -- see the module docstring for why) -- every
        settable key the camera reports, blank when unset, plus
        ``camera_position`` only when one is configured (never a
        ``null`` placeholder). Answers exactly what was last pushed or
        installed on disk, never a live hardware readback merged in
        (UC-03.S-1, UC-24.S-3).
        """
        config = connection.call(lambda d: d.get_camera_config(camera), host=daemon)
        return config.to_dict()

    @mcp.tool()
    @handle_tool_errors
    def set_camera_config(document: dict[str, Any], daemon: Optional[str] = None) -> None:
        """Push a whole camera config document (UC-03, UC-23), targeted
        by the document's own ``"name"`` field -- there is no separate
        camera argument. Applies immediately, in memory; a rejected or
        unsupported push leaves the previous config exactly as it was
        (UC-03.S-3). *document* is validated client-side by
        :meth:`~aprilcam.config.loader.ConfigLoader.parse_camera_config`
        before any daemon connection is even attempted -- a malformed
        document (no ``"name"``, a non-string ``"playfield"``, a
        non-object ``"settings"``) raises ``MalformedDocument`` naming
        exactly what was wrong, with zero RPCs (UC-23.S-4).
        """
        config = loader.parse_camera_config(document)
        connection.call(lambda d: d.set_camera_config(config), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def reset_camera_config(camera: str, daemon: Optional[str] = None) -> None:
        """Discard any live override for *camera* and re-apply the
        stored on-disk config immediately (UC-03) -- how an experiment
        ends without a daemon restart.
        """
        connection.call(lambda d: d.reset_camera_config(camera), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def close_camera(camera: str, force: bool = False, daemon: Optional[str] = None) -> None:
        """Release *camera*'s device so other processes can use it,
        without restarting the daemon. The camera stays listed (``closed``
        in ``list_cameras``) and reopens transparently on next use.
        Refused (``CameraInUse``) while a stream or read is active unless
        *force*; a forced close leaves existing subscribers on the NO
        SIGNAL card until a new request reopens the camera.
        """
        connection.call(lambda d: d.close_camera(camera, force=force), host=daemon)

    # -- playfields ----------------------------------------------------------

    @mcp.tool()
    @handle_tool_errors
    def get_playfield(name: str, daemon: Optional[str] = None) -> dict[str, Any]:
        """The whole live playfield definition named *name* (UC-02,
        UC-24), in its ``to_dict()``-equivalent shape -- loadable
        verbatim by :meth:`~aprilcam.config.loader.ConfigLoader.
        parse_playfield`, the same round-trip contract
        ``config/loader.py`` documents and ``cli/playfield.py``'s own
        ``--dump`` flag prints.
        """
        playfield = connection.call(lambda d: d.get_playfield(name), host=daemon)
        return playfield.definition.to_dict()

    @mcp.tool()
    @handle_tool_errors
    def set_playfield(document: dict[str, Any], daemon: Optional[str] = None) -> None:
        """Push a whole playfield definition document (UC-02, UC-23),
        targeted by the document's own ``"name"`` field. Rejected whole
        if malformed, leaving any previous definition intact
        (UC-02.S-2). *document* is validated client-side by
        :meth:`~aprilcam.config.loader.ConfigLoader.parse_playfield`
        before any daemon connection is even attempted -- a malformed
        document (no ``"name"``, missing/invalid geometry, a feature
        outside the field's bounds, a duplicate ArUco/AprilTag id at
        different coordinates, ...) raises ``MalformedDocument`` naming
        exactly what was wrong, with zero RPCs (UC-23.S-4).
        """
        definition = loader.parse_playfield(document)
        connection.call(lambda d: d.set_playfield(definition), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def list_playfields(daemon: Optional[str] = None) -> tuple[dict[str, Any], ...]:
        """Every playfield definition this daemon currently holds
        (Decision 4: no single-slot limit), each in the same
        ``to_dict()``-equivalent shape :func:`get_playfield` returns for
        one.
        """
        playfields = connection.call(lambda d: d.list_playfields(), host=daemon)
        return tuple(p.definition.to_dict() for p in playfields)

    # -- calibration -----------------------------------------------------

    @mcp.tool()
    @handle_tool_errors
    def calibrate(camera: str, daemon: Optional[str] = None) -> CalibrationResult:
        """Run calibration for *camera* against its linked playfield
        (UC-04, UC-15) -- all vision happens in the daemon; this sends a
        command and reports the result. There is no partial success:
        either every required ArUco id was matched and a homography
        solved (``succeeded=True``, ``matched_ids`` full), or the
        attempt failed (``missing_ids`` naming what was not found, or
        ``reason`` naming a degenerate-geometry problem when every id
        *was* found) and any prior-good calibration is left untouched
        (UC-04.S-2). A camera with no playfield link fails with
        ``NoPlayfieldLinked`` *before any frame is requested*
        (UC-15.S-2) -- a precondition error, not a calibration failure;
        this tool does nothing beyond forwarding to ``Daemon.calibrate``,
        so that ordering is the daemon's own guarantee, not something
        reimplemented here.

        ``lens_model_source`` (sprint 044) reports which lens model this
        call applied -- ``"measured"`` (the per-camera ChArUco lens
        store), ``"estimated"`` or ``"legacy"`` (carried from an older
        file) or ``"none"``. ``lens_warning`` is set when a stored
        lens model was ignored for a resolution mismatch.

        Pose provenance (sprint 045), all ``None``/empty when not
        applicable: ``method`` is ``"pose"`` (planar pose from the marker
        corners, using the measured lens model) or ``"centre-point"`` (the
        fallback fit to marker centres; ``method_reason`` says why).
        ``confidence`` is ``"full"``, ``"reduced"`` (e.g. two markers) or
        ``"low"`` (one marker, or clustered markers) -- trust the field
        mapping accordingly. Numbers: ``corner_count``,
        ``reprojection_error_px`` / ``reprojection_error_cm`` (corner fit),
        ``scale_agreement`` (1.0 = the markers agree on scale),
        ``angular_spread_deg``, ``layout_extent_ratio`` (low = nearly
        collinear), and the derived camera position ``camera_height_cm``,
        ``camera_nadir`` (world point under the camera) and
        ``camera_tilt_deg``. ``leave_one_out_cm`` is the per-marker
        leave-one-out residual as ``[[marker_id, cm], ...]`` (how far each
        marker lands from where the others place it) and
        ``leave_one_out_worst_id`` / ``leave_one_out_worst_cm`` name the
        worst one -- a mis-surveyed or moved marker shows up here.
        ``notes`` lists remarks from the solve; ``helpful_missing_ids``
        lists optional markers not detected. On a gate failure
        (``succeeded`` false) the measured numbers are still returned and
        ``reason`` names the failing gate, its threshold and the value.
        """
        return connection.call(lambda d: d.calibrate(camera), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def get_calibration(camera: str, daemon: Optional[str] = None) -> Optional[Calibration]:
        """*camera*'s current calibration, or ``None`` if it has never
        been calibrated -- never a fabricated default (mirrors
        ``get_tag``'s "``None`` means not detected, never a zero-valued
        record" discipline). ``stale`` flags whether the camera or field
        may have moved since calibrating; a stale calibration is still
        returned, not hidden, so a caller can decide what to do with it
        (UC-11.S-3).

        ``lens_source`` (sprint 044) says which lens model this
        calibration was solved with -- ``"measured"``, ``"estimated"``,
        ``"legacy"`` or ``None`` (no lens); for a measured lens,
        ``lens_views``/``lens_rms_px``/``lens_residual_px``/
        ``lens_installed_at`` summarise the measurement. A camera with a
        stored lens but no calibration still returns ``None`` here; its
        lens status is ``list_cameras``' ``lens_model``/``lens_pending``.

        Pose provenance (sprint 045), ``None`` for a pre-045 calibration:
        ``method`` (``"pose"`` | ``"centre-point"``), ``confidence``
        (``"full"`` | ``"reduced"`` | ``"low"``), ``reprojection_error_px``,
        ``corner_count``, ``scale_agreement`` and the derived camera
        position ``camera_height_cm`` / ``camera_nadir_x_cm`` /
        ``camera_nadir_y_cm`` / ``camera_tilt_deg``. The per-marker
        leave-one-out residuals are only in ``calibrate``'s result (they
        are measured at solve time, not persisted). ``list_cameras``
        carries ``calibration_method`` / ``calibration_confidence`` per
        camera.
        """
        return connection.call(lambda d: d.get_calibration(camera), host=daemon)

    # -- flat-field gain map (sprint 037 ticket 005) ----------------------

    @mcp.tool()
    @handle_tool_errors
    def capture_flatfield(
        camera: str,
        exposure: Optional[float] = None,
        frames: Optional[int] = None,
        force: bool = False,
        dry_run: bool = False,
        daemon: Optional[str] = None,
    ) -> dict[str, Any]:
        """Build (and, unless refused or *dry_run*, persist) a per-camera
        flat-field gain-map correction from a fresh capture burst (sprint
        037) -- all vision happens in the daemon, on this camera's
        current view of its field; this sends a command and reports the
        result, exactly like :func:`calibrate`. Lay a plain white/light
        sheet over the whole field first -- the gain map is built from
        how unevenly *that* reads back, so a scene with no full-field
        reference produces a poor map even on success.

        There is no partial success: either every quality-gate check
        passed and a map was built and persisted
        (``succeeded=True``), or the attempt was refused
        (``reason`` naming which check failed) and any prior-good map is
        left untouched -- the same "explicit failure is data, never an
        exception" contract :func:`calibrate` already uses for
        ``CalibrationResult``. A camera with no derivable field-interior
        mask (no calibration, and no linked-playfield ArUco corners
        detectable live) fails with ``NoFieldMask`` *before any capture
        burst is taken* -- a precondition error, not a refused attempt;
        this tool does nothing beyond forwarding to ``Daemon.
        capture_flat_field``, so that ordering is the daemon's own
        guarantee.

        *exposure*, when given, is a temporary override for the capture
        burst only -- the daemon restores this camera's prior
        stored/live configuration afterward, on success, a refusal, or
        an exception alike; a low exposure is normally needed here since
        a white sheet saturates most cameras at their working exposure.
        *frames* is how many frames to average for the burst (daemon
        default when omitted). *force* persists the built map even if a
        quality-gate check failed, naming what was overridden via
        ``FlatFieldResult.forced_checks``. *dry_run* evaluates and
        reports without writing anything to disk or changing the
        live-applied map. The measured per-channel spread
        (``spread_before``/``spread_after``) is always reported, whether
        the attempt succeeded, was refused, or was only evaluated.

        Returned as a plain dict (``succeeded``, ``spread_before``,
        ``spread_after``, ``reason``, ``forced_checks``) rather than
        ``FlatFieldResult`` itself -- see the module docstring:
        ``FlatFieldResult.gain`` is a ``numpy.ndarray``-typed field
        FastMCP's schema introspection cannot handle, and it is never
        exposed here regardless (always ``None`` on this client-library
        path).
        """
        result = connection.call(
            lambda d: d.capture_flat_field(
                camera, exposure=exposure, frames=frames, force=force, dry_run=dry_run
            ),
            host=daemon,
        )
        return _flatfield_result_to_dict(result)

    @mcp.tool()
    @handle_tool_errors
    def get_flatfield(camera: str, daemon: Optional[str] = None) -> Optional[FlatFieldInfo]:
        """*camera*'s current flat-field gain-map metadata, or ``None``
        if it has never had one built (or it was cleared) -- never a
        fabricated default, mirroring :func:`get_calibration`'s "``None``
        means never, not a zero-valued record" discipline. ``stale``
        flags whether the camera's mounting or lighting may have changed
        since the map was captured; a stale map is still returned and
        still applied, not hidden, so a caller can decide what to do
        with it (mirrors ``Calibration.stale``'s own contract).
        """
        return connection.call(lambda d: d.get_flatfield(camera), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def clear_flatfield(camera: str, daemon: Optional[str] = None) -> None:
        """Delete *camera*'s stored gain map and metadata sidecar, and
        drop the live-applied map immediately -- no daemon restart
        needed, mirroring how a freshly captured map takes effect live.
        Clearing an already-absent map is a no-op, enforced daemon-side.
        """
        connection.call(lambda d: d.clear_flat_field(camera), host=daemon)
