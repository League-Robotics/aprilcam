"""Suite-wide pytest fixtures — process-global state isolation.

**SIGPIPE disposition guard.** ``aprilcam.cli.main()`` resets
``SIGPIPE``'s disposition process-wide to ``SIG_DFL`` as the very first
thing it does (see ``src/aprilcam/cli/__init__.py``'s
``_reset_sigpipe_to_default()``, ticket 031-003 SUC-003) — correct and
intentional for a real ``aprilcam`` subprocess invocation, but signal
dispositions are process-global, not per-call. Any test that calls
``main()`` in-process (as several tests in
``tests/aprilcam/cli/test_main.py`` do, to exercise the dispatch/
exception-handling seam without spawning a real subprocess) mutates
that global state for the rest of the *entire* pytest process, not just
for the duration of that one test.

Left unrestored, this silently changes behaviour for every later test
in the suite: an in-process write to an already-closed pipe or socket
(not uncommon with imperfect thread/subprocess/socket teardown across a
large suite) that would otherwise raise a catchable ``BrokenPipeError``
instead kills the interpreter outright via the real signal — a bare
``exit 141`` with no traceback and no pytest summary line, at a crash
location that shifts depending on test execution order (whichever test
happens to hit a closed pipe next).

This autouse fixture snapshots ``SIGPIPE``'s disposition before every
test and restores it after, unconditionally, regardless of whether the
test touches signals at all. Placed at the top of the whole ``tests/``
tree (not just alongside ``test_main.py``) because the failure mode is
not specific to that file — any future test anywhere that calls
``main()`` in-process would reintroduce the exact same class of bug.
"""

from __future__ import annotations

import signal
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def _restore_sigpipe_disposition() -> Iterator[None]:
    """Undo any process-global ``SIGPIPE`` disposition change a test
    makes (directly, or indirectly via an in-process ``aprilcam.cli.main()``
    call) so it cannot leak into later, unrelated tests."""
    sigpipe = getattr(signal, "SIGPIPE", None)
    if sigpipe is None:
        # No SIGPIPE on this platform (Windows) -- nothing to snapshot.
        yield
        return
    previous = signal.getsignal(sigpipe)
    try:
        yield
    finally:
        signal.signal(sigpipe, previous)
