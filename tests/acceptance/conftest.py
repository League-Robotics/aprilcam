"""Foundation for ``tests/acceptance/`` -- the L2 layer (sprint 035
ticket-group 4, ``UC-29`` / ``SUC-003``).

**Why this layer exists.** L0 (``tests/image_matrix/`` etc.) and L1
(``tests/aprilcam/*_hardware.py``) both run under isolated
``config_dir``/``state_dir`` roots -- a throwaway ``tmp_path`` standing
in for the real thing, so a bug can write garbage without consequence.
L2 is the opposite, deliberately: it proves the *real*, persistent
daemon and its real, already-calibrated field state work correctly,
which structurally requires running against the operator's actual
``~/.config/aprilcam`` / ``~/.local/share/aprilcam`` roots. Running
uncontrolled tests against real state is normally a bad idea for
exactly the reason this module exists to rule out: the one hand-solved
value that would be expensive to redo is
``camera_position`` in
``~/.config/aprilcam/cameras/arducam-ov9782-usb-camera/config.json``
(currently ``height_cm=128.22990496814816,
nadir_x_cm=3.0570142285481654, nadir_y_cm=-2.798700632324644``,
sprint.md Decision 3). This conftest is the mechanism that makes "never
overwritten" *provable*, not assumed.

**A fourth thing, added by ticket 015:** a collection-time node-id
allowlist (:func:`find_disallowed_calls`, wired into
:func:`pytest_collection_modifyitems` alongside the marker audit below)
that fails collection outright if any test module under this directory
contains a ``Daemon.calibrate`` call or a non-dry (no ``--dry-run``)
``locate`` call -- both mutate real, persisted state
(``camera_position``/calibration), which is exactly what this layer's
read-only/reversible contract forbids. See that function's own
docstring for the two AST patterns it checks and why an AST scan is
used instead of a plain string search.

**Three things this file builds, per ticket 014:**

1. **Real-roots requirement** (:func:`pytest_configure`) -- hard-refuses
   to run (a ``pytest.UsageError``, not a skip) if
   ``APRILCAM_CONFIG_DIR``/``APRILCAM_STATE_DIR`` isolation overrides
   are set. This suite exists specifically to test the real thing; an
   isolated root here would silently defeat the entire point of the
   layer.
2. **The sha256 no-write guard** (:func:`hash_tree` /
   :func:`assert_no_writes`, wired up as the autouse ``_no_write_guard``
   fixture below) -- hashes every real file under both roots once at
   session start, and re-hashes + compares after *every* test, so a
   violation is caught (and named, per-file) as close as possible to
   the test that caused it rather than only at the very end of a long
   run.
3. **Marker audit** (:func:`pytest_collection_modifyitems`) -- refuses
   to *collect* any test in this directory that is missing one of
   ``acceptance``/``camera``/``integration``, because sprint.md
   Decision 4 deliberately layers ``acceptance`` on top of the existing
   ``camera``/``integration`` markers rather than replacing them, so
   that the existing gate's ``-m "not camera and not integration"``
   exclusion expression keeps excluding this whole layer with zero
   edits. A test that forgot one of the three markers would silently
   join the fast gate instead of failing loudly, so this hook makes
   that impossible rather than trusting convention.

Plus a terminal-summary hook (:func:`pytest_terminal_summary`) that
renders a human-readable PASS/FAIL/SKIP report for whatever ran from
this directory.

**What the no-write guard does *not* prove -- the live-override
distinction.** ``daemon.service.DaemonService`` supports pushing a
``CameraConfig`` at runtime (``push_config`` / the MCP
``set_camera_config`` tool); that push replaces the service's in-memory
``_camera_configs`` entry for the camera immediately, and *does not
write to* ``config_dir/cameras/<slug>/config.json`` at all --
``Camera.reset()`` is what re-reads the on-disk file and is the only
thing that discards a live override (``daemon/service.py``, "discarding
whatever live override this service held"; ``camera/camera.py``
sprint.md Decision 3). Concretely: if some other process pokes the real
daemon with a live ``camera_position`` override while this suite is
running, the sha256 sweep below stays green -- the file genuinely did
not change -- while the *running daemon* is answering geometry queries
with different numbers than what is on disk. The guard's pass is
honest about **files**, and is not, and was never meant to be, a
statement about **runtime daemon state**. Read the other direction:
this suite's own tests must never call anything that pushes a live
override in the first place (the read-only/reversible inventory ticket
015 builds keeps to read-only + explicitly-reversed RPCs for exactly
this reason) -- if they did, a green no-write guard next to a daemon
now serving altered numbers would be false confidence. Documented here,
at the guard's own definition, so neither misreading (false confidence
that "green implies nothing changed anywhere", or a false alarm that
some *other* live-override-pushing process nearby is somehow going to
trip a file-content guard) survives contact with this code.

Kept dependency-free by design (no ``daemon``/``camera`` imports beyond
``aprilcam.config.directories``, which is a config-resolution module,
not a live-system one) so :mod:`tests.acceptance.test_no_write_guard`
can exercise the guard itself against a fake ``tmp_path`` tree with
zero hardware and zero real-system risk.
"""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from aprilcam.config.directories import (
    APRILCAM_CONFIG_DIR_ENV,
    APRILCAM_STATE_DIR_ENV,
    Directories,
)

