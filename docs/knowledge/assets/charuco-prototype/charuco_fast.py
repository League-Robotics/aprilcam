#!/usr/bin/env python
"""Fast ChArUco intrinsics: solve continuously, stop when the answer stops moving.

  python charuco_fast.py --cameras 4,2 [--install] [--root DIR]

Hold the board high (40-60 cm above the table), tilted ~30°, and sweep it slowly
in a big loop around the field, rolling the tilt as you go. Then one slow pass
flat on the table through the middle. The screen shows, per camera, a 3x3
coverage grid, tilt-direction coverage, the latest solve, and DONE when two
successive solves agree. With --install the lens model is written into the
daemon's calibration.json for that camera (backup kept); restart the daemon
afterwards so it loads the file.
"""
from __future__ import annotations

import argparse
import curses
import json
import math
import shutil
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import charuco_cal as cc  # noqa: E402
import charuco_tui as tui  # noqa: E402  (solve_full, converged, az_bucket, spacing_px)

CELL_ROWS = [["NW", "N", "NE"], ["W", "C", "E"], ["SW", "S", "SE"]]


def coverage(views):
    cells = set()
    dirs = set()
    tilted = 0
    for v in views:
        cells.update(cc.view_cells(v))
        if v.get("tilt") is not None and v["tilt"] >= tui.TILT_MIN:
            tilted += 1
            dirs.add(tui.az_bucket(v["az"]))
    return cells, dirs, tilted


