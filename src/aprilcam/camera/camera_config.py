"""Per-camera config.json helpers.

Each camera directory may contain a ``config.json`` file that records which
named playfield the camera is linked to, plus static hardware settings
(device_name, resolution, UVC controls, camera_position, static_marker_ids).
This module provides atomic read/write helpers.

**Daemon-boundary rule**: ``daemon/camera_pipeline.py`` may import
``load_camera_config`` to read hardware settings (UVC controls, device_name,
resolution) needed at camera-open time.  However, daemon modules must
**never call** ``save_camera_config`` — ``config.json`` is written exclusively
by the MCP server and human operators.  The daemon reads config for hardware
parameters only and must treat it as read-only.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def load_camera_config(camera_dir: Path | str) -> Optional[dict]:
    """Read ``<camera_dir>/config.json`` and return the parsed dict.

    Returns ``None`` when the file is absent or its contents cannot be
    decoded as JSON.  Never raises on a missing or malformed file.

    Parameters
    ----------
    camera_dir:
        Directory that may contain ``config.json``.

    Returns
    -------
    dict or None
        The parsed JSON object, or ``None``.
    """
    path = Path(camera_dir) / "config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        log.warning("Could not parse %s — treating as absent", path)
        return None


def parse_camera_config(d: dict) -> dict:
    """Validate and return a camera config dict from a ``GetCameraConfig`` RPC blob.

    This is a pure (no I/O) companion to :func:`load_camera_config` for the MCP
    server side: the daemon returns ``config.json`` content as a JSON blob via
    gRPC, the MCP server calls ``json.loads()`` on the blob, then calls this
    function to obtain a usable dict.

    Currently performs no structural validation beyond type-checking that *d* is
    a dict.  Returns *d* unchanged; callers may read any key they expect.

    Parameters
    ----------
    d:
        Parsed JSON dict exactly as it appears in ``config.json`` on disk.

    Returns
    -------
    dict
        The same dict (validated and returned as-is).

    Raises
    ------
    TypeError
        If *d* is not a dict.
    """
    if not isinstance(d, dict):
        raise TypeError(f"Expected dict, got {type(d).__name__}")
    return d


def save_camera_config(camera_dir: Path | str, config_dict: dict) -> Path:
    """Write *config_dict* to ``<camera_dir>/config.json`` atomically.

    The write is atomic: the JSON is written to a sibling ``config.json.tmp``
    file and then ``os.replace``-d over the target, so a crash mid-write
    never leaves a partial ``config.json`` and no ``.tmp`` file is left
    behind on success.

    ``camera_dir`` is created with ``parents=True`` if it does not exist.

    Parameters
    ----------
    camera_dir:
        Directory in which to write (or update) ``config.json``.
    config_dict:
        Serialisable dict to write as JSON (``indent=2``, ``sort_keys=True``).

    Returns
    -------
    Path
        Absolute path of the file that was written (``config.json``).
    """
    camera_dir = Path(camera_dir)
    camera_dir.mkdir(parents=True, exist_ok=True)
    path = camera_dir / "config.json"
    tmp = path.with_suffix(".json.tmp")
    text = json.dumps(config_dict, indent=2, sort_keys=True) + "\n"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        # Remove leftover tmp on failure; on success it is already gone.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return path
