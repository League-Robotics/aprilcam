"""Persistent camera registry — record schema and atomic persistence.

The registry remembers every camera the system has ever seen, keyed by the
stable ``unique_id`` from :mod:`aprilcam.camera.identity`. It is the source of
truth that lets the daemon survive unplug/replug and re-enumeration without a
restart, and lets the CLI list previously-seen (offline) cameras alongside
connected ones.

This module provides:

* :class:`CameraRecord` — the on-disk record schema (ticket 011-001).
* :class:`CameraRegistry` — atomic load/save of the
  ``data/aprilcam/cameras/registry.json`` index plus an ``upsert(record)`` API
  (ticket 011-001), and the enumeration / reconnect-reuse / connect-time
  dir-adoption logic (tickets 011-002, 011-003).

Enumeration and reconnect reuse (ticket 011-002)
------------------------------------------------
Each newly-seen camera is assigned the next monotonic enumeration number on
first sight; the counter (``next_enum``) is persisted in ``registry.json`` and
survives reloads. :meth:`CameraRegistry.resolve` looks a camera up by its
stable ``unique_id`` (from :mod:`aprilcam.camera.identity`): a known camera
reuses its existing enumeration number *and* its per-camera dir, so an
unplug/replug presents the same record with no renumber and no new dir; a
genuinely new ``unique_id`` gets a fresh number and record.

Connect-time data-dir adoption policy (ticket 011-003)
------------------------------------------------------
**The registry only ever holds records for cameras that have actually been
resolved** (connected at least once in this registry's life). It does *not*
fabricate placeholder records for arbitrary on-disk
``data/aprilcam/cameras/<slug>/`` directories. A bare on-disk dir with no
matching record is simply not listed until its camera connects.

When a camera is resolved for the **first time**, :meth:`resolve` adopts the
existing slug dir if one is present and unowned: the record's ``dir`` is set to
the bare ``<slug>`` so the camera reuses its existing ``calibration.json`` /
``paths.json`` / ``info.json`` in place. **Directories are never renamed.**
Only when that slug dir is already owned by another *live* record — the genuine
two-identical-model-cameras case — does the newcomer fall back to a
disambiguated dir (``<slug>-<enum>``), leaving the already-owned dir untouched.

Known limitation — USB-port moves: a serial-less camera moved to a different
USB port may resolve to a new ``unique_id`` (see
:mod:`aprilcam.camera.identity`) and therefore present as a new camera with a
fresh enumeration number. This is an accepted, documented limitation.

On-disk format
--------------
``registry.json`` is a single JSON object::

    {
      "version": 1,
      "next_enum": 3,
      "cameras": {
        "<unique_id>": {
          "unique_id": "...",
          "enum": 1,
          "dir": "arducam-ov9782-usb-camera",
          "name": "Arducam OV9782 USB Camera",
          "vid": 3141,
          "pid": 25446,
          "serial": null,
          "location": "0x21141400c456366",
          "last_seen": "2026-06-07T18:00:00+00:00"
        },
        ...
      }
    }

Writes are atomic: the JSON is written to a sibling ``*.tmp`` file and then
``os.replace``-d over the target, so a crash mid-write never leaves a partial
``registry.json`` and no ``.tmp`` file is left behind on success.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

from .identity import CameraIdentity


REGISTRY_VERSION = 1
REGISTRY_FILENAME = "registry.json"

#: First enumeration number assigned to the first-ever-seen camera.
FIRST_ENUM = 1


def dir_slug(name: Optional[str]) -> str:
    """Slugify a device name the same way the legacy per-camera dirs were.

    Mirrors ``calibration.device_name_slug`` so adoption matches existing
    ``data/aprilcam/cameras/<slug>/`` directories. Falls back to ``"camera"``
    when the name is empty.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "camera"


