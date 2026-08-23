---
status: pending
---

# `daemon start` should fail loudly when no capture backend is available, instead of running camera-less

## Description

Observed twice on 2026-08-23: `aprilcam daemon restart` spawned the
pipx-installed build, whose venv lacks the `[daemon]` extra (no
OpenCV). The daemon started "successfully", printed
"daemon restarted (5 camera(s))" (a registry count, not live devices),
reported `running` in `daemon status`, and enumerated **zero** physical
cameras — every camera `absent`/`unusable` — with no hint that the
process simply has no capture backend. The operator-visible failure
(dead views, absent cameras) is far downstream of the actual problem.

Two layers worth fixing, either or both:

1. **Fail loudly**: if the daemon process cannot import its capture
   backend (cv2), `daemon start` should refuse to start (or start and
   print a prominent warning naming the missing extra and the install
   command), rather than running camera-less. "5 camera(s)" from the
   registry when live enumeration found 0 is exactly the misleading
   shape to avoid.
2. **Start the right build**: `daemon start`/`restart` execs whatever
   `aprilcam` resolves on PATH. On a dev host where the repo venv is
   the real daemon environment, consider having start/restart re-exec
   the daemon with the same interpreter that owns the daemon deps, or
   at least print which interpreter/venv the spawned daemon runs from
   so a mismatch is visible in one line.

Operational workaround documented in
`docs/knowledge/daemon-restart-launches-pipx-build-no-cameras.md`.
