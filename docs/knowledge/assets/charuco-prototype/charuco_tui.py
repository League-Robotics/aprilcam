#!/usr/bin/env python
"""Live ChArUco intrinsics checklist (curses TUI).

Run in a terminal:
  python charuco_tui.py --cameras 4,2 --root ~/.local/share/aprilcam/charuco-sessions/main

Every needed board pose is a row; a column per camera shows ✓ once that camera has
a good view satisfying it. Keep moving until every cell is ✓. Keys: q = quit and
solve, s = solve now (keeps running), r = reset the "now seeing" baseline.
"""
from __future__ import annotations

import argparse
import curses
import json
import math
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import charuco_cal as cc  # noqa: E402

CORNERS = ["NW", "NE", "SW", "SE"]
EDGES = ["N", "E", "S", "W"]
FLAT_MAX_TILT = 15.0
TILT_MIN = 20.0
RAISED_FACTOR = 1.3
DIRS = ["N", "E", "S", "W"]


def az_bucket(az):
    a = az % 360
    if a < 45 or a >= 315:
        return "E"
    if a < 135:
        return "S"
    if a < 225:
        return "W"
    return "N"


def lean_word(b):
    # the board normal leans toward bucket b, i.e. the OPPOSITE edge is lifted
    return {"N": "bottom edge up", "S": "top edge up", "E": "left edge up", "W": "right edge up"}[b]


def spacing_px(corners):
    pts = corners.reshape(-1, 2)
    if len(pts) < 2:
        return 0.0
    d = np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=2)
    d[d == 0] = np.inf
    return float(np.median(d.min(axis=1)))


# ---- checklist definition -------------------------------------------------

def build_targets():
    """Suggested poses. DONE is decided by solve convergence, not by this list."""
    t = []
    for cell in ["NW", "NE", "C", "SW", "SE"]:
        t.append(("Flat on table", f"flat {cell}", ("flat", cell)))
    for d in DIRS:
        t.append(("Tilted, anywhere", f"{lean_word(d)}", ("tiltAny", d)))
    for cell in CORNERS:
        t.append(("Tilted, corners", f"{cell} tilt", ("tiltCorner", cell, 1)))
    for cell in ["C", "NW", "SE"]:
        t.append(("Raised 30-50 cm, any tilt", f"raised {cell}", ("raised", cell)))
    return t


def evaluate(views, targets):
    """Which targets are satisfied by this camera's kept views -> set of indices."""
    done = set()
    flat = [v for v in views if v.get("tilt") is not None and v["tilt"] < FLAT_MAX_TILT]
    table_sp = min((v["spacing"] for v in flat), default=None)
    tilted = [v for v in views if v.get("tilt") is not None and v["tilt"] >= TILT_MIN]
    vc = cc.view_cells
    for i, (_, _, spec) in enumerate(targets):
        kind = spec[0]
        if kind == "flat":
            if any(spec[1] in vc(v) and (table_sp is None or v["spacing"] < RAISED_FACTOR * table_sp) for v in flat):
                done.add(i)
        elif kind == "tiltAny":
            if any(az_bucket(v["az"]) == spec[1] for v in tilted):
                done.add(i)
        elif kind == "tiltCorner":
            buckets = {az_bucket(v["az"]) for v in tilted if spec[1] in vc(v)}
            if len(buckets) >= spec[2]:
                done.add(i)
        elif kind == "tiltEdge":
            if any(spec[1] in vc(v) for v in tilted):
                done.add(i)
        elif kind == "raised":
            if table_sp is not None and any(spec[1] in vc(v) and v["spacing"] >= RAISED_FACTOR * table_sp for v in views):
                done.add(i)
    return done, table_sp


# ---- solve ----------------------------------------------------------------

def solve(session, fix_k3=True):
    msg, _ = solve_full(session, fix_k3)
    return msg


