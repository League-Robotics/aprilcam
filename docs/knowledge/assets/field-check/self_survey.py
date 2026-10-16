"""Self-survey a field from one calibrated-lens camera: solve the planar pose from
the marker corners (46 mm known size), project each marker centre onto the field
plane, re-anchor the frame on the four corner markers, iterate. Prints the
measured survey and writes a definition with it."""
import json, os, sys, cv2, numpy as np
from aprilcam.vision.detector import TagDetector, DetectorConfig
from aprilcam.types import TagFamily, PixelPoint
from aprilcam.calibration.interfaces import MarkerObservation
from aprilcam.calibration import pose as P

frame, defn_path, lens_path, out_path = sys.argv[1:5]
defn = json.load(open(defn_path)); size = float(defn['playfield']['marker_size_mm'])
survey = {t['id']: (float(t['x']), float(t['y'])) for t in defn['aruco_tags']}
lens = json.load(open(lens_path)); K = np.array(lens['camera_matrix'], float); D = np.array(lens['dist_coeffs'], float)
gray = cv2.cvtColor(cv2.imread(frame), cv2.COLOR_BGR2GRAY)
det = TagDetector(DetectorConfig(corner_refine="subpix", detect_inverted=False))
obs = {}
for d in det.detect(gray):
    if d.family == TagFamily.ARUCO and d.number in survey:
        c = [PixelPoint(x=float(x), y=float(y)) for x, y in d.corners]
        obs[d.number] = MarkerObservation(tag_id=d.number, centre=d.center, corners=tuple(c), frames=1, corner_spread_px=0.0)
ids = sorted(obs); print("markers used:", ids)
cur = {i: survey[i] for i in ids}
corner_ids = [i for i in (2, 0, 3, 1) if i in ids]  # NW NE SW SE
und = lambda pts: cv2.undistortPoints(np.asarray(pts, float).reshape(-1, 1, 2), K, D, P=K).reshape(-1, 2)
for it in range(8):
    surveyed = {i: P.SurveyedMarker(tag_id=i, centre_cm=cur[i], size_mm=size) for i in ids}
    rots = P.detect_marker_rotations([obs[i] for i in ids], surveyed, K, D)
    obj = np.vstack([P.marker_object_points(cur[i], size, rots[i].quarter_turns) for i in ids])
    img = np.vstack([[[c.x, c.y] for c in obs[i].corners] for i in ids]).astype(float)
    pose = P.solve_planar_pose(obj, img, K, D)
    H = P.homography_from_pose(pose, K)
    new = {}
    for i in ids:
        cu = und([[obs[i].centre.x, obs[i].centre.y]])
        w = cv2.perspectiveTransform(cu.reshape(-1, 1, 2), H).reshape(2); new[i] = (float(w[0]), float(w[1]))
    # re-anchor: origin = mean of corners; x axis = west-side mean -> east-side mean
    pts = np.array([new[i] for i in corner_ids]); o = pts.mean(axis=0)
    west = np.mean([new[i] for i in corner_ids if i in (2, 3)], axis=0); east = np.mean([new[i] for i in corner_ids if i in (0, 1)], axis=0)
    ang = np.arctan2(east[1] - west[1], east[0] - west[0]); R = np.array([[np.cos(-ang), -np.sin(-ang)], [np.sin(-ang), np.cos(-ang)]])
    new = {i: tuple((R @ (np.array(new[i]) - o)).tolist()) for i in ids}
    shift = max(np.hypot(new[i][0] - cur[i][0], new[i][1] - cur[i][1]) for i in ids)
    cur = new
    h, nadir, tilt = P.camera_position_from_pose(pose)
    print(f"iter {it}: rms {pose.rms_px:.2f} px, height {h:.1f} cm, tilt {tilt:.1f} deg, max centre shift {shift:.2f} cm")
    if shift < 0.01: break
print("\nmeasured survey (cm) vs nominal:")
for i in ids:
    x, y = cur[i]; nx, ny = survey[i]
    print(f"  id {i}: ({x:7.2f}, {y:7.2f})   nominal ({nx:6.1f}, {ny:6.1f})   moved {np.hypot(x-nx, y-ny):.2f} cm   quarter_turns {rots[i].quarter_turns}")
w = np.mean([cur[0][0], cur[1][0]]) - np.mean([cur[2][0], cur[3][0]]); hgt = np.mean([cur[2][1], cur[0][1]]) - np.mean([cur[3][1], cur[1][1]])
print(f"corner rectangle: {w:.1f} x {hgt:.1f} cm (nominal {defn['playfield']['width_cm']} x {defn['playfield']['height_cm']})")
for t in defn['aruco_tags']:
    if t['id'] in cur: t['x'], t['y'] = round(cur[t['id']][0], 1), round(cur[t['id']][1], 1)
defn['playfield']['width_cm'] = round(float(w), 1); defn['playfield']['height_cm'] = round(float(hgt), 1)
json.dump(defn, open(out_path, 'w'), indent=2); open(out_path, 'a').write('\n'); print("wrote", out_path)
