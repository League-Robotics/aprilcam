---
id: '003'
title: Add pytest skip markers for optional dependencies
status: done
use-cases:
- SUC-005
depends-on:
- '002'
github-issue: ''
issue: plan-split-client-vs-daemon-dependencies.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add pytest skip markers for optional dependencies

## Description

After ticket 001 narrows the base install, running `pytest` in a base-only venv
will encounter collection errors because many test files have top-level imports
from `aprilcam.daemon` (which requires OpenCV and the full daemon stack) or use
`cv2` directly. This ticket makes the test suite green in a base-install venv by
adding skip markers and conftest logic, without removing any existing test coverage.

## Acceptance Criteria

- [x] `tests/conftest.py` exists and defines:
  - A `needs_cv2` pytest mark that skips the test if `cv2` is not importable,
    with reason "requires aprilcam[imaging]".
  - A `needs_daemon` pytest mark that skips the test if `aprilcam.daemon` modules
    are not importable (i.e., OpenCV / daemon deps absent), with reason
    "requires aprilcam[daemon]".
  - The marks are registered via `pytest_configure` to avoid `PytestUnknownMarkWarning`.
- [x] All test files that have a top-level `from aprilcam.daemon import ...` or
  `import aprilcam.daemon...` are either:
  (a) decorated with `@pytest.mark.needs_daemon` at the module or class/function
      level, or
  (b) guard their top-level daemon imports with a `try/except ImportError` and
      use `pytest.importorskip` or `pytestmark = pytest.mark.needs_daemon`.
- [x] `tests/test_stream_consumers.py` tests using `cv2` directly are marked with
  `@pytest.mark.needs_cv2` or wrapped so they skip gracefully.
- [x] `pytest` exits 0 in a base-install venv. The output shows daemon tests as
  SKIPPED, not as errors.
- [x] `pytest` exits 0 in a `[daemon]`-install venv. All previously-passing tests
  still pass (no regressions).

## Implementation Plan

### Approach

Create `tests/conftest.py` (currently absent) with:

```python
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "needs_cv2: skip test if opencv-contrib-python is not installed",
    )
    config.addinivalue_line(
        "markers",
        "needs_daemon: skip test if aprilcam[daemon] dependencies are not installed",
    )


def _cv2_available() -> bool:
    try:
        import cv2  # noqa: F401, PLC0415
        return True
    except ModuleNotFoundError:
        return False


def _daemon_available() -> bool:
    try:
        import cv2  # noqa: F401, PLC0415
        return True
    except ModuleNotFoundError:
        return False


# Session-scoped skip fixtures (alternative to decorators; use decorators where
# tests have top-level imports that would cause collection errors)
```

For test files with **top-level** daemon imports (which cause collection errors
before any marker is evaluated), use module-level `pytestmark` combined with
`pytest.importorskip` at the top of the file, e.g.:

```python
cv2 = pytest.importorskip("cv2", reason="requires aprilcam[imaging]")
```

or for daemon deps:

```python
pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")
```

placed before the actual import of the daemon module.

### Files to Create

- `tests/conftest.py` (new)

### Files to Modify

The following test files have top-level daemon imports and must be guarded:

- `tests/test_daemon_control.py` — `from aprilcam.daemon.grpc_server import ...`
- `tests/test_daemon_protocol.py` — `from aprilcam.daemon.protocol import ...`
- `tests/test_daemon_stream.py` — `from aprilcam.daemon.stream import ...`
- `tests/test_grpc_servicer.py` — `from aprilcam.daemon.grpc_server import ...`
- `tests/test_grpc_smoke.py` — `from aprilcam.daemon.server import ...`
- `tests/test_grpc_reflection.py` — `from aprilcam.daemon.grpc_server import ...`
- `tests/test_daemon_spawn_race.py` — `from aprilcam.daemon import client ...`
- `tests/test_daemon_server.py` — `from aprilcam.daemon.server import ...`
- `tests/test_daemon_backpressure.py` — inspect and guard if needed
- `tests/test_mdns_advertiser.py` — inspect; imports are inside functions (may be fine)
- `tests/test_stream_consumers.py` — `import cv2` inside test functions (needs
  `@pytest.mark.needs_cv2` or `pytest.importorskip` guard)

Files known to be safe on a base install (no daemon/cv2 imports):
- `tests/test_client_models.py` — pure pydantic, no changes needed
- `tests/test_config_loader.py` — inspect; likely safe
- `tests/test_playfield_homography.py` — uses numpy/cv2; needs guard
- `tests/test_calibration_parallax.py` — likely cv2 dep; needs guard
- `tests/test_draw_paths.py`, `tests/test_paths.py`, `tests/test_mcp_path_tools.py` — inspect

The programmer should do a final audit with `python -m pytest --collect-only` in a
base-install venv to catch any remaining collection errors before marking complete.

### Testing Plan

Primary verification:

1. In a base-install venv (only `pip install -e .`):
   ```
   pytest --tb=short -q
   ```
   Expected: all daemon/cv2 tests show as SKIPPED; `test_client_models.py` passes;
   exit code 0.

2. In a `[daemon]`-install venv:
   ```
   pytest --tb=short -q
   ```
   Expected: full test suite runs; exit code 0; no regressions from daemon tests.

### Documentation Updates

None. The skip markers are self-documenting via their reason strings.

### Commit Message

`test: add needs_cv2 and needs_daemon skip markers for optional deps [010-003]`
