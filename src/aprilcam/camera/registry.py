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
  (ticket 011-001), and the enumeration / reconnect-reuse / data-dir adoption
  logic (ticket 011-002).

Enumeration and reconnect reuse (ticket 011-002)
------------------------------------------------
Each newly-seen camera is assigned the next monotonic enumeration number on
first sight; the counter (``next_enum``) is persisted in ``registry.json`` and
survives reloads. :meth:`CameraRegistry.resolve` looks a camera up by its
stable ``unique_id`` (from :mod:`aprilcam.camera.identity`): a known camera
reuses its existing enumeration number *and* its per-camera dir, so an
unplug/replug presents the same record with no renumber and no new dir; a
genuinely new ``unique_id`` gets a fresh number and record.

Data-dir adoption policy (ticket 011-002)
-----------------------------------------
On first registry load, existing per-camera ``data/aprilcam/cameras/<slug>/``
directories are adopted into records, matched by slug. **Directories are never
renamed**: the record's ``dir`` key keeps the existing slug dir so the
``calibration.json``, ``paths.json``, and ``info.json`` paths inside it stay
valid. The registry index is the source of truth mapping ``unique_id →
existing dir``. When two distinct cameras would collide on the same slug dir
(the identical-model case), the newcomer's dir is disambiguated with an enum
suffix (``<slug>-<enum>``) while the already-populated dir is left untouched.

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
        self, cameras_dir: str | os.PathLike, *, adopt: bool = True
    ) -> None:
        self.cameras_dir = Path(cameras_dir)
        self.path = self.cameras_dir / REGISTRY_FILENAME
        self._records: Dict[str, CameraRecord] = {}
        self._next_enum: int = FIRST_ENUM
        self.load()
        if adopt:
            self.adopt_existing_dirs()

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
        """Pick an unused per-camera dir for a brand-new record.

        Prefers the bare ``slug`` (so a single camera keeps the historical dir
        name). On collision with a dir another record already owns — the
        identical-model case — disambiguates with the camera's enumeration
        number (``<slug>-<enum>``), leaving the colliding record's dir
        untouched.
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
        number and a fresh, collision-disambiguated dir.

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

    # -- data-dir adoption / migration (ticket 011-002) ---------------------

    def adopt_existing_dirs(self, *, save: bool = True) -> int:
        """Adopt pre-existing per-camera dirs into records without renaming.

        Scans ``cameras_dir`` for legacy ``<slug>/`` directories that no record
        already claims and creates a record for each, keyed by a placeholder
        ``unique_id`` derived from the slug. The directory is **not renamed** —
        the record's ``dir`` keeps the existing slug so the
        ``calibration.json`` / ``paths.json`` / ``info.json`` inside it stay at
        their original paths. Adopted records get an enumeration number so they
        appear in listings; when the device later reconnects, :meth:`resolve`
        keys on the real hardware ``unique_id`` (a distinct record) — adoption
        only guarantees no existing data is orphaned.

        Returns the number of directories newly adopted. Idempotent: dirs
        already owned by a record (including on a second call) are skipped.
        """
        try:
            entries = sorted(p for p in self.cameras_dir.iterdir() if p.is_dir())
        except (FileNotFoundError, NotADirectoryError, OSError):
            return 0

        owned = self._used_dirs()
        owned_uids = set(self._records.keys())
        adopted = 0
        for entry in entries:
            slug = entry.name
            if slug in owned:
                continue
            placeholder_uid = f"dir:{slug}"
            if placeholder_uid in owned_uids:
                continue
            enum = self._next_enum
            self._next_enum = enum + 1
            record = CameraRecord(
                unique_id=placeholder_uid,
                enum=enum,
                dir=slug,
                name=_name_from_slug(slug),
                last_seen=None,
            )
            self._records[record.unique_id] = record
            owned.add(slug)
            owned_uids.add(placeholder_uid)
            adopted += 1

        if adopted and save:
            self.save()
        return adopted


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (timezone-aware)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _name_from_slug(slug: str) -> str:
    """Best-effort human-ish name from a dir slug (``a-b-c`` → ``A B C``)."""
    return " ".join(part for part in slug.split("-") if part).title() or slug
