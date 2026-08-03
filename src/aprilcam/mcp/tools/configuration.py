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
dataclass itself -- unlike ``config.interfaces.CameraConfig`` (which has
no separate implementation class, so its fields already match
``to_dict()``'s shape field-for-field), the concrete
``config.loader.PlayfieldDefinition`` renames fields on the way out
(``tag_id`` -> ``id``, ``colour`` -> ``color``) and nests geometry under
a ``"playfield"`` key -- ``to_dict()`` is the one canonical, loadable
shape (``ConfigLoader.parse_playfield(get_playfield(name)) ==`` what the
daemon holds), matching the round-trip contract ``config/loader.py``
already documents and the same shape ``cli/playfield.py``'s own
``--dump`` flag prints. ``get_camera_config`` returns the
``CameraConfig`` dataclass directly (FastMCP's pydantic ``TypeAdapter``
handles it natively, ticket 002's precedent) since that already *is*
the ``to_dict()`` shape.

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

Every tool takes an optional ``daemon`` parameter, matching tickets
001/002's per-call target-host override.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from aprilcam.calibration.interfaces import Calibration, CalibrationResult
from aprilcam.config.interfaces import CameraConfig
from aprilcam.config.loader import ConfigLoader
from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.errors import handle_tool_errors

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
    def get_camera_config(camera: str, daemon: Optional[str] = None) -> CameraConfig:
        """The whole live camera config document for *camera* (UC-03,
        UC-24) -- every settable key the camera reports, blank when
        unset. Answers exactly what was last pushed or installed on
        disk, never a live hardware readback merged in (UC-03.S-1,
        UC-24.S-3).
        """
        return connection.call(lambda d: d.get_camera_config(camera), host=daemon)

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
        """
        return connection.call(lambda d: d.get_calibration(camera), host=daemon)
