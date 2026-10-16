# ChArUco intrinsics prototype (2026-10-15)

> **Superseded (sprint 044).** The daemon workflow replaces this prototype:
> run `aprilcam camera intrinsics <camera...>` (see
> `docs/knowledge/2026-10-15-charuco-intrinsics-calibration-lessons.md`).
> Kept as evidence only; do not run it against a current daemon.

Out-of-process prototype that produced the first measured lens models for
cameras 2, 3 and 4. **Evidence and ideas, not code to port** -- it runs
OpenCV in a client process, which violates the vision boundary; the real
implementation lives in the daemon (see the issue
`clasi/issues/charuco-board-intrinsics-calibration-as-a-daemon-workflow.md`).

- `charuco_cal.py` -- detection pipeline, quality gates, session store, offline solve.
- `charuco_tui.py` -- first checklist TUI (superseded) plus `solve_full`/`converged`.
- `charuco_fast.py` -- background solver, convergence, `install()` into calibration.json.
- `charuco_gui.py` -- the fullscreen Tk GUI the stakeholder accepted; the
  "good enough" metric (`mapping_residual_px`) and the NEXT-hint logic live here.

Run (pipx venv has cv2 + aprilcam client):
`~/.local/pipx/venvs/aprilcam/bin/python charuco_gui.py --cameras 4 --board 5x7:37:28:DICT_5X5_100 --install`
