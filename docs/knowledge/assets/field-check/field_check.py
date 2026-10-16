#!/usr/bin/env python
"""Out-of-process field check: detect every tag in a raw frame, compare the
ArUco layout against the playfield definition, and write an annotated image.

Usage:
  uv run python field_check.py FRAME.png DEFINITION.json OUT.png [--calibration calibration.json]

Reports: ids seen / missing / stray / duplicated, marker side in px, marker
orientation (rotation of the top edge vs. image-up), a least-squares
pixel->world homography over the surveyed markers with per-marker residual
(cm) and a leave-one-out residual, and the AprilTag centre position in that
frame.
"""
from __future__ import annotations

import argparse
import json
import math
import sys

import cv2
import numpy as np

from aprilcam.vision.detector import TagDetector
from aprilcam.types import TagFamily


def load_lens(path):
    if not path:
        return None, None
    d = json.load(open(path))
    K = d.get("camera_matrix")
    D = d.get("dist_coeffs")
    if K is None or D is None:
        return None, None
    return np.array(K, dtype=np.float64), np.array(D, dtype=np.float64)


def undistort(pts, K, D):
    if K is None:
        return pts
    p = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.undistortPoints(p, K, D, P=K).reshape(-1, 2)


def homography_fit(pix, world):
    H, _ = cv2.findHomography(pix, world, method=0)
    return H


