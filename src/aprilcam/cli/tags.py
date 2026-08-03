"""``aprilcam tags mount register|clear|list`` (ticket 025-005, UC-12).

**Spelling decision.** The issue asked for either ``aprilcam tags
mount`` or ``aprilcam camera mount`` -- this module chooses ``tags
mount``, the issue's own suggested spelling, because the capability
being configured is about a *tag's* physical mount on its robot
(size, offset, yaw), not about a *camera's* configuration
(``cli/camera.py``'s ``camera config``). A mount registration has no
camera in it at all -- ``register_tag``/``unregister_tag``/
``list_tag_parameters`` (``daemon.interfaces.Daemon``) never take a
camera argument -- so nesting it under ``camera`` would misname what
is being described.

``tags/`` currently has zero implementation files (only
``tags/interfaces.py``'s ``TagGenerator``, unimplemented); this module
is the first concrete thing in that noun. Sprint 028's later
``aprilcam tags generate`` (UC-21) is expected to add its own
subcommand to *this same file* rather than conflict with it -- both
are argv-to-client-call shells for the ``tags`` top-level noun.

Thin argv-to-client-call shell over ``client/daemon_client.py``'s
``register_tag``/``unregister_tag``/``list_tag_parameters`` --
``adapters.md``'s "adapters hold no authoritative state, adapters do
no vision" rules apply here exactly as in ``cli/camera.py``/
``cli/playfield.py``. No vision, no math beyond the one small unit
conversion below.

**Degrees at the CLI boundary, radians on the wire.**
``MountParameters.mount_yaw_rad`` is the wire/Python unit; ``--yaw-deg``
is the CLI-friendly one. The conversion happens here, at the CLI
boundary, and nowhere else -- mirroring ``cli/playfield.py``'s module
docstring ("each side owns its own small conversion") and
``client/daemon_client.py``'s "request marshalling lives here" stance:
neither ``daemon/service.py`` nor the proto layer ever sees degrees.

**Family strings need no translation table.** ``family`` is
``apriltag``/``aruco``, restricted by ``argparse``'s own ``choices=``
to exactly :class:`~aprilcam.types.TagFamily`'s enum string values
(``TagFamily`` is itself a ``str`` subclass) -- so ``TagFamily(args.
family)`` always succeeds once argparse has accepted the argument, and
there is no separate mapping to keep in sync with the enum.

**``register``/``clear`` are single-target; ``list`` fans out.** Per
``docs/design/use-cases/cli.md``'s resolved fan-out rule ("read-only
only -- list, status, tags, image"), a mount write does not fan out
(refused up front, before any connection is attempted, mirroring
``cmd_calibrate``/``cmd_view``'s own up-front ``--all`` refusal shape);
``mount list`` is read-only and fans out exactly like ``camera list``/
``playfield list``.

**``clear`` on an absent registration is a no-op, not an error** --
``Daemon.unregister_tag``'s own contract -- and prints a confirmation
either way, matching ``cmd_annotations --clear``'s existing tone: a
client asking to clear a mount it never set, or already cleared, gets
the same reassuring message as one that actually removed something.

**``list`` prints ``(no registered tags)`` when empty**, matching this
codebase's established ``(no cameras)``/``(no playfields)``/``(no
annotations)``/``(no matches)`` convention rather than silent empty
output.
"""

from __future__ import annotations

import argparse
import math
import sys
from typing import Callable, Dict

from aprilcam.cli._common import (
    EXIT_ERROR,
    EXIT_OK,
    add_all_option,
    add_daemon_option,
    fan_out,
    incomplete_command,
    report_error,
)
from aprilcam.client.discovery import Discovery
from aprilcam.errors import AprilCamError
from aprilcam.types import MountParameters, TagFamily, TagId

__all__ = [
    "add_subparser",
    "cmd_register",
    "cmd_clear",
    "cmd_list",
]


def _tag_from_args(args: argparse.Namespace) -> TagId:
    """*family* is already restricted to a valid :class:`TagFamily`
    value by ``argparse``'s own ``choices=`` (see the module
    docstring), so this conversion cannot fail."""
    return TagId(family=TagFamily(args.family), number=args.number)


