"""Unit tests for aprilcam.daemon.mdns.MDNSAdvertiser."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Tests: _primary_routable_ipv4()
# Regression for Defect C (014-010): Ubuntu /etc/hosts maps hostname to
# 127.0.1.1; the daemon must advertise the routable interface IP instead.
# ---------------------------------------------------------------------------


def test_primary_routable_ipv4_returns_routable_address(monkeypatch):
    """_primary_routable_ipv4() returns the address from getsockname() when routable."""
    from aprilcam.daemon.mdns import _primary_routable_ipv4

    mock_sock = MagicMock()
    mock_sock.getsockname.return_value = ("192.168.1.42", 53)
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: mock_sock)

    addr = _primary_routable_ipv4()
    assert addr == "192.168.1.42"
    mock_sock.connect.assert_called_once_with(("192.0.2.1", 53))


def test_primary_routable_ipv4_skips_loopback(monkeypatch):
    """_primary_routable_ipv4() falls back to 127.0.0.1 when getsockname() returns 127.*."""
    from aprilcam.daemon.mdns import _primary_routable_ipv4

    mock_sock = MagicMock()
    mock_sock.getsockname.return_value = ("127.0.1.1", 53)
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: mock_sock)

    addr = _primary_routable_ipv4()
    # 127.0.1.1 is a loopback alias — must NOT be returned as the routable IP.
    assert addr == "127.0.0.1", f"Expected loopback fallback, got: {addr}"


def test_primary_routable_ipv4_falls_back_on_os_error(monkeypatch):
    """_primary_routable_ipv4() returns 127.0.0.1 when the UDP trick fails."""
    from aprilcam.daemon.mdns import _primary_routable_ipv4

    def _fail(*a, **kw):
        raise OSError("Network unreachable")

    monkeypatch.setattr(socket, "socket", _fail)

    addr = _primary_routable_ipv4()
    assert addr == "127.0.0.1"


def test_start_uses_routable_ip_not_hostname_resolution(monkeypatch):
    """MDNSAdvertiser.start() uses _primary_routable_ipv4(), not gethostbyname().

    On Ubuntu, gethostbyname(hostname) often returns 127.0.1.1 because the
    hostname is mapped to a loopback alias in /etc/hosts.  The advertised
    address must be the routable interface IP instead.
    """
    from aprilcam.daemon.mdns import MDNSAdvertiser

    # Patch _primary_routable_ipv4 to return a known routable IP.
    with patch("aprilcam.daemon.mdns._primary_routable_ipv4", return_value="10.0.0.99"):
        mock_zc, mock_zc_class, mock_si_class = _make_mock_zeroconf()

        with patch.dict(
            "sys.modules",
            {"zeroconf": MagicMock(Zeroconf=mock_zc_class, ServiceInfo=mock_si_class)},
        ):
            advertiser = MDNSAdvertiser()
            advertiser.start(tcp_port=5280)

    _, kwargs = mock_si_class.call_args
    addresses = kwargs.get("addresses", [])
    assert addresses, "addresses must not be empty"
    # 10.0.0.99 → b'\n\x00\x00c'
    assert addresses[0] == socket.inet_aton("10.0.0.99"), (
        f"Expected routable IP bytes, got: {addresses[0]!r}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_zeroconf():
    """Return a mock Zeroconf instance and a mock ServiceInfo class."""
    mock_zc = MagicMock(name="Zeroconf_instance")
    mock_zc_class = MagicMock(name="Zeroconf_class", return_value=mock_zc)
    mock_si_class = MagicMock(name="ServiceInfo_class")
    return mock_zc, mock_zc_class, mock_si_class


# ---------------------------------------------------------------------------
# Tests: start()
# ---------------------------------------------------------------------------


def test_start_calls_register_service():
    """start() creates a ServiceInfo and registers it with Zeroconf."""
    from aprilcam.daemon.mdns import MDNSAdvertiser, _SERVICE_TYPE

    mock_zc, mock_zc_class, mock_si_class = _make_mock_zeroconf()
    mock_info = MagicMock(name="ServiceInfo_instance")
    mock_si_class.return_value = mock_info

    with patch.dict(
        "sys.modules",
        {"zeroconf": MagicMock(Zeroconf=mock_zc_class, ServiceInfo=mock_si_class)},
    ):
        advertiser = MDNSAdvertiser()
        advertiser.start(tcp_port=5280)

    # ServiceInfo must be called with correct type_ and port
    args, kwargs = mock_si_class.call_args
    assert kwargs.get("type_") == _SERVICE_TYPE or (args and args[0] == _SERVICE_TYPE)
    assert kwargs.get("port") == 5280

    # register_service must be called exactly once with the info object
    mock_zc.register_service.assert_called_once_with(mock_info)


def test_start_service_name_contains_hostname():
    """Service name includes the local hostname."""
    from aprilcam.daemon.mdns import MDNSAdvertiser

    mock_zc, mock_zc_class, mock_si_class = _make_mock_zeroconf()

    with patch.dict(
        "sys.modules",
        {"zeroconf": MagicMock(Zeroconf=mock_zc_class, ServiceInfo=mock_si_class)},
    ):
        advertiser = MDNSAdvertiser()
        advertiser.start(tcp_port=5280)

    _, kwargs = mock_si_class.call_args
    hostname = socket.gethostname()
    assert hostname in kwargs.get("name", "")


def test_start_does_not_raise_on_zeroconf_error(caplog):
    """If Zeroconf() raises, start() logs a warning and does not propagate."""
    import logging

    from aprilcam.daemon.mdns import MDNSAdvertiser

    mock_zc_class = MagicMock(side_effect=RuntimeError("network unavailable"))
    mock_si_class = MagicMock()

    with patch.dict(
        "sys.modules",
        {"zeroconf": MagicMock(Zeroconf=mock_zc_class, ServiceInfo=mock_si_class)},
    ):
        advertiser = MDNSAdvertiser()
        with caplog.at_level(logging.WARNING, logger="aprilcam.daemon.mdns"):
            advertiser.start(tcp_port=5280)  # must not raise

    assert any("registration failed" in r.message for r in caplog.records)
    # Internal state must be cleared so stop() is a no-op
    assert advertiser._zeroconf is None


def test_start_does_not_raise_on_register_service_error(caplog):
    """If register_service() raises, start() logs a warning and does not propagate."""
    import logging

    from aprilcam.daemon.mdns import MDNSAdvertiser

    mock_zc = MagicMock()
    mock_zc.register_service.side_effect = OSError("bind failed")
    mock_zc_class = MagicMock(return_value=mock_zc)
    mock_si_class = MagicMock()

    with patch.dict(
        "sys.modules",
        {"zeroconf": MagicMock(Zeroconf=mock_zc_class, ServiceInfo=mock_si_class)},
    ):
        advertiser = MDNSAdvertiser()
        with caplog.at_level(logging.WARNING, logger="aprilcam.daemon.mdns"):
            advertiser.start(tcp_port=5280)  # must not raise

    assert any("registration failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Tests: stop()
# ---------------------------------------------------------------------------


def test_stop_unregisters_and_closes():
    """stop() calls unregister_service and close on the Zeroconf instance."""
    from aprilcam.daemon.mdns import MDNSAdvertiser

    mock_zc, mock_zc_class, mock_si_class = _make_mock_zeroconf()
    mock_info = MagicMock(name="info")
    mock_si_class.return_value = mock_info

    with patch.dict(
        "sys.modules",
        {"zeroconf": MagicMock(Zeroconf=mock_zc_class, ServiceInfo=mock_si_class)},
    ):
        advertiser = MDNSAdvertiser()
        advertiser.start(tcp_port=5280)
        advertiser.stop()

    mock_zc.unregister_service.assert_called_once_with(mock_info)
    mock_zc.close.assert_called_once()

    # Internal state cleared
    assert advertiser._zeroconf is None
    assert advertiser._info is None


def test_stop_is_idempotent():
    """Calling stop() twice does not raise."""
    from aprilcam.daemon.mdns import MDNSAdvertiser

    mock_zc, mock_zc_class, mock_si_class = _make_mock_zeroconf()

    with patch.dict(
        "sys.modules",
        {"zeroconf": MagicMock(Zeroconf=mock_zc_class, ServiceInfo=mock_si_class)},
    ):
        advertiser = MDNSAdvertiser()
        advertiser.start(tcp_port=5280)
        advertiser.stop()
        advertiser.stop()  # second call must not raise


def test_stop_before_start_is_noop():
    """stop() before start() is a no-op and does not raise."""
    from aprilcam.daemon.mdns import MDNSAdvertiser

    advertiser = MDNSAdvertiser()
    advertiser.stop()  # must not raise


# ---------------------------------------------------------------------------
# Tests: DaemonServer integration — mDNS lifecycle
# ---------------------------------------------------------------------------


def _make_short_tmp_cfg():
    """Return a (Config, base_path) with short socket paths for macOS AF_UNIX limit."""
    import stat
    import tempfile
    from pathlib import Path

    from aprilcam.config import Config

    base = Path(tempfile.mkdtemp(prefix="amd_", dir="/tmp"))
    base.chmod(base.stat().st_mode | stat.S_IRWXO)
    sock_dir = base / "s"
    data_dir = base / "d"
    sock_dir.mkdir()
    data_dir.mkdir()
    cfg = Config(
        data_dir=data_dir,
        socket_dir=sock_dir,
        calibration_dir=data_dir / "cal",
        daemon_pidfile=sock_dir / "aprilcamd.pid",
    )
    return cfg, base


@pytest.mark.needs_daemon
def test_daemon_server_starts_mdns_when_tcp_enabled():
    """DaemonServer creates and starts MDNSAdvertiser when tcp_enabled=True."""
    import shutil
    import threading

    from aprilcam.daemon.server import DaemonServer

    cfg, base = _make_short_tmp_cfg()
    try:
        mock_advertiser = MagicMock(name="MDNSAdvertiser_instance")
        mock_advertiser_class = MagicMock(return_value=mock_advertiser)

        unix_path = str(cfg.socket_dir / "ctrl.sock")
        server = DaemonServer(
            cfg,
            unix_enabled=True,
            tcp_enabled=True,
            unix_path=unix_path,
            tcp_port=15282,
        )

        import aprilcam.daemon.mdns as mdns_mod

        real_class = mdns_mod.MDNSAdvertiser
        mdns_mod.MDNSAdvertiser = mock_advertiser_class  # type: ignore[attr-defined]
        try:
            t = threading.Thread(target=server.run, daemon=True)
            t.start()
            assert server.started_event.wait(timeout=3.0), "Server did not start"
        finally:
            server._shutdown_event.set()
            t.join(timeout=5.0)
            mdns_mod.MDNSAdvertiser = real_class  # type: ignore[attr-defined]

        mock_advertiser.start.assert_called_once_with(tcp_port=15282)
        mock_advertiser.stop.assert_called_once()
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.needs_daemon
def test_daemon_server_skips_mdns_when_tcp_disabled():
    """DaemonServer does NOT start MDNSAdvertiser when tcp_enabled=False."""
    import shutil
    import threading

    from aprilcam.daemon.server import DaemonServer

    cfg, base = _make_short_tmp_cfg()
    try:
        unix_path = str(cfg.socket_dir / "ctrl.sock")
        server = DaemonServer(
            cfg,
            unix_enabled=True,
            tcp_enabled=False,
            unix_path=unix_path,
        )

        import aprilcam.daemon.mdns as mdns_mod

        mock_advertiser = MagicMock()
        mock_advertiser_class = MagicMock(return_value=mock_advertiser)
        real_class = mdns_mod.MDNSAdvertiser
        mdns_mod.MDNSAdvertiser = mock_advertiser_class  # type: ignore[attr-defined]

        try:
            t = threading.Thread(target=server.run, daemon=True)
            t.start()
            assert server.started_event.wait(timeout=3.0), "Server did not start"
        finally:
            server._shutdown_event.set()
            t.join(timeout=5.0)
            mdns_mod.MDNSAdvertiser = real_class  # type: ignore[attr-defined]

        mock_advertiser_class.assert_not_called()
        mock_advertiser.start.assert_not_called()
    finally:
        shutil.rmtree(base, ignore_errors=True)
