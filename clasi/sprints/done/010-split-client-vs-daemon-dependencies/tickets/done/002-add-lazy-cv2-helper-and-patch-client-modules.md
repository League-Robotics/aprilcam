---
id: '002'
title: Add lazy cv2 helper and patch client modules
status: done
use-cases:
- SUC-001
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: plan-split-client-vs-daemon-dependencies.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add lazy cv2 helper and patch client modules

## Description

Create `src/aprilcam/client/_imaging.py` with the `require_cv2()` lazy-import
helper. Remove the top-level `import cv2` from `client/control.py` and
`client/stream.py`, replacing each `cv2.imdecode` call with a `require_cv2()`
call. After this ticket, importing `aprilcam.client` in a base-install venv
succeeds; calling a frame-decode method raises a clear, actionable RuntimeError.

## Acceptance Criteria

- [x] `src/aprilcam/client/_imaging.py` exists and contains a `require_cv2()`
  function matching the specification in `architecture-update.md §2`.
- [x] `client/control.py` has no top-level `import cv2` statement.
- [x] `client/stream.py` has no top-level `import cv2` statement.
- [x] In a base-install venv (no OpenCV): `from aprilcam.client import DaemonControl`
  succeeds without error.
- [x] In a base-install venv: calling the frame-decode path raises `RuntimeError`
  with a message containing `` `aprilcam[imaging]` ``.
- [x] In a `[imaging]` or `[daemon]` venv: `capture_frame` and
  `ImageStreamConsumer.read` continue to work correctly (no regression).
- [x] `import numpy as np` is preserved in both `control.py` and `stream.py`
  (numpy stays in base and is used by `np.frombuffer`).

## Implementation Plan

### Approach

1. Create `src/aprilcam/client/_imaging.py`:

```python
"""Lazy OpenCV loader for the aprilcam client package.

Importing this module never requires OpenCV. Call ``require_cv2()`` at the
point of use; it returns the ``cv2`` module or raises RuntimeError with an
install hint.
"""


def require_cv2():
    """Return the cv2 module, raising RuntimeError if OpenCV is not installed."""
    try:
        import cv2  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Decoding camera frames requires OpenCV. "
            "Install it with `pip install 'aprilcam[imaging]'` "
            "(or `aprilcam[daemon]`)."
        ) from exc
    return cv2
```

2. Edit `src/aprilcam/client/control.py`:
   - Remove: `import cv2` (line 18)
   - Add import at top: `from aprilcam.client._imaging import require_cv2`
   - In `capture_frame` (approximately line 240), before the `cv2.imdecode` call,
     add: `cv2 = require_cv2()`

3. Edit `src/aprilcam/client/stream.py`:
   - Remove: `import cv2` (line 15)
   - Add import at top: `from aprilcam.client._imaging import require_cv2`
   - In `ImageStreamConsumer.read` (approximately line 117), before the
     `cv2.imdecode` call, add: `cv2 = require_cv2()`

### Files to Create

- `src/aprilcam/client/_imaging.py` (new)

### Files to Modify

- `src/aprilcam/client/control.py` — remove top-level import, add lazy call
- `src/aprilcam/client/stream.py` — remove top-level import, add lazy call

### Testing Plan

Manual verification in a temporary base venv (no OpenCV):
1. `python -c "from aprilcam.client import DaemonControl; print('import ok')"` — must succeed.
2. `python -c "from aprilcam.client._imaging import require_cv2; require_cv2()"` — must raise `RuntimeError` with `[imaging]` in the message.
3. `python -c "from aprilcam.client import DaemonControl; d = DaemonControl.__new__(DaemonControl); from aprilcam.client.control import capture_frame"` — verify the method exists.

In a `[daemon]` venv, run:
```
uv run pytest tests/test_client_models.py tests/test_daemon_control.py -v
```

No new unit tests are required; the acceptance criteria are verified by the
base-venv import checks and the existing test suite under `[daemon]`.

### Documentation Updates

None in this ticket.

### Commit Message

`feat(client): lazy-load cv2 via require_cv2() helper [010-002]`
