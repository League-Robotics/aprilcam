"""Persistent camera registry — record schema and atomic persistence.

The registry remembers every camera the system has ever seen, keyed by the
stable ``unique_id`` from :mod:`aprilcam.camera.identity`. It is the source of
truth that lets the daemon survive unplug/replug and re-enumeration without a
restart, and lets the CLI list previously-seen (offline) cameras alongside
connected ones.

This module provides, for ticket 011-001:

* :class:`CameraRecord` — the on-disk record schema.
* :class:`CameraRegistry` — atomic load/save of the
  ``data/aprilcam/cameras/registry.json`` index plus an ``upsert(record)`` API.

Enumeration-number assignment and reconnect reuse are a later ticket (011-002);
this module deliberately does not assign enumeration numbers or match against
the live device list.

On-disk format
--------------
``registry.json`` is a single JSON object::

    {
      "version": 1,
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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional


REGISTRY_VERSION = 1
REGISTRY_FILENAME = "registry.json"


@dataclass
class CameraRecord:
    """One registry entry for a single camera.

    Attributes
    ----------
    unique_id:
        Stable hardware id (see :mod:`aprilcam.camera.identity`). Primary key.
    enum:
        Monotonic enumeration number assigned on first sight. ``None`` until a
        later ticket assigns it; this ticket only round-trips the field.
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

    def __init__(self, cameras_dir: str | os.PathLike) -> None:
        self.cameras_dir = Path(cameras_dir)
        self.path = self.cameras_dir / REGISTRY_FILENAME
        self._records: Dict[str, CameraRecord] = {}
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> None:
        """(Re)load records from disk. Missing/corrupt files start empty."""
        self._records = {}
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
        cameras = data.get("cameras") if isinstance(data, dict) else None
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

    def save(self) -> None:
        """Atomically write the registry to ``registry.json``.

        Writes to a sibling ``.tmp`` file then ``os.replace``-s it over the
        target, leaving no ``.tmp`` behind on success.
        """
        self.cameras_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": REGISTRY_VERSION,
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