@dataclass
class CameraRecord:
    """One registry entry for a single camera.

    Attributes
    ----------
    unique_id:
        Stable hardware id (see :mod:`aprilcam.camera.identity`). Primary key.
    enum:
        Monotonic enumeration number assigned on first sight and reused on
        reconnect (see :meth:`CameraRegistry.resolve`). ``None`` only for
        records that predate enumeration assignment.
    dir:
        Per-camera data directory name under ``data/aprilcam/cameras/`` (the
        existing slug dir is retained as the dir key — see the sprint
        architecture's adoption decision).
    name:
        OS-reported device name at last sight.
    vid, pid, serial, location:
        Identity component fields, carried for display and re-resolution.
    last_seen:
        ISO-8601 timestamp string of the last time the camera was observed.
    """

    unique_id: str
    enum: Optional[int] = None
    dir: Optional[str] = None
    name: Optional[str] = None
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial: Optional[str] = None
    location: Optional[str] = None
    last_seen: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CameraRecord":
        if not data.get("unique_id"):
            raise ValueError("camera record requires a non-empty 'unique_id'")
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class CameraRegistry:
    """Load/save the persistent camera registry index.

    This is a thin persistence layer over :class:`CameraRecord`. It owns the
    ``registry.json`` file: reads it on construction (tolerating absence or
    corruption by starting empty), writes it atomically, and exposes an
    ``upsert`` to add or replace a record by ``unique_id``.
    """

    def __init__(
        self, cameras_dir: str | os.PathLike, *, adopt: bool = False
    ) -> None:
        # ``adopt`` is retained for call-site compatibility but defaults to
        # ``False`` and does nothing: the registry no longer fabricates
        # placeholder records for on-disk dirs. Per-camera dirs are adopted at
        # connect time by :meth:`resolve` instead (ticket 011-003).
        self.cameras_dir = Path(cameras_dir)
        self.path = self.cameras_dir / REGISTRY_FILENAME
        self._records: Dict[str, CameraRecord] = {}
        self._next_enum: int = FIRST_ENUM
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> None:
        """(Re)load records from disk. Missing/corrupt files start empty."""
        self._records = {}
        self._next_enum = FIRST_ENUM
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # Corrupt registry: start empty rather than crash the daemon.
            return
        if not isinstance(data, dict):
            return
        cameras = data.get("cameras")
        if not isinstance(cameras, dict):
            return
        for uid, rec in cameras.items():
            if not isinstance(rec, dict):
                continue
            rec = dict(rec)
            rec.setdefault("unique_id", uid)
            try:
                self._records[uid] = CameraRecord.from_dict(rec)
            except (ValueError, TypeError):
                continue
        # Restore the monotonic counter, clamping it above every stored enum so
        # a corrupt/missing counter can never re-issue a number already in use.
        raw_next = data.get("next_enum")
        next_enum = raw_next if isinstance(raw_next, int) else FIRST_ENUM
        max_enum = max(
            (r.enum for r in self._records.values() if r.enum is not None),
            default=FIRST_ENUM - 1,
        )
        self._next_enum = max(next_enum, max_enum + 1, FIRST_ENUM)

    def save(self) -> None:
        """Atomically write the registry to ``registry.json``.

        Writes to a sibling ``.tmp`` file then ``os.replace``-s it over the
        target, leaving no ``.tmp`` behind on success.
        """
        self.cameras_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": REGISTRY_VERSION,
            "next_enum": self._next_enum,
            "cameras": {uid: rec.to_dict() for uid, rec in self._records.items()},
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            # Clean up a leftover tmp on failure; on success it is gone.
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    # -- record access ------------------------------------------------------

    def get(self, unique_id: str) -> Optional[CameraRecord]:
        """Return the record for ``unique_id`` or ``None``."""
        return self._records.get(unique_id)

    def records(self) -> Iterable[CameraRecord]:
        """Iterate over all records."""
        return list(self._records.values())

    def __contains__(self, unique_id: object) -> bool:
        return unique_id in self._records

    def __len__(self) -> int:
        return len(self._records)

    def upsert(self, record: CameraRecord, *, save: bool = True) -> CameraRecord:
        """Insert or replace ``record`` by ``unique_id`` and persist.

        Returns the stored record. When ``save`` is ``True`` (default) the
        registry is written to disk atomically.
        """
        if not record.unique_id:
            raise ValueError("cannot upsert a record without a unique_id")
        self._records[record.unique_id] = record
        if save:
            self.save()
        return record

    # -- enumeration & reconnect reuse (ticket 011-002) ---------------------

    @property
    def next_enum(self) -> int:
        """The enumeration number the next first-seen camera would receive."""
        return self._next_enum

    def _used_dirs(self, *, exclude: Optional[str] = None) -> set:
        """Set of per-camera dir names already claimed by records."""
        return {
            r.dir
            for uid, r in self._records.items()
            if r.dir and uid != exclude
        }

    def _allocate_dir(self, slug: str, enum: int) -> str:
        """Pick a per-camera dir for a record, adopting an existing slug dir.

        Prefers the bare ``slug`` so a first-seen camera **adopts** any existing
        ``cameras_dir/<slug>/`` directory (reusing its ``calibration.json`` and
        siblings) and a single camera keeps the historical dir name. Only when
        the bare ``slug`` is already owned by another live record — the genuine
        identical-model case — does it disambiguate with the camera's
        enumeration number (``<slug>-<enum>``), leaving the owned dir untouched.
        """
        used = self._used_dirs()
        if slug not in used:
            return slug
        candidate = f"{slug}-{enum}"
        while candidate in used:
            candidate = f"{candidate}-x"
        return candidate

    def resolve(
        self, identity: CameraIdentity, *, save: bool = True
    ) -> CameraRecord:
        """Resolve a live camera identity to its registry record.

        Looks the camera up by its stable ``unique_id``. A **known** camera
        reuses its existing enumeration number and per-camera dir (identity
        fields and ``last_seen`` are refreshed in place) — an unplug/replug
        therefore resolves to the same record with no renumber and no new dir.
        A **new** ``unique_id`` is assigned the next monotonic enumeration
        number and a per-camera dir: it **adopts** an existing matching-slug
        dir if one is present and unowned (reusing its calibration), otherwise
        it disambiguates (``<slug>-<enum>``) only against dirs already owned by
        another live record.

        Parameters
        ----------
        identity:
            The resolved :class:`~aprilcam.camera.identity.CameraIdentity` for
            the currently-connected device.
        save:
            When ``True`` (default) the registry is persisted after the resolve.

        Returns the stored :class:`CameraRecord`.
        """
        if not identity.unique_id:
            raise ValueError("cannot resolve a camera without a unique_id")

        existing = self._records.get(identity.unique_id)
        if existing is not None:
            # Reconnect reuse: keep enum + dir, refresh identity/last_seen.
            existing.name = identity.name or existing.name
            existing.vid = identity.vid if identity.vid is not None else existing.vid
            existing.pid = identity.pid if identity.pid is not None else existing.pid
            existing.serial = identity.serial or existing.serial
            existing.location = identity.location or existing.location
            existing.last_seen = _now_iso()
            if existing.dir is None:
                existing.dir = self._allocate_dir(
                    dir_slug(existing.name), existing.enum or self._next_enum
                )
            if save:
                self.save()
            return existing

        # First sight: assign the next monotonic enumeration number + dir.
        enum = self._next_enum
        self._next_enum = enum + 1
        slug = dir_slug(identity.name)
        record = CameraRecord(
            unique_id=identity.unique_id,
            enum=enum,
            dir=self._allocate_dir(slug, enum),
            name=identity.name,
            vid=identity.vid,
            pid=identity.pid,
            serial=identity.serial,
            location=identity.location,
            last_seen=_now_iso(),
        )
        self._records[record.unique_id] = record
        if save:
            self.save()
        return record

    # -- data-dir adoption (ticket 011-003) ---------------------------------

    def adopt_existing_dirs(self, *, save: bool = True) -> int:
        """Deprecated no-op — kept only for call-site compatibility.

        The registry no longer fabricates placeholder records for on-disk
        ``cameras_dir/<slug>/`` directories: doing so produced phantom/duplicate
        entries (an offline ``dir:<slug>`` placeholder plus a separate live
        ``<slug>-<enum>`` record once the real camera connected, orphaning the
        camera's existing calibration). Per-camera dirs are now adopted at
        **connect time** by :meth:`resolve`, which sets a first-seen camera's
        ``dir`` to its existing matching-slug dir when one is present and
        unowned. This method does nothing and always returns ``0``.
        """
        return 0


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (timezone-aware)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Enumeration-number → live OS index resolution (user-facing camera selector)
# ---------------------------------------------------------------------------