def half_sample_stable(session, draws=3):
    """Solve on random halves of the views; stable when every draw agrees with
    the full solve: fx within 1.5 %, principal point within 12 px, k1 within 0.03."""
    import random
    board = cc.make_board(bool(cc.LEGACY))
    objs, imgs = [], []
    for v in list(session.views):
        co, ids = session._kept_corners[v["npz"]]
        o, im = board.matchImagePoints(co, ids)
        if o is not None and len(o) >= 6:
            objs.append(o.astype(np.float32)); imgs.append(im.astype(np.float32))
    if len(objs) < 12:
        return False, "too few views"
    size = (session.views[0]["w"], session.views[0]["h"])

    def solve(sel):
        _, K, D, _, _ = cv2.calibrateCamera([objs[i] for i in sel], [imgs[i] for i in sel], size, None, None,
                                            flags=cv2.CALIB_FIX_K3)
        return K[0, 0], K[0, 2], K[1, 2], D.ravel()[0]
    full = solve(range(len(objs)))
    rng = random.Random(len(objs))
    worst = [0.0, 0.0, 0.0]
    for _ in range(draws):
        fx, cx, cy, k1 = solve(rng.sample(range(len(objs)), len(objs) // 2))
        worst[0] = max(worst[0], abs(fx - full[0]) / full[0])
        worst[1] = max(worst[1], abs(cx - full[1]), abs(cy - full[2]))
        worst[2] = max(worst[2], abs(k1 - full[3]))
    ok = worst[0] < 0.015 and worst[1] < 12 and worst[2] < 0.03
    return ok, f"fx {100*worst[0]:.1f}% pp {worst[1]:.0f}px k1 {worst[2]:.3f}"


class Solver(threading.Thread):
    """Background solver: re-solves whenever >= SOLVE_EVERY new views arrived."""

    def __init__(self, session):
        super().__init__(daemon=True)
        self.session = session
        self.prev = None
        self.cur = None
        self.msg = ""
        self.conv = (False, "")
        self.solved_views = 0
        self.busy = False
        self.stop = False
        self.converged_at = None

    def run(self):
        while not self.stop:
            n = len(self.session.views)
            if n >= tui.SOLVE_MIN_VIEWS and n - self.solved_views >= tui.SOLVE_EVERY:
                self.busy = True
                try:
                    msg, out = tui.solve_full(self.session)
                except Exception as e:  # noqa: BLE001
                    msg, out = f"solve error: {e}", None
                self.msg = msg
                if out is not None:
                    self.prev, self.cur = self.cur, out
                    ok, why = tui.converged(self.prev, self.cur)
                    if ok:
                        ok2, why2 = half_sample_stable(self.session)
                        ok, why = ok and ok2, why + " | halves: " + why2
                    self.conv = (ok, why)
                    if self.conv[0] and self.converged_at is None:
                        self.converged_at = time.time()
                    elif not self.conv[0]:
                        self.converged_at = None
                self.solved_views = n
                self.busy = False
            time.sleep(0.2)


def install(daemon, cam, intrinsics):
    """Write camera_matrix/dist_coeffs into the daemon's calibration.json."""
    name = None
    for st in daemon.list_cameras():
        if str(st.number) == str(cam) or st.name == cam:
            name = st.name
    if name is None:
        return f"camera {cam}: not in daemon registry, not installed"
    p = Path.home() / ".local/share/aprilcam/cameras" / name / "calibration.json"
    if not p.exists():
        return f"{name}: no calibration.json yet (run `aprilcam camera calibrate {cam}` first), not installed"
    bak = p.with_name("calibration.json.bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(p, bak)
    cal = json.loads(p.read_text())
    cal["camera_matrix"] = intrinsics["camera_matrix"]
    cal["dist_coeffs"] = intrinsics["dist_coeffs"]
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cal, indent=1))
    tmp.replace(p)
    return f"{name}: lens model installed into {p} (backup {bak.name}) -- restart the daemon to load it"


def main(stdscr, args):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color(); curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_CYAN, -1)
    curses.init_pair(4, curses.COLOR_GREEN, curses.COLOR_GREEN)
    curses.init_pair(5, curses.COLOR_RED, -1)
    GREEN, YELLOW, CYAN, BLOCK, RED = (curses.color_pair(i) for i in range(1, 6))

    board = cc.make_board()
    det = cv2.aruco.CharucoDetector(board)
    aruco_det = cv2.aruco.ArucoDetector(board.getDictionary())
    daemon = cc.Discovery().connect()
    cams = [c.strip() for c in args.cameras.split(",")]
    root = Path(args.root).expanduser()
    sessions = {c: cc.Session(root, c) for c in cams}
    quality = {c: cc.Quality() for c in cams}
    solvers = {c: Solver(sessions[c]) for c in cams}
    for s in solvers.values():
        s.start()
    last_ts = {c: None for c in cams}
    now = {c: "no board" for c in cams}
    frames = 0
    t0 = time.time()
    done_time = None
    final = {}

    while True:
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break
        for c in cams:
            try:
                fr = daemon.get_frame(c, encoding="png")
            except Exception as e:  # noqa: BLE001
                now[c] = f"get_frame error: {str(e)[:60]}"
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
                now[c] = "no board"
                continue
            if cc.LEGACY is not None and board.getLegacyPattern() != cc.LEGACY:
                board = cc.make_board(cc.LEGACY)
            sharps = [cc.sharpness(gray, co) for co, _ in boards]
            verdicts = quality[c].check([(co, ids, sh) for (co, ids), sh in zip(boards, sharps)])
            descr = []
            for (corners, ids), sh, (moving, blurry) in zip(boards, sharps, verdicts):
                pg = cc.pose_guess(board, corners, ids, w, h)
                tilt, az, _, conf = pg if pg else (None, None, None, False)
                cells = cc.cells_covered(corners, w, h)
                cx, cy = corners.reshape(-1, 2).mean(axis=0)
                sp = tui.spacing_px(corners)
                if tilt is None:
                    pose = "pose ?"
                elif not conf:
                    pose = f"tilt ~{tilt:.0f}°?"
                elif tilt >= tui.TILT_MIN:
                    pose = f"tilt {tilt:.0f}° {tui.lean_word(tui.az_bucket(az))}"
                else:
                    pose = f"flat {tilt:.0f}°"
                flag = "MOVING" if moving else ("BLUR" if blurry else "")
                descr.append(f"{'+'.join(cells)} {pose} {len(ids)}c {flag}")
                if moving or blurry or tilt is None:
                    continue
                if not sessions[c].is_novel(corners, ids, tilt):
                    continue
                sessions[c].keep(gray, corners, ids, dict(
                    n=int(len(ids)), sharp=round(sh, 1), tilt=round(tilt, 1) if conf else None,
                    az=round(az, 1) if conf else None, cell=cc.cell_of(cx, cy, w, h), cells=cells,
                    w=w, h=h, cx=round(float(cx), 1), cy=round(float(cy), 1), spacing=round(sp, 1),
                    t=fr.timestamp))
            now[c] = " | ".join(descr)

        # ---- draw ----
        stdscr.erase()
        H, W = stdscr.getmaxyx()
        el = time.time() - t0
        line = 0
        stdscr.addnstr(line, 0, f"ChArUco FAST  cams={','.join(cams)}  {el:4.0f}s  {frames/el if el else 0:.1f} fps   "
                       f"q=quit", W - 1, curses.A_BOLD); line += 1
        stdscr.addnstr(line, 0, "Hold board HIGH (40-60 cm up), tilted ~30°, sweep a slow loop around the field "
                       "rolling the tilt; then one slow flat pass.", W - 1, YELLOW); line += 2
        all_conv = True
        for c in cams:
            v = sessions[c].views
            cells, dirs, tilted = coverage(v)
            s = solvers[c]
            conv = s.conv[0]
            all_conv &= conv
            stdscr.addnstr(line, 0, f"CAM {c}   views {len(v):3d}   tilted {tilted:3d}", W - 1, curses.A_BOLD)
            if conv:
                stdscr.addnstr(line, 40, "  DONE  ", 8, BLOCK | curses.A_BOLD)
                stdscr.addnstr(line, 50, "solution stable", W - 51, GREEN | curses.A_BOLD)
            elif s.cur is not None:
                stdscr.addnstr(line, 40, "solving... keep going", W - 41, YELLOW)
            else:
                stdscr.addnstr(line, 40, f"need {max(0, tui.SOLVE_MIN_VIEWS - len(v))} more views for first solve", W - 41, YELLOW)
            line += 1
            stdscr.addnstr(line, 0, f"  now: {now[c]}", W - 1, CYAN); line += 1
            # 3x3 coverage grid + tilt dirs
            for r, row in enumerate(CELL_ROWS):
                x = 2
                for cell in row:
                    ok = cell in cells
                    stdscr.addnstr(line, x, f" {cell:^4}", 6, BLOCK if ok else curses.A_DIM)
                    x += 7
                if r == 0:
                    stdscr.addnstr(line, x + 2, "tilt dirs: " + " ".join(
                        (d if d in dirs else "·") for d in ["N", "E", "S", "W"]), W - x - 3)
                if r == 1 and s.msg:
                    stdscr.addnstr(line, x + 2, f"solve: {s.msg}", W - x - 3, GREEN if conv else 0)
                if r == 2 and s.cur is not None:
                    stdscr.addnstr(line, x + 2, f"change since previous solve: {s.conv[1]}", W - x - 3)
                line += 1
            line += 1
        if all_conv and all(s.cur is not None for s in solvers.values()):
            if done_time is None:
                done_time = time.time()
            left = 3 - (time.time() - done_time)
            stdscr.addnstr(line, 0, f"ALL CAMERAS CONVERGED -- finishing in {max(0, left):.0f}s (press q now to stop immediately)",
                           W - 1, GREEN | curses.A_BOLD)
            if left <= 0:
                break
        else:
            done_time = None
        stdscr.refresh()
        time.sleep(0.01)

    for s in solvers.values():
        s.stop = True
    for c in cams:
        msg, out = tui.solve_full(sessions[c]) if sessions[c].views else ("no views", None)
        final[c] = [msg]
        if out is not None and args.install:
            final[c].append(install(daemon, c, out))
    return final


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--root", default="~/.local/share/aprilcam/charuco-sessions/fast-" + time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--install", action="store_true", help="write the lens model into the daemon's calibration.json")
    ap.add_argument("--board", default="6x8:28:22:DICT_4X4_50",
                    help="COLSxROWS:SQUARE_MM:MARKER_MM:DICT, e.g. 5x7:37:28:DICT_5X5_100")
    args = ap.parse_args()
    cc.configure_board(args.board)
    res = curses.wrapper(main, args)
    print("final:")
    for c, msgs in res.items():
        for m in msgs:
            print(f"  [{c}] {m}")
    print(f"session: {Path(args.root).expanduser()}")
