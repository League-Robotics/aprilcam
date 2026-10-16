#!/usr/bin/env python
"""Fullscreen, far-away-readable ChArUco intrinsics GUI.

  python charuco_gui.py --cameras 4,2 --board 5x7:37:28:DICT_5X5_100 --install

Shows, per camera, in very large type:
  * the error of the solve you would get RIGHT NOW (rms px)
  * how much the world mapping could still change (max px residual after a
    homography, between the full solve and half-sample solves) -- this is the
    "good enough" number: < GOOD_RESIDUAL_PX means more views won't move anything
  * a 3x3 coverage grid and tilt-direction tiles
  * one plain-language NEXT instruction, picked from the biggest gap
Banner turns green = GOOD ENOUGH; it auto-finishes a few seconds later (or press
Enter to accept at any time, Esc/q to quit without installing).
"""
from __future__ import annotations

import argparse
import math
import random
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import charuco_cal as cc  # noqa: E402
import charuco_tui as tui  # noqa: E402
import charuco_fast as fast  # noqa: E402

GOOD_RESIDUAL_PX = 4.0   # world-mapping disagreement between half-sample solves (~3 mm)
FIELD_FRACTION = 0.85    # judge over the central 85% of the frame: where the playfield is
GOOD_RMS_PX = 0.6
MIN_VIEWS = 12
SOLVE_EVERY = 3
AUTO_FINISH_S = 4

CELL_ROWS = [["NW", "N", "NE"], ["W", "C", "E"], ["SW", "S", "SE"]]
CELL_WORDS = {"NW": "TOP-LEFT", "N": "TOP", "NE": "TOP-RIGHT", "W": "LEFT", "C": "CENTER",
              "E": "RIGHT", "SW": "BOTTOM-LEFT", "S": "BOTTOM", "SE": "BOTTOM-RIGHT"}
DIR_WORDS = {"N": "bottom edge up", "E": "left edge up", "S": "top edge up", "W": "right edge up"}

BG, FG, DIM = "#101010", "#f0f0f0", "#404040"
GREEN, YELLOW, RED, CYAN = "#19c84a", "#e8c000", "#e03030", "#40c8ff"


# ---------------------------------------------------------------- analysis --