__all__ = [
    "NoWriteViolation",
    "hash_tree",
    "diff_trees",
    "assert_no_writes",
    "find_disallowed_calls",
]

#: The two env vars that must be *absent* for this layer to run at all.
_ISOLATION_ENV_VARS = (APRILCAM_CONFIG_DIR_ENV, APRILCAM_STATE_DIR_ENV)

#: Every test under this directory must carry all three, per sprint.md
#: Decision 4 -- see :func:`pytest_collection_modifyitems`.
_REQUIRED_MARKERS = {"acceptance", "camera", "integration"}

_ACCEPTANCE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# The no-write guard -- dependency-free, unit-tested independently in
# test_no_write_guard.py against a fake tree.
# ---------------------------------------------------------------------------


class NoWriteViolation(AssertionError):
    """Raised when the real ``config_dir``/``state_dir`` tree no longer
    matches its no-write-guard baseline snapshot.

    An ``AssertionError`` subclass (not a bare ``RuntimeError``) so
    pytest reports it exactly like any other failed invariant, with the
    full per-file diagnostic list in the message.
    """


def hash_tree(root: Path) -> Dict[Path, str]:
    """sha256 every regular file under *root*, keyed by absolute path.

    A root that does not exist yields ``{}`` rather than raising -- a
    fresh install that has not created ``state_dir`` yet is a legitimate
    starting point, not a guard failure. Symlinks are skipped (hashing
    through a symlink would double-count or chase outside the tree).

    ``Path.rglob`` does not itself guarantee a stable order across
    calls/platforms, so the walk is sorted explicitly: two snapshots of
    an unchanged tree must produce identical dicts, key order included,
    for the "before" and "after" comparison to be reproducible.
    """
    if not root.exists():
        return {}
    digests: Dict[Path, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        digests[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def diff_trees(baseline: Dict[Path, str], current: Dict[Path, str]) -> List[str]:
    """Human-readable, per-file diagnostics between two :func:`hash_tree`
    snapshots taken of the same root(s). Empty list means no difference.
    """
    lines: List[str] = []
    for path in sorted(set(baseline) | set(current)):
        before = baseline.get(path)
        after = current.get(path)
        if before == after:
            continue
        if before is None:
            lines.append(f"ADDED    {path}")
        elif after is None:
            lines.append(f"REMOVED  {path}")
        else:
            lines.append(f"MODIFIED {path}")
    return lines


def assert_no_writes(
    baseline: Dict[Path, str], current: Dict[Path, str], *, label: str
) -> None:
    """Raise :class:`NoWriteViolation` naming every changed file, or
    return silently when *baseline* and *current* agree exactly.

    *label* identifies which comparison tripped (e.g. the nodeid of the
    test just run) so the raised message is actionable without needing
    to re-run under ``-v``.
    """
    diffs = diff_trees(baseline, current)
    if diffs:
        raise NoWriteViolation(
            f"no-write guard tripped {label}: the real config_dir/"
            f"state_dir tree changed during this acceptance run "
            f"(see the module docstring in tests/acceptance/conftest.py "
            f"for what this guard does and does not prove):\n"
            + "\n".join(diffs)
        )


# ---------------------------------------------------------------------------
# 1. Real-roots requirement
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Hard-refuse (not skip) to run under an isolation-var override.

    Fires once, at session start, before any test in this directory
    collects or runs -- ``tests/acceptance/`` exists specifically to
    exercise the operator's real environment, so an isolated root here
    would silently defeat the entire point of the layer rather than
    just producing a wrong-but-passing result.
    """
    del config  # unused -- required by the pytest_configure hook signature
    offending = [
        name for name in _ISOLATION_ENV_VARS if os.environ.get(name, "").strip()
    ]
    if offending:
        raise pytest.UsageError(
            "tests/acceptance/ requires the REAL config_dir/state_dir "
            "roots -- this layer exists specifically to test the "
            "operator's actual environment, never an isolated one. "
            f"Found isolation override(s) set: {', '.join(offending)}. "
            "Unset them before running this layer (isolated-root "
            "coverage belongs in tests/aprilcam/ and the *_hardware.py "
            "modules instead)."
        )


# ---------------------------------------------------------------------------
# 2. The no-write guard, wired up as fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def real_directories() -> Directories:
    """The real, un-isolated ``config_dir``/``state_dir`` -- resolved
    with zero overrides, exactly as a live ``aprilcam`` invocation on
    this operator's machine would resolve them. Guaranteed real by
    :func:`pytest_configure` above refusing to even start otherwise.
    """
    return Directories()


@pytest.fixture(scope="session")
def _no_write_baseline(real_directories: Directories) -> Dict[Path, str]:
    """One sha256 snapshot of every file under both real roots, taken
    once, before any acceptance test in this session runs.

    Held for the rest of the session and compared against after every
    test (see ``_no_write_guard`` below) rather than re-snapshotted
    mid-session -- comparing against the original, untouched baseline
    every time is what lets a failure message point at the exact file
    that changed, on the very first test where the guard notices.
    """
    return {
        **hash_tree(real_directories.config_dir),
        **hash_tree(real_directories.state_dir),
    }


@pytest.fixture(autouse=True)
def _no_write_guard(
    request: pytest.FixtureRequest,
    real_directories: Directories,
    _no_write_baseline: Dict[Path, str],
) -> Iterator[None]:
    """Session-baselined, per-test-checked no-write guard.

    Every test collected under ``tests/acceptance/`` gets this fixture
    automatically (``autouse=True``). After each test completes, the
    guard re-hashes both real roots and compares against the session
    baseline, raising :class:`NoWriteViolation` naming the exact file(s)
    that changed if anything differs.
    """
    yield
    current = {
        **hash_tree(real_directories.config_dir),
        **hash_tree(real_directories.state_dir),
    }
    assert_no_writes(
        _no_write_baseline, current, label=f"after {request.node.nodeid}"
    )


# ---------------------------------------------------------------------------
# 3. Marker audit
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(
    config: pytest.Config, items: List[pytest.Item]
) -> None:
    """Refuse to collect any ``tests/acceptance/`` test missing one of
    the three required markers, or any test module under this directory
    that contains a structurally-forbidden call (:func:`find_disallowed_calls`).

    Only inspects items whose test file lives under this directory --
    ``pytest_collection_modifyitems`` hooks receive the *whole* session's
    item list, not just this directory's, since conftest hooks are
    session-wide once loaded.
    """
    del config  # unused -- required by the hook signature
    violations: List[str] = []
    scanned_files: Dict[Path, None] = {}
    allowlist_violations: List[str] = []
    for item in items:
        item_path = Path(str(getattr(item, "path", item.fspath))).resolve()
        try:
            item_path.relative_to(_ACCEPTANCE_DIR)
        except ValueError:
            continue  # not one of ours
        present = {marker.name for marker in item.iter_markers()}
        missing = _REQUIRED_MARKERS - present
        if missing:
            violations.append(f"  {item.nodeid}: missing {sorted(missing)}")
        if item_path not in scanned_files:
            scanned_files[item_path] = None
            try:
                source = item_path.read_text(encoding="utf-8")
            except OSError:
                source = ""
            allowlist_violations.extend(
                find_disallowed_calls(source, filename=str(item_path))
            )
    if violations:
        raise pytest.UsageError(
            "every test under tests/acceptance/ must carry all three of "
            "acceptance+camera+integration markers (sprint 035 Decision "
            "4) so the existing gate's -m \"not camera and not "
            "integration\" exclusion keeps excluding this whole layer "
            "without ever being edited. Missing on:\n" + "\n".join(violations)
        )
    if allowlist_violations:
        raise pytest.UsageError(
            "tests/acceptance/ node-id allowlist violation(s) -- "
            "'calibrate' and any non-dry 'locate' call are structurally "
            "excluded from this layer (sprint 035 ticket 015: both mutate "
            "real, persisted state, which this read-only/reversible layer "
            "must never do). Found:\n"
            + "\n".join(f"  {v}" for v in allowlist_violations)
        )


# ---------------------------------------------------------------------------
# 4. Node-id allowlist -- structurally excludes calibrate / non-dry locate
# ---------------------------------------------------------------------------

#: Attribute names that are forbidden to call anywhere under this
#: directory, full stop -- ``Daemon.calibrate`` mutates real,
#: persisted calibration state unconditionally (no "dry" mode exists
#: for it), so unlike ``locate`` there is no exception to allow for.
_FORBIDDEN_ATTRIBUTE_CALLS = frozenset({"calibrate"})


def _iter_call_string_constants(call: ast.Call) -> List[str]:
    """Every string literal that appears as a direct argument to *call*
    -- either a bare positional/keyword string constant, or one nested
    one level inside a list/tuple literal argument (the shape
    ``subprocess.run([str(bin_path), "camera", "locate", ...])`` takes)
    -- so :func:`find_disallowed_calls` catches both calling
    conventions this codebase's real-subprocess hardware tests use
    (most go through a local ``_run(bin_path, "camera", "locate", ...,
    env=env)`` helper, which is the flat-positional-args shape; a
    handful call ``subprocess.run`` with an explicit list literal
    directly).
    """
    strings: List[str] = []

    def _collect(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node.value)
        elif isinstance(node, (ast.List, ast.Tuple)):
            for elt in node.elts:
                _collect(elt)

    for arg in call.args:
        _collect(arg)
    for kw in call.keywords:
        if kw.value is not None:
            _collect(kw.value)
    return strings


def find_disallowed_calls(source: str, *, filename: str = "<test>") -> List[str]:
    """Every calibrate-shaped or non-dry-locate-shaped call in *source*
    (already-read module text; *filename* is used only to label
    messages/violations with a real, greppable location) -- the
    node-id allowlist ticket 015's acceptance criteria calls for:
    "structurally excludes calibrate and any non-dry locate call ...
    not merely avoided by convention." Returns an empty list when
    *source* contains neither pattern.

    Two independent patterns are checked, both by AST inspection --
    never a plain string search over the raw text, which would also
    match comments and docstrings (this module's own docstring
    mentions both "calibrate" and "locate" by name) and produce
    constant false positives:

    1. Any attribute access named ``calibrate`` (``d.calibrate(...)``,
       ``daemon.calibrate(...)``, or the same thing one level removed
       inside a ``connection.call(lambda d: d.calibrate(...))``
       closure -- the AST attribute access is identical either way) --
       forbidden outright, no exception.
    2. Any call whose direct arguments (see
       :func:`_iter_call_string_constants`) include the string
       ``"locate"`` without ``"--dry-run"`` among those same
       arguments -- mirrors ``test_camera_geometry_hardware.py``'s own
       ``_run(bin_path, "camera", "locate", ..., "--dry-run",
       env=env)`` calling convention exactly, so a genuinely dry
       ``camera locate --dry-run`` call (read-only, never writes) is
       allowed while a bare, write-risking ``camera locate`` is not.

    A malformed *source* (should never happen for a real, importable
    test module, since pytest would already have failed to collect it)
    propagates ``SyntaxError`` rather than being swallowed -- silently
    skipping the scan on unparseable input would defeat the guard's
    entire "structural, not conventional" premise.
    """
    tree = ast.parse(source, filename=filename)
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_ATTRIBUTE_CALLS:
            violations.append(
                f"{filename}:{node.lineno}: forbidden '.{node.attr}(...)' call -- "
                "Daemon.calibrate mutates real calibration state and is excluded "
                "from tests/acceptance/ by design (sprint 035 ticket 015's "
                "node-id allowlist)"
            )
        elif isinstance(node, ast.Call):
            call_strings = _iter_call_string_constants(node)
            if "locate" in call_strings and "--dry-run" not in call_strings:
                violations.append(
                    f"{filename}:{node.lineno}: forbidden non-dry 'locate' call -- "
                    "'camera locate' without '--dry-run' writes real "
                    "camera_position and is excluded from tests/acceptance/ by "
                    "design (sprint 035 ticket 015's node-id allowlist); add "
                    "'--dry-run' to this call or remove it"
                )
    return violations


# ---------------------------------------------------------------------------
# Terminal-summary hook
# ---------------------------------------------------------------------------


def _acceptance_reports(stats: Dict[str, List[Any]], status: str) -> List[Any]:
    return [
        report
        for report in stats.get(status, [])
        if report.nodeid.replace("\\", "/").startswith("tests/acceptance/")
    ]


def pytest_terminal_summary(
    terminalreporter: Any, exitstatus: int, config: pytest.Config
) -> None:
    """Human-readable PASS/FAIL/SKIP report for this run's acceptance
    tests, appended to pytest's normal terminal output.

    Filtered to nodeids under ``tests/acceptance/`` so a full-suite run
    that deselects this whole layer (``-m "not camera and not
    integration"``) does not print a misleading all-zero section.
    """
    del exitstatus, config  # unused -- required by the hook signature
    stats = terminalreporter.stats
    passed = _acceptance_reports(stats, "passed")
    failed = _acceptance_reports(stats, "failed") + _acceptance_reports(
        stats, "error"
    )
    skipped = _acceptance_reports(stats, "skipped")
    if not (passed or failed or skipped):
        return

    terminalreporter.write_sep("=", "acceptance layer summary")
    terminalreporter.write_line(f"result:  {'FAIL' if failed else 'PASS'}")
    terminalreporter.write_line(f"passed:  {len(passed)}")
    terminalreporter.write_line(f"failed:  {len(failed)}")
    terminalreporter.write_line(f"skipped: {len(skipped)}")
    for report in failed:
        terminalreporter.write_line(f"  FAIL {report.nodeid}")
    for report in skipped:
        longrepr = getattr(report, "longrepr", None)
        reason = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) >= 3 else ""
        suffix = f" -- {reason}" if reason else ""
        terminalreporter.write_line(f"  SKIP {report.nodeid}{suffix}")