def solve_full(session, fix_k3=True):
    """-> (one-line message, result dict or None). Writes intrinsics.json on success."""
    board = cc.make_board(bool(cc.LEGACY))
    w, h = session.views[0]["w"], session.views[0]["h"]
    objs, imgs = [], []
    for v in list(session.views):
        corners, ids = session._kept_corners[v["npz"]]
        o, im = board.matchImagePoints(corners, ids)
        if o is not None and len(o) >= 6:
            objs.append(o.astype(np.float32)); imgs.append(im.astype(np.float32))
    if len(objs) < 6:
        return f"only {len(objs)} usable views", None
    flags = cv2.CALIB_FIX_K3 if fix_k3 else 0

    def run(objs, imgs):
        rms, K, D, rv, tv = cv2.calibrateCamera(objs, imgs, (w, h), None, None, flags=flags)
        per = []
        for o, i, r, t in zip(objs, imgs, rv, tv):
            p, _ = cv2.projectPoints(o, r, t, K, D)
            per.append(float(np.sqrt(np.mean(np.sum((p.reshape(-1, 2) - i.reshape(-1, 2)) ** 2, axis=1)))))
        return rms, K, D, per

    rms, K, D, per = run(objs, imgs)
    med = float(np.median(per))
    keep = [i for i, e in enumerate(per) if e <= max(2.5 * med, 1.0)]
    n_drop = len(per) - len(keep)
    if n_drop:
        objs = [objs[i] for i in keep]; imgs = [imgs[i] for i in keep]
        rms, K, D, per = run(objs, imgs)
    d = D.ravel().tolist()
    hfov = 2 * math.degrees(math.atan(w / (2 * K[0, 0])))
    out = dict(camera=session.dir.name, width=w, height=h, views=len(objs), dropped=n_drop,
               rms_px=round(rms, 4), per_view_rms_px=[round(e, 3) for e in per],
               camera_matrix=K.tolist(), dist_coeffs=d,
               board=dict(squares=cc.SQUARES, square_m=cc.SQUARE_M, marker_m=cc.MARKER_M, dict=cc.DICT_NAME, legacy=cc.LEGACY),
               solved_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    (session.dir / "intrinsics.json").write_text(json.dumps(out, indent=1))
    msg = (f"{len(objs)} views (dropped {n_drop})  rms={rms:.3f}px  fx={K[0,0]:.0f} fy={K[1,1]:.0f} "
           f"cx={K[0,2]:.0f} cy={K[1,2]:.0f}  k1={d[0]:+.3f} k2={d[1]:+.3f} k3={d[4]:+.3f}  HFOV≈{hfov:.0f}°")
    return msg, out


# Convergence: a solve counts as converged when, compared with the previous
# solve (>= SOLVE_EVERY new views apart), the focal length moved < 1 %, the
# principal point < 8 px, k1 < 0.01, and the fit is tight (rms < 0.6 px).
SOLVE_MIN_VIEWS = 15
SOLVE_EVERY = 8
CONV_FX_FRAC, CONV_PP_PX, CONV_K1, CONV_RMS = 0.01, 8.0, 0.01, 0.6


def converged(prev, cur):
    if prev is None or cur is None:
        return False, "first solve"
    Kp, Kc = np.array(prev["camera_matrix"]), np.array(cur["camera_matrix"])
    dfx = abs(Kc[0, 0] - Kp[0, 0]) / Kc[0, 0]
    dpp = max(abs(Kc[0, 2] - Kp[0, 2]), abs(Kc[1, 2] - Kp[1, 2]))
    dk1 = abs(cur["dist_coeffs"][0] - prev["dist_coeffs"][0])
    ok = dfx < CONV_FX_FRAC and dpp < CONV_PP_PX and dk1 < CONV_K1 and cur["rms_px"] < CONV_RMS
    return ok, f"Δ fx {100*dfx:.1f}%  pp {dpp:.0f}px  k1 {dk1:.3f}  rms {cur['rms_px']:.2f}"


# ---- TUI ------------------------------------------------------------------

def main(stdscr, args):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color(); curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_CYAN, -1)
    curses.init_pair(4, curses.COLOR_GREEN, curses.COLOR_GREEN)   # solid green block
    curses.init_pair(5, curses.COLOR_WHITE, -1)
    GREEN, YELLOW, CYAN = curses.color_pair(1), curses.color_pair(2), curses.color_pair(3)
    BLOCK, DIMTXT = curses.color_pair(4), curses.color_pair(5) | curses.A_DIM

    board = cc.make_board(bool(cc.LEGACY))
    det = cv2.aruco.CharucoDetector(board)
    aruco_det = cv2.aruco.ArucoDetector(board.getDictionary())
    daemon = cc.Discovery().connect()
    cams = [c.strip() for c in args.cameras.split(",")]
    root = Path(args.root).expanduser()
    sessions = {c: cc.Session(root, c) for c in cams}
    quality = {c: cc.Quality() for c in cams}
    targets = build_targets()
    last_ts = {c: None for c in cams}
    now_seeing = {c: "no board" for c in cams}
    solve_msg = {c: "" for c in cams}
    kept_total = {c: 0 for c in cams}
    frames = 0
    t0 = time.time()
    status = ""
    all_done_announced = False

    while True:
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break
        if key in (ord("s"), ord("S")):
            for c in cams:
                solve_msg[c] = solve(sessions[c]) if sessions[c].views else "no views"
            status = "solved at " + time.strftime("%H:%M:%S")

        for c in cams:
            try:
                fr = daemon.get_frame(c, encoding="png")
            except Exception as e:  # noqa: BLE001
                now_seeing[c] = f"get_frame error: {str(e)[:60]}"
                time.sleep(0.3)
                continue
            if fr.timestamp == last_ts[c]:
                continue
            last_ts[c] = fr.timestamp
            frames += 1
            gray = cc.decode_gray(fr.data)
            h, w = gray.shape
            boards = cc.detect_boards(aruco_det, det, gray)
            if not boards:
                now_seeing[c] = "no board"
                continue
            descr = []
            sharps = [cc.sharpness(gray, corners) for corners, _ in boards]
            verdicts = quality[c].check([(co, ids, sh) for (co, ids), sh in zip(boards, sharps)])
            for (corners, ids), sh, (moving, blurry) in zip(boards, sharps, verdicts):
                n = len(ids)
                pg = cc.pose_guess(board, corners, ids, w, h)
                tilt, az, _, conf = pg if pg else (None, None, None, False)
                cx, cy = corners.reshape(-1, 2).mean(axis=0)
                cell = cc.cell_of(cx, cy, w, h)
                cells = cc.cells_covered(corners, w, h)
                sp = spacing_px(corners)
                if tilt is None:
                    pose_txt = "pose ?"
                elif not conf:
                    pose_txt = f"tilt ~{tilt:.0f}°? (need more corners/tilt)"
                elif tilt >= TILT_MIN:
                    pose_txt = f"tilt {tilt:.0f}° {lean_word(az_bucket(az))}"
                else:
                    pose_txt = f"tilt {tilt:.0f}° flat"
                flag = "MOVING" if moving else ("BLUR" if blurry else "")
                descr.append(f"{'+'.join(cells)} {pose_txt} size {sp:.0f}px {n}c {flag}")
                if moving or blurry or tilt is None:
                    continue
                if not sessions[c].is_novel(corners, ids, tilt):
                    continue
                # an unconfident pose is still a valid calibration view, but it
                # must not tick any checklist item: store tilt=None for it
                sessions[c].keep(gray, corners, ids, dict(
                    n=int(n), sharp=round(sh, 1), tilt=round(tilt, 1) if conf else None,
                    az=round(az, 1) if conf else None, cell=cell, cells=cells,
                    w=w, h=h, cx=round(float(cx), 1), cy=round(float(cy), 1), spacing=round(sp, 1),
                    t=fr.timestamp))
                kept_total[c] += 1
            now_seeing[c] = " | ".join(descr)

        # ---- draw ----
        stdscr.erase()
        H, W = stdscr.getmaxyx()
        elapsed = time.time() - t0
        done = {}
        table_sp = {}
        for c in cams:
            done[c], table_sp[c] = evaluate(sessions[c].views, targets)
        remaining = sum(len(targets) - len(done[c]) for c in cams)
        line = 0
        stdscr.addnstr(line, 0, f"ChArUco intrinsics  cams={','.join(cams)}  {elapsed:5.0f}s  "
                       f"{frames/elapsed if elapsed else 0:.1f} fps  views " +
                       "  ".join(f"[{c}] {len(sessions[c].views)}" for c in cams) +
                       f"   remaining: {remaining}", W - 1, curses.A_BOLD); line += 1
        for c in cams:
            stdscr.addnstr(line, 0, f"[{c}] now: {now_seeing[c]}", W - 1, CYAN); line += 1
        line += 1
        colw = 12
        gap = 2
        hdr = f"{'':<34}" + "".join(f"{'CAM ' + c:^{colw + gap}}" for c in cams)
        stdscr.addnstr(line, 0, hdr, W - 1, curses.A_BOLD | curses.A_UNDERLINE); line += 1
        group = None
        for i, (g, name, _) in enumerate(targets):
            if line >= H - 4:
                break
            if g != group:
                group = g
                stdscr.addnstr(line, 0, g.upper(), W - 1, YELLOW | curses.A_BOLD); line += 1
            all_ok = all(i in done[c] for c in cams)
            row = f"  {name:<32}"
            stdscr.addnstr(line, 0, row, W - 1, (GREEN | curses.A_BOLD) if all_ok else curses.A_BOLD)
            x = len(row)
            for c in cams:
                ok = i in done[c]
                if ok:
                    stdscr.addnstr(line, x, " " * colw, colw, BLOCK)
                else:
                    stdscr.addnstr(line, x, f"{'- - - -':^{colw}}", colw, DIMTXT)
                x += colw + gap
            line += 1
        if remaining == 0 and not all_done_announced:
            all_done_announced = True
            for c in cams:
                solve_msg[c] = solve(sessions[c])
            status = "ALL DONE — solved. Keep adding views or press q."
        y = H - 2 - len(cams)
        for c in cams:
            if solve_msg[c]:
                stdscr.addnstr(y, 0, f"[{c}] {solve_msg[c]}", W - 1, GREEN); y += 1
        stdscr.addnstr(H - 1, 0, f"{status}   q=quit+solve  s=solve now   root={root}", W - 1, curses.A_REVERSE)
        stdscr.refresh()

    results = {}
    for c in cams:
        results[c] = solve(sessions[c]) if sessions[c].views else "no views"
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", required=True, help="comma-separated camera numbers/names, e.g. 4,2")
    ap.add_argument("--root", default="~/.local/share/aprilcam/charuco-sessions/" + time.strftime("%Y-%m-%d"))
    args = ap.parse_args()
    res = curses.wrapper(main, args)
    print("final solve:")
    for c, msg in res.items():
        print(f"  [{c}] {msg}")
    print(f"sessions under {Path(args.root).expanduser()}  (intrinsics.json per camera)")
