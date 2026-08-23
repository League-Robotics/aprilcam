from __future__ import annotations

import math
from typing import Optional, Tuple


class VelocityEstimator:
    """Per-tag EMA-smoothed velocity estimation with deadband suppression.

    One instance per tracked tag. Call update() with each new position observation.
    """

    def __init__(self, *, ema_alpha: float = 0.3, deadband: float = 50.0):
        self._ema_alpha = ema_alpha
        self._deadband = deadband
        self._prev_pos: Optional[Tuple[float, float]] = None
        self._prev_ts: Optional[float] = None
        self._ema_speed: Optional[float] = None
        self._vel: Tuple[float, float] = (0.0, 0.0)
        self._speed: float = 0.0

    def update(
        self, position: Tuple[float, float], timestamp: float
    ) -> Tuple[Tuple[float, float], float]:
        """Update with a new position observation.

        Returns (velocity_vector, speed) after EMA smoothing and deadband.
        velocity_vector is (vx, vy) in pixels/sec.
        speed is the EMA-smoothed magnitude in pixels/sec.
        """
        if self._prev_pos is not None and self._prev_ts is not None:
            dt = max(1e-3, timestamp - self._prev_ts)
            dx = (position[0] - self._prev_pos[0]) / dt
            dy = (position[1] - self._prev_pos[1]) / dt
            inst_speed = math.hypot(dx, dy)

            # EMA smoothing
            if self._ema_speed is not None:
                smoothed = (
                    self._ema_alpha * inst_speed
                    + (1 - self._ema_alpha) * self._ema_speed
                )
            else:
                smoothed = inst_speed
            self._ema_speed = smoothed

            # Deadband suppression
            if smoothed < self._deadband:
                self._vel = (0.0, 0.0)
                self._speed = 0.0
            else:
                self._vel = (dx, dy)
                self._speed = smoothed

        self._prev_pos = position
        self._prev_ts = timestamp
        return self._vel, self._speed

    def predict_position(self, t: float) -> Tuple[float, float]:
        """Linearly extrapolate position to time t."""
        if self._prev_pos is None or self._prev_ts is None:
            return (0.0, 0.0)
        dt = t - self._prev_ts
        return (
            self._prev_pos[0] + self._vel[0] * dt,
            self._prev_pos[1] + self._vel[1] * dt,
        )

    def reset(self) -> None:
        """Clear all state."""
        self._prev_pos = None
        self._prev_ts = None
        self._ema_speed = None
        self._vel = (0.0, 0.0)
        self._speed = 0.0

    @property
    def velocity(self) -> Tuple[float, float]:
        return self._vel

    @property
    def speed(self) -> float:
        return self._speed
