---
status: done
---

# `aprilcam tags` crashes on base install with `ModuleNotFoundError: No module named 'cv2'`

## Summary

`aprilcam tags` is a **thin daemon client** — its docstring states "No
local `cv.VideoCapture` is opened — the daemon is the sole camera
owner." Yet on the base pipx install (which deliberately excludes
OpenCV per the Vision Boundary), *every* invocation crashes before it
does anything useful:

```
$ aprilcam tags --help
...
  File ".../aprilcam/cli/tags_cli.py", line 67, in main
    from ..camera.camutil import list_cameras, select_camera_by_pattern
  File ".../aprilcam/camera/camutil.py", line 13, in <module>
    import cv2 as cv
ModuleNotFoundError: No module named 'cv2'
```

Even `aprilcam tags --help` fails, because the import runs before
argparse.

## Root causes

1. **Eager import.** `cli/tags_cli.py` line 67 does
   `from ..camera.camutil import list_cameras, select_camera_by_pattern`
   at the top of `main()`, so it executes unconditionally — for
   `--help`, for alpha-code camera args (`A`, `FB`), and for numeric
   camera args — none of which actually use those symbols. `camutil`
   does `import cv2 as cv` at module top ([camera/camutil.py:13]),
   so pulling anything from it requires OpenCV.

2. **Vision-Boundary violation on the name-pattern path.** The one
   branch that *does* use `camutil` — resolving a non-numeric name
   pattern ([cli/tags_cli.py:141]) — calls local `list_cameras()`,
   which enumerates devices via OpenCV. A thin client must not open or
   probe local cameras; the daemon owns that. The alpha-code branch
   already resolves through `dc.enumerate_cameras()`
   ([cli/tags_cli.py:104]); the name-pattern branch should do the same.

## Proper fix

- Move the `camutil` import out of the unconditional top of `main()` so
  `--help` / alpha-code / numeric paths never touch `cv2`.
- Resolve name patterns through the daemon
  (`dc.enumerate_cameras()` + a slug/name match) instead of local
  `list_cameras()` / `select_camera_by_pattern()`, mirroring the
  alpha-code path. This removes the last OpenCV dependency from the
  command entirely — no lazy import needed either.
- Net result: `aprilcam tags` works on the base (OpenCV-free) install,
  as the architecture intends.

## Workaround (applied)

`pipx inject aprilcam opencv-contrib-python` — makes the command work
today but defeats the point of the OpenCV-free base install.

## Related (out of scope for this issue)

`cli/calibrate_cli.py` imports `cv2` at module top (line 19) and
genuinely decodes daemon JPEG frames + runs `cv.calibrateCamera`
locally. That's a separate, larger Vision-Boundary question (does
`calibrate` legitimately need local CV, or should it move into the
daemon?) and is **not** addressed here.
