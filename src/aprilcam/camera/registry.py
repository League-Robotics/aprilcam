"""Persistent camera registry — identity to stable enumeration + slug.

Converted from ``archive/src/aprilcam/camera/registry.py`` (v1's
``CameraRecord``/``CameraRegistry``, ~457 lines) into the exact shape of
``camera.interfaces.CameraRegistry`` (``resolve(identity) -> str``,
``forget(slug) -> None``), wired to ``config/directories.py``'s
``state_dir`` (``docs/design/configuration.md``) instead of v1's own
directory resolution.

Lives in the **state** directory (regenerable, daemon-owned, never
hand-edited — ``configuration.md`` §1): ``registry.json`` under
``state_dir/cameras/`` (``docs/design/design.md`` §7). Construct with
that directory, e.g. ``CameraRegistry(Directories().state_dir /
"cameras")``. Delete the file and re-probing rebuilds it — though every
enumeration number then starts over, since the number is a convenience
this module owns, not a promise that survives deleting its own storage.

Two keys, two jobs (mirrors ``camera/identity.py``'s "two different
questions")
-----------------------------------------------------------------------
* ``hardware_key`` (``CameraIdentity.hardware_key``) is the **primary
  key** — "is this the same physical camera I saw before." A replugged
  camera with a real serial or AVFoundation id resolves to the same
  record (UC-25.S-3).
* ``slug`` (``CameraIdentity.slug``, and this module's return value) is
  the **readable, registry-assigned** name —
  usually identical to the identity's proposed slug, except when a
  *different* ``hardware_key`` proposes a slug already owned by a live
  record (two identical camera models on different ports — UC-25's
  open-question caveat): the newcomer is disambiguated with a numeric
  suffix (``<slug>-<n>``) rather than merged into the first camera's
  entry. The first camera to claim a slug always keeps the bare name.

Reconnect does not rename. Per UC-25's still-open "what if a name comes
out different than last time" question, this module takes the
conservative reading: a camera's persisted ``slug`` never changes on
reconnect, even if its freshly-resolved identity proposes a different
one — only ``device_name``/``last_seen`` refresh in place. Silently
renaming a slug would orphan whatever config file is installed under the
old name; that is a worse failure than displaying a slightly stale
device name.

Atomic writes (tmp + fsync + rename, ``design.md`` §6)
--------------------------------------------------------
``save`` writes to a sibling ``.tmp`` file, ``fsync``s it, then
``os.replace``s it over ``registry.json`` — a crash between those steps
leaves the previous, fully-written file exactly as it was; there is no
window where a reader observes a half-written registry.

Directory creation is deferred to write time — ticket 001's
``Directories`` does not create ``state_dir``, so this module creates
``registry.json``'s parent directory on demand in ``save``, wrapping any
``OSError`` (e.g. an unwritable ``state_dir``) with the path that failed
rather than surfacing a bare traceback.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from aprilcam.camera.interfaces import CameraIdentity
from aprilcam.camera.interfaces import CameraRegistry as _CameraRegistryBase
from aprilcam.errors import UnknownCamera

__all__ = ["CameraRecord", "CameraRegistry", "REGISTRY_FILENAME", "REGISTRY_VERSION"]

logger = logging.getLogger(__name__)

REGISTRY_VERSION = 1
REGISTRY_FILENAME = "registry.json"

#: First enumeration number ever assigned — ``CameraState.number``'s
#: contract starts counting here (matches v1's ``FIRST_ENUM``).
FIRST_NUMBER = 1

PathLike = Union[str, Path]


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (timezone-aware)."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CameraRecord:
    """One persisted registry entry — a camera the registry has resolved
    at least once.

    ``number`` is :attr:`aprilcam.types.CameraState.number`'s contract:
    assigned once, on first sight, and never reassigned or reshuffled.
    ``slug`` is the registry-assigned, possibly-disambiguated name (see
    the module docstring) — usually, but not always, identical to the
    proposing ``CameraIdentity.slug``.
    """

    hardware_key: str
    slug: str
    number: int
    device_name: str
    last_seen: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CameraRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class CameraRegistry(_CameraRegistryBase):
    """Concrete ``camera.interfaces.CameraRegistry``.

    Derives from ``camera.interfaces.CameraRegistry`` (a plain base
    class, not ``Protocol`` -- see ``daemon.interfaces``'s module
    docstring for the design rule).

    Loads eagerly at construction, tolerating an absent or corrupt file
    by starting empty (matching v1 — a corrupt registry must never crash
    the daemon), and persists after every mutation.
    """

    def __init__(self, cameras_dir: PathLike) -> None:
        self.cameras_dir = Path(cameras_dir)
        self.path = self.cameras_dir / REGISTRY_FILENAME
        self._records: Dict[str, CameraRecord] = {}  # keyed by hardware_key
        self._next_number = FIRST_NUMBER
        self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        self._records = {}
        self._next_number = FIRST_NUMBER
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            # Absent (common — first run) or unreadable: start empty.
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning(
                "corrupt camera registry at %s; starting empty", self.path
            )
            return
        if not isinstance(data, dict):
            return
        cameras = data.get("cameras")
        if not isinstance(cameras, dict):
            return
        for key, rec in cameras.items():
            if not isinstance(rec, dict):
                continue
            rec = dict(rec)
            rec.setdefault("hardware_key", key)
            try:
                self._records[key] = CameraRecord.from_dict(rec)
            except TypeError:
                continue
        # Clamp the counter above every stored number so a corrupt or
        # missing counter can never re-issue a number already in use —
        # reissuing would break the "numbers never reshuffle" contract.
        raw_next = data.get("next_number")
        next_number = raw_next if isinstance(raw_next, int) else FIRST_NUMBER
        max_number = max(
            (r.number for r in self._records.values()), default=FIRST_NUMBER - 1
        )
        self._next_number = max(next_number, max_number + 1, FIRST_NUMBER)

    def _save(self) -> None:
        """Atomically write the registry to ``registry.json``.

        Writes to a sibling ``.tmp`` file, ``fsync``s it, then
        ``os.replace``s it over the target — a crash at any point before
        the ``os.replace`` call leaves the prior file untouched.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(
                f"cannot create camera registry directory {self.path.parent}: {exc}"
            ) from exc

        payload = {
            "version": REGISTRY_VERSION,
            "next_number": self._next_number,
            "cameras": {key: rec.to_dict() for key, rec in self._records.items()},
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            raise OSError(f"cannot write camera registry {self.path}: {exc}") from exc
        finally:
            # Clean up a leftover tmp on failure; on success it is gone
            # (os.replace consumed it).
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    # -- CameraRegistry interface --------------------------------------------

    def resolve(self, identity: CameraIdentity) -> str:
        """See ``camera.interfaces.CameraRegistry.resolve``.

        Keys on ``identity.hardware_key``, which every
        :class:`~aprilcam.camera.interfaces.CameraIdentity` provides;
        the ``or identity.slug`` fallback below is defensive only —
        ``hardware_key`` is a required field — and keeps the same
        degraded behaviour v1 documented for cameras with no
        distinguishing hardware info if it were ever empty.
        """
        hardware_key = getattr(identity, "hardware_key", None) or identity.slug
        if not hardware_key:
            raise ValueError("cannot resolve a camera identity without a slug")

        existing = self._records.get(hardware_key)
        if existing is not None:
            # Reconnect: refresh identity/last_seen, keep slug + number
            # fixed (see the module docstring's "reconnect does not
            # rename" note).
            updated = replace(
                existing,
                device_name=identity.device_name or existing.device_name,
                last_seen=_now_iso(),
            )
            self._records[hardware_key] = updated
            self._save()
            return updated.slug

        # First sight: assign the next monotonic number and a slug,
        # disambiguating against slugs already owned by a *different*
        # hardware_key rather than merging into that camera's entry.
        proposed_slug = identity.slug
        used_slugs = {r.slug for r in self._records.values()}
        slug = proposed_slug
        if slug in used_slugs:
            suffix = 2
            while f"{proposed_slug}-{suffix}" in used_slugs:
                suffix += 1
            slug = f"{proposed_slug}-{suffix}"

        number = self._next_number
        self._next_number += 1
        record = CameraRecord(
            hardware_key=hardware_key,
            slug=slug,
            number=number,
            device_name=identity.device_name,
            last_seen=_now_iso(),
        )
        self._records[hardware_key] = record
        self._save()
        return slug

    def forget(self, slug: str) -> None:
        """See ``camera.interfaces.CameraRegistry.forget``.

        Raises :class:`aprilcam.errors.UnknownCamera` (naming the slugs
        that do exist) for a slug not in the registry — forgetting is a
        targeted operation on a named camera, the same class of failure
        ``errors.py`` reserves ``UnknownCamera`` for elsewhere.
        """
        key = next((k for k, r in self._records.items() if r.slug == slug), None)
        if key is None:
            raise UnknownCamera(
                slug, tuple(sorted(r.slug for r in self._records.values()))
            )
        del self._records[key]
        self._save()

    # -- beyond the interface: read access for callers/tests -----------------

    def get(self, slug: str) -> Optional[CameraRecord]:
        """The record for ``slug``, or ``None`` if it is not registered."""
        return next((r for r in self._records.values() if r.slug == slug), None)

    def get_by_number(self, number: int) -> Optional[CameraRecord]:
        """The record currently holding enumeration ``number``, or
        ``None`` if no record does (never issued, or the holder was
        later :meth:`forget`-ten -- gaps are structural, not an error,
        per the module docstring's "numbers are never reissued or
        reshuffled" contract).

        Mirrors :meth:`get`'s shape -- a linear scan over
        ``self._records.values()``, the same "small registry, no new
        index structure needed" reasoning as ``get``'s own docstring
        note in the sprint plan. This is the daemon's number-addressing
        half of ``_camera_for`` (``daemon/service.py``, ticket 021-002)
        — ``get`` remains the slug half.
        """
        return next((r for r in self._records.values() if r.number == number), None)

    def records(self) -> Tuple[CameraRecord, ...]:
        """Every record, ordered by enumeration number (a stable listing
        order for ``camera list``, ticket 006)."""
        return tuple(sorted(self._records.values(), key=lambda r: r.number))

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, slug: object) -> bool:
        return any(r.slug == slug for r in self._records.values())
