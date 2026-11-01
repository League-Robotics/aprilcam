"""Pytest wrapper for scripts/hotplug_acceptance.py (sprint 047-009).

Skipped unless ``APRILCAM_HOTPLUG_ACCEPTANCE=1``, so the default suite
never unbinds a real USB device. Configure with
``APRILCAM_HOTPLUG_ARGS``, e.g. ``--camera 2 --daemon buzzkill.home --ssh buzzkill``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.hardware

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hotplug_acceptance.py"


@pytest.mark.skipif(
    os.environ.get("APRILCAM_HOTPLUG_ACCEPTANCE") != "1",
    reason="set APRILCAM_HOTPLUG_ACCEPTANCE=1 (and APRILCAM_HOTPLUG_ARGS) to run on real hardware",
)
def test_hotplug_acceptance_script() -> None:
    args = shlex.split(os.environ.get("APRILCAM_HOTPLUG_ARGS", ""))
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1000:]
