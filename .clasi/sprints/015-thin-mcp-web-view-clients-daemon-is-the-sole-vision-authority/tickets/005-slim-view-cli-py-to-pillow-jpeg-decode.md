---
id: "005"
title: "Slim view_cli.py to Pillow JPEG decode"
status: open
use-cases: [SUC-005]
depends-on: []
github-issue: ""
issue: ""
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Slim view_cli.py to Pillow JPEG decode

## Description

Remove all `cv2` usage from `src/aprilcam/cli/view_cli.py`. There are three cv2
import sites to eliminate:

1. Line ~120: `import cv2 as _cv` inside `_draw_object_boxes()` — used for
   `cv.rectangle` and `cv.putText` on a numpy frame. Replace with Pillow
   `ImageDraw` or remove the function if objects are no longer drawn on the frame
   in `view_cli` (they will now come from `GetObjects` RPC as structured dicts,
   not from a numpy frame in the view process).

2. Line ~223: `cv.imdecode(buf, cv.IMREAD_COLOR)` — used to decode JPEG bytes
   from the stream. Replace with `Image.open(BytesIO(jpeg_bytes))` (Pillow).

3. Line ~374: `import cv2 as cv` — another import site. Remove it.

Pillow (`pillow>=10.0`) is a base dependency after ticket 006, but it is already
present in the `imaging` extra. This ticket can proceed independently of ticket 006
(Pillow is already installed in the dev environment).

## Acceptance Criteria

- [ ] `view_cli.py` contains zero occurrences of `import cv2`, `from cv2`, or
  `import cv2 as`.
- [ ] JPEG bytes from `ImageStreamConsumer` are decoded with
  `Image.open(io.BytesIO(jpeg_bytes))` and converted to `ImageTk.PhotoImage`.
- [ ] `_draw_object_boxes()` either:
  - Uses `ImageDraw.rectangle()` and `ImageDraw.text()` on a Pillow `Image`, or
  - Is removed (if the object-box overlay is superseded by the daemon overlay channel).
- [ ] `import aprilcam.cli.view_cli` succeeds with cv2 monkeypatched to raise
  `ImportError`.
- [ ] Any `numpy` imports that remain in `view_cli.py` exist only for non-cv2
  computation (e.g., corner coordinate math). Comment each with `# numpy-only`.
- [ ] `uv run pytest` green.

## Implementation Plan

### Step 1: Replace JPEG decode (~line 223)

Find the `cv.imdecode` call that decodes a JPEG buffer into a numpy array for
tkinter display. Replace:

```python
# Old:
import cv2 as cv
arr = cv.imdecode(np.frombuffer(buf, dtype=np.uint8), cv.IMREAD_COLOR)
img = cv.cvtColor(arr, cv.COLOR_BGR2RGB)
pil_img = PIL.Image.fromarray(img)

# New:
from io import BytesIO
from PIL import Image
pil_img = Image.open(BytesIO(buf))
# pil_img is already RGB when decoded from JPEG
```

Then convert `pil_img` to `ImageTk.PhotoImage` as before.

### Step 2: Remove or rewrite `_draw_object_boxes()` (~line 119)

The function currently draws colored rectangles on a numpy array using
`cv.rectangle` and `cv.putText`. After this sprint:
- Objects come from `GetObjects` RPC via `DaemonStreamEntry` / structured dicts,
  not from a numpy frame in the view process.
- The view displays the JPEG from the daemon's image stream (already has overlays
  if the daemon renders them) or a clean frame.

Decision: if the view is not responsible for rendering object boxes (the daemon's
tag stream already carries overlay frames), remove `_draw_object_boxes` entirely.
If the view must render boxes client-side, rewrite using Pillow `ImageDraw`:

```python
from PIL import ImageDraw

def _draw_object_boxes(img: "PIL.Image.Image", objects: list) -> None:
    draw = ImageDraw.Draw(img)
    for obj in objects:
        x, y, w, h = obj["bbox"]
        color_name = obj.get("color", "gray")
        rgb = _OBJ_RGB.get(color_name, (180, 180, 180))
        draw.rectangle([x, y, x + w, y + h], outline=rgb, width=2)
        label = color_name
        if obj.get("world_xy"):
            wx, wy = obj["world_xy"]
            label += f" ({wx:.0f},{wy:.0f})"
        draw.text((x, max(y - 14, 0)), label, fill=rgb)
```

Replace `_OBJ_BGR` dict (BGR tuples) with `_OBJ_RGB` (RGB tuples).

### Step 3: Remove cv2 import at line ~374

Find `import cv2 as cv` later in the file. This is likely in a helper for frame
annotation or tag overlay rendering. After steps 1-2, if the only usage was
`cv2.imdecode` (now using Pillow), remove this import entirely. If the import
serves other functions, rewrite those functions to use Pillow.

### Step 4: Audit numpy

Run `grep -n "np\." view_cli.py` after removing cv2. Any remaining numpy usage
must be for pure array/matrix math (not frame pixel operations). Add a
`# numpy-only` comment at each site to document the intent.

If numpy is no longer used at all, remove `import numpy as np`.

### Testing

- `tests/test_015_005_view_cli_pillow.py`: monkeypatch `__import__` so `import cv2`
  raises `ImportError`; assert `import aprilcam.cli.view_cli` succeeds.
- Existing view CLI tests: run and fix any that depended on cv2 being available.

### Files to modify/create

- `src/aprilcam/cli/view_cli.py` — remove cv2 imports and usages; replace with Pillow
- `tests/test_015_005_view_cli_pillow.py` — new test file (cv2-block import test)
