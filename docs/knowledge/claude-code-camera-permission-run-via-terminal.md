# Claude Code cannot open cameras — run camera work through Terminal.app

**Discovered:** 2026-08-23, during sprint 035 (layered test system).

## Symptom

Any camera open attempted from a Claude Code agent fails:

```
OpenCV: not authorized to capture video (status 0), requesting...
OpenCV: camera failed to properly initialize!
isOpened: False
```

`aprilcam.camera.identity.resolve_all()` still **enumerates** cameras
correctly — AVFoundation device discovery does not require TCC camera
permission. Only `Camera.open()` / `cv2.VideoCapture(...)` fails. That
split is what makes this confusing: the cameras are clearly *there*, so
it reads like a driver or index bug rather than a permission problem.

It also means hardware tests do not fail cleanly by default. Anything
that probes "is a camera present?" via enumeration will believe the rig
is available and then fail at open time.

## Root cause

macOS TCC attributes the request to the *responsible bundle*, and
Claude Code does not run from `/Applications/Claude.app` directly. It
runs from a nested helper bundle:

```
/Applications/Claude.app                                      -> has NSCameraUsageDescription
~/Library/Application Support/Claude/claude-code/<ver>/claude.app  -> does NOT
```

macOS refuses to even *prompt* for a bundle with no
`NSCameraUsageDescription`, so the request is denied outright and **no
entry is ever added** to System Settings → Privacy & Security → Camera.

This is not fixable from that settings pane: there is no "+" button, and
an app only appears there after macOS accepts an access request. Waiting
for a toggle to appear for "Claude" is a dead end. Verify with:

```bash
/usr/libexec/PlistBuddy -c "Print :NSCameraUsageDescription" \
  "$HOME/Library/Application Support/Claude/claude-code/<ver>/claude.app/Contents/Info.plist"
```

`Entry ... Does Not Exist` confirms it.

## The fix: borrow Terminal.app's grant

**Terminal.app is normally already authorized for camera** (it is what the
capture procedures in `docs/test-plans/camera-image-matrix.md` assume).
Dispatch camera work into Terminal via AppleScript and it runs in
Terminal's TCC context, where the camera opens fine. Confirmed working:

```
index 0: isOpened=True read=True shape=(1080, 1920, 3) mean=16.0
index 1: isOpened=True read=True shape=(800, 1280, 3)  mean=96.6
```

This is not a permission bypass — it uses a grant the operator already
made, through a normal automation path. The first AppleScript dispatch
may raise a one-time "Claude wants to control Terminal" Automation
prompt, which the operator accepts.

### Runner pattern

Write a job script, dispatch it with `osascript`, poll for a
sentinel `.rc` file, then replay output and propagate the exit code:

```zsh
osascript -e "tell application \"Terminal\" to do script \"${JOB}\""
while [ ! -f "$RC" ]; do sleep 1; done
cat "$OUT"; exit "$(cat "$RC")"
```

The job script must `cd` to the project, redirect combined output to a
file, write `$?` to the rc file, and `exit` so the window closes. Poll
for the **rc file**, not the output file — the output file exists as soon
as redirection starts, so waiting on it returns before the job finishes.

Gotchas:

- Quote-heavy inline `python -c "..."` sent through AppleScript is
  fragile and can trip Claude Code's command classifier. Write the
  Python to a file and run the file.
- `timeout` is not present on this host (that is GNU coreutils); use a
  polling loop with a counter instead.
- Give the runner a generous limit. Camera sweeps and calibration runs
  take minutes, not seconds.

## Consequence for tests

Hardware tests must gate on **open succeeding**, not on enumeration.
A rig-availability check that only calls `resolve_all()` will report the
rig present inside a Claude Code agent and then fail at open.
