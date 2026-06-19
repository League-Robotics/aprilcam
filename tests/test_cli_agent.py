"""Subprocess smoke tests for the aprilcam --agent CLI flag (Sprint 013, ticket 007)."""
import subprocess
import sys


def test_agent_flag_prints_content():
    result = subprocess.run(
        [sys.executable, "-m", "aprilcam", "--agent"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert len(result.stdout) > 100  # non-trivial content


def test_agent_robot_flag():
    result = subprocess.run(
        [sys.executable, "-m", "aprilcam", "--agent", "robot"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert len(result.stdout) > 100


def test_agent_unknown_exits_nonzero():
    result = subprocess.run(
        [sys.executable, "-m", "aprilcam", "--agent", "nosuchguide"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
