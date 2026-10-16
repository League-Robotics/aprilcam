"""Per-camera lens model storage (sprint 044 ticket 001).

A lens model -- intrinsic matrix plus distortion coefficients -- is a
property of one camera, one lens and one resolution. It must outlive
playfield changes and failed field calibrations, so it lives in its own
per-camera file, ``<state_dir>/cameras/<slug>/intrinsics.json``, rather
than inside the per-playfield ``calibration.json`` (sprint.md Decision 1).

This module is deliberately **cv2-free** (numpy-only at most; in fact it
needs neither): the value types here (:class:`LensModel`) are what thin
clients import, and ``tests/test_no_cv2_in_client_modules.py`` guards
that. Producing a lens model (ChArUco solving) is a later ticket; this
module only stores and looks one up.

Writes are atomic -- tmp file, ``fsync``, ``os.replace`` -- the same
pattern ``daemon.service.DaemonService._persist_calibration`` uses, so a
crash leaves the previous file intact. A missing or corrupt file is
tolerated as "no lens model held" with a logged warning, mirroring the
daemon's stance toward a corrupt ``calibration.json``.

``Calibrator`` consults the store through a ``get_lens_model`` callable
(see :meth:`LensStore.get_lens_model`) *before* it falls back to the
lens embedded in a prior ``calibration.json``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: File name under ``<state_dir>/cameras/<slug>/``.
INTRINSICS_FILENAME = "intrinsics.json"

#: Provenance values for a lens model held in the store or embedded in a
#: ``Calibration``. ``legacy`` is inferred (never stored in
#: ``intrinsics.json``) for a pre-sprint-044 ``calibration.json`` that
#: carries a matrix but no ``lens_source``.
LENS_SOURCE_MEASURED = "measured"
LENS_SOURCE_ESTIMATED = "estimated"
LENS_SOURCE_LEGACY = "legacy"
#: ``CalibrationResult.lens_model_source`` value when no lens was applied.
LENS_SOURCE_NONE = "none"

_Row3 = Tuple[float, float, float]
ImageSize = Tuple[int, int]


@dataclass(frozen=True, kw_only=True)
class LensModel:
    """One camera's measured lens at one resolution."""

    camera_matrix: Tuple[_Row3, _Row3, _Row3]
    dist_coeffs: Tuple[float, ...]
    image_size: ImageSize
    """``(width, height)`` in pixels the model was solved at."""
    source: str = LENS_SOURCE_MEASURED
    board_spec: Optional[Dict[str, Any]] = None
    """Description of the calibration board used, if known."""
    views: Optional[int] = None
    """How many board views the solve used."""
    rms_px: Optional[float] = None
    residual_px: Optional[float] = None
    """Held-out (half-sample mapping) residual, pixels."""
    correction_px: Optional[float] = None
    """Size of the undistortion correction, pixels."""
    installed_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "camera_matrix": [list(row) for row in self.camera_matrix],
            "dist_coeffs": list(self.dist_coeffs),
            "image_size": list(self.image_size),
            "source": self.source,
            "board_spec": self.board_spec,
            "views": self.views,
            "rms_px": self.rms_px,
            "residual_px": self.residual_px,
            "correction_px": self.correction_px,
            "installed_at": self.installed_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LensModel":
        """Inverse of :meth:`to_dict`. Raises ``KeyError``/``ValueError``/
        ``TypeError`` on a malformed record."""
        m = data["camera_matrix"]
        if len(m) != 3 or any(len(row) != 3 for row in m):
            raise ValueError("camera_matrix must be 3x3")
        size = data["image_size"]
        if len(size) != 2:
            raise ValueError("image_size must be [width, height]")

        def _opt(key: str) -> Optional[float]:
            return float(data[key]) if data.get(key) is not None else None

        board_spec = data.get("board_spec")
        if board_spec is not None and not isinstance(board_spec, dict):
            raise TypeError("board_spec must be an object or null")
        return cls(
            camera_matrix=tuple(tuple(float(v) for v in row) for row in m),  # type: ignore[arg-type]
            dist_coeffs=tuple(float(c) for c in data["dist_coeffs"]),
            image_size=(int(size[0]), int(size[1])),
            source=str(data.get("source", LENS_SOURCE_MEASURED)),
            board_spec=board_spec,
            views=int(data["views"]) if data.get("views") is not None else None,
            rms_px=_opt("rms_px"),
            residual_px=_opt("residual_px"),
            correction_px=_opt("correction_px"),
            installed_at=float(data.get("installed_at", 0.0)),
        )


class LensStore:
    """``<state_dir>/cameras/<slug>/intrinsics.json`` persistence."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)

    def path(self, slug: str) -> Path:
        return self._state_dir / "cameras" / slug / INTRINSICS_FILENAME

    def load(self, slug: str) -> Optional[LensModel]:
        """The stored model regardless of resolution, or ``None`` when
        the file is missing or unreadable (corrupt -> warning)."""
        path = self.path(slug)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("cannot read lens model %s: %s; treating as absent", path, exc)
            return None
        try:
            return LensModel.from_dict(json.loads(text))
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("corrupt lens model %s: %s; treating as absent", path, exc)
            return None

    def get(
        self, slug: str, image_size: ImageSize
    ) -> Tuple[Optional[LensModel], Optional[str]]:
        """``(model, reason)``. *model* only when one is stored **and**
        its ``image_size`` equals *image_size*; otherwise ``None`` plus,
        for a size mismatch, a *reason* the caller can surface. No model
        stored at all is ``(None, None)`` -- nothing to explain."""
        model = self.load(slug)
        if model is None:
            return None, None
        want = (int(image_size[0]), int(image_size[1]))
        if tuple(model.image_size) != want:
            return None, (
                f"stored lens model for {slug!r} was measured at "
                f"{model.image_size[0]}x{model.image_size[1]} but frames are "
                f"{want[0]}x{want[1]}; not applying it"
            )
        return model, None

    def get_lens_model(self, slug: str, image_size: ImageSize) -> Optional[LensModel]:
        """The ``Calibrator.get_lens_model`` callable: :meth:`get` with
        the mismatch reason logged."""
        model, reason = self.get(slug, image_size)
        if reason:
            logger.warning(reason)
        return model

    def put(self, slug: str, model: LensModel) -> None:
        """Atomically write *model* (stamping ``installed_at`` if unset)."""
        if not model.installed_at:
            model = LensModel(**{**model.__dict__, "installed_at": time.time()})
        path = self.path(slug)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(f"cannot create lens directory {path.parent}: {exc}") from exc
        text = json.dumps(model.to_dict(), indent=2, sort_keys=True) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise OSError(f"cannot write lens file {path}: {exc}") from exc
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass
