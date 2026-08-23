"""Tests for ``aprilcam.config.directories`` (headless, no daemon/hardware).

Covers ticket 020-001's acceptance criteria: the flag > env > default >
unprivileged-fallback cascade for ``config_dir``/``state_dir``, the
separate (non-configurable) resolution of ``socket_dir``, and that the
resolution is always reported (logged and returned) per UC-01's "never
a mystery" requirement.

System roots (``/etc/aprilcam``, ``/var/lib/aprilcam``,
``/run/aprilcam``) are never touched directly — every test monkeypatches
the module's ``SYSTEM_*`` globals to point at ``tmp_path`` instead, per
the ticket's testing note ("tmp_path standing in for system roots").
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from aprilcam.config import directories
from aprilcam.config.directories import Directories
from aprilcam.config.interfaces import Directories as DirectoriesProtocol


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No stray env vars from the host leak into any test."""
    for key in (
        directories.APRILCAM_CONFIG_DIR_ENV,
        directories.APRILCAM_STATE_DIR_ENV,
        directories.XDG_RUNTIME_DIR_ENV,
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _writable_system_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """Point SYSTEM_CONFIG_DIR/STATE_DIR/SOCKET_DIR at real, writable
    tmp_path subdirectories — standing in for an available system
    install (scenario (a))."""
    cfg = tmp_path / "etc" / "aprilcam"
    state = tmp_path / "var-lib" / "aprilcam"
    sock = tmp_path / "run" / "aprilcam"
    for d in (cfg, state, sock):
        d.mkdir(parents=True)
    monkeypatch.setattr(directories, "SYSTEM_CONFIG_DIR", cfg)
    monkeypatch.setattr(directories, "SYSTEM_STATE_DIR", state)
    monkeypatch.setattr(directories, "SYSTEM_SOCKET_DIR", sock)
    return cfg, state, sock


def _unavailable_system_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point SYSTEM_CONFIG_DIR/STATE_DIR/SOCKET_DIR at tmp_path
    subdirectories that never exist, and force every access check to
    report "unavailable" regardless of the real (test-runner-owned,
    therefore actually writable) permissions on tmp_path — standing in
    for a system install with no access (scenario (b)).

    Forcing ``_can_access`` rather than relying on real ``chmod``
    permission bits keeps this deterministic in a sandbox that may run
    as root, where ``os.access`` does not behave like an unprivileged
    process.
    """
    monkeypatch.setattr(directories, "SYSTEM_CONFIG_DIR", tmp_path / "etc" / "aprilcam")
    monkeypatch.setattr(directories, "SYSTEM_STATE_DIR", tmp_path / "var-lib" / "aprilcam")
    monkeypatch.setattr(directories, "SYSTEM_SOCKET_DIR", tmp_path / "run" / "aprilcam")
    monkeypatch.setattr(directories, "_can_access", lambda path, mode: False)


# ---------------------------------------------------------------------------
# (a) No env vars, no flags, system paths writable -> system defaults
# ---------------------------------------------------------------------------


def test_defaults_when_system_paths_writable(tmp_path, monkeypatch, fake_home):
    cfg, state, sock = _writable_system_roots(tmp_path, monkeypatch)

    dirs = Directories()

    assert dirs.config_dir == cfg
    assert dirs.state_dir == state
    assert dirs.socket_dir == sock
    assert dirs.config_dir_source == "default"
    assert dirs.state_dir_source == "default"
    assert dirs.socket_dir_source == "default"


# ---------------------------------------------------------------------------
# (b) System paths unavailable -> fall back to the user's home
# ---------------------------------------------------------------------------


def test_fallback_when_system_paths_unavailable(tmp_path, monkeypatch, fake_home):
    _unavailable_system_roots(tmp_path, monkeypatch)

    dirs = Directories()

    assert dirs.config_dir == fake_home / ".config" / "aprilcam"
    assert dirs.state_dir == fake_home / ".local" / "share" / "aprilcam"
    assert dirs.config_dir_source == "fallback"
    assert dirs.state_dir_source == "fallback"


def test_config_dir_falls_back_when_system_dir_simply_does_not_exist(
    tmp_path, monkeypatch, fake_home
):
    """The common real-world case (configuration.md §2): /etc/aprilcam
    just isn't there. No permission trickery needed — non-existence
    alone is enough for config_dir, since nothing ever creates it."""
    monkeypatch.setattr(directories, "SYSTEM_CONFIG_DIR", tmp_path / "etc" / "aprilcam")

    dirs = Directories()

    assert dirs.config_dir == fake_home / ".config" / "aprilcam"
    assert dirs.config_dir_source == "fallback"


# ---------------------------------------------------------------------------
# (c) APRILCAM_CONFIG_DIR / APRILCAM_STATE_DIR override the default
# ---------------------------------------------------------------------------


def test_env_vars_override_default(tmp_path, monkeypatch, fake_home):
    _writable_system_roots(tmp_path, monkeypatch)
    env_config = tmp_path / "explicit-config"
    env_state = tmp_path / "explicit-state"
    monkeypatch.setenv(directories.APRILCAM_CONFIG_DIR_ENV, str(env_config))
    monkeypatch.setenv(directories.APRILCAM_STATE_DIR_ENV, str(env_state))

    dirs = Directories()

    assert dirs.config_dir == env_config
    assert dirs.state_dir == env_state
    assert dirs.config_dir_source == "env"
    assert dirs.state_dir_source == "env"


def test_empty_env_var_is_treated_as_unset(tmp_path, monkeypatch, fake_home):
    cfg, state, _sock = _writable_system_roots(tmp_path, monkeypatch)
    monkeypatch.setenv(directories.APRILCAM_CONFIG_DIR_ENV, "")

    dirs = Directories()

    assert dirs.config_dir == cfg
    assert dirs.config_dir_source == "default"


# ---------------------------------------------------------------------------
# Cascade precedence: flag > env > default
# ---------------------------------------------------------------------------


def test_flag_beats_env_and_default(tmp_path, monkeypatch, fake_home):
    _writable_system_roots(tmp_path, monkeypatch)
    monkeypatch.setenv(directories.APRILCAM_CONFIG_DIR_ENV, str(tmp_path / "from-env"))
    flag_config = tmp_path / "from-flag"

    dirs = Directories(config_dir=flag_config)

    assert dirs.config_dir == flag_config
    assert dirs.config_dir_source == "flag"


def test_flag_accepts_str_path(tmp_path, monkeypatch, fake_home):
    _writable_system_roots(tmp_path, monkeypatch)
    flag_state = tmp_path / "from-flag-str"

    dirs = Directories(state_dir=str(flag_state))

    assert dirs.state_dir == flag_state
    assert dirs.state_dir_source == "flag"


# ---------------------------------------------------------------------------
# (d) Resolution is reported: logged, and returned
# ---------------------------------------------------------------------------


def test_resolution_is_logged(tmp_path, monkeypatch, fake_home, caplog):
    _writable_system_roots(tmp_path, monkeypatch)

    with caplog.at_level(logging.INFO, logger="aprilcam.config.directories"):
        Directories()

    messages = "\n".join(record.message for record in caplog.records)
    assert "config_dir" in messages
    assert "state_dir" in messages
    assert "socket_dir" in messages
    assert "source=default" in messages


def test_resolution_sources_are_returned(tmp_path, monkeypatch, fake_home):
    _unavailable_system_roots(tmp_path, monkeypatch)

    dirs = Directories()

    assert dirs.config_dir_source == "fallback"
    assert dirs.state_dir_source == "fallback"
    assert isinstance(dirs.socket_dir_source, str)


def test_describe_names_every_root_and_source(tmp_path, monkeypatch, fake_home):
    cfg, state, sock = _writable_system_roots(tmp_path, monkeypatch)

    description = Directories().describe()

    assert str(cfg) in description
    assert str(state) in description
    assert str(sock) in description
    assert "default" in description


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_directories_protocol(tmp_path, monkeypatch, fake_home):
    _writable_system_roots(tmp_path, monkeypatch)

    dirs = Directories()

    assert isinstance(dirs, DirectoriesProtocol)


# ---------------------------------------------------------------------------
# socket_dir: /run/aprilcam -> $XDG_RUNTIME_DIR/aprilcam -> temp fallback
# ---------------------------------------------------------------------------


def test_socket_dir_default_when_system_socket_dir_writable(tmp_path, monkeypatch, fake_home):
    _, _, sock = _writable_system_roots(tmp_path, monkeypatch)

    dirs = Directories()

    assert dirs.socket_dir == sock
    assert dirs.socket_dir_source == "default"


def test_socket_dir_falls_back_to_xdg_runtime_dir(tmp_path, monkeypatch, fake_home):
    monkeypatch.setattr(directories, "SYSTEM_SOCKET_DIR", tmp_path / "run" / "aprilcam")
    monkeypatch.setattr(directories, "_can_access", lambda path, mode: False)
    xdg_runtime = tmp_path / "xdg-runtime"
    monkeypatch.setenv(directories.XDG_RUNTIME_DIR_ENV, str(xdg_runtime))

    dirs = Directories()

    assert dirs.socket_dir == xdg_runtime / "aprilcam"
    assert dirs.socket_dir_source == "xdg-runtime"


def test_socket_dir_falls_back_to_tempdir_when_nothing_else_available(
    tmp_path, monkeypatch, fake_home
):
    """The macOS / headless-Linux case this sprint's bring-up actually
    runs under: no /run/aprilcam, no XDG_RUNTIME_DIR."""
    monkeypatch.setattr(directories, "SYSTEM_SOCKET_DIR", tmp_path / "run" / "aprilcam")
    monkeypatch.setattr(directories, "_can_access", lambda path, mode: False)
    monkeypatch.delenv(directories.XDG_RUNTIME_DIR_ENV, raising=False)

    dirs = Directories()

    assert dirs.socket_dir_source == "fallback"
    assert not str(dirs.socket_dir).startswith("/run")
    assert "aprilcam-" in dirs.socket_dir.name


# ---------------------------------------------------------------------------
# Availability helpers, tested directly against real permissions
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits are not meaningful for root or on non-POSIX systems",
)
def test_config_dir_available_false_when_unreadable(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o000)
    try:
        assert directories._config_dir_available(blocked) is False
    finally:
        blocked.chmod(0o755)


def test_config_dir_available_false_when_missing(tmp_path):
    assert directories._config_dir_available(tmp_path / "does-not-exist") is False


def test_config_dir_available_true_for_readable_dir(tmp_path):
    assert directories._config_dir_available(tmp_path) is True


def test_writable_or_creatable_true_when_parent_writable(tmp_path):
    target = tmp_path / "state" / "aprilcam"
    assert directories._writable_or_creatable(target) is True


@pytest.mark.skipif(
    os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits are not meaningful for root or on non-POSIX systems",
)
def test_writable_or_creatable_false_when_parent_unwritable(tmp_path):
    parent = tmp_path / "readonly-parent"
    parent.mkdir()
    parent.chmod(0o555)
    try:
        assert directories._writable_or_creatable(parent / "aprilcam") is False
    finally:
        parent.chmod(0o755)
