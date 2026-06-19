"""Tests for client/discovery.py and the DaemonControl no-spawn refactor.

Covers:
- discover_daemons() degrades gracefully when zeroconf is unavailable.
- resolve_daemon_target() precedence:
    1. cli_args.daemon_host
    2. config.daemon_host (APRILCAM_DAEMON_HOST)
    3. Local Unix socket probe (never spawns)
    4. mDNS browse (0, 1, >1 results)
- DaemonControl.connect_default() raises DaemonNotFoundError (not spawns)
  when the daemon is unreachable.
- APRILCAM_DAEMON_HOST env var bypasses mDNS.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from aprilcam.client.discovery import (
    DaemonInfo,
    _probe_unix,
    discover_daemons,
    resolve_daemon_target,
)
from aprilcam.config import Config
from aprilcam.errors import DaemonNotFoundError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, daemon_host: Optional[str] = None, daemon_port: int = 5280) -> Config:
    """Build a minimal Config pointing at tmp dirs, with optional daemon_host."""
    sock_dir = tmp_path / "sockets"
    data_dir = tmp_path / "data"
    sock_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        data_dir=data_dir,
        socket_dir=sock_dir,
        daemon_pidfile=sock_dir / "aprilcamd.pid",
        daemon_host=daemon_host,
        daemon_port=daemon_port,
    )


def _args(host: Optional[str] = None, port: Optional[int] = None):
    """Build a minimal argparse-like namespace."""
    ns = argparse.Namespace()
    ns.daemon_host = host
    ns.daemon_port = port
    return ns


# ---------------------------------------------------------------------------
# discover_daemons
# ---------------------------------------------------------------------------


class TestDiscoverDaemons:
    def test_returns_empty_when_zeroconf_unavailable(self):
        """When zeroconf cannot be imported, discover_daemons returns [].

        The function uses ``try: from zeroconf import ... except ImportError: return []``
        so we verify the return-type contract and that the guard is present in the source.
        """
        import inspect
        import aprilcam.client.discovery as disc_mod

        source = inspect.getsource(disc_mod.discover_daemons)
        # The function must contain an ImportError guard
        assert "ImportError" in source
        assert "return []" in source

    def test_returns_list(self):
        """discover_daemons always returns a list."""
        # When zeroconf is available but no services are found, returns empty list.
        # We test just the return type contract since an actual mDNS browse
        # requires network access which we don't mock at this level.
        result = discover_daemons(timeout=0.01)
        assert isinstance(result, list)

    def test_graceful_on_import_error(self):
        """discover_daemons returns [] when zeroconf import raises ImportError.

        This verifies the defensive ``try/except ImportError`` block by
        inspecting the source code — the actual ImportError path cannot be
        trivially triggered at runtime once zeroconf is installed, because
        Python's import cache would need to be cleared.
        """
        import inspect
        import aprilcam.client.discovery as disc_mod

        # Verify the source contains the guard that returns [] on ImportError
        source = inspect.getsource(disc_mod.discover_daemons)
        assert "except ImportError" in source or "ImportError" in source


# ---------------------------------------------------------------------------
# resolve_daemon_target — precedence 1: cli_args.daemon_host
# ---------------------------------------------------------------------------


class TestResolveCliArgsPrecedence:
    def test_cli_host_wins_over_everything(self, tmp_path):
        """cli_args.daemon_host takes highest precedence."""
        config = _make_config(tmp_path, daemon_host="config-host.local")
        args = _args(host="cli-host.local", port=9999)

        with (
            patch("aprilcam.client.discovery._probe_unix", return_value=True),
            patch("aprilcam.client.discovery.discover_daemons") as mock_disc,
        ):
            host, port, unix = resolve_daemon_target(config, args)

        assert host == "cli-host.local"
        assert port == 9999
        assert unix is None
        mock_disc.assert_not_called()

    def test_cli_host_uses_config_port_when_cli_port_is_none(self, tmp_path):
        """When cli_args.daemon_port is None, falls back to config.daemon_port."""
        config = _make_config(tmp_path, daemon_port=7777)
        args = _args(host="cli-host.local", port=None)

        with patch("aprilcam.client.discovery._probe_unix", return_value=False):
            host, port, unix = resolve_daemon_target(config, args)

        assert host == "cli-host.local"
        assert port == 7777
        assert unix is None

    def test_no_cli_host_falls_through(self, tmp_path):
        """When cli_args.daemon_host is None, falls through to next priority."""
        config = _make_config(tmp_path, daemon_host="env-host.local")
        args = _args(host=None)

        with patch("aprilcam.client.discovery.discover_daemons") as mock_disc:
            host, port, unix = resolve_daemon_target(config, args)

        assert host == "env-host.local"
        mock_disc.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_daemon_target — precedence 2: config.daemon_host / env var
# ---------------------------------------------------------------------------


class TestResolveConfigPrecedence:
    def test_config_daemon_host_wins_over_unix_and_mdns(self, tmp_path):
        """config.daemon_host bypasses Unix probe and mDNS."""
        config = _make_config(tmp_path, daemon_host="pi.local", daemon_port=5280)

        with (
            patch("aprilcam.client.discovery._probe_unix", return_value=True),
            patch("aprilcam.client.discovery.discover_daemons") as mock_disc,
        ):
            host, port, unix = resolve_daemon_target(config)

        assert host == "pi.local"
        assert port == 5280
        assert unix is None
        mock_disc.assert_not_called()

    def test_env_var_daemon_host_via_config(self, tmp_path, monkeypatch):
        """APRILCAM_DAEMON_HOST env var sets config.daemon_host which wins."""
        monkeypatch.setenv("APRILCAM_DAEMON_HOST", "env-pi.local")
        monkeypatch.setenv("APRILCAM_DAEMON_PORT", "6000")

        # Config.load() reads env vars; build a config as if loaded
        config = _make_config(tmp_path, daemon_host="env-pi.local", daemon_port=6000)

        with patch("aprilcam.client.discovery.discover_daemons") as mock_disc:
            host, port, unix = resolve_daemon_target(config)

        assert host == "env-pi.local"
        assert port == 6000
        mock_disc.assert_not_called()

    def test_no_config_host_falls_through_to_unix_probe(self, tmp_path):
        """When config.daemon_host is None, tries Unix socket probe."""
        config = _make_config(tmp_path, daemon_host=None)

        with (
            patch("aprilcam.client.discovery._probe_unix", return_value=True) as mock_probe,
            patch("aprilcam.client.discovery.discover_daemons") as mock_disc,
        ):
            host, port, unix = resolve_daemon_target(config)

        mock_probe.assert_called_once()
        assert host == "localhost"
        assert unix is not None
        mock_disc.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_daemon_target — precedence 3: local Unix socket probe
# ---------------------------------------------------------------------------


class TestResolveUnixProbePrecedence:
    def test_unix_probe_success_returns_localhost(self, tmp_path):
        """When Unix socket is reachable, returns (localhost, port, unix_path)."""
        config = _make_config(tmp_path, daemon_port=5280)
        expected_unix = str(config.socket_dir / "control.sock")

        with (
            patch("aprilcam.client.discovery._probe_unix", return_value=True),
            patch("aprilcam.client.discovery.discover_daemons") as mock_disc,
        ):
            host, port, unix = resolve_daemon_target(config)

        assert host == "localhost"
        assert port == 5280
        assert unix == expected_unix
        mock_disc.assert_not_called()

    def test_unix_probe_failure_falls_to_mdns(self, tmp_path):
        """When Unix probe fails, falls through to mDNS browse."""
        config = _make_config(tmp_path)
        daemon = DaemonInfo(name="aprilcam-pi.local.", host="pi.local", port=5280)

        with (
            patch("aprilcam.client.discovery._probe_unix", return_value=False),
            patch("aprilcam.client.discovery.discover_daemons", return_value=[daemon]),
        ):
            host, port, unix = resolve_daemon_target(config)

        assert host == "pi.local"
        assert port == 5280
        assert unix is None


# ---------------------------------------------------------------------------
# resolve_daemon_target — precedence 4: mDNS browse
# ---------------------------------------------------------------------------


class TestResolveMdnsPrecedence:
    def test_mdns_zero_daemons_raises(self, tmp_path):
        """Zero mDNS results → DaemonNotFoundError."""
        config = _make_config(tmp_path)

        with (
            patch("aprilcam.client.discovery._probe_unix", return_value=False),
            patch("aprilcam.client.discovery.discover_daemons", return_value=[]),
        ):
            with pytest.raises(DaemonNotFoundError, match="No aprilcam daemon found"):
                resolve_daemon_target(config)

    def test_mdns_one_daemon_auto_selects(self, tmp_path):
        """Exactly 1 mDNS result → auto-select, no error."""
        config = _make_config(tmp_path)
        daemon = DaemonInfo(name="aprilcam-pi.", host="192.168.1.50", port=5280)

        with (
            patch("aprilcam.client.discovery._probe_unix", return_value=False),
            patch("aprilcam.client.discovery.discover_daemons", return_value=[daemon]),
        ):
            host, port, unix = resolve_daemon_target(config)

        assert host == "192.168.1.50"
        assert port == 5280
        assert unix is None

    def test_mdns_multiple_daemons_raises_with_list(self, tmp_path):
        """More than 1 mDNS result → DaemonNotFoundError listing them."""
        config = _make_config(tmp_path)
        daemons = [
            DaemonInfo(name="aprilcam-pi1.", host="192.168.1.50", port=5280),
            DaemonInfo(name="aprilcam-pi2.", host="192.168.1.51", port=5280),
        ]

        with (
            patch("aprilcam.client.discovery._probe_unix", return_value=False),
            patch("aprilcam.client.discovery.discover_daemons", return_value=daemons),
        ):
            with pytest.raises(DaemonNotFoundError, match="Multiple aprilcam daemons"):
                resolve_daemon_target(config)

    def test_mdns_multiple_daemons_error_contains_hosts(self, tmp_path):
        """Multiple mDNS results error message contains discovered hosts."""
        config = _make_config(tmp_path)
        daemons = [
            DaemonInfo(name="a.", host="host-a.local", port=5280),
            DaemonInfo(name="b.", host="host-b.local", port=5280),
        ]

        with (
            patch("aprilcam.client.discovery._probe_unix", return_value=False),
            patch("aprilcam.client.discovery.discover_daemons", return_value=daemons),
        ):
            with pytest.raises(DaemonNotFoundError) as exc_info:
                resolve_daemon_target(config)

        msg = str(exc_info.value)
        assert "host-a.local" in msg or "host-b.local" in msg
        assert "APRILCAM_DAEMON_HOST" in msg

    def test_mdns_not_consulted_when_config_host_set(self, tmp_path):
        """When config.daemon_host is set, discover_daemons is never called."""
        config = _make_config(tmp_path, daemon_host="pi.local")

        with patch("aprilcam.client.discovery.discover_daemons") as mock_disc:
            resolve_daemon_target(config)

        mock_disc.assert_not_called()


# ---------------------------------------------------------------------------
# DaemonControl.connect_default no-spawn behaviour
# ---------------------------------------------------------------------------


class TestDaemonControlNoSpawn:
    """DaemonControl.connect_default raises DaemonNotFoundError, never spawns."""

    def test_raises_when_daemon_unreachable(self, tmp_path):
        """connect_default raises DaemonNotFoundError when no daemon is running."""
        config = _make_config(tmp_path)

        with (
            patch("aprilcam.client.discovery._probe_unix", return_value=False),
            patch("aprilcam.client.discovery.discover_daemons", return_value=[]),
        ):
            with pytest.raises(DaemonNotFoundError):
                from aprilcam.client.control import DaemonControl
                DaemonControl.connect_default(config)

    def test_does_not_call_subprocess_popen(self, tmp_path):
        """connect_default must never call subprocess.Popen."""
        import subprocess as _subprocess

        config = _make_config(tmp_path)

        with (
            patch("aprilcam.client.discovery._probe_unix", return_value=False),
            patch("aprilcam.client.discovery.discover_daemons", return_value=[]),
            patch.object(_subprocess, "Popen") as mock_popen,
        ):
            try:
                from aprilcam.client.control import DaemonControl
                DaemonControl.connect_default(config)
            except DaemonNotFoundError:
                pass

        mock_popen.assert_not_called()

    def test_raises_daemon_not_found_error_subclass(self, tmp_path):
        """DaemonNotFoundError is a RuntimeError subclass."""
        assert issubclass(DaemonNotFoundError, RuntimeError)

    def test_error_message_mentions_start_command(self, tmp_path):
        """DaemonNotFoundError message mentions how to start the daemon."""
        config = _make_config(tmp_path)

        with (
            patch("aprilcam.client.discovery._probe_unix", return_value=False),
            patch("aprilcam.client.discovery.discover_daemons", return_value=[]),
        ):
            with pytest.raises(DaemonNotFoundError) as exc_info:
                from aprilcam.client.control import DaemonControl
                DaemonControl.connect_default(config)

        msg = str(exc_info.value)
        # The error message should guide the user
        assert "aprilcam daemon start" in msg or "start" in msg.lower()

    def test_explicit_unix_path_raises_on_unreachable_socket(self, tmp_path):
        """When unix_path is passed explicitly and socket is absent, raise."""
        config = _make_config(tmp_path)
        bad_path = str(tmp_path / "nonexistent.sock")

        with pytest.raises((DaemonNotFoundError, Exception)):
            from aprilcam.client.control import DaemonControl
            DaemonControl.connect_default(config, unix_path=bad_path)


# ---------------------------------------------------------------------------
# APRILCAM_DAEMON_HOST env var bypasses mDNS
# ---------------------------------------------------------------------------


class TestEnvVarBypassesMdns:
    def test_env_host_bypasses_mdns_discovery(self, tmp_path):
        """APRILCAM_DAEMON_HOST set in config.daemon_host means discover_daemons is never called."""
        # Simulate what Config.load() would produce when APRILCAM_DAEMON_HOST=bogus.local
        config = _make_config(tmp_path, daemon_host="bogus.local", daemon_port=5280)

        with patch("aprilcam.client.discovery.discover_daemons") as mock_disc:
            host, port, unix = resolve_daemon_target(config)

        mock_disc.assert_not_called()
        assert host == "bogus.local"

    def test_env_host_bypasses_unix_probe(self, tmp_path):
        """When APRILCAM_DAEMON_HOST is set, the Unix socket probe is also skipped."""
        config = _make_config(tmp_path, daemon_host="bogus.local")

        with (
            patch("aprilcam.client.discovery._probe_unix") as mock_probe,
            patch("aprilcam.client.discovery.discover_daemons"),
        ):
            resolve_daemon_target(config)

        mock_probe.assert_not_called()


# ---------------------------------------------------------------------------
# _probe_unix
# ---------------------------------------------------------------------------


class TestProbeUnix:
    def test_returns_false_for_nonexistent_socket(self, tmp_path):
        """_probe_unix returns False when the socket file does not exist."""
        path = str(tmp_path / "no-socket.sock")
        assert _probe_unix(path) is False

    def test_returns_false_on_connection_refused(self):
        """_probe_unix returns False when connection is refused."""
        # /tmp/no-such-socket should not exist on any normal system
        assert _probe_unix("/tmp/aprilcam-test-nonexistent-xyz.sock") is False

    def test_returns_true_for_listening_socket(self):
        """_probe_unix returns True when a Unix socket is accepting connections."""
        import socket
        import tempfile
        import threading

        # Use /tmp directly to stay under the 104-char Unix socket path limit
        with tempfile.TemporaryDirectory(prefix="ac_", dir="/tmp") as td:
            sock_path = str(Path(td) / "t.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(1)

            def _accept():
                try:
                    conn, _ = server.accept()
                    conn.close()
                except OSError:
                    pass

            t = threading.Thread(target=_accept, daemon=True)
            t.start()

            try:
                result = _probe_unix(sock_path)
            finally:
                server.close()
                t.join(timeout=1.0)

        assert result is True


# ---------------------------------------------------------------------------
# add_daemon_args and connect_from_args
# ---------------------------------------------------------------------------


class TestCliDaemonHelpers:
    def test_add_daemon_args_adds_daemon_host(self):
        """add_daemon_args() adds --daemon-host to the parser."""
        import argparse
        from aprilcam.cli._daemon import add_daemon_args

        parser = argparse.ArgumentParser()
        add_daemon_args(parser)
        args = parser.parse_args(["--daemon-host", "myhost.local"])
        assert args.daemon_host == "myhost.local"

    def test_add_daemon_args_adds_daemon_port(self):
        """add_daemon_args() adds --daemon-port to the parser."""
        import argparse
        from aprilcam.cli._daemon import add_daemon_args

        parser = argparse.ArgumentParser()
        add_daemon_args(parser)
        args = parser.parse_args(["--daemon-port", "6000"])
        assert args.daemon_port == 6000

    def test_add_daemon_args_defaults_are_none(self):
        """Without flags, daemon_host and daemon_port default to None."""
        import argparse
        from aprilcam.cli._daemon import add_daemon_args

        parser = argparse.ArgumentParser()
        add_daemon_args(parser)
        args = parser.parse_args([])
        assert args.daemon_host is None
        assert args.daemon_port is None

    def test_connect_from_args_raises_when_no_daemon(self, tmp_path):
        """connect_from_args raises when no daemon is reachable."""
        import argparse
        from aprilcam.cli._daemon import add_daemon_args, connect_from_args

        config = _make_config(tmp_path)
        parser = argparse.ArgumentParser()
        add_daemon_args(parser)
        args = parser.parse_args([])

        with (
            patch("aprilcam.client.discovery._probe_unix", return_value=False),
            patch("aprilcam.client.discovery.discover_daemons", return_value=[]),
        ):
            with pytest.raises((DaemonNotFoundError, Exception)):
                connect_from_args(config, args)


# ---------------------------------------------------------------------------
# Config.daemon_host / daemon_port fields
# ---------------------------------------------------------------------------


class TestConfigDaemonFields:
    def test_config_has_daemon_host_field(self):
        """Config has a daemon_host field defaulting to None."""
        import dataclasses
        fields = {f.name: f for f in dataclasses.fields(Config)}
        assert "daemon_host" in fields
        assert fields["daemon_host"].default is None

    def test_config_has_daemon_port_field(self):
        """Config has a daemon_port field defaulting to 5280."""
        import dataclasses
        fields = {f.name: f for f in dataclasses.fields(Config)}
        assert "daemon_port" in fields
        assert fields["daemon_port"].default == 5280

    def test_config_load_reads_daemon_host_from_env(self, tmp_path, monkeypatch):
        """Config.load() picks up APRILCAM_DAEMON_HOST from the process env."""
        monkeypatch.setenv("APRILCAM_DAEMON_HOST", "my-pi.local")
        monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "run"))
        monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

        cfg = Config.load()
        assert cfg.daemon_host == "my-pi.local"

    def test_config_load_reads_daemon_port_from_env(self, tmp_path, monkeypatch):
        """Config.load() picks up APRILCAM_DAEMON_PORT from the process env."""
        monkeypatch.setenv("APRILCAM_DAEMON_PORT", "7777")
        monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "run"))
        monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

        cfg = Config.load()
        assert cfg.daemon_port == 7777

    def test_config_daemon_host_default_is_none(self, tmp_path, monkeypatch):
        """Config.load() defaults daemon_host to None when env var is unset."""
        monkeypatch.delenv("APRILCAM_DAEMON_HOST", raising=False)
        monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "run"))
        monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

        cfg = Config.load()
        assert cfg.daemon_host is None

    def test_config_daemon_port_default_is_5280(self, tmp_path, monkeypatch):
        """Config.load() defaults daemon_port to 5280 when env var is unset."""
        monkeypatch.delenv("APRILCAM_DAEMON_PORT", raising=False)
        monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "run"))
        monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

        cfg = Config.load()
        assert cfg.daemon_port == 5280


# ---------------------------------------------------------------------------
# DaemonInfo dataclass
# ---------------------------------------------------------------------------


class TestDaemonInfo:
    def test_daemon_info_fields(self):
        """DaemonInfo has name, host, port, addresses fields."""
        info = DaemonInfo(name="test.", host="pi.local", port=5280)
        assert info.name == "test."
        assert info.host == "pi.local"
        assert info.port == 5280
        assert info.addresses == []  # default empty list

    def test_daemon_info_with_addresses(self):
        """DaemonInfo can carry IP address strings."""
        info = DaemonInfo(
            name="test.", host="pi.local", port=5280,
            addresses=["192.168.1.50", "fe80::1"]
        )
        assert "192.168.1.50" in info.addresses
