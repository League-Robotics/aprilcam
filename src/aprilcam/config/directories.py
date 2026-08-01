"""Resolves ``config_dir``, ``state_dir``, and ``socket_dir``.

Implements the ``Directories`` Protocol
(``src/aprilcam/config/interfaces.py``) per the two-directory model in
``docs/design/configuration.md``:

* ``config_dir`` — ``/etc/aprilcam``, human-owned, daemon **reads
  only**. Falls back to ``~/.config/aprilcam`` when the system path is
  unavailable (configuration.md §2).
* ``state_dir`` — ``/var/lib/aprilcam``, daemon-owned, regenerable.
  Falls back to ``~/.local/share/aprilcam``.
* ``socket_dir`` — where the unix control-plane socket is bound. **Not
  independently configurable** (configuration.md §1): no flag, no
  ``APRILCAM_*`` env var. Resolves to ``/run/aprilcam``, then
  ``$XDG_RUNTIME_DIR/aprilcam``, then a per-uid directory under the OS
  temp root for machines with neither (macOS, headless Linux without
  systemd-logind — the exact situation this sprint's bring-up runs
  under).

**The cascade, and why it stops at three tiers for a directory root.**
``configuration.md`` §3 states the general four-level cascade — flag >
env > file > default — for settings in general. For ``config_dir`` and
``state_dir`` **themselves** the "file" tier does not apply: a setting
read from a file *inside* the config directory cannot also determine
where that directory is without circularity. The file tier is real for
other settings (e.g. ``APRILCAM_DAEMON_HOST`` read from a file once
``config_dir`` is already known) but is a client-/daemon-side concern
layered on top of this module, not something ``Directories`` itself
does. So the cascade implemented here is flag > env > default, with an
unprivileged-fallback tier below "default" per §2 — four tiers in
total, just not the same four as §3's general list.

**Sprint 020 Open Question 2 — unix-socket-only, deliberately.** The
sprint architecture flagged whether the daemon should also open a TCP
listener (spec §4.1 names ``TCP:5280``) alongside the unix socket this
sprint. It does not: ``socket_dir`` below resolves only the directory
that holds the unix control-plane socket, and nothing in this module or
the sprint's lifecycle ticket (004) binds a TCP port. Every in-scope
CLI command this sprint works against a local daemon (unix socket) or
an explicitly-named ``-d HOST`` via a direct TCP *client* connect,
neither of which requires the daemon to *listen* on TCP. Opening
``TCP:5280`` is deferred to whichever future sprint actually exercises
a daemon-to-daemon or CLI-to-remote-daemon path end-to-end against a
real second host. This is recorded here, in the module that owns
socket resolution, so the omission reads as a decision rather than an
oversight.

**Reporting.** UC-01 requires that "which roots were chosen is never a
mystery." Every resolved root is logged at INFO on construction *and*
returned: ``config_dir_source`` / ``state_dir_source`` /
``socket_dir_source`` name the cascade tier (``"flag"``, ``"env"``,
``"default"``, ``"fallback"``, or (socket only) ``"xdg-runtime"``) that
produced each path, and :meth:`Directories.describe` renders all three
as one line for a startup banner.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union

from aprilcam.config.interfaces import Directories as _DirectoriesBase

__all__ = ["Directories"]

logger = logging.getLogger(__name__)

# Hard-coded system roots (configuration.md §1). Module globals, not
# values baked into a function body, so tests can monkeypatch them to
# point at a tmp_path standing in for the real system roots instead of
# touching /etc, /var/lib, or /run.
SYSTEM_CONFIG_DIR = Path("/etc/aprilcam")
SYSTEM_STATE_DIR = Path("/var/lib/aprilcam")
SYSTEM_SOCKET_DIR = Path("/run/aprilcam")

APRILCAM_CONFIG_DIR_ENV = "APRILCAM_CONFIG_DIR"
APRILCAM_STATE_DIR_ENV = "APRILCAM_STATE_DIR"
XDG_RUNTIME_DIR_ENV = "XDG_RUNTIME_DIR"

PathLike = Union[str, Path]


@dataclass(frozen=True)
class _Resolved:
    """One resolved root plus which cascade tier produced it."""

    path: Path
    source: str


def _can_access(path: Path, mode: int) -> bool:
    """Thin wrapper over :func:`os.access`.

    Exists so tests can substitute permission outcomes (``monkeypatch``
    this function) instead of fighting real filesystem permissions —
    which is unreliable in a sandbox that may run as root, where
    ``os.access``'s normal semantics do not hold.
    """
    return os.access(path, mode)


def _config_dir_available(path: Path) -> bool:
    """A config root is usable if it already exists and is readable.

    Nothing ever creates ``config_dir`` — it is human-owned — so
    "available" here means "already there", never "creatable". This
    matches configuration.md §2's motivating case: an unprivileged
    developer "may not be able to read a ``/etc/aprilcam`` that does
    not exist."
    """
    return path.is_dir() and _can_access(path, os.R_OK | os.X_OK)


def _writable_or_creatable(path: Path) -> bool:
    """A state/socket root is usable if it exists and is writable, or
    does not exist yet but its nearest existing ancestor is — i.e. the
    daemon could create it on demand.

    Matches configuration.md §2's other motivating case: an
    unprivileged developer "cannot write ``/var/lib/aprilcam``" even
    though ``/var/lib`` itself exists.
    """
    if path.exists():
        return path.is_dir() and _can_access(path, os.W_OK | os.X_OK)
    for ancestor in path.parents:
        if ancestor.exists():
            return _can_access(ancestor, os.W_OK | os.X_OK)
    return False


def _user_config_fallback() -> Path:
    return Path.home() / ".config" / "aprilcam"


def _user_state_fallback() -> Path:
    return Path.home() / ".local" / "share" / "aprilcam"


def _resolve_root(
    *,
    flag: Optional[Path],
    env_var: str,
    system_default: Path,
    user_fallback: Path,
    available: Callable[[Path], bool],
) -> _Resolved:
    """flag > env > default > unprivileged fallback, for one root."""
    if flag is not None:
        return _Resolved(flag, "flag")
    env_value = os.environ.get(env_var, "").strip()
    if env_value:
        return _Resolved(Path(env_value), "env")
    if available(system_default):
        return _Resolved(system_default, "default")
    return _Resolved(user_fallback, "fallback")


def _resolve_socket_dir() -> _Resolved:
    """``/run/aprilcam``, else ``$XDG_RUNTIME_DIR/aprilcam``, else a
    per-uid directory under the OS temp root.

    Not part of the flag/env cascade — deliberately, per
    configuration.md §1: sockets are runtime plumbing nobody needs to
    choose the location of.
    """
    if _writable_or_creatable(SYSTEM_SOCKET_DIR):
        return _Resolved(SYSTEM_SOCKET_DIR, "default")
    xdg_runtime = os.environ.get(XDG_RUNTIME_DIR_ENV, "").strip()
    if xdg_runtime:
        return _Resolved(Path(xdg_runtime) / "aprilcam", "xdg-runtime")
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return _Resolved(Path(tempfile.gettempdir()) / f"aprilcam-{uid}", "fallback")


class Directories(_DirectoriesBase):
    """Concrete :class:`aprilcam.config.interfaces.Directories`.

    Derives from ``config.interfaces.Directories`` (a plain base class,
    not ``Protocol`` -- see ``daemon.interfaces``'s module docstring for
    the design rule).

    Resolution happens once, eagerly, at construction — matching
    "reports which roots it resolved at startup" — and the result is
    cached for the object's lifetime rather than recomputed per
    property access, so a mid-process env change cannot make
    ``config_dir`` disagree with itself between two calls in the same
    run.

    *config_dir* / *state_dir* are the "flag" tier of the cascade —
    e.g. a future ``--config-dir``/``--state-dir`` CLI option (ticket
    006 wires the actual ``argparse`` flags; this constructor is where
    they land). Passing neither exercises env > default > fallback
    exactly as a flag-less invocation would. There is no ``socket_dir``
    parameter: sockets are not configurable at any tier.
    """

    def __init__(
        self,
        *,
        config_dir: Optional[PathLike] = None,
        state_dir: Optional[PathLike] = None,
    ) -> None:
        self._config = _resolve_root(
            flag=Path(config_dir) if config_dir is not None else None,
            env_var=APRILCAM_CONFIG_DIR_ENV,
            system_default=SYSTEM_CONFIG_DIR,
            user_fallback=_user_config_fallback(),
            available=_config_dir_available,
        )
        self._state = _resolve_root(
            flag=Path(state_dir) if state_dir is not None else None,
            env_var=APRILCAM_STATE_DIR_ENV,
            system_default=SYSTEM_STATE_DIR,
            user_fallback=_user_state_fallback(),
            available=_writable_or_creatable,
        )
        self._socket = _resolve_socket_dir()
        for label, resolved in (
            ("config_dir", self._config),
            ("state_dir", self._state),
            ("socket_dir", self._socket),
        ):
            logger.info(
                "%s resolved to %s (source=%s)", label, resolved.path, resolved.source
            )

    @property
    def config_dir(self) -> Path:
        return self._config.path

    @property
    def state_dir(self) -> Path:
        return self._state.path

    @property
    def socket_dir(self) -> Path:
        return self._socket.path

    @property
    def config_dir_source(self) -> str:
        """Which cascade tier produced ``config_dir``.

        The "returned" half of UC-01's reporting requirement, alongside
        the INFO log line emitted at construction.
        """
        return self._config.source

    @property
    def state_dir_source(self) -> str:
        return self._state.source

    @property
    def socket_dir_source(self) -> str:
        return self._socket.source

    def describe(self) -> str:
        """One human-readable line naming every resolved root and tier.

        For a daemon/CLI startup banner — the concrete answer to "which
        config did this instance load."
        """
        return (
            f"config_dir={self.config_dir} ({self.config_dir_source}), "
            f"state_dir={self.state_dir} ({self.state_dir_source}), "
            f"socket_dir={self.socket_dir} ({self.socket_dir_source})"
        )
