#!/usr/bin/env python
"""ChArUco intrinsics capture + solve, pulling frames through the aprilcam daemon.

Board: calib.io ChArUco 6x8 squares, 28 mm squares, 22 mm markers, DICT_4X4_50,
OpenCV non-legacy layout (verified empirically on 2026-10-15).

Subcommands
  capture  --cameras 4,2 --seconds 120 --root DIR   poll frames, keep novel sharp views
  report   --root DIR --camera 4                     coverage summary of a session
  solve    --root DIR --camera 4                     calibrateCamera over kept views
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from aprilcam.client.discovery import Discovery

SQUARES = (6, 8)
SQUARE_M = 0.028
MARKER_M = 0.022
DICT_NAME = "DICT_4X4_50"
#: None = auto: try OpenCV's current layout first, fall back to the legacy
#: (white-first) layout the first time markers are seen but no corners
#: interpolate, and lock in whichever works.
LEGACY = None


def configure_board(spec: str) -> None:
    """``COLSxROWS:SQUARE_MM:MARKER_MM:DICT_NAME`` e.g. ``5x7:37:28:DICT_5X5_100``."""
    global SQUARES, SQUARE_M, MARKER_M, DICT_NAME, LEGACY, _TUNED_ARUCO, _DETECTORS
    sq, sqmm, mkmm, dname = spec.split(":")
    cols, rows = (int(x) for x in sq.lower().split("x"))
    SQUARES = (cols, rows)
    SQUARE_M = float(sqmm) / 1000.0
    MARKER_M = float(mkmm) / 1000.0
    DICT_NAME = dname
    LEGACY = None
    _TUNED_ARUCO = None
    _DETECTORS = None
MIN_CORNERS = 12          # interior corners needed to keep a view (board has 35)
MIN_SHARPNESS = 60.0      # Laplacian variance over the board ROI
MIN_SHIFT_PX = 25.0       # mean corner move vs every kept view to count as novel
MIN_TILT_DELTA_DEG = 10.0 # or a tilt change this large


def make_board(legacy: bool = False) -> cv2.aruco.CharucoBoard:
    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, DICT_NAME))
    b = cv2.aruco.CharucoBoard(SQUARES, SQUARE_M, MARKER_M, d)
    b.setLegacyPattern(bool(legacy))
    return b


def decode_gray(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    im = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if im is None:
        raise RuntimeError("imdecode failed")
    return cv2.cvtColor(im, cv2.COLOR_BGR2GRAY) if im.ndim == 3 else im


def marker_clusters(corners, ids, link_factor: float = 2.5):
    """Group detected markers by proximity so each physical board is isolated
    from the playfield's own ArUco markers (same dictionary, overlapping ids)
    and from the other board. Returns [(corners_list, ids_(n,1))] for clusters
    of >= 4 markers with no duplicate ids."""
    if ids is None or len(ids) == 0:
        return []
    ids = np.asarray(ids).ravel()
    c = np.array([x.reshape(4, 2) for x in corners])
    cen = c.mean(axis=1)
    side = float(np.median(np.linalg.norm(c[:, 0] - c[:, 1], axis=1)))
    thr = link_factor * side
    n = len(cen)
    lab = -np.ones(n, int)
    k = 0
    for i in range(n):
        if lab[i] >= 0:
            continue
        stack = [i]
        lab[i] = k
        while stack:
            j = stack.pop()
            near = np.where((lab < 0) & (np.linalg.norm(cen - cen[j], axis=1) < thr))[0]
            for m in near:
                lab[m] = k
                stack.append(int(m))
        k += 1
    out = []
    for g in range(k):
        idx = [i for i in range(n) if lab[i] == g]
        if len(idx) < 4:
            continue
        gid = [int(ids[i]) for i in idx]
        if len(set(gid)) != len(gid):
            continue
        out.append(([corners[i] for i in idx], np.array(gid, dtype=np.int32).reshape(-1, 1)))
    return out


#: Markers are ~16 px across at table height on these cameras (<3 px per code
#: bit), which is at the detector's limit -- the colour OV9782 especially.
#: Upscaling 2x before marker detection and loosening the candidate filters
#: took camera 4 from ~10 to ~30 ChArUco corners on the same frame.
DETECT_SCALE = 2.0


def _tuned_params() -> cv2.aruco.DetectorParameters:
    p = cv2.aruco.DetectorParameters()
    p.minMarkerPerimeterRate = 0.01
    p.adaptiveThreshWinSizeMin = 3
    p.adaptiveThreshWinSizeMax = 23
    p.adaptiveThreshWinSizeStep = 4
    p.polygonalApproxAccuracyRate = 0.05
    p.perspectiveRemovePixelPerCell = 8
    p.perspectiveRemoveIgnoredMarginPerCell = 0.2
    return p


_TUNED_ARUCO = None
_DETECTORS = None  # (current-layout CharucoDetector, legacy-layout CharucoDetector)


def _detectors():
    global _DETECTORS
    if _DETECTORS is None:
        _DETECTORS = (cv2.aruco.CharucoDetector(make_board(False)),
                      cv2.aruco.CharucoDetector(make_board(True)))
    return _DETECTORS


def detect_boards(aruco_det, charuco_det, gray):
    """Every board visible in *gray* as [(charuco_corners, charuco_ids)].
    *aruco_det* is ignored in favour of a tuned detector run on a 2x upscale;
    ChArUco corner interpolation then runs on the native image so corners stay
    in native pixel coordinates."""
    global _TUNED_ARUCO
    if _TUNED_ARUCO is None:
        _TUNED_ARUCO = cv2.aruco.ArucoDetector(aruco_det.getDictionary(), _tuned_params())
    s = DETECT_SCALE
    up = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC) if s != 1.0 else gray
    mc, mids, _ = _TUNED_ARUCO.detectMarkers(up)
    if s != 1.0:
        mc = [c / s for c in mc]
    global LEGACY
    found = []
    cur, leg = _detectors()
    for cc, ci in marker_clusters(mc, mids):
        order = [(False, cur), (True, leg)] if LEGACY is None else [(LEGACY, leg if LEGACY else cur)]
        for is_legacy, det in order:
            chc, chi, _, _ = det.detectBoard(gray, markerCorners=cc, markerIds=ci)
            n = 0 if chi is None else len(chi)
            if n >= MIN_CORNERS:
                if LEGACY is None and len(ci) >= 6:
                    LEGACY = is_legacy  # lock in the layout that interpolates
                found.append((chc, chi))
                break
    return found


MAX_MOTION_PX = 12.0    # mean corner move vs previous frame (~5 fps) => really moving
BLUR_FRACTION = 0.45    # sharpness below this fraction of the camera's best => blurred


class Quality:
    """Per-camera steadiness/blur gate. Absolute Laplacian variance differs
    wildly between sensors (noisy colour OV9782 ~8000 sharp, mono OV9281
    ~3200 sharp, blurred OV9782 ~1800), so blur is judged relative to the
    best this camera has produced, and motion is judged against the previous
    frame's corners (matched by nearest board centroid when two are in view)."""

    def __init__(self):
        self.max_sharp = 0.0
        self.prev = []  # [(centroid, {id: pt})]

    def check(self, boards_with_sharp):
        """boards_with_sharp: [(corners, ids, sharp)] -> [(moving, blurry)]"""
        cur = []
        verdicts = []
        for corners, ids, sharp in boards_with_sharp:
            pts = {int(i): p for i, p in zip(np.asarray(ids).ravel(), corners.reshape(-1, 2))}
            cen = corners.reshape(-1, 2).mean(axis=0)
            moving = False
            if self.prev:
                pc, pd = min(self.prev, key=lambda e: np.linalg.norm(e[0] - cen))
                common = [i for i in pts if i in pd]
                if len(common) >= 4:
                    shift = float(np.mean([np.linalg.norm(pts[i] - pd[i]) for i in common]))
                    moving = shift > MAX_MOTION_PX
                else:
                    moving = True
            else:
                moving = True  # need a previous frame to judge
            self.max_sharp = max(self.max_sharp, sharp)
            blurry = sharp < BLUR_FRACTION * self.max_sharp
            cur.append((cen, pts))
            verdicts.append((moving, blurry))
        self.prev = cur
        return verdicts


