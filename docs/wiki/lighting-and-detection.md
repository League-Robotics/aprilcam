---
title: Tag Detection Under Variable Lighting
blurb: Why tags drop out under glare and low contrast, and the multi-scale preprocessing pipeline that recovers 11/13 tags.
order: 60
updated: 2026-06-20
tags: [detection, lighting, opencv, tuning]
---

# Tag Detection Under Variable Lighting

## Problem

The ArduCam 9782 color camera at 1280x800 struggles to detect all
AprilTag 36h11 and ArUco 4x4 markers on the playfield under normal
room lighting. The dark playfield surface creates several challenges:

- **Glare/overexposure**: Tags near the playfield edges or under
  direct overhead light get washed out — the white tag border bleeds
  into the data cells, destroying the bit pattern.
- **Low contrast**: Interior tags appear as faint grey-on-darker-grey,
  with the tag border barely distinguishable from the background.
- **Uneven illumination**: The center of the playfield may be darker
  than edges, or vice versa, making a single threshold fail.

## Findings

### Baseline (raw grayscale, default detector)

Only **3/13** tags detected — the 3 largest/highest-contrast AprilTags.
ArUco corners often fail completely.

### Best Single Pipeline

**High-pass filter + CLAHE** at native resolution: **7/13** tags.

```python
gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
blur = cv2.GaussianBlur(gray, (51, 51), 0)
hp = cv2.subtract(gray, blur)
hp = cv2.add(hp, 128)
result = cv2.createCLAHE(3.0, (8, 8)).apply(hp)
```

The high-pass filter removes the low-frequency illumination gradient,
and CLAHE normalizes local contrast.

### Multi-Scale Union Pipeline: 11/13

The winning approach runs multiple preprocessing strategies at
different scales and takes the union of all detections:

| Strategy               | Scale | Finds                       |
|------------------------|-------|-----------------------------|
| highpass + CLAHE       | 1x    | ArUco 0,1,2,3 + AT 4,6,7   |
| highpass + CLAHE       | 1.5x  | AT 4,6,7 (redundant)        |
| CLAHE (clip=4)         | 2x    | AT 9                        |
| Histogram equalize     | 2x    | AT 11                       |
| Strong CLAHE + highpass| 3x    | AT 9, 10                    |
| Tiled local detection  | 4x    | AT 3                        |

**Union total: 11/13 — all 4 ArUco + AprilTags 3,4,6,7,9,10,11**

### Tags That Cannot Be Detected: 5 and 8

- **Tag 8** (interior, left side): Completely blown out by glare. At
  5x upscale with maximum CLAHE, the tag appears as a solid white
  square — the black data cells have been overexposed to the point
  where no contrast remains. This is a **sensor saturation problem**,
  not a software problem. No preprocessing pipeline can recover data
  that was never captured.

- **Tag 5** (top-left edge): Borderline case. Sometimes detectable
  with very aggressive CLAHE (clip=12) at 3-4x upscale on a local
  crop, depending on exact lighting conditions. Edge glare from the
  playfield border reduces contrast.

### Why Different Strategies Find Different Tags

Each preprocessing approach has a different operating point on the
contrast/noise tradeoff:

- **High-pass filter** removes illumination gradients but amplifies
  local noise and can reduce the effective contrast of small tags.
- **CLAHE** enhances local contrast but with low clip limits, areas
  with already-high contrast (edges) dominate while low-contrast
  interior tags remain invisible.
- **Histogram equalize** spreads the full intensity range, sometimes
  revealing tags that CLAHE misses because it operates on a different
  part of the histogram.
- **Upscaling** makes small tags larger so the detector's adaptive
  threshold has more pixels to work with, improving decodability.
- **Tiled local detection** allows equalize/CLAHE to adapt to each
  tag's local intensity range rather than being dominated by the
  global image statistics.

### Detector Parameter Tuning

The most impactful parameter change:

```python
params = cv2.aruco.DetectorParameters()
params.adaptiveThreshWinSizeMin = 3
params.adaptiveThreshWinSizeMax = 53   # default 23
params.adaptiveThreshWinSizeStep = 4   # default 10
```

Increasing `adaptiveThreshWinSizeMax` and decreasing `Step` runs more
threshold passes, catching tags at different contrast levels. This is
more effective than relaxing geometric thresholds.

## Recommendations

### For Real-Time Detection (30fps)

Use highpass + CLAHE at native resolution. Gets 7/13 — sufficient for
tracking when tags are re-acquired from the ring buffer.

### For Reliable Detection (calibration, initial setup)

Use the multi-scale union pipeline. Takes ~10-15 seconds per frame
but finds 11/13 tags with zero false positives.

### Physical Improvements

1. **Diffuse the lighting**: The biggest improvement would come from
   reducing specular reflection on the playfield surface. A diffuser
   sheet or indirect lighting eliminates the glare that destroys tags
   5 and 8.

2. **Print tags larger**: At 31px per side, the interior tags are at
   the lower limit of reliable detection. Doubling tag size would
   significantly improve detection without any software changes.

3. **Use matte tag prints**: Glossy paper reflects light directly back
   at the camera. Matte paper scatters it, maintaining contrast.

4. **Consider tag placement**: Tags near the playfield edge are
   affected by edge glare. Moving them 5cm inward may help.

## Test Script

Run the detection test:

```bash
# Against saved sample frame
python testdev/detect_test.py

# Against live camera
python testdev/detect_test.py --camera 2

# Save debug images
python testdev/detect_test.py --camera 2 --save
```

The test passes if 12/13 or more tags are found with 0 false positives.
Currently achieves 11/13 with the sample frame under normal lighting.
