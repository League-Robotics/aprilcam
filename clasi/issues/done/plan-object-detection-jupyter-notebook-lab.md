---
status: pending
---

# Plan: Object Detection Jupyter Notebook Lab

## Context

The live-view object detector is producing too many false positives (wood grain,
robot, strobing-induced color shifts). The strobing issue is key: LED/fluorescent
lighting cycles cause per-frame brightness variation that throws off absolute HSV
thresholds. We need a controlled experiment to find a robust detection method.

The approach: capture a burst of frames from camera 4 to sample across the strobe
cycle, store them locally, then work through detection strategies interactively in
a Jupyter notebook where Claude's vision can annotate ground-truth box locations and
the CV code can be tuned against them.

## What Gets Built

### Directory
```
tests/object_detection/
├── capture.py          # one-shot script to grab N frames from camera 4
├── frame_00.jpg        # 10–15 frames captured in quick burst
├── frame_01.jpg
├── ...
├── 01_explore.ipynb    # exploration: load frames, vision-annotate boxes, measure HSV variance
└── 02_detector.ipynb   # build & tune the final detector, output updated ColorClassifier params
```

### capture.py
- Uses `sys.path.insert(0, "../../src")` so imports work when run from the directory
- Opens camera 4 via `DaemonControl`, captures 12 frames as fast as possible (no sleep)
- Saves as `frame_00.jpg` … `frame_11.jpg` using relative paths
- Run once by the user before opening notebooks: `cd tests/object_detection && python capture.py`

### 01_explore.ipynb — Exploration notebook
Cells:
1. **Setup** — `sys.path.insert(0, "../../src")`, imports, load all `frame_*.jpg` as BGR arrays
2. **Vision annotation** — Call Claude API (`claude-sonnet-4-6`) with each frame; ask it to return JSON `[{color, cx, cy}]` for each colored box it sees. Store as ground-truth.
3. **Strobe analysis** — At each ground-truth (cx, cy) location, sample a 20×20 px ROI across all frames. Plot H, S, V distributions to quantify the strobe-induced variance.
4. **Wood grain comparison** — Same ROI sampling at several non-box locations. Compare distributions to understand overlap.
5. **Summary table** — For each color, show median HSV ± std across all frames.

### 02_detector.ipynb — Detector tuning notebook
Cells:
1. **Setup** — same path setup, load frames + ground truth from 01
2. **Approach A: Adaptive HSV** — normalize each frame's V channel before thresholding (removes strobe effect). Test against all frames.
3. **Approach B: Saturation-weighted voting** — across N frames, accumulate detections; require a location to appear in ≥ K frames to be accepted (strobe robustness via temporal consistency).
4. **Approach C: Per-frame normalization + tighter HSV** — divide each channel by a per-frame median to make thresholds lighting-independent.
5. **Scoring** — For each approach, score precision/recall vs. ground truth boxes. Print winner.
6. **Output** — Write the winning `DEFAULT_COLOR_RANGES` dict and any pre-processing steps as a code cell that can be copy-pasted into `color_classifier.py`.

## Critical Files

- `src/aprilcam/vision/color_classifier.py` — `ColorClassifier`, `DEFAULT_COLOR_RANGES`
- `src/aprilcam/vision/objects.py` — `SquareDetector`, `ObjectRecord`
- `src/aprilcam/client/control.py` — `DaemonControl.capture_frame(cam_name) -> np.ndarray`
- `src/aprilcam/config.py` — `Config.load()`
- `src/aprilcam/cli/view_cli.py` — live view detection path to update once tuning is done

## Notebook Path Convention

All file I/O in notebooks uses paths relative to the notebook file:
```python
import pathlib
HERE = pathlib.Path(__file__).parent  # in scripts
# In notebooks:
HERE = pathlib.Path().resolve()  # works when cwd = notebook dir
frames = sorted(HERE.glob("frame_*.jpg"))
```
No `/tmp`, no absolute paths outside `tests/object_detection/`.

## Strobe-Robustness Strategy (key insight)

Capturing 12 frames in a burst samples the full strobe cycle (~2 cycles at 60 fps).
The boxes will be at constant positions but their HSV values will fluctuate.
The approach with highest expected robustness: **per-frame V-channel normalization**
before HSV thresholding. Divide V by `np.percentile(V, 90)` to make exposure-independent.
This should be tested in 02_detector and, if it wins, implemented in `ColorClassifier.classify()`.

## Verification

1. Run `capture.py` to populate frames
2. Run 01_explore end-to-end; confirm vision annotation finds all 4 boxes
3. Run 02_detector end-to-end; confirm winning approach gets precision ≥ 0.8, recall = 1.0
4. Copy winning params into `color_classifier.py`
5. Reopen live view, click Objects: On — only colored boxes should appear