def sharpness(gray: np.ndarray, corners: np.ndarray) -> float:
    pts = corners.reshape(-1, 2)
    x0, y0 = np.floor(pts.min(axis=0)).astype(int)
    x1, y1 = np.ceil(pts.max(axis=0)).astype(int)
    pad = 10
    roi = gray[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
    if roi.size == 0:
        return 0.0
    return float(cv2.Laplacian(roi, cv2.CV_64F).var())


def pose_guess(board, corners, ids, w, h):
    """Rough tilt (deg from fronto-parallel) and tilt azimuth using a guessed K."""
    obj, img = board.matchImagePoints(corners, ids)
    if obj is None or len(obj) < 6:
        return None
    K = np.array([[w, 0, w / 2], [0, w, h / 2], [0, 0, 1]], dtype=np.float64)
    # IPPE returns two candidate poses (mirror ambiguity for near-flat boards);
    # only trust the tilt when the best is clearly better than the runner-up.
    n_sol, rvecs, tvecs, errs = cv2.solvePnPGeneric(
        obj, img, K, None, flags=cv2.SOLVEPNP_IPPE)
    if n_sol < 1:
        return None
    errs = np.asarray(errs).ravel()
    R, _ = cv2.Rodrigues(rvecs[0])
    n = R[:, 2]
    tilt = math.degrees(math.acos(min(1.0, abs(float(n[2])))))
    az = math.degrees(math.atan2(float(n[1]), float(n[0])))
    confident = len(obj) >= 18 and (n_sol < 2 or errs[1] > 1.3 * max(errs[0], 1e-6))
    return tilt, az, float(np.asarray(tvecs[0]).ravel()[2]), confident


#: Outer regions are the outer 40% of the frame on each axis (centre band 20%),
#: and a view counts for EVERY region any detected corner reaches -- generous
#: on purpose: what calibration needs is corner samples reaching into each
#: part of the frame, not the board's centre parked there.
EDGE_FRACTION = 0.4
_CELL_NAMES = [["NW", "N", "NE"], ["W", "C", "E"], ["SW", "S", "SE"]]


def cell_of(cx, cy, w, h):
    col = 0 if cx < EDGE_FRACTION * w else (2 if cx > (1 - EDGE_FRACTION) * w else 1)
    row = 0 if cy < EDGE_FRACTION * h else (2 if cy > (1 - EDGE_FRACTION) * h else 1)
    return _CELL_NAMES[row][col]


def cells_covered(corners, w, h):
    """Every region any detected corner falls in, in fixed display order."""
    found = {cell_of(float(x), float(y), w, h) for x, y in corners.reshape(-1, 2)}
    return [c for row in _CELL_NAMES for c in row if c in found]


def view_cells(v):
    """Regions a stored view covers (older records only carry the centroid cell)."""
    return v.get("cells") or [v["cell"]]


class Session:
    def __init__(self, root: Path, camera: str):
        self.dir = root / camera
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        self.views = json.loads(self.index_path.read_text()) if self.index_path.exists() else []
        self._kept_corners = {}
        for v in self.views:
            z = np.load(self.dir / v["npz"])
            self._kept_corners[v["npz"]] = (z["corners"], z["ids"])

    def is_novel(self, corners, ids, tilt):
        if not self.views:
            return True
        mine = {int(i): c for i, c in zip(ids.ravel(), corners.reshape(-1, 2))}
        for v in self.views:
            kc, kids = self._kept_corners[v["npz"]]
            theirs = {int(i): c for i, c in zip(kids.ravel(), kc.reshape(-1, 2))}
            common = [i for i in mine if i in theirs]
            if len(common) < 4:
                continue
            shift = float(np.mean([np.linalg.norm(mine[i] - theirs[i]) for i in common]))
            dtilt = abs(tilt - v["tilt"]) if v.get("tilt") is not None else 0.0
            if shift < MIN_SHIFT_PX and dtilt < MIN_TILT_DELTA_DEG:
                return False
        return True

    def keep(self, gray, corners, ids, meta):
        n = len(self.views)
        stem = f"view_{n:03d}"
        cv2.imwrite(str(self.dir / f"{stem}.png"), gray)
        np.savez(self.dir / f"{stem}.npz", corners=corners, ids=ids)
        rec = dict(npz=f"{stem}.npz", png=f"{stem}.png", **meta)
        self.views.append(rec)
        self._kept_corners[rec["npz"]] = (corners, ids)
        self.index_path.write_text(json.dumps(self.views, indent=1))
        return rec

    def report(self, w=None, h=None) -> str:
        if not self.views:
            return "  no views kept"
        w = w or self.views[0]["w"]
        h = h or self.views[0]["h"]
        cells = {}
        for v in self.views:
            cells[v["cell"]] = cells.get(v["cell"], 0) + 1
        grid = [["NW", "N", "NE"], ["W", "C", "E"], ["SW", "S", "SE"]]
        lines = [f"  views kept: {len(self.views)}"]
        for row in grid:
            lines.append("   " + "  ".join(f"{c:>2}:{cells.get(c, 0):<3}" for c in row))
        tilts = [v["tilt"] for v in self.views if v.get("tilt") is not None]
        flat = sum(t < 15 for t in tilts)
        med = sum(15 <= t < 30 for t in tilts)
        steep = sum(t >= 30 for t in tilts)
        lines.append(f"  tilt: flat(<15°)={flat}  medium(15-30°)={med}  steep(>30°)={steep}")
        azs = [v["az"] for v in self.views if v.get("tilt", 0) >= 15]
        buckets = {"N": 0, "E": 0, "S": 0, "W": 0}
        for a in azs:
            a = a % 360
            if a < 45 or a >= 315: buckets["E"] += 1
            elif a < 135: buckets["S"] += 1
            elif a < 225: buckets["W"] += 1
            else: buckets["N"] += 1
        lines.append(f"  tilt direction (views >=15°): {buckets}")
        ncorn = [v["n"] for v in self.views]
        lines.append(f"  corners/view: min={min(ncorn)} mean={np.mean(ncorn):.1f}")
        return "\n".join(lines)


def cmd_capture(args):
    board = make_board()
    det = cv2.aruco.CharucoDetector(board)
    aruco_det = cv2.aruco.ArucoDetector(board.getDictionary())
    daemon = Discovery().connect()
    cams = [c.strip() for c in args.cameras.split(",")]
    root = Path(args.root)
    sessions = {c: Session(root, c) for c in cams}
    quality = {c: Quality() for c in cams}
    last_ts = {c: None for c in cams}
    stats = {c: dict(frames=0, seen=0, kept=0, blurry=0, dup=0) for c in cams}
    t_end = time.time() + args.seconds
    print(f"capturing {args.seconds}s from {cams} -> {root}", flush=True)
    while time.time() < t_end:
        for c in cams:
            try:
                fr = daemon.get_frame(c, encoding="png")
            except Exception as e:  # noqa: BLE001
                print(f"[{c}] get_frame error: {e}", flush=True)
                time.sleep(0.5)
                continue
            if fr.timestamp == last_ts[c]:
                continue
            last_ts[c] = fr.timestamp
            st = stats[c]
            st["frames"] += 1
            gray = decode_gray(fr.data)
            h, w = gray.shape
            boards = detect_boards(aruco_det, det, gray)
            if not boards:
                continue
            st["seen"] += 1
            sharps = [sharpness(gray, co) for co, _ in boards]
            verdicts = quality[c].check([(co, ids, sh) for (co, ids), sh in zip(boards, sharps)])
            for (corners, ids), sh, (moving, blurry) in zip(boards, sharps, verdicts):
                n = len(ids)
                if moving or blurry:
                    st["blurry"] += 1
                    continue
                pg = pose_guess(board, corners, ids, w, h)
                tilt, az, dist, _conf = pg if pg else (None, None, None, False)
                if not sessions[c].is_novel(corners, ids, tilt if tilt is not None else 0):
                    st["dup"] += 1
                    continue
                cx, cy = corners.reshape(-1, 2).mean(axis=0)
                cell = cell_of(cx, cy, w, h)
                sessions[c].keep(gray, corners, ids, dict(
                    n=int(n), sharp=round(sh, 1), tilt=None if tilt is None else round(tilt, 1),
                    az=None if az is None else round(az, 1), cell=cell, w=w, h=h,
                    cx=round(float(cx), 1), cy=round(float(cy), 1), t=fr.timestamp))
                st["kept"] += 1
                print(f"[{c}] KEEP #{len(sessions[c].views):02d} corners={n} sharp={sh:.0f} "
                      f"tilt={tilt if tilt is None else round(tilt)}° cell={cell}", flush=True)
    print("\n=== capture done ===")
    for c in cams:
        print(f"[{c}] frames={stats[c]['frames']} board_seen={stats[c]['seen']} "
              f"kept={stats[c]['kept']} blurry={stats[c]['blurry']} dup={stats[c]['dup']}")
        print(sessions[c].report())


def cmd_report(args):
    s = Session(Path(args.root), args.camera)
    print(f"[{args.camera}]")
    print(s.report())


def cmd_solve(args):
    board = make_board()
    s = Session(Path(args.root), args.camera)
    if len(s.views) < 6:
        sys.exit(f"only {len(s.views)} views; need more")
    w, h = s.views[0]["w"], s.views[0]["h"]
    obj_all, img_all, names = [], [], []
    for v in s.views:
        corners, ids = s._kept_corners[v["npz"]]
        obj, img = board.matchImagePoints(corners, ids)
        if obj is None or len(obj) < 6:
            continue
        obj_all.append(obj.astype(np.float32))
        img_all.append(img.astype(np.float32))
        names.append(v["npz"])

    def run(objs, imgs, flags):
        rms, K, D, rvecs, tvecs = cv2.calibrateCamera(objs, imgs, (w, h), None, None, flags=flags)
        per = []
        for o, i, r, t in zip(objs, imgs, rvecs, tvecs):
            p, _ = cv2.projectPoints(o, r, t, K, D)
            per.append(float(np.sqrt(np.mean(np.sum((p.reshape(-1, 2) - i.reshape(-1, 2)) ** 2, axis=1)))))
        return rms, K, D, per

    flags = 0
    if args.fix_k3:
        flags |= cv2.CALIB_FIX_K3
    rms, K, D, per = run(obj_all, img_all, flags)
    print(f"pass 1: {len(obj_all)} views, rms={rms:.3f} px, per-view max={max(per):.2f}")
    med = float(np.median(per))
    keep = [i for i, e in enumerate(per) if e <= max(2.5 * med, 1.0)]
    dropped = [names[i] for i in range(len(per)) if i not in keep]
    if dropped:
        print(f"dropping {len(dropped)} outlier views: {dropped}")
        obj_all = [obj_all[i] for i in keep]
        img_all = [img_all[i] for i in keep]
        names = [names[i] for i in keep]
        rms, K, D, per = run(obj_all, img_all, flags)
        print(f"pass 2: {len(obj_all)} views, rms={rms:.3f} px, per-view max={max(per):.2f}")
    fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
    d = D.ravel().tolist()
    print(f"fx={fx:.1f} fy={fy:.1f} cx={cx:.1f} cy={cy:.1f}  (image {w}x{h}, centre {w/2:.0f},{h/2:.0f})")
    print(f"k1={d[0]:+.4f} k2={d[1]:+.4f} p1={d[2]:+.5f} p2={d[3]:+.5f} k3={d[4]:+.4f}")
    hfov = 2 * math.degrees(math.atan(w / (2 * fx)))
    print(f"horizontal FOV ≈ {hfov:.1f}°")
    out = dict(camera=args.camera, width=w, height=h, views=len(obj_all), rms_px=round(rms, 4),
               per_view_rms_px=[round(e, 3) for e in per],
               camera_matrix=K.tolist(), dist_coeffs=d,
               board=dict(squares=SQUARES, square_m=SQUARE_M, marker_m=MARKER_M, dict=DICT_NAME, legacy=LEGACY),
               solved_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    p = s.dir / "intrinsics.json"
    p.write_text(json.dumps(out, indent=1))
    print(f"wrote {p}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture"); c.add_argument("--cameras", required=True); c.add_argument("--seconds", type=float, default=60); c.add_argument("--root", required=True); c.set_defaults(fn=cmd_capture)
    r = sub.add_parser("report"); r.add_argument("--root", required=True); r.add_argument("--camera", required=True); r.set_defaults(fn=cmd_report)
    s = sub.add_parser("solve"); s.add_argument("--root", required=True); s.add_argument("--camera", required=True); s.add_argument("--fix-k3", action="store_true"); s.set_defaults(fn=cmd_solve)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