def mapping_residual_px(session, draws=3):
    """Full solve vs half-sample solves: max px residual of the undistortion map
    after removing a homography (what a planar world mapping could absorb)."""
    board = cc.make_board(bool(cc.LEGACY))
    objs, imgs = [], []
    for v in list(session.views):
        co, ids = session._kept_corners[v["npz"]]
        o, im = board.matchImagePoints(co, ids)
        if o is not None and len(o) >= 6:
            objs.append(o.astype(np.float32)); imgs.append(im.astype(np.float32))
    n = len(objs)
    if n < 6:
        return None
    w, h = session.views[0]["w"], session.views[0]["h"]

    def solve(sel):
        rms, K, D, _, _ = cv2.calibrateCamera([objs[i] for i in sel], [imgs[i] for i in sel], (w, h), None, None,
                                              flags=cv2.CALIB_FIX_K3)
        return rms, K, D

    rms, K, D = solve(range(n))
    m = (1 - FIELD_FRACTION) / 2
    gx, gy = np.meshgrid(np.linspace(m * w, (1 - m) * w, 7), np.linspace(m * h, (1 - m) * h, 5))
    grid = np.stack([gx.ravel(), gy.ravel()], axis=1).astype(np.float64).reshape(-1, 1, 2)
    ref = cv2.undistortPoints(grid, K, D, P=K).reshape(-1, 2)
    # how much the lens model changes the mapping at all, vs no model (after a homography)
    Hn, _ = cv2.findHomography(grid.reshape(-1, 2).astype(np.float32), ref.astype(np.float32), 0)
    corr = float(np.max(np.linalg.norm(
        cv2.perspectiveTransform(grid.astype(np.float32), Hn.astype(np.float32)).reshape(-1, 2) - ref, axis=1))) if Hn is not None else 0.0
    worst = 0.0
    if n >= MIN_VIEWS:
        rng = random.Random(n)
        for _ in range(draws):
            _, K2, D2 = solve(rng.sample(range(n), n // 2))
            alt = cv2.undistortPoints(grid, K2, D2, P=K2).reshape(-1, 2)
            H, _ = cv2.findHomography(alt.astype(np.float32), ref.astype(np.float32), 0)
            if H is None:
                worst = 99.0
                continue
            proj = cv2.perspectiveTransform(alt.reshape(-1, 1, 2).astype(np.float32), H.astype(np.float32)).reshape(-1, 2)
            worst = max(worst, float(np.max(np.linalg.norm(proj - ref, axis=1))))
    else:
        worst = None
    hfov = 2 * math.degrees(math.atan(w / (2 * K[0, 0])))
    return dict(views=n, rms=float(rms), residual=worst, correction=corr, fx=float(K[0, 0]), cx=float(K[0, 2]),
                cy=float(K[1, 2]), k1=float(D.ravel()[0]), hfov=hfov, w=w, h=h)


def coverage(views):
    cells = {c: 0 for row in CELL_ROWS for c in row}
    dirs = {d: 0 for d in "NESW"}
    tilted = 0
    for v in views:
        for c in cc.view_cells(v):
            cells[c] += 1
        if v.get("tilt") is not None and v["tilt"] >= tui.TILT_MIN:
            tilted += 1
            dirs[tui.az_bucket(v["az"])] += 1
    return cells, dirs, tilted


def next_hint(cells, dirs, tilted, views):
    if views < 4:
        return "HOLD THE BOARD UP HIGH, TILTED, OVER THE CENTER"
    corners = [c for c in ["NW", "NE", "SW", "SE"] if cells[c] < 2]
    edges = [c for c in ["N", "E", "S", "W"] if cells[c] < 2]
    missing_dirs = [d for d in "NESW" if dirs[d] == 0]
    if corners:
        d = missing_dirs[0] if missing_dirs else "NESW"[len(corners) % 4]
        return f"{CELL_WORDS[corners[0]]} corner, tilt it ({DIR_WORDS[d]})"
    if missing_dirs:
        return f"tilt more: {DIR_WORDS[missing_dirs[0]]}, anywhere"
    if edges:
        return f"{CELL_WORDS[edges[0]]} edge, tilted"
    if tilted < 10:
        return "a few more TILTED views, anywhere, 30-40 degrees"
    return "keep sweeping slowly, high and tilted"


# ------------------------------------------------------------------ threads --

class State:
    def __init__(self, cams):
        self.lock = threading.Lock()
        self.now = {c: "no board" for c in cams}
        self.analysis = {c: None for c in cams}
        self.solved_views = {c: 0 for c in cams}
        self.fps = 0.0
        self.stop = False
        self.accept = False
        self.t0 = time.time()
        self.good_since = {c: None for c in cams}


def capture_thread(args, cams, sessions, state, daemon):
    board = cc.make_board(bool(cc.LEGACY))
    det = cv2.aruco.CharucoDetector(board)
    aruco_det = cv2.aruco.ArucoDetector(board.getDictionary())
    quality = {c: cc.Quality() for c in cams}
    last_ts = {c: None for c in cams}
    frames = 0
    t0 = time.time()
    while not state.stop:
        for c in cams:
            try:
                fr = daemon.get_frame(c, encoding="png")
            except Exception as e:  # noqa: BLE001
                state.now[c] = f"camera error: {str(e)[:50]}"
                time.sleep(0.3)
                continue
            if fr.timestamp == last_ts[c]:
                continue
            last_ts[c] = fr.timestamp
            frames += 1
            state.fps = frames / max(1e-6, time.time() - t0)
            gray = cc.decode_gray(fr.data)
            h, w = gray.shape
            boards = cc.detect_boards(aruco_det, det, gray)
            if not boards:
                state.now[c] = "NO BOARD"
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
                where = "+".join(CELL_WORDS[x].lower() for x in cells[:2])
                if moving:
                    descr.append(f"{where}: MOVING")
                elif blurry:
                    descr.append(f"{where}: BLURRY")
                elif tilt is None or not conf:
                    descr.append(f"{where}: tilt unclear ({len(ids)} corners)")
                elif tilt >= tui.TILT_MIN:
                    descr.append(f"{where}: tilt {tilt:.0f}° ok")
                else:
                    descr.append(f"{where}: flat")
                if moving or blurry or tilt is None:
                    continue
                if not sessions[c].is_novel(corners, ids, tilt):
                    continue
                sessions[c].keep(gray, corners, ids, dict(
                    n=int(len(ids)), sharp=round(sh, 1), tilt=round(tilt, 1) if conf else None,
                    az=round(az, 1) if conf else None, cell=cc.cell_of(cx, cy, w, h), cells=cells,
                    w=w, h=h, cx=round(float(cx), 1), cy=round(float(cy), 1), spacing=round(sp, 1),
                    t=fr.timestamp))
            state.now[c] = "   ".join(descr)


def solver_thread(cams, sessions, state):
    while not state.stop:
        for c in cams:
            n = len(sessions[c].views)
            if n >= 6 and n - state.solved_views[c] >= SOLVE_EVERY:
                try:
                    a = mapping_residual_px(sessions[c])
                except Exception as e:  # noqa: BLE001
                    a = {"error": str(e)}
                with state.lock:
                    state.analysis[c] = a
                    state.solved_views[c] = n
        time.sleep(0.2)


# ---------------------------------------------------------------------- GUI --

def is_good(a):
    return (a is not None and "error" not in a and a["views"] >= MIN_VIEWS and a["rms"] < GOOD_RMS_PX
            and a["residual"] is not None and a["residual"] < GOOD_RESIDUAL_PX)


def run_gui(args, cams, sessions, state, daemon):
    root = tk.Tk()
    root.title("ChArUco calibration")
    root.configure(bg=BG)
    root.attributes("-fullscreen", True)
    root.update_idletasks()
    W, H = root.winfo_screenwidth(), root.winfo_screenheight()
    F_BANNER = max(24, H // 9)
    F_BIG = max(18, H // 16)
    F_MED = max(14, H // 26)
    F_SMALL = max(10, H // 44)
    fam = "Helvetica"

    banner = tk.Label(root, text="STARTING", font=(fam, F_BANNER, "bold"), bg=DIM, fg=FG, pady=H // 60)
    banner.pack(fill="x")

    cols = tk.Frame(root, bg=BG)
    cols.pack(fill="both", expand=True, padx=W // 60, pady=H // 80)
    widgets = {}
    for c in cams:
        col = tk.Frame(cols, bg=BG)
        col.pack(side="left", fill="both", expand=True, padx=W // 80)
        title = tk.Label(col, text=f"CAMERA {c}", font=(fam, F_BIG, "bold"), bg=BG, fg=FG)
        title.pack(anchor="w")
        err = tk.Label(col, text="error: --", font=(fam, F_BIG, "bold"), bg=BG, fg=FG, anchor="w")
        err.pack(anchor="w")
        res = tk.Label(col, text="could still change by: --", font=(fam, F_MED), bg=BG, fg=FG, anchor="w")
        res.pack(anchor="w")
        views = tk.Label(col, text="views: 0", font=(fam, F_MED), bg=BG, fg=FG, anchor="w")
        views.pack(anchor="w")
        now = tk.Label(col, text="", font=(fam, F_SMALL), bg=BG, fg=CYAN, anchor="w", wraplength=W // len(cams) - W // 20, justify="left")
        now.pack(anchor="w", pady=(0, H // 80))
        gridf = tk.Frame(col, bg=BG)
        gridf.pack(anchor="w")
        tile = max(30, min(W // (len(cams) * 9), H // 9))
        tiles = {}
        for r, row in enumerate(CELL_ROWS):
            for k, cell in enumerate(row):
                t = tk.Label(gridf, text=cell, font=(fam, F_SMALL, "bold"), bg=DIM, fg=FG, width=4, height=2)
                t.grid(row=r, column=k, padx=4, pady=4, ipadx=tile // 6, ipady=tile // 8)
                tiles[cell] = t
        dirf = tk.Frame(col, bg=BG)
        dirf.pack(anchor="w", pady=(H // 80, 0))
        tk.Label(dirf, text="TILT", font=(fam, F_SMALL, "bold"), bg=BG, fg=FG).pack(side="left", padx=(0, 10))
        dtiles = {}
        for d in "NESW":
            t = tk.Label(dirf, text=d, font=(fam, F_SMALL, "bold"), bg=DIM, fg=FG, width=3, height=1)
            t.pack(side="left", padx=4, ipadx=tile // 8, ipady=tile // 10)
            dtiles[d] = t
        widgets[c] = dict(err=err, res=res, views=views, now=now, tiles=tiles, dtiles=dtiles)

    hint = tk.Label(root, text="", font=(fam, F_BIG, "bold"), bg=BG, fg=YELLOW, wraplength=W - W // 20)
    hint.pack(fill="x", pady=(0, H // 80))
    footer = tk.Label(root, text="Enter = accept what I have now      Esc = quit without installing",
                      font=(fam, F_SMALL), bg=BG, fg="#909090")
    footer.pack(fill="x", pady=(0, H // 80))

    finish = {"why": None}

    def do_accept(_=None):
        finish["why"] = "accepted"
        state.stop = True
        root.destroy()

    def do_quit(_=None):
        finish["why"] = "quit"
        state.stop = True
        root.destroy()

    root.bind("<Return>", do_accept)
    root.bind("<KP_Enter>", do_accept)
    root.bind("<Escape>", do_quit)
    root.bind("q", do_quit)

    def refresh():
        if state.stop:
            return
        all_good = True
        hints = []
        for c in cams:
            wdg = widgets[c]
            v = list(sessions[c].views)
            with state.lock:
                a = state.analysis[c]
            good = is_good(a)
            all_good &= good
            cells, dirs, tilted = coverage(v)
            wdg["views"].config(text=f"views: {len(v)}   tilted: {tilted}")
            wdg["now"].config(text=state.now[c])
            if a is None:
                wdg["err"].config(text=f"error: need {max(0, 6 - len(v))} more views", fg=FG)
                wdg["res"].config(text="could still change by: --", fg=FG)
            elif "error" in a:
                wdg["err"].config(text="solve failed", fg=RED)
                wdg["res"].config(text=a["error"][:60], fg=RED)
            else:
                wdg["err"].config(text=f"error now: {a['rms']:.2f} px", fg=GREEN if a["rms"] < GOOD_RMS_PX else YELLOW)
                if a["residual"] is None:
                    wdg["res"].config(text=f"could still change by: (need {MIN_VIEWS - a['views']} more views)", fg=YELLOW)
                else:
                    ok = a["residual"] < GOOD_RESIDUAL_PX
                    wdg["res"].config(text=f"could still change by: {a['residual']:.1f} px   (good < {GOOD_RESIDUAL_PX:.0f})"
                                           f"      lens correction itself: {a['correction']:.1f} px",
                                      fg=GREEN if ok else YELLOW)
            for cell, t in wdg["tiles"].items():
                n = cells[cell]
                t.config(bg=GREEN if n >= 2 else (YELLOW if n == 1 else DIM), fg="#000000" if n else FG)
            for d, t in wdg["dtiles"].items():
                n = dirs[d]
                t.config(bg=GREEN if n >= 2 else (YELLOW if n == 1 else DIM), fg="#000000" if n else FG)
            if not good:
                hints.append(f"CAM {c}: {next_hint(cells, dirs, tilted, len(v))}")
            if good and state.good_since[c] is None:
                state.good_since[c] = time.time()
            if not good:
                state.good_since[c] = None
        if all_good and all(state.analysis[c] is not None for c in cams):
            banner.config(text="GOOD ENOUGH", bg=GREEN, fg="#000000")
            hint.config(text="Press Enter to finish and install.  Or keep going to tighten it further.", fg=GREEN)
        else:
            any_solve = any(state.analysis[c] is not None for c in cams)
            banner.config(text="KEEP GOING" if any_solve else "SHOW ME THE BOARD", bg=YELLOW if any_solve else DIM,
                          fg="#000000" if any_solve else FG)
            hint.config(text="\n".join(hints[:2]), fg=YELLOW)
        root.after(250, refresh)

    root.after(250, refresh)
    root.mainloop()
    return finish["why"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--board", default="5x7:37:28:DICT_5X5_100")
    ap.add_argument("--resume", default=None, metavar="DIR",
                    help="continue a previous session directory instead of starting a fresh one")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--test-seconds", type=float, default=0, help="auto-close after N s (smoke test)")
    args = ap.parse_args()
    cc.configure_board(args.board)
    cams = [c.strip() for c in args.cameras.split(",")]
    # Every run is a new session unless --resume is given explicitly.
    root = Path(args.resume).expanduser() if args.resume else \
        Path("~/.local/share/aprilcam/charuco-sessions").expanduser() / ("gui-" + time.strftime("%Y%m%d-%H%M%S"))
    root.mkdir(parents=True, exist_ok=True)
    print(f"session: {root}  ({'resumed' if args.resume else 'fresh'})")
    sessions = {c: cc.Session(root, c) for c in cams}
    state = State(cams)
    daemon = cc.Discovery().connect()
    threads = [threading.Thread(target=capture_thread, args=(args, cams, sessions, state, daemon), daemon=True),
               threading.Thread(target=solver_thread, args=(cams, sessions, state), daemon=True)]
    for t in threads:
        t.start()
    if args.test_seconds:
        def killer():
            time.sleep(args.test_seconds)
            state.stop = True
        threading.Thread(target=killer, daemon=True).start()
        try:
            why = _run_gui_with_watchdog(args, cams, sessions, state, daemon)
        except tk.TclError:
            why = "closed"
    else:
        why = run_gui(args, cams, sessions, state, daemon)
    state.stop = True
    print(f"finished: {why}")
    for c in cams:
        if not sessions[c].views:
            print(f"  [{c}] no views")
            continue
        msg, out = tui.solve_full(sessions[c])
        print(f"  [{c}] {msg}")
        if out is not None and args.install and why == "accepted":
            print(f"  [{c}] {fast.install(daemon, c, out)}")
    print(f"session: {root}")


def _run_gui_with_watchdog(args, cams, sessions, state, daemon):
    """run_gui, but a Tk after() loop closes the window once state.stop is set."""
    import tkinter as _tk
    orig_tk = _tk.Tk

    class WatchedTk(orig_tk):
        def mainloop(self, n=0):
            def watch():
                if state.stop:
                    try:
                        self.destroy()
                    except _tk.TclError:
                        pass
                    return
                self.after(200, watch)
            self.after(200, watch)
            return super().mainloop(n)
    _tk.Tk = WatchedTk
    tk.Tk = WatchedTk
    try:
        return run_gui(args, cams, sessions, state, daemon) or "accepted"
    finally:
        _tk.Tk = orig_tk
        tk.Tk = orig_tk


if __name__ == "__main__":
    main()
