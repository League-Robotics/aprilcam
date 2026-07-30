"""Unit tests for VelocityEstimator."""

import math
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.core import VelocityEstimator


class TestVelocityEstimator:

    def test_first_update_no_velocity(self):
        ve = VelocityEstimator()
        vel, speed = ve.update((100.0, 200.0), 1000.0)
        assert vel == (0.0, 0.0)
        assert speed == 0.0

    def test_second_update_computes_velocity(self):
        ve = VelocityEstimator(deadband=0.0)
        ve.update((100.0, 200.0), 1000.0)
        vel, speed = ve.update((200.0, 200.0), 1001.0)
        assert vel[0] == pytest.approx(100.0, abs=1.0)
        assert vel[1] == pytest.approx(0.0, abs=1.0)
        assert speed > 0

    def test_deadband_suppression(self):
        ve = VelocityEstimator(deadband=100.0)
        ve.update((100.0, 200.0), 1000.0)
        vel, speed = ve.update((101.0, 200.0), 1001.0)  # 1 px/s < deadband
        assert vel == (0.0, 0.0)
        assert speed == 0.0

    def test_ema_smoothing(self):
        ve = VelocityEstimator(ema_alpha=0.5, deadband=0.0)
        ve.update((0.0, 0.0), 0.0)
        ve.update((100.0, 0.0), 1.0)  # inst_speed = 100
        _, speed1 = ve.update((200.0, 0.0), 2.0)  # inst_speed = 100, ema = 0.5*100 + 0.5*100 = 100
        assert speed1 > 0

    def test_reset_clears_state(self):
        ve = VelocityEstimator(deadband=0.0)
        ve.update((0.0, 0.0), 0.0)
        ve.update((100.0, 0.0), 1.0)
        assert ve.speed > 0
        ve.reset()
        assert ve.speed == 0.0
        assert ve.velocity == (0.0, 0.0)

    def test_predict_position(self):
        ve = VelocityEstimator(deadband=0.0)
        ve.update((100.0, 200.0), 1000.0)
        ve.update((110.0, 200.0), 1001.0)  # 10 px/s in x
        predicted = ve.predict_position(1002.0)
        assert predicted[0] == pytest.approx(120.0, abs=1.0)
        assert predicted[1] == pytest.approx(200.0, abs=1.0)

    def test_predict_with_no_data(self):
        ve = VelocityEstimator()
        pos = ve.predict_position(1000.0)
        assert pos == (0.0, 0.0)
