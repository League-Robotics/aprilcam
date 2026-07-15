"""Smoke tests for the sprint 018 daemon singleton-guard error types (018-001).

These exception classes are not yet raised anywhere in this ticket — that
wiring lands in tickets 002-004. This file only confirms the classes exist,
construct correctly, and carry the attributes/docstrings the later tickets
depend on.
"""
from __future__ import annotations

from aprilcam.errors import (
    DaemonAlreadyRunningError,
    DaemonBindError,
    DaemonPortInUseError,
)


# ---------------------------------------------------------------------------
# DaemonAlreadyRunningError
# ---------------------------------------------------------------------------


def test_daemon_already_running_error_is_runtime_error():
    assert issubclass(DaemonAlreadyRunningError, RuntimeError)


def test_daemon_already_running_error_stores_existing_pid():
    err = DaemonAlreadyRunningError(existing_pid=1234)
    assert err.existing_pid == 1234
    assert "1234" in str(err)


def test_daemon_already_running_error_defaults_existing_pid_to_none():
    err = DaemonAlreadyRunningError()
    assert err.existing_pid is None


# ---------------------------------------------------------------------------
# DaemonPortInUseError / DaemonBindError
# ---------------------------------------------------------------------------


def test_daemon_port_in_use_error_is_runtime_error():
    assert issubclass(DaemonPortInUseError, RuntimeError)
    err = DaemonPortInUseError("port 5280 already bound")
    assert "5280" in str(err)


def test_daemon_bind_error_is_runtime_error():
    assert issubclass(DaemonBindError, RuntimeError)
    err = DaemonBindError("unix socket bind failed")
    assert "unix socket" in str(err)


def test_daemon_port_in_use_and_bind_errors_are_distinct_classes():
    """The two transport-specific errors must not be aliases of each other."""
    assert DaemonPortInUseError is not DaemonBindError
    assert not issubclass(DaemonPortInUseError, DaemonBindError)
    assert not issubclass(DaemonBindError, DaemonPortInUseError)


def test_daemon_port_in_use_error_docstring_mentions_tcp():
    doc = DaemonPortInUseError.__doc__ or ""
    assert "TCP" in doc


def test_daemon_bind_error_docstring_mentions_unix_socket():
    doc = DaemonBindError.__doc__ or ""
    assert "Unix" in doc
