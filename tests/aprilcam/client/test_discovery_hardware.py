"""Hardware verification: ``Discovery.find_all()`` (and, by extension,
``aprilcam daemon list``) finding a genuine second PHYSICAL machine
(sprint 028 Hardware Constraints item 2; ticket 006).

**Not about a webcam -- read this before assuming ``@pytest.mark.camera``
is a mislabel.** This project's two hardware-adjacent markers are
``camera`` ("needs real, unavailable physical resources" --
``pyproject.toml``) and ``integration`` ("real local daemon/hardware
processes", no *extra* physical resource needed beyond what any dev
machine already has). This scenario needs a second physical host -- e.g.
a Raspberry Pi -- actually present and running an AprilCam daemon
somewhere on the same LAN as the machine this test runs on. That is
squarely the ``camera`` marker's "blocked by unavailable physical
resources" idiom, per sprint.md's own Hardware Constraints section and
ticket 006's own instructions, even though no camera is involved.

**Distinct from the already-passing two-local-daemon-alternate-port
tests.** ``tests/aprilcam/client/test_discovery.py``'s
``test_find_all_discovers_two_real_local_daemons_on_alternate_ports_via_real_mdns``
and ``tests/aprilcam/cli/test_daemon_cli.py``'s
``test_daemon_list_reports_two_real_local_daemons_on_alternate_ports`` are
``@pytest.mark.integration`` and already pass for real, unconditionally,
on this and every other dev machine -- they prove the full
advertise/browse/self-filter/connect/fan-out *mechanism* end to end using
two real daemon *processes* on *one* physical machine, with a synthetic
``hostname=`` override standing in for a second host (see
``daemon/mdns.py``'s ``MdnsAdvertiser.start`` docstring for exactly why
that override exists). That is the documented, sprint.md-sanctioned
stand-in for hardware this sprint cannot provide -- it is **not** a test
of real multicast traversal across a genuine LAN with a second host on
it, and this module exists specifically to be that second, honestly
distinct thing.

This module performs a real ``zeroconf`` browse -- no dependency-injected
fake, no synthetic hostname, no second local process standing in for
anything -- against whatever is actually reachable on this machine's real
network interface right now, and looks for a daemon advertisement whose
hostname differs from this machine's own (i.e. a genuinely different
physical host, not this process's own loopback advertisement). Skips
cleanly, naming exactly what is missing, when none is found -- the
expected outcome on essentially every development machine and CI runner,
which has no second AprilCam host anywhere on its LAN. If a real second
host *is* reachable (a Raspberry Pi running an AprilCam daemon, brought
onto the same network as the machine this suite runs on), this test runs
for real and records the result honestly, per sprint 027 ticket 008's own
precedent of not assuming the worst case without actually checking.
"""

from __future__ import annotations

import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from aprilcam.client.discovery import Discovery
from aprilcam.config.directories import Directories
from aprilcam.errors import DiscoveryFailed

pytestmark = pytest.mark.camera


def _short_tmp_dir(prefix: str) -> Path:
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit on macOS/Linux, and pytest's ``tmp_path``
    nests too deeply (matches every other ``@pytest.mark.camera``
    module's identically-named workaround)."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


def test_find_all_discovers_a_genuine_second_physical_host_on_the_lan(
    tmp_path, monkeypatch
):
    """Real ``Discovery().find_all()``, real default ``browse=`` (no
    override), against this machine's actual network interface.

    The *local* lock/socket probe is isolated to a private, guaranteed-
    empty directory (mirrors ``test_discovery.py``'s own
    ``_writable_or_creatable``-monkeypatch-plus-``XDG_RUNTIME_DIR``
    pattern) so this test's result is never entangled with whether some
    unrelated real daemon happens to be running locally on this machine
    -- only the *remote* half of ``find_all()``'s result matters here.
    The mDNS *browse* itself is untouched and real, going out over this
    machine's actual network stack exactly as ``aprilcam daemon list``
    would.
    """
    short_dir = _short_tmp_dir("ac-006-secondhost-")
    monkeypatch.setattr("aprilcam.config.directories._writable_or_creatable", lambda path: False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))
    try:
        directories = Directories(
            config_dir=tmp_path / "config", state_dir=tmp_path / "state"
        )
        assert directories.socket_dir == short_dir / "aprilcam", (
            "local probe did not isolate to the private tmp dir as "
            "expected -- cannot trust this run's remote-only result"
        )

        discovery = Discovery(directories=directories)
        own_hostname = socket.gethostname()

        try:
            results = discovery.find_all()
        except DiscoveryFailed as exc:
            pytest.skip(
                "the real mDNS browse itself failed in this environment "
                f"({exc}) -- cannot determine whether a genuine second "
                "physical host is reachable here. This is a different "
                "condition from 'no second host found': it means this "
                "sandbox/runner cannot open a multicast socket at all, "
                "which most CI environments cannot. Re-run on a machine "
                "with real LAN/multicast access to check for a genuine "
                "second host (sprint 028 Hardware Constraints item 2)."
            )

        remote = tuple(r for r in results if not r.is_local)

        if not remote:
            pytest.skip(
                "no genuine second physical AprilCam host answered a real "
                f"mDNS browse from {own_hostname!r} on this machine's LAN "
                "-- this is the expected result on a development machine "
                "or CI runner with no second host (e.g. a Raspberry Pi) "
                "anywhere on the network. This is distinct from "
                "test_discovery.py's/test_daemon_cli.py's "
                "@pytest.mark.integration two-local-daemon-alternate-port "
                "tests, which already pass unconditionally by simulating "
                "a second host with a synthetic hostname on this same "
                "machine -- see this module's docstring. To exercise this "
                "test for real: bring a genuine second physical host "
                "(e.g. a Raspberry Pi) running an AprilCam daemon onto "
                "this machine's LAN and re-run (sprint 028 Hardware "
                "Constraints item 2)."
            )

        # A genuine second physical host answered -- record the result
        # honestly rather than silently discarding it, per sprint 027
        # ticket 008's own precedent (running the hardware suite anyway
        # found real, informative results even where planning assumed it
        # could not).
        print(
            f"\n[028-006 hardware] genuine second physical host(s) found "
            f"via real mDNS browse from {own_hostname!r}: "
            f"{[(r.host, r.reachable) for r in remote]}"
        )
        for entry in remote:
            assert ":" in entry.host, (
                f"remote DaemonInfo.host {entry.host!r} does not carry a "
                "port -- Discovery.find_all's load-bearing 'ip:port' "
                "formatting contract has regressed"
            )
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)