class CameraSelectError(Exception):
    """Raised when an enumeration number cannot be resolved to a live camera.

    Carries a human-readable message suitable for printing to the CLI user
    (e.g. ``"no camera #5"`` or ``"camera #5 is not connected"``).
    """


def resolve_enum_to_index(
    enum_no: int,
    registry: "CameraRegistry",
    live_identities: Dict[int, CameraIdentity],
) -> int:
    """Resolve a user-facing enumeration number to a live OS camera index.

    The enumeration number is the stable, user-facing handle printed by
    ``aprilcam cameras`` (the ``enum`` field on a :class:`CameraRecord`). This
    maps it to the OS index the camera is *currently* connected at:

    1. Find the :class:`CameraRecord` whose ``enum`` equals ``enum_no``.
    2. Take that record's ``unique_id`` and match it against ``live_identities``
       (the ``{os_index: CameraIdentity}`` table for currently-connected
       cameras, as returned by
       :func:`aprilcam.camera.identity.resolve_all`).
    3. Return the live OS ``index`` for that ``unique_id``.

    Parameters
    ----------
    enum_no:
        The enumeration number the user typed (the ``#`` shown by
        ``aprilcam cameras``).
    registry:
        The :class:`CameraRegistry` to look the enumeration number up in.
    live_identities:
        ``{os_index: CameraIdentity}`` for currently-connected cameras.

    Returns
    -------
    int
        The live OS index for the camera with that enumeration number.

    Raises
    ------
    CameraSelectError
        If no record carries that enumeration number (``"no camera #N"``), or
        the camera is known but not currently connected
        (``"camera #N is not connected"``).
    """
    record: Optional[CameraRecord] = next(
        (r for r in registry.records() if r.enum == enum_no), None
    )
    if record is None:
        raise CameraSelectError(f"no camera #{enum_no}")

    for index, identity in live_identities.items():
        if identity.unique_id == record.unique_id:
            return index

    name = record.name or record.dir or record.unique_id
    raise CameraSelectError(f"camera #{enum_no} ({name}) is not connected")