def cmd_register(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam tags mount register <family> <number> [--x CM] [--y CM]
    [--z CM] [--yaw-deg DEG] [--size CM] [-d HOST]`` (UC-12).

    All offset flags default to ``0.0``, matching
    :class:`~aprilcam.types.MountParameters`'s own zero defaults --
    there is nothing to validate for an all-default call, it is simply
    the "tag mounted flush at the robot's reference point, facing
    forward" case. ``--size`` defaults to ``None`` (the daemon's own
    configured default size applies, per ``MountParameters.size_cm``'s
    docstring).

    A nonzero ``--z`` with no calibration anywhere raises
    :class:`~aprilcam.errors.NotCalibrated` (UC-12.S-5) --
    surfaced here exactly like any other typed error, via
    :func:`~aprilcam.cli._common.report_error`.
    """
    if args.all:
        print(
            "error: 'tags mount register' is single-target -- a mount "
            "registration does not fan out (UC-12, matching 'camera "
            "calibrate'/'playfield view'). Drop --all and target one "
            "daemon.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    tag = _tag_from_args(args)
    params = MountParameters(
        size_cm=args.size,
        mount_x=args.x,
        mount_y=args.y,
        mount_z=args.z,
        mount_yaw_rad=math.radians(args.yaw_deg),
    )

    try:
        discovery_factory().connect(args.daemon).register_tag(tag, params)
    except AprilCamError as exc:
        return report_error(exc)

    size = f"{args.size:g}cm" if args.size is not None else "(default)"
    print(
        f"registered {tag.family.value} {tag.number}: "
        f"x={args.x:g} y={args.y:g} z={args.z:g} yaw={args.yaw_deg:g}deg size={size}"
    )
    return EXIT_OK


def cmd_clear(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam tags mount clear <family> <number> [-d HOST]``.

    Calls ``unregister_tag`` -- an absent registration is a no-op (see
    the module docstring); this always prints a confirmation and
    returns success, never an error, for that case.
    """
    if args.all:
        print(
            "error: 'tags mount clear' is single-target -- a mount "
            "write does not fan out (UC-12, matching 'tags mount "
            "register'). Drop --all and target one daemon.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    tag = _tag_from_args(args)

    try:
        discovery_factory().connect(args.daemon).unregister_tag(tag)
    except AprilCamError as exc:
        return report_error(exc)

    print(f"cleared mount for {tag.family.value} {tag.number}")
    return EXIT_OK


def _format_registration_row(tag: TagId, params: MountParameters) -> str:
    """One registration as a single aligned line -- size and yaw
    printed in the same CLI-friendly units ``register`` accepts them
    in (cm, degrees), converted back from the wire's radians here at
    the CLI boundary, matching the module docstring's conversion
    rule."""
    size = f"{params.size_cm:g}cm" if params.size_cm is not None else "-"
    yaw_deg = math.degrees(params.mount_yaw_rad)
    return (
        f"  {tag.family.value:<10} {tag.number:<6} "
        f"x={params.mount_x:.2f} y={params.mount_y:.2f} z={params.mount_z:.2f} "
        f"yaw={yaw_deg:.1f}deg size={size}"
    )


def _print_registrations(registrations: Dict[TagId, MountParameters]) -> None:
    """A ``list_tag_parameters`` result, printed verbatim. An empty
    registry prints an explicit ``(no registered tags)`` rather than
    nothing at all, matching this codebase's established ``(no X)``
    convention (see the module docstring)."""
    if not registrations:
        print("  (no registered tags)")
        return
    for tag in sorted(registrations, key=lambda t: (t.family.value, t.number)):
        print(_format_registration_row(tag, registrations[tag]))


def cmd_list(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam tags mount list [-d HOST] [--all]``.

    Read-only, so it fans out like ``camera list``/``playfield list``
    (``cli.md``'s fan-out table).
    """
    discovery = discovery_factory()
    if args.all:
        def render(label: str, registrations: Dict[TagId, MountParameters]) -> None:
            print(f"{label}:")
            _print_registrations(registrations)

        return fan_out(discovery, lambda daemon: daemon.list_tag_parameters(), render)

    try:
        registrations = discovery.connect(args.daemon).list_tag_parameters()
    except AprilCamError as exc:
        return report_error(exc)
    _print_registrations(registrations)
    return EXIT_OK


def add_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Wire ``tags mount register|clear|list`` into the top-level
    parser. ``tags`` is its own top-level command (not nested under
    ``camera`` or ``playfield``, see the module docstring), with
    ``mount`` as its first sub-noun -- leaving room for sprint 028's
    ``tags generate`` (UC-21) to add a sibling sub-noun to the same
    ``tags`` subparsers action without conflict.
    """
    parser = subparsers.add_parser("tags", help="tag mount registration (and, later, generation)")
    parser.set_defaults(func=incomplete_command(parser))
    sub = parser.add_subparsers(dest="tags_command", metavar="<command>")

    p_mount = sub.add_parser(
        "mount", help="mobile-tag mount parameter registry (UC-12)"
    )
    p_mount.set_defaults(func=incomplete_command(p_mount))
    mount_sub = p_mount.add_subparsers(dest="mount_command", metavar="<command>")

    families = [family.value for family in TagFamily]

    p_register = mount_sub.add_parser(
        "register", help="register a tag's size and mount offset/yaw (UC-12)"
    )
    p_register.add_argument("family", choices=families, help="tag family")
    p_register.add_argument("number", type=int, help="tag number")
    p_register.add_argument(
        "--x", type=float, default=0.0, metavar="CM",
        help="mount_x offset, robot's own frame (+x forward) (default: 0.0)",
    )
    p_register.add_argument(
        "--y", type=float, default=0.0, metavar="CM",
        help="mount_y offset, robot's own frame (+y left) (default: 0.0)",
    )
    p_register.add_argument(
        "--z", type=float, default=0.0, metavar="CM",
        help="mount_z height above the field plane, drives parallax "
        "correction; nonzero requires a calibrated camera (default: 0.0)",
    )
    p_register.add_argument(
        "--yaw-deg", type=float, default=0.0, metavar="DEG",
        help="mount yaw in degrees, converted to radians before the RPC (default: 0.0)",
    )
    p_register.add_argument(
        "--size", type=float, default=None, metavar="CM",
        help="tag edge length override (default: the daemon's own configured default)",
    )
    add_daemon_option(p_register)
    add_all_option(p_register, note=" (refused -- registration is single-target)")
    p_register.set_defaults(func=cmd_register)

    p_clear = mount_sub.add_parser(
        "clear", help="unregister a tag's mount parameters (no-op if absent)"
    )
    p_clear.add_argument("family", choices=families, help="tag family")
    p_clear.add_argument("number", type=int, help="tag number")
    add_daemon_option(p_clear)
    add_all_option(p_clear, note=" (refused -- clearing is single-target)")
    p_clear.set_defaults(func=cmd_clear)

    p_list = mount_sub.add_parser("list", help="list every registered tag's mount parameters")
    add_daemon_option(p_list)
    add_all_option(p_list)
    p_list.set_defaults(func=cmd_list)
