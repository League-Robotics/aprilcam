"""``aprilcam config`` (OOP change, 2026-08-22 -- see
``clasi/issues/later/aprilcam-config-command-to-report-config-vars-and-file-locations.md``).

Reports the resolved configuration -- which cascade tier produced each
root, every environment variable the config cascade consults (with its
current value and set/unset status), the file locations derived from
those roots (each flagged exists/missing), and every
``ConfigLoader.load_config_dir`` load error. That last one is the
issue's own "arguably the highest-value part of the command": a
v1-shaped ``config.json`` missing a ``name`` field is silently skipped
today (sprint 023's Decision 1, skip-and-warn), so a camera just never
appears with no other symptom. This command is what turns that into a
five-second diagnosis.

**A deliberate exception, the same one ``cli/guide.py`` documents.**
This command's whole reason to exist is diagnosing why the daemon
won't start or is reading the wrong tree, so it must work with **no
daemon running at all** (UC-01's "which roots were chosen is never a
mystery," answered client-side, before any daemon has ever started).
It therefore never imports ``client.discovery``/``client.lifecycle``/
``daemon.lifecycle``/``daemon.service``/``daemon.rpc`` or ``grpc`` --
see ``tests/aprilcam/cli/test_config_cli.py``'s import-scan test, the
same technique ``test_guide_cli.py`` uses to prove ``cli/guide.py``
never constructs a ``Discovery``/``Daemon``. Reads straight from
``config.directories.Directories`` and ``config.loader.ConfigLoader``,
neither of which touches a socket.

**No ``camera.registry.CameraRegistry`` construction, deliberately.**
That class self-heals (merges) a registry with cross-tier duplicate
records *on load* and persists the merge (``camera/registry.py``'s
module docstring, ticket 021-007) -- a side effect a read-only
diagnostic command must never trigger just by being run.
``registry.json`` is therefore parsed here directly, best-effort,
tolerating anything not shaped like a registry by simply contributing
no slugs (never raising) -- a broken registry is exactly the kind of
thing this command exists to surface gracefully, not choke on.
``REGISTRY_FILENAME`` (the constant, not the class) is still imported
for real: ``camera.registry`` pulls in nothing heavier than ``numpy``
transitively, so there is no coupling concern importing just that name.

**``CALIBRATION_FILENAME`` is duplicated here as a bare string**
rather than imported from ``daemon.service`` -- that module imports
``cv2`` at module scope, and this CLI module must stay part of the
OpenCV-free base install (project overview "Key Constraints": OpenCV is
a **daemon-only** dependency). Same "duplicate a two-line constant
rather than add a coupling edge" call ``client/discovery.py`` already
makes for ``_SERVICE_TYPE`` vs. ``daemon/mdns.py``. The daemon
host/port/log-level env var names below are duplicated for the
identical reason -- ``APRILCAM_DAEMON_HOST_ENV`` lives in
``client.discovery``, which is safe to import (no cv2) but pulls in
``grpc``/``zeroconf`` for what is otherwise a two-line string; the
other two have no named constant anywhere in this codebase to import.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Callable, Tuple

from aprilcam.camera.registry import REGISTRY_FILENAME
from aprilcam.cli._common import EXIT_OK
from aprilcam.config.directories import (
    APRILCAM_CONFIG_DIR_ENV,
    APRILCAM_STATE_DIR_ENV,
    XDG_RUNTIME_DIR_ENV,
    Directories,
)
from aprilcam.config.loader import ConfigLoader, ScanResult

__all__ = ["add_subparser", "cmd_config"]

#: See the module docstring's final paragraph for why these three are
#: duplicated as bare strings rather than imported.
APRILCAM_DAEMON_HOST_ENV = "APRILCAM_DAEMON_HOST"
#: Documented in ``docs/design/configuration.md`` §4's "whole set" table
#: as the daemon's default port override, but **no current v2 code path
#: actually reads it** -- confirmed by grepping ``src/aprilcam`` for
#: ``os.environ``/``os.getenv`` (2026-08-22): only
#: ``config.directories.DEFAULT_DAEMON_PORT`` (a fixed constant, 5280)
#: is used anywhere. Reported below anyway, per the design doc's own
#: authority and this command's job of reporting the *documented*
#: cascade -- but its "set" state currently changes nothing at runtime.
#: This is a real gap between doc and implementation, not fabricated
#: wiring; do not remove this comment without re-checking the grep.
APRILCAM_DAEMON_PORT_ENV = "APRILCAM_DAEMON_PORT"
#: Read as a bare literal by ``daemon/__main__.py`` (``os.environ.get
#: ("APRILCAM_LOG_LEVEL", "INFO")``); no named constant exists there to
#: import.
APRILCAM_LOG_LEVEL_ENV = "APRILCAM_LOG_LEVEL"

#: Every environment variable the config cascade consults, per
#: ``docs/design/configuration.md`` §4 ("the whole set -- if a variable
#: is not here, it does not exist") plus ``XDG_RUNTIME_DIR``, which
#: §4's table omits (it is not an ``APRILCAM_*`` name) but
#: ``config.directories._resolve_socket_dir`` genuinely consults it.
#: Order: the two directory roots, then the socket-dir fallback input,
#: then the three settings-file-shaped vars.
_ENV_VARS: Tuple[str, ...] = (
    APRILCAM_CONFIG_DIR_ENV,
    APRILCAM_STATE_DIR_ENV,
    XDG_RUNTIME_DIR_ENV,
    APRILCAM_DAEMON_HOST_ENV,
    APRILCAM_DAEMON_PORT_ENV,
    APRILCAM_LOG_LEVEL_ENV,
)

#: See the module docstring's "``CALIBRATION_FILENAME`` is duplicated"
#: paragraph. Must match ``daemon/service.py``'s ``CALIBRATION_FILENAME``.
CALIBRATION_FILENAME = "calibration.json"


def cmd_config(
    args: argparse.Namespace,
    *,
    directories_factory: Callable[[], Directories] = Directories,
    loader_factory: Callable[[], ConfigLoader] = ConfigLoader,
) -> int:
    """``aprilcam config``. Always succeeds (:data:`EXIT_OK`) -- there is
    no daemon connection that can fail, and every read below is already
    best-effort: a missing file or directory is a normal, reportable
    state (an empty/fresh install, UC-01.S-2), never an error this
    command raises over. *args* is accepted only to match every other
    ``cmd_*`` function's ``(args, *, ...)`` dispatch signature
    (``cli/__init__.py`` calls ``func(args)`` uniformly) and carries no
    options today.
    """
    directories = directories_factory()
    _print_roots(directories)
    print()
    _print_environment()
    print()
    _print_playfields(directories)
    print()
    _print_cameras(directories)
    print()
    _print_load_errors(loader_factory().load_config_dir(directories))
    return EXIT_OK


def _print_roots(directories: Directories) -> None:
    print("roots:")
    print(f"  config_dir: {directories.config_dir}  ({directories.config_dir_source})")
    print(f"  state_dir:  {directories.state_dir}  ({directories.state_dir_source})")
    print(f"  socket_dir: {directories.socket_dir}  ({directories.socket_dir_source})")


def _print_environment() -> None:
    print("environment variables:")
    for name in _ENV_VARS:
        print(f"  {_env_status_line(name)}")


def _env_status_line(name: str) -> str:
    raw = os.environ.get(name)
    if raw is None:
        return f"{name:<21} unset"
    if not raw.strip():
        # config.directories._resolve_root / _resolve_socket_dir strip()
        # every env value they read and treat a blank result exactly
        # like unset -- surfaced here so "I set it and nothing changed"
        # is never a mystery for the two directory-root vars and
        # XDG_RUNTIME_DIR. The other three vars are not read by this
        # module's own cascade code, but a blank value is unlikely to
        # ever be intentional, so the same caveat is shown for all six.
        return f"{name:<21} set = {raw!r}  (blank -- treated as unset by the cascade)"
    return f"{name:<21} set = {raw!r}"


def _print_playfields(directories: Directories) -> None:
    playfields_dir = directories.config_dir / "playfields"
    print(f"playfields ({playfields_dir}):")
    files = sorted(playfields_dir.glob("*.json")) if playfields_dir.is_dir() else []
    if not files:
        print("  (none)")
        return
    for path in files:
        print(f"  {path.name}  exists")


def _print_cameras(directories: Directories) -> None:
    print("cameras:")
    slugs = _known_camera_slugs(directories)
    if not slugs:
        print("  (no known cameras)")
    for slug in slugs:
        config_path = directories.config_dir / "cameras" / slug / "config.json"
        calibration_path = directories.state_dir / "cameras" / slug / CALIBRATION_FILENAME
        print(f"  {slug}:")
        print(f"    {config_path}  {_marker(config_path)}")
        print(f"    {calibration_path}  {_marker(calibration_path)}")
    registry_path = directories.state_dir / "cameras" / REGISTRY_FILENAME
    print(f"  registry: {registry_path}  {_marker(registry_path)}")


def _marker(path: Path) -> str:
    return "exists" if path.exists() else "missing"


def _known_camera_slugs(directories: Directories) -> Tuple[str, ...]:
    """Every camera slug this command can find evidence of, from any of
    three sources: a per-slug directory under ``config_dir/cameras``
    (config pushed/installed, maybe never calibrated), a per-slug
    directory under ``state_dir/cameras`` (calibrated at least once,
    config file possibly since removed), and ``registry.json``'s own
    slug list (known to the registry, no per-camera directory created
    under either root yet). All three are "known" and worth reporting
    -- the union, not just the intersection -- since the whole point of
    this command is surfacing exactly this kind of partial state.
    """
    slugs: set = set()
    for root in (directories.config_dir, directories.state_dir):
        cameras_dir = root / "cameras"
        if cameras_dir.is_dir():
            slugs.update(p.name for p in cameras_dir.iterdir() if p.is_dir())
    slugs.update(_registry_slugs(directories.state_dir / "cameras" / REGISTRY_FILENAME))
    return tuple(sorted(slugs))


def _registry_slugs(registry_path: Path) -> Tuple[str, ...]:
    """Best-effort slugs read directly from ``registry.json`` -- see the
    module docstring for why this avoids constructing a real
    ``CameraRegistry``. Never raises: anything not shaped like a
    registry (absent, corrupt JSON, unexpected structure) simply
    contributes no slugs. This helper's only job is camera-name
    enumeration; a broken *config* document's error text comes from
    :func:`_print_load_errors` instead -- ``registry.json`` is state,
    not scanned by ``ConfigLoader.load_config_dir`` at all, so it has no
    equivalent error-reporting path today.
    """
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    cameras = data.get("cameras") if isinstance(data, dict) else None
    if not isinstance(cameras, dict):
        return ()
    return tuple(
        record["slug"]
        for record in cameras.values()
        if isinstance(record, dict) and isinstance(record.get("slug"), str)
    )


def _print_load_errors(scan: ScanResult) -> None:
    """The issue's own "arguably the highest-value part": every file
    ``ConfigLoader.load_config_dir`` rejected, named, with its message.
    Normally invisible -- skip-and-warn reaches only a log line (sprint
    023's Design Rationale, Decision 1) -- surfaced here as the
    first-class diagnostic the issue calls for."""
    print("load errors:")
    if not scan.errors:
        print("  (none)")
        return
    for error in scan.errors:
        print(f"  {error.path}: {error.message}")


def add_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Wire the top-level ``config`` command in. Deliberately no
    ``-d``/``--daemon`` and no ``--all`` -- see the module docstring:
    this is the second command in this package (after ``guide``) that
    never targets, or needs, a daemon."""
    parser = subparsers.add_parser(
        "config",
        help=(
            "report resolved config/state/socket roots, env vars, and "
            "file locations -- works with no daemon running"
        ),
    )
    parser.set_defaults(func=cmd_config)
