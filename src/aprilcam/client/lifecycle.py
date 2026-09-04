"""``DaemonLifecycle`` -- the ``DaemonLifecycle`` base class, client side
(ticket 020-005): local process control by spawning and signalling
``python -m aprilcam.daemon`` from the *caller's* process.

Converted from the process-control parts of
``archive/src/aprilcam/daemon/__main__.py`` (subprocess bring-up,
already-running detection) and ``archive/src/aprilcam/cli/_daemon.py``
(edge cases around daemon-connection resolution) -- v1's CLI framework
glue (``argparse`` groups, host-code resolution) is not imported, only
the underlying logic.

**This is not the server-side ``DaemonLifecycle``.** See
``daemon/lifecycle.py``'s module docstring for the full split: that
class *is* the daemon process's own lock/listener mechanics, running
*inside* the daemon; this one is how an *outside* process starts,
stops, and checks on it. Both structurally satisfy ``daemon.
interfaces.DaemonLifecycle`` -- "two different ``DaemonLifecycle``\\ s,
by design."

**Local-only, structurally -- this ticket's chosen answer to where
``-d remote`` is refused (sprint architecture, UC-13.S-4).** This class
takes no host parameter anywhere in its surface: there is nothing here
that could even *accept* a remote target, let alone refuse one. Refusing
``aprilcam daemon start -d pi-field.local`` is therefore entirely the
CLI's job (ticket 006) -- by the time a call would reach this class,
there is no remote destination left to carry, since ``start``/``stop``/
``status`` below only ever mean "the daemon on this machine." Recorded
here because the sprint architecture asked this ticket to pick one layer
and document the choice rather than leave it ambiguous.

**Pre-flight lock probe, not "spawn and see."** ``start()`` checks the
same lock file the daemon itself would take
(``daemon.lifecycle.LOCK_FILENAME``, via
``client.discovery.probe_lock_holder`` -- a non-blocking flock read,
mirroring ``DaemonLifecycle._acquire_lock``/``_read_holder`` on the
reading side) *before* spawning a subprocess at all, so a contended
start raises :class:`~aprilcam.errors.AlreadyRunning` naming the
existing holder without ever touching the process table (UC-01.S-3:
"does not disturb the running daemon"). A small window remains between
this probe and the spawned daemon's own lock acquisition, during which
a second, unrelated ``start()`` could win the race first -- accepted for
a single-operator CLI tool, not a distributed coordinator; if it
happens, the spawned process exits with its own uncaught
``AlreadyRunning`` and :meth:`start` re-probes the lock to report that
race honestly (see :meth:`start`'s body) rather than a bare "failed to
start."

**Directories, not paths.** Every path below (``lock_path``,
``socket_path``) is derived from a :class:`~aprilcam.config.directories.
Directories` instance, exactly like the daemon side -- never overridden
independently of it, and never surfaced as a CLI flag or env var
(``docs/design/configuration.md``: sockets are not configurable). The
spawned subprocess inherits the *same* environment this process has
(``subprocess.Popen``'s default), so ``APRILCAM_CONFIG_DIR``/
``APRILCAM_STATE_DIR``/``XDG_RUNTIME_DIR`` -- whatever a human's shell
or a test has set -- apply identically on both sides, which is what
makes the two ``Directories()`` resolutions agree without this module
ever needing to pass a path across the process boundary itself.

**The start-readiness race, and why the fix lives in ``daemon/
lifecycle.py`` instead of here (020-007).** :meth:`start` below waits
for :func:`~aprilcam.client.discovery.probe_socket_reachable` to return
``True`` and then returns success -- "the control-plane socket accepts
a connection" is this method's entire definition of ready. This
sprint's acceptance pass found that a real, human-shaped sequence --
``aprilcam daemon start`` immediately followed by ``aprilcam camera
list`` -- could observe an empty registry on this machine's AVFoundation
backend, because the server-side ``DaemonLifecycle.start()``
(``daemon/lifecycle.py``) used to build and start the control-plane
listener *before* running its initial camera enumeration: the socket
was reachable, and RPCs were already being dispatched on the listener's
own thread pool, while the enumeration that populates the registry was
still in flight on a different thread. Two layers could have absorbed
the fix: this method could poll something richer than raw socket
reachability (e.g. a real RPC, retried until the reported camera set
stabilises), or the daemon could simply not open the socket until it
is genuinely ready to answer. The latter was chosen: ``daemon/
lifecycle.py``'s ``start()`` now runs the initial enumeration *before*
building the listener, so socket-reachable and enumeration-complete
become the same event -- fixing the race for *every* reader of that
signal (this class, a bare ``grpc.channel_ready_future``, a future
health-check RPC), not just this one call site. A client-side retry
loop would have had to guess at a stabilisation heuristic (how many
consecutive identical answers count as "settled"?) and would only have
fixed the callers that used it; moving the line in the daemon fixes it
structurally, once, for the whole system. See that module's docstring
("Ordering: enumeration before the listener") for the fix itself and
``tests/aprilcam/daemon/test_lifecycle.py``'s
``test_start_calls_watch_before_building_the_control_plane_listener``
for its regression test. This class needed no code change as a result
-- :meth:`start` here was already correct given a daemon whose socket
reachability *means* readiness; it was the daemon's promise that was
broken.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from aprilcam.client.discovery import Discovery, probe_lock_holder, probe_socket_reachable
from aprilcam.config.directories import Directories
from aprilcam.daemon.interfaces import DaemonLifecycle as _DaemonLifecycleBase
from aprilcam.daemon.interfaces import DaemonStatus
from aprilcam.daemon.lifecycle import LOCK_FILENAME, SOCKET_FILENAME
from aprilcam.errors import AlreadyRunning, AprilCamError, ConnectionFailed

__all__ = ["DaemonLifecycle"]

logger = logging.getLogger(__name__)

#: How long ``start()`` waits for the spawned daemon's control socket
#: before killing it and reporting "exited before becoming ready". The
#: daemon's ``start()`` runs a full camera probe (open + settle reads on
#: every attached camera) *before* it opens the control plane, and that
#: probe alone measured 11 s on this rig with six cameras even when
#: every open was refused, longer still when they succeed -- so the
#: original 10 s made ``aprilcam daemon start``/``restart`` fail every
#: time from a shell that has camera access while a foreground
#: ``python -m aprilcam.daemon`` came up fine (2026-09-03, out of
#: process). 90 s is generous on purpose: a genuinely dead child is
#: caught by ``poll()`` immediately regardless of this bound.
_DEFAULT_START_TIMEOUT = 90.0
_DEFAULT_STOP_TIMEOUT = 10.0
_DEFAULT_POLL_INTERVAL = 0.05

#: Wall-clock budget for one ``ps`` invocation in :func:`_list_daemon_processes`
#: -- a wedged subprocess must not hang ``status()`` (mirrors
#: ``camera.backends.uvc_util``'s own subprocess-timeout discipline).
_PROCESS_SCAN_TIMEOUT_S = 5.0

#: Matches the exact argv ``start()`` above spawns
#: (``[python_executable, "-m", "aprilcam.daemon"]``), against a ``ps``
#: command-line column that may carry the full interpreter path plus
#: any trailing arguments a *different* invocation added -- name-
#: matching only, per this ticket's "report, never auto-kill" design
#: rationale (sprint.md Design Rationale).
_DAEMON_SPAWN_RE = re.compile(r"-m\s+aprilcam\.daemon(?:\s|$)")


def _list_daemon_processes() -> Tuple[int, ...]:
    """Real (default) process-table probe for :meth:`DaemonLifecycle.status`'s
    orphan report (ticket 031-002, SUC-002): every pid in the local
    process table whose command line matches ``-m aprilcam.daemon``,
    caller included (:meth:`DaemonLifecycle.status` is what excludes the
    current lock holder's own pid -- this function reports every match,
    unfiltered).

    Shells out to ``ps -eo pid=,args=`` -- present on both this
    project's target platforms (macOS dev machines, Linux/Raspberry Pi
    deployment) with a stable, headerless, single-line-per-process
    output, unlike ``/proc`` which only exists on Linux. Mirrors
    ``camera.backends.uvc_util``'s subprocess conventions: always
    ``shell=False`` (an argument list, never a shell string), a bounded
    timeout, and **never raises** -- any subprocess failure, non-zero
    exit, or unparseable line is skipped or degrades to an empty result,
    matching ``camera.backends.libcamera.list_loopback_cameras``'s same
    "never raises" discipline for a platform probe. This is the
    best-effort half of the ticket's contract; :meth:`DaemonLifecycle.
    status` adds a second, outer safety net around the *injectable*
    ``list_daemon_processes`` seam (this function is only its default),
    so a test double that raises is also tolerated.
    """
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            shell=False,
            capture_output=True,
            text=True,
            timeout=_PROCESS_SCAN_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("process-table scan (ps) failed: %s", exc)
        return ()
    if result.returncode != 0:
        return ()

    pids = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_str, _, rest = stripped.partition(" ")
        if not _DAEMON_SPAWN_RE.search(rest):
            continue
        try:
            pids.append(int(pid_str))
        except ValueError:
            continue
    return tuple(pids)


def _holder_pid(holder: str) -> Optional[int]:
    """Extract the numeric pid from a ``probe_lock_holder`` result
    (``"pid <N>"``), or ``None`` if it is not in that shape (e.g. the
    ``"unknown holder"`` fallback) -- there is nothing to signal in
    that case."""
    prefix = "pid "
    if not holder.startswith(prefix):
        return None
    try:
        return int(holder[len(prefix):])
    except ValueError:
        return None


class DaemonLifecycle(_DaemonLifecycleBase):
    """See the module docstring for the client-side/server-side split
    and the local-only decision.

    Derives from ``daemon.interfaces.DaemonLifecycle`` (see that
    module's design-rule docstring): a plain base class, not
    ``Protocol``, so this and ``daemon.lifecycle.DaemonLifecycle`` are
    both mechanically real ``DaemonLifecycle``\\ s, not two classes that
    merely happen to share a shape.

    Parameters
    ----------
    directories:
        Resolves the lock/socket paths this instance watches and the
        ``config_dir``/``state_dir`` reported while not running.
        Defaults to a fresh :class:`Directories`.
    discovery:
        Used by :meth:`status` to fetch the richer, RPC-answered view
        (cameras, resolved directories) once the lock shows something
        is running. Defaults to a :class:`Discovery` built from the
        same *directories*.
    python_executable:
        Interpreter :meth:`start` spawns ``-m aprilcam.daemon`` with.
        Defaults to ``sys.executable`` -- the interpreter running this
        code, which is what makes this work unmodified inside a venv.
    spawn / lock_holder / socket_reachable / kill:
        Overridable seams (default ``subprocess.Popen``,
        :func:`~aprilcam.client.discovery.probe_lock_holder`,
        :func:`~aprilcam.client.discovery.probe_socket_reachable`,
        ``os.kill``) so the polling/race/error-mapping logic in
        :meth:`start`/:meth:`stop`/:meth:`status` is unit-testable
        without spawning a real process -- and, for ``kill``
        specifically, so a test exercising :meth:`stop` against a
        *fabricated* holder pid never risks signalling a real,
        unrelated process that happens to have that pid on the machine
        running the suite.
    list_daemon_processes (ticket 031-002):
        Overridable seam (default :func:`_list_daemon_processes`, a
        ``ps``-backed process-table scan) feeding :meth:`status`'s
        orphan report -- same DI style as ``lock_holder``/
        ``socket_reachable`` above, so a test can fabricate "another
        daemon-shaped process is running" (or a scan failure) with no
        real process table involved. :meth:`status` treats this seam as
        best-effort: any exception it raises is caught and degrades to
        today's lock-only status rather than propagating.
    start_timeout / stop_timeout / poll_interval:
        How long :meth:`start`/:meth:`stop` wait for the control plane
        to come up / the lock to release, and how often they check.
    """

    def __init__(
        self,
        *,
        directories: Optional[Directories] = None,
        discovery: Optional[Discovery] = None,
        python_executable: Optional[str] = None,
        spawn: Callable[..., "subprocess.Popen"] = subprocess.Popen,
        lock_holder: Callable[[Path], Optional[str]] = probe_lock_holder,
        socket_reachable: Callable[[Path], bool] = probe_socket_reachable,
        kill: Callable[[int, int], None] = os.kill,
        list_daemon_processes: Callable[[], Tuple[int, ...]] = _list_daemon_processes,
        start_timeout: float = _DEFAULT_START_TIMEOUT,
        stop_timeout: float = _DEFAULT_STOP_TIMEOUT,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._directories = directories if directories is not None else Directories()
        self._discovery = discovery if discovery is not None else Discovery(
            directories=self._directories
        )
        self._python_executable = python_executable or sys.executable
        self._spawn = spawn
        self._lock_holder = lock_holder
        self._socket_reachable = socket_reachable
        self._kill = kill
        self._list_daemon_processes = list_daemon_processes
        self._start_timeout = start_timeout
        self._stop_timeout = stop_timeout
        self._poll_interval = poll_interval

    @property
    def lock_path(self) -> Path:
        return self._directories.socket_dir / LOCK_FILENAME

    @property
    def socket_path(self) -> Path:
        return self._directories.socket_dir / SOCKET_FILENAME

    # -- daemon.interfaces.DaemonLifecycle ---------------------------------

    def start(self) -> None:
        """See ``daemon.interfaces.DaemonLifecycle.start``. See the
        module docstring for the pre-flight-probe design and the race
        it can still lose."""
        holder = self._lock_holder(self.lock_path)
        if holder is not None:
            raise AlreadyRunning(holder)

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        process = self._spawn(
            [self._python_executable, "-m", "aprilcam.daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        deadline = time.monotonic() + self._start_timeout
        stderr_tail = b""
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr_tail = process.stderr.read() if process.stderr else b""
                break
            if self._socket_reachable(self.socket_path):
                return
            time.sleep(self._poll_interval)
        else:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                pass
            stderr_tail = process.stderr.read() if process.stderr else b""

        # The process exited (or we gave up and killed it) before the
        # control plane came up. Re-probe the lock: if someone else now
        # holds it, this was the race the module docstring names --
        # report it as AlreadyRunning, not a bare startup failure.
        holder = self._lock_holder(self.lock_path)
        if holder is not None:
            raise AlreadyRunning(holder)
        detail = stderr_tail.decode(errors="replace").strip()
        raise ConnectionFailed(
            "localhost",
            reason="daemon process exited before becoming ready"
            + (f": {detail}" if detail else ""),
        )

    def stop(self) -> None:
        """See ``daemon.interfaces.DaemonLifecycle.stop``. A quiet
        no-op when nothing is running (UC-13's "stop when not running:
        succeed quietly")."""
        holder = self._lock_holder(self.lock_path)
        if holder is None:
            return

        pid = _holder_pid(holder)
        if pid is None:
            logger.warning(
                "cannot signal daemon holding %s: unrecognised holder %r", self.lock_path, holder
            )
            return
        try:
            self._kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return  # already gone

        deadline = time.monotonic() + self._stop_timeout
        while time.monotonic() < deadline:
            if self._lock_holder(self.lock_path) is None:
                return
            time.sleep(self._poll_interval)
        logger.warning(
            "daemon (pid %d) did not release %s within %.1fs of SIGTERM",
            pid,
            self.lock_path,
            self._stop_timeout,
        )

    def _scan_orphan_processes(self, holder_pid: Optional[int]) -> Tuple[int, ...]:
        """Best-effort orphan-process report for :meth:`status` (ticket
        031-002, SUC-002): every pid the injected ``list_daemon_processes``
        seam reports, excluding *holder_pid* (so the daemon's own,
        lock-holding process is never reported as its own orphan --
        AC "no false positives against the daemon's own process").

        The seam itself is treated as untrusted: any exception it
        raises (not only the real implementation's own already-caught
        ``OSError``/``TimeoutExpired``) is caught here and degrades to
        an empty result -- "the scan is best-effort... degrades to
        today's lock-only behavior rather than raising" applies to a
        fabricated test double exactly as much as to the real ``ps``
        probe.
        """
        try:
            candidates = self._list_daemon_processes()
        except Exception:
            logger.warning(
                "process-table scan for orphaned daemon processes failed; "
                "degrading to lock-only status()",
                exc_info=True,
            )
            return ()
        return tuple(pid for pid in candidates if pid != holder_pid)

    def status(self) -> DaemonStatus:
        """See ``daemon.interfaces.DaemonLifecycle.status``. The
        **local process view** (is anything running on *this*
        machine): the lock, not an RPC, decides ``running``. While
        running, delegates to the RPC-answered ``Daemon.status()`` for
        the richer picture (cameras, the daemon's own resolved
        directories) when reachable; if the lock is held but the
        control plane is not (yet, or no longer) answering, reports a
        minimal running status rather than raising -- this method never
        raises for "not answering," only :meth:`start`/:meth:`stop` do.

        **Orphan report (ticket 031-002, SUC-002).** Every branch below
        also runs a best-effort process-table scan
        (:meth:`_scan_orphan_processes`) and carries its result in the
        returned status's ``other_daemon_processes`` -- regardless of
        whether the lock is held, since the gap this ticket closes
        (``daemon/interfaces.py``'s ``DaemonStatus.other_daemon_processes``
        docstring) includes a process that never completed ``start()``'s
        lock acquisition at all, i.e. one that can be alive with *no*
        lock holder anywhere. This is the one call site in the codebase
        that ever populates that field with a real scan -- the RPC-
        answered ``daemon.status()`` value used two branches below
        always reports it empty (local-only, never true of a remote
        daemon), so this method rebuilds the returned ``DaemonStatus``
        explicitly rather than passing the RPC response through
        unchanged.
        """
        holder = self._lock_holder(self.lock_path)
        holder_pid = _holder_pid(holder) if holder is not None else None
        orphans = self._scan_orphan_processes(holder_pid)
        if holder is None:
            return DaemonStatus(
                running=False,
                cameras=(),
                config_dir=str(self._directories.config_dir),
                state_dir=str(self._directories.state_dir),
                other_daemon_processes=orphans,
            )
        try:
            daemon = self._discovery.connect(None)
            remote_status = daemon.status()
        except AprilCamError:
            return DaemonStatus(
                running=True,
                cameras=(),
                config_dir=str(self._directories.config_dir),
                state_dir=str(self._directories.state_dir),
                other_daemon_processes=orphans,
            )
        return DaemonStatus(
            running=remote_status.running,
            cameras=remote_status.cameras,
            config_dir=remote_status.config_dir,
            state_dir=remote_status.state_dir,
            other_daemon_processes=orphans,
        )