def apply_h(H, pts):
    p = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(p, H).reshape(-1, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frame")
    ap.add_argument("definition")
    ap.add_argument("out")
    ap.add_argument("--calibration", default=None)
    args = ap.parse_args()

    img = cv2.imread(args.frame)
    if img is None:
        sys.exit(f"cannot read {args.frame}")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    print(f"frame {w}x{h}")

    defn = json.load(open(args.definition))
    surveyed = {int(t["id"]): (float(t["x"]), float(t["y"]), t.get("cardinal", "")) for t in defn["aruco_tags"]}
    april_surveyed = {int(t["id"]): (float(t["x"]), float(t["y"])) for t in defn.get("april_tags", [])}

    K, D = load_lens(args.calibration)
    print("lens model:", "yes" if K is not None else "none (raw pixels)")

    det = TagDetector()
    # plain grayscale, plus a CLAHE pass merged in if it finds more
    dets = det.detect(gray)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    dets2 = det.detect(clahe)
    seen = {(d.family, d.number) for d in dets}
    extra = [d for d in dets2 if (d.family, d.number) not in seen]
    if extra:
        print(f"CLAHE pass found {len(extra)} more tag(s): {[(d.family.name, d.number) for d in extra]}")
        dets = dets + extra

    aruco = {}
    dup = []
    for d in dets:
        if d.family != TagFamily.ARUCO:
            continue
        if d.number in aruco:
            dup.append(d.number)
            # keep the first, but note the second position
            aruco.setdefault(("dup", d.number), []).append(d)
            continue
        aruco[d.number] = d
    april = {d.number: d for d in dets if d.family == TagFamily.APRILTAG}

    ids_seen = sorted(k for k in aruco if isinstance(k, int))
    missing = sorted(i for i in surveyed if i not in aruco)
    stray = sorted(i for i in ids_seen if i not in surveyed)
    print(f"\nArUco seen: {ids_seen}")
    print(f"missing (in definition, not seen): {missing}")
    print(f"stray (seen, not in definition): {stray}")
    print(f"duplicate ids: {sorted(set(dup))}")
    print(f"AprilTags seen: {sorted(april)} (definition expects {sorted(april_surveyed)})")

    # per-marker geometry
    print("\nid  cardinal    centre(px)        side(px)  rot(deg)  world(cm)")
    rows = []
    for i in sorted(surveyed):
        if i not in aruco:
            print(f"{i:>2}  {surveyed[i][2]:<10}  MISSING")
            continue
        d = aruco[i]
        c = d.corners
        side = float(np.mean([np.linalg.norm(c[k] - c[(k + 1) % 4]) for k in range(4)]))
        # OpenCV ArUco corner order: top-left, top-right, bottom-right, bottom-left (marker frame)
        top = c[1] - c[0]
        rot = math.degrees(math.atan2(top[1], top[0]))  # 0 = top edge points image-right (marker upright)
        rows.append((i, d))
        print(f"{i:>2}  {surveyed[i][2]:<10}  ({d.center.x:7.1f},{d.center.y:7.1f})  {side:6.1f}   {rot:6.1f}   ({surveyed[i][0]:.1f},{surveyed[i][1]:.1f})")

    # homography over surveyed+seen markers (centres), leave-one-out residual
    if len(rows) >= 4:
        pix = np.array([[d.center.x, d.center.y] for _, d in rows])
        pix_u = undistort(pix, K, D)
        world = np.array([[surveyed[i][0], surveyed[i][1]] for i, _ in rows])
        H = homography_fit(pix_u, world)
        proj = apply_h(H, pix_u)
        res = np.linalg.norm(proj - world, axis=1)
        print(f"\nhomography over {len(rows)} markers: rms residual {math.sqrt(float(np.mean(res**2))):.2f} cm, max {res.max():.2f} cm")
        print("id   residual(cm)   leave-one-out(cm)   -> flags")
        for k, (i, d) in enumerate(rows):
            mask = np.ones(len(rows), dtype=bool)
            mask[k] = False
            if mask.sum() >= 4:
                Hk = homography_fit(pix_u[mask], world[mask])
                loo = float(np.linalg.norm(apply_h(Hk, pix_u[k : k + 1])[0] - world[k]))
            else:
                loo = float("nan")
            flag = ""
            if loo > 2.0:
                flag = "  <-- likely misplaced"
            elif loo > 1.0:
                flag = "  <-- check"
            print(f"{i:>2}   {res[k]:8.2f}        {loo:8.2f}{flag}")
        # where does the AprilTag centre land?
        for aid, d in april.items():
            p = undistort(np.array([[d.center.x, d.center.y]]), K, D)
            wpt = apply_h(H, p)[0]
            exp = april_surveyed.get(aid)
            s = f"AprilTag {aid}: world ({wpt[0]:.1f}, {wpt[1]:.1f}) cm"
            if exp:
                s += f", expected {exp}, off by {np.linalg.norm(wpt - np.array(exp)):.1f} cm"
            print(s)
        # scale check: marker side in cm implied by the homography
        print("\nimplied marker side (cm) from homography, per marker:")
        for i, d in rows:
            cu = undistort(d.corners, K, D)
            cw = apply_h(H, cu)
            sides = [np.linalg.norm(cw[k] - cw[(k + 1) % 4]) for k in range(4)]
            print(f"{i:>2}: {np.mean(sides):.2f} cm  (corner-to-corner spread {max(sides)-min(sides):.2f})")
    else:
        print("\nfewer than 4 surveyed markers seen; no homography")

    # annotate
    out = img.copy()
    for d in dets:
        c = d.corners.astype(int)
        col = (0, 200, 0) if d.family == TagFamily.ARUCO else (255, 120, 0)
        if d.family == TagFamily.ARUCO and d.number not in surveyed:
            col = (0, 0, 255)
        cv2.polylines(out, [c.reshape(-1, 1, 2)], True, col, 2)
        cv2.circle(out, tuple(c[0]), 5, (0, 255, 255), -1)  # top-left corner marked
        label = f"{'A' if d.family == TagFamily.ARUCO else 'T'}{d.number}"
        cv2.putText(out, label, (int(d.center.x) + 8, int(d.center.y) - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.8, col, 2)
    for i in missing:
        cv2.putText(out, f"missing A{i} ({surveyed[i][2]})", (10, 30 + 25 * missing.index(i)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    cv2.imwrite(args.out, out)
    print(f"\nannotated image: {args.out}")


if __name__ == "__main__":
    main()
