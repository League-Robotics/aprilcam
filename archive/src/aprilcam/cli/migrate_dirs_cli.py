"""`aprilcam migrate-dirs` — relocate legacy co-located config into config_dir.

Sprint 019 split AprilCam's single ``data_dir`` tree into two FHS/XDG
categories: admin-owned **config** (``config_dir`` — per-camera
``config.json``, ``playfields/*.json``) and app-generated **state**
(``data_dir`` — ``calibration.json``, ``registry.json``, ``hosts.json``,
``info.json``, ``paths.json``). Installs created before sprint 019 have both
categories co-located under ``data_dir``; :func:`Config.effective_config_dir`
transparently falls back to reading config from there so nothing breaks, but
the files never move on their own — see architecture-update.md "No automatic
migration — explicit CLI only".

This command is the **only** thing that moves those files. It is explicit,
operator-initiated, and safe to run repeatedly:

- Scans ``data_dir/cameras/*/config.json`` and ``data_dir/playfields/*.json``
  (the legacy locations) and moves each to the equivalent path under the
  true ``config_dir`` (never ``effective_config_dir`` — the whole point is to
  move *out of* the legacy fallback tree).
- Leaves every state-category file exactly where it is.
- Is idempotent: files already migrated (absent from the legacy location)
  are silently skipped on a re-run.
- ``--dry-run`` prints the plan and performs zero filesystem writes.
- A destination file that already exists is left alone (skip-and-warn); use
  ``--force`` to overwrite it.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class _Move:
    """One planned or completed config-file relocation."""

    src: Path
    dst: Path
    kind: str  # "camera" or "playfield"


@dataclass
class _Conflict:
    """A move that was skipped because the destination already exists."""

    src: Path
    dst: Path


def _plan_moves(config) -> List[_Move]:
    """Scan the legacy locations under ``data_dir`` and compute the plan.

    Returns the list of files that currently live under the legacy,
    config-in-state-tree location and have a computed destination under the
    true ``config_dir``. Does not check whether the destination already
    exists (see :func:`_execute` / :func:`_dry_run` for that).
    """
    moves: List[_Move] = []

    cameras_root = config.data_dir / "cameras"
    if cameras_root.is_dir():
        for camera_dir in sorted(cameras_root.iterdir()):
            if not camera_dir.is_dir():
                continue
            src = camera_dir / "config.json"
            if src.is_file():
                dst = config.config_cameras_dir / camera_dir.name / "config.json"
                moves.append(_Move(src=src, dst=dst, kind="camera"))

    playfields_root = config.data_dir / "playfields"
    if playfields_root.is_dir():
        for entry in sorted(playfields_root.iterdir()):
            if entry.is_file() and entry.suffix == ".json":
                dst = config.config_playfields_dir / entry.name
                moves.append(_Move(src=entry, dst=dst, kind="playfield"))

    return moves


def _move_file(src: Path, dst: Path) -> None:
    """Move *src* to *dst*, creating parent dirs. Atomic on the same filesystem.

    Falls back to copy-then-verify-then-unlink for cross-filesystem moves
    (``Path.rename`` raises ``OSError``/``EXDEV`` in that case).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dst)
    except OSError:
        shutil.copy2(src, dst)
        if dst.stat().st_size != src.stat().st_size:
            raise OSError(f"copy verification failed for {src} -> {dst}")
        src.unlink()


def run_migration(config, *, dry_run: bool = False, force: bool = False) -> int:
    """Run (or plan) the migration. Returns a process exit code.

    Args:
        config: A loaded :class:`~aprilcam.config.Config`.
        dry_run: When True, print the plan and perform zero writes.
        force: When True, overwrite a destination file that already exists
            instead of skipping it as a conflict.

    Returns:
        0 on success (including the no-op / nothing-to-migrate case);
        nonzero when any file failed to move (permission error, etc.) or hit
        an unresolved conflict.
    """
    if config.config_dir == config.data_dir:
        print(
            "aprilcam migrate-dirs: config_dir and data_dir are the same path "
            f"({config.data_dir}) — nothing to migrate."
        )
        return 0

    moves = _plan_moves(config)

    if not moves:
        print("aprilcam migrate-dirs: nothing to migrate — already up to date.")
        return 0

    moved: List[_Move] = []
    conflicts: List[_Conflict] = []
    errors: List[str] = []

    for mv in moves:
        if mv.dst.exists():
            conflicts.append(_Conflict(src=mv.src, dst=mv.dst))
            continue

        if dry_run:
            print(f"would move: {mv.src} -> {mv.dst}")
            moved.append(mv)
            continue

        try:
            _move_file(mv.src, mv.dst)
        except OSError as exc:
            errors.append(f"{mv.src} -> {mv.dst}: {exc}")
            continue

        print(f"moved: {mv.src} -> {mv.dst}")
        moved.append(mv)

    # --force: overwrite conflicting destinations too (real run only — a
    # dry-run under --force still just reports the plan, no writes).
    if force and conflicts:
        remaining_conflicts: List[_Conflict] = []
        for c in conflicts:
            if dry_run:
                print(f"would overwrite (--force): {c.src} -> {c.dst}")
                moved.append(_Move(src=c.src, dst=c.dst, kind="camera"))
                continue
            try:
                c.dst.unlink()
                _move_file(c.src, c.dst)
            except OSError as exc:
                errors.append(f"{c.src} -> {c.dst}: {exc}")
                continue
            print(f"overwrote: {c.src} -> {c.dst}")
            moved.append(_Move(src=c.src, dst=c.dst, kind="camera"))
        conflicts = remaining_conflicts

    for c in conflicts:
        print(
            f"conflict (skipped, destination exists — use --force to overwrite): "
            f"{c.src} -> {c.dst}"
        )

    print()
    if dry_run:
        print(f"aprilcam migrate-dirs --dry-run: {len(moved)} file(s) would move.")
    else:
        print(f"aprilcam migrate-dirs: moved {len(moved)} file(s).")
    if conflicts:
        print(f"  {len(conflicts)} conflict(s) skipped (see above).")
    if errors:
        print(f"  {len(errors)} error(s):")
        for err in errors:
            print(f"    {err}")
    print(f"config now lives under: {config.config_dir}")

    if errors or conflicts:
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate-dirs",
        description=(
            "Move admin-owned config files (per-camera config.json, "
            "playfields/*.json) out of the legacy single-tree layout under "
            "data_dir into the proper config_dir (FHS: /etc/aprilcam, "
            "XDG: ~/.config/aprilcam). State files (calibration.json, "
            "registry.json, hosts.json, info.json, paths.json) are never "
            "touched and remain under data_dir."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned moves without writing or deleting anything.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite a destination config file that already exists instead of skipping it.",
    )
    args = parser.parse_args(argv)

    from ..config import Config

    config = Config.load()
    return run_migration(config, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
