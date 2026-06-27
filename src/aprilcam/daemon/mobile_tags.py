"""Persistent registry of client-registered **mobile tags**.

A *mobile tag* is a tag a client has declared to be mounted on a moving object
(a robot), together with the tag's pose **relative to the object's reference
point** — its centre of rotation. A tag is "mobile" precisely because a client
registered it here; there is no other static/mobile distinction. Anything not
registered is treated as a fixed marker on the playfield.

Each entry carries the tag's mount offset:

- ``x_mm`` — forward of the object centre (object frame, +x forward)
- ``y_mm`` — left of the object centre (object frame, +y left)
- ``z_cm`` — height above the playfield (drives parallax correction)
- ``yaw_deg`` — the tag's heading relative to the object's forward
- ``owner`` — informational label for the client/robot that owns the tag

The registry is persisted in ``<data_dir>/tags.json`` under the ``"tags"`` key
and applied to world-position reporting by :class:`CameraPipeline`. The daemon
reads/writes it; clients register and clear via the gRPC API (a robot typically
registers once at start-up). Legacy ``{"tag_heights": {id: z_cm}}`` files are
read transparently and migrated on the next write.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

_FILENAME = "tags.json"


@dataclass
class MobileTag:
    """A tag's mount pose relative to its robot's centre of rotation."""

    tag_id: int
    x_mm: float = 0.0
    y_mm: float = 0.0
    z_cm: float = 0.0
    yaw_deg: float = 0.0
    owner: str = ""

    def to_json(self) -> dict:
        # Round to shed float32 wire noise (e.g. 11.8 -> 11.800000190734863) so
        # the persisted file stays human-readable; 4 dp is well below any
        # meaningful mount precision.
        return {
            "x_mm": round(self.x_mm, 4),
            "y_mm": round(self.y_mm, 4),
            "z_cm": round(self.z_cm, 4),
            "yaw_deg": round(self.yaw_deg, 4),
            "owner": self.owner,
        }


def _path(data_dir) -> Path:
    return Path(data_dir) / _FILENAME


def load(data_dir) -> Dict[int, MobileTag]:
    """Load the mobile-tag registry from ``tags.json``.

    Tolerates a missing or corrupt file (returns ``{}``). Imports legacy
    ``{"tag_heights": {id: z_cm}}`` entries; the 4D ``"tags"`` block overrides.
    """
    out: Dict[int, MobileTag] = {}
    try:
        raw = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except Exception:
        return out
    if not isinstance(raw, dict):
        return out

    for k, v in (raw.get("tag_heights") or {}).items():
        try:
            tid = int(k)
            out[tid] = MobileTag(tag_id=tid, z_cm=float(v))
        except Exception:
            continue

    for k, v in (raw.get("tags") or {}).items():
        try:
            tid = int(k)
            prev = out.get(tid)
            out[tid] = MobileTag(
                tag_id=tid,
                x_mm=float(v.get("x_mm", 0.0)),
                y_mm=float(v.get("y_mm", 0.0)),
                z_cm=float(v.get("z_cm", prev.z_cm if prev else 0.0)),
                yaw_deg=float(v.get("yaw_deg", 0.0)),
                owner=str(v.get("owner", "")),
            )
        except Exception:
            continue
    return out


def save(data_dir, registry: Dict[int, MobileTag]) -> None:
    """Atomically write *registry* to ``tags.json`` under the ``"tags"`` key."""
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = {"tags": {str(t.tag_id): t.to_json() for t in registry.values()}}
    text = json.dumps(blob, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tags_tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, str(p))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def register(data_dir, tag: MobileTag) -> Dict[int, MobileTag]:
    """Add or replace *tag* in the registry and persist. Returns the new registry."""
    reg = load(data_dir)
    reg[tag.tag_id] = tag
    save(data_dir, reg)
    return reg


def clear(data_dir, tag_id: Optional[int] = None) -> Dict[int, MobileTag]:
    """Remove one tag (``tag_id``) or all (``tag_id is None``). Returns the new registry."""
    reg = load(data_dir)
    if tag_id is None:
        reg = {}
    else:
        reg.pop(int(tag_id), None)
    save(data_dir, reg)
    return reg
