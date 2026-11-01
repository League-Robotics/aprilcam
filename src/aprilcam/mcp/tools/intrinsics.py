"""ChArUco-board intrinsics calibration tools (sprint 044 ticket 009).

Thin wrappers over ``Daemon.start_intrinsics`` / ``get_intrinsics_status``
/ ``finish_intrinsics`` / ``abort_intrinsics``, following the sprint 037
pattern in ``configuration.py``: ``@handle_tool_errors`` registered after
``@mcp.tool()``, ``connection.call`` for daemon resolution, optional
``daemon`` per-call override.

Returns are plain JSON-friendly dicts built from the cv2-free value types
in ``aprilcam.intrinsics.interfaces`` (never the cv2-backed modules).
``None`` fields stay ``None`` (JSON null); tuples become lists; enums
become their string values; any numpy scalar/array is converted to plain
Python by :func:`_plain`. Typed session errors (``IntrinsicsSessionActive``,
``NoIntrinsicsSession``, ``UnknownCamera``) surface as tool errors via
``handle_tool_errors``.

Per ``.claude/rules/tool-call-empty-args.md`` an optional ``board`` of
``None``, ``""`` or the literal ``"NONE"`` all mean "use the default
board".
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.errors import handle_tool_errors

__all__ = ["register"]


def _plain(value: Any) -> Any:
    """Recursively convert *value* to JSON-friendly plain Python."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, enum.Enum):
        return _plain(value.value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if hasattr(value, "tolist"):  # numpy array or scalar, without importing numpy
        return _plain(value.tolist())
    if isinstance(value, (int, float)):
        return value
    return str(value)


def _status_to_dict(status: Any) -> dict[str, Any]:
    """``IntrinsicsStatus`` as a plain dict (field names unchanged)."""
    return {
        "camera": status.camera,
        "state": status.state,
        "board_spec": status.board_spec,
        "layout": _plain(status.layout),
        "image_size": _plain(status.image_size),
        "board_visible": status.board_visible,
        "view_count": status.view_count,
        "tilted_count": status.tilted_count,
        "cell_coverage": _plain(status.cell_coverage),
        "tilt_coverage": _plain(status.tilt_coverage),
        "rms_px": _plain(status.rms_px),
        "residual_px": _plain(status.residual_px),
        "correction_px": _plain(status.correction_px),
        "fx": _plain(status.fx),
        "hfov_deg": _plain(status.hfov_deg),
        "good": status.good,
        "good_now": _plain(getattr(status, "good_now", None)),
        "banner": status.banner,
        "next_hint": status.next_hint,
        "last_reject": status.last_reject,
        "errors": _plain(status.errors),
        "started_at": status.started_at,
        "solved_at": _plain(status.solved_at),
        "board_outline": _plain(getattr(status, "board_outline", None)),
    }


def _finish_to_dict(result: Any) -> dict[str, Any]:
    """``IntrinsicsFinishResult`` as a plain dict."""
    return {
        "installed": result.installed,
        "reason": result.reason,
        "status": _status_to_dict(result.status),
        "lens": _plain(result.lens),
    }


def register(mcp: FastMCP, connection: ConnectionManager, temp_dir: Path) -> None:
    """Register the four intrinsics tools on *mcp*. *temp_dir* is accepted
    only to match the uniform ``register(mcp, connection, temp_dir)``
    shape; these tools return no images."""

    @mcp.tool()
    @handle_tool_errors
    def start_intrinsics(
        camera: str, board: Optional[str] = None, daemon: Optional[str] = None
    ) -> dict[str, Any]:
        """Start a ChArUco-board lens-intrinsics calibration session on
        *camera*. A human must hold a printed ChArUco board in front of
        the camera, showing it from varied positions and tilts; you
        drive the session and relay guidance to them.

        *board* is an optional spec ``COLSxROWS:SQUARE_MM:MARKER_MM:DICT``
        (e.g. ``5x7:37:28:DICT_5X5_100``); omit it to use the daemon's
        default board. Returns the initial status dict (see
        :func:`get_intrinsics_status`). Fails with a tool error if a
        session is already active on this camera -- finish or abort it
        first.

        Workflow: start_intrinsics -> poll get_intrinsics_status -> when
        ``banner`` is ``good_enough``, finish_intrinsics(install=True);
        if you give up for any reason, abort_intrinsics. The session
        aborts itself after 120 s without polling.
        """
        spec = None if board in (None, "", "NONE") else board
        status = connection.call_camera(
            camera, lambda d, camera: d.start_intrinsics(camera, spec), host=daemon
        )
        return _status_to_dict(status)

    @mcp.tool()
    @handle_tool_errors
    def get_intrinsics_status(camera: str, daemon: Optional[str] = None) -> dict[str, Any]:
        """Current status of the intrinsics session on *camera*. Poll
        this at a modest rate (about once per second; faster adds
        nothing) -- polling also keeps the session alive: it aborts
        itself after 120 s with no polling.

        Read ``next_hint`` and relay it to the human holding the board
        (e.g. "tilt the board toward the north edge"). ``banner`` is one
        of ``show_board`` (no board seen), ``keep_going`` (collecting,
        not yet good) or ``good_enough`` (the solved lens passes the
        quality gate). Other fields: ``state`` (collecting/finished/
        aborted/failed), ``board_visible``, ``view_count``,
        ``tilted_count``, ``cell_coverage`` (3x3 counts keyed NW..SE),
        ``tilt_coverage`` (N/E/S/W), ``rms_px``, ``residual_px``,
        ``correction_px``, ``fx``, ``hfov_deg``, ``good`` (latched: two
        consecutive passes to set, two clearly bad to clear), ``good_now``
        (raw verdict of the latest solve),
        ``last_reject`` and ``errors``. Unknown values are null.

        Do not stop just because the banner turns green: let the human
        keep adding views if you want a better fit, and finish only when
        you are satisfied.
        """
        status = connection.call_camera(
            camera, lambda d, camera: d.get_intrinsics_status(camera), host=daemon
        )
        return _status_to_dict(status)

    @mcp.tool()
    @handle_tool_errors
    def finish_intrinsics(
        camera: str,
        install: bool = False,
        force: bool = False,
        daemon: Optional[str] = None,
    ) -> dict[str, Any]:
        """Solve and end the intrinsics session on *camera*. With
        ``install=True`` the solved lens model is installed live on the
        daemon (no restart) -- do this only when the status ``banner``
        is ``good_enough``. If the solve is not good the daemon declines
        to install and returns ``installed: false`` with a ``reason``
        (that is data, not an error); ``force=True`` installs anyway and
        should be used only with explicit human approval. With
        ``install=False`` the session ends and the solution is only
        reported. Returns ``{installed, reason, status, lens}``.
        """
        result = connection.call_camera(
            camera,
            lambda d, camera: d.finish_intrinsics(camera, install, force=force), host=daemon
        )
        return _finish_to_dict(result)

    @mcp.tool()
    @handle_tool_errors
    def abort_intrinsics(camera: str, daemon: Optional[str] = None) -> dict[str, Any]:
        """Abort the intrinsics session on *camera*, discarding all
        collected views and installing nothing. Always call this when
        you give up, are interrupted, or the human walks away, so the
        camera is freed immediately rather than waiting for the 120 s
        no-polling self-abort. Returns the final status dict.
        """
        status = connection.call_camera(
            camera, lambda d, camera: d.abort_intrinsics(camera), host=daemon
        )
        return _status_to_dict(status)
