---
status: pending
---

# The `aprilcam` CLI fails to import on a headless host without tkinter

## Description

2026-11-01, donald (Ubuntu Server 24.04, Python 3.12, no `python3-tk`):
every `aprilcam` command, even `--version`, dies at import:

```
File ".../aprilcam/cli/intrinsics.py", line 34, in <module>
    from aprilcam.view.viewer import NoDisplayAvailable, _headless_reason
File ".../aprilcam/view/viewer.py", line 136, in <module>
    import tkinter as tk
ModuleNotFoundError: No module named 'tkinter'
```

`view/viewer.py` imports tkinter at module scope (since ab37e5f), and
`cli/intrinsics.py` imports the viewer at module scope, so the whole CLI
needs tkinter. The daemon is unaffected (it does not import the CLI), and
buzzkill happens to have `python3-tk`.

### Needed

- Import tkinter lazily (inside the GUI paths), and have the viewer
  report `NoDisplayAvailable` with a clear "install python3-tk" message
  when it is missing, so headless CLI commands (`--version`, `camera
  list`, `camera image`, `daemon list`, ...) work without tkinter.
- A test that imports `aprilcam.cli` with `tkinter` made unimportable.
