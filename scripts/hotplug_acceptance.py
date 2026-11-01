#!/usr/bin/env python3
"""Linux hardware acceptance for camera hotplug (sprint 047 ticket 009).

Simulates unplug / re-plug of ONE real USB camera by writing its USB
device id to ``/sys/bus/usb/drivers/usb/unbind`` and ``.../bind``, and
asserts -- with no human action -- that the daemon:

  1. with an image stream and a tag stream subscribed: lists the camera
     absent within ~2 s and the streams deliver placeholder frames /
     ``camera_absent`` heartbeats (no silence, no error);
  2. ``camera image N`` while absent writes the card and exits 3;
  3. after bind, real frames resume IN THE SAME STREAMS within ~5 s, at
     the configured frame size, with the stored controls re-applied
     (checked with ``v4l2-ctl -C``) and no daemon restart / manual probe;
  4. ``camera probe`` with the camera held reports it present + usable
     (no EBUSY second open);
  5. ``camera close N`` -> ``camera list`` shows it ``closed``; the next
     ``camera image N`` reopens it with config; optionally idle close
     (``--idle-close``; temporarily sets APRILCAM_IDLE_CLOSE_S through a
     systemd drop-in, removed again in the ``finally``).

Usage (from a checkout, driving a remote daemon host over ssh)::

    uv run scripts/hotplug_acceptance.py --camera 2 \\
        --daemon buzzkill.home --ssh buzzkill [--usb-id 3-2.1.4]

or on the daemon host itself, as root::

    sudo python3 scripts/hotplug_acceptance.py --camera 2

``--usb-id`` is auto-detected from /sys/class/video4linux when omitted
(product match against the camera's name slug).  The camera is ALWAYS
re-bound in a ``finally``.  Exit 0 = all assertions passed; 1 = a
failure; 0 with a ``SKIP`` line when not Linux / not root / no ssh target.
Never run by the default test suite (only via the ``hardware``-marked
pytest wrapper when ``APRILCAM_HOTPLUG_ACCEPTANCE=1``).
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

# grpc + subprocess from a threaded process: silence the fork-handler chatter.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

T0 = time.monotonic()


def log(msg: str) -> None:
    print(f"[{time.monotonic() - T0:7.2f}s] {msg}", flush=True)


class Failure(AssertionError):
    pass


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)
    log(f"PASS  {msg}")


class Host:
    """Runs privileged / host-side commands locally or through ssh."""

    def __init__(self, ssh: Optional[str]) -> None:
        self.ssh = ssh

    def sh(self, cmd: str, *, sudo: bool = False, timeout: float = 30) -> subprocess.CompletedProcess:
        if sudo and os.geteuid() != 0 or (sudo and self.ssh):
            cmd = f"sudo bash -c {shlex.quote(cmd)}"
        argv = ["ssh", "-o", "BatchMode=yes", self.ssh, cmd] if self.ssh else ["bash", "-c", cmd]
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


class Streams:
    """Background readers recording every frame of an image + tag stream."""

    def __init__(self, daemon: Any, camera: str) -> None:
        self.lock = threading.Lock()
        self.images: List[Tuple[float, bool, int, int]] = []  # t, placeholder, w, h
        self.tags: List[Tuple[float, bool]] = []  # t, camera_absent
        self.errors: List[str] = []
        self.ended = {"image": False, "tag": False}
        self._streams = [daemon.stream_images(camera), daemon.stream_tags(camera)]
        for kind, stream, fn in (
            ("image", self._streams[0], self._read_images),
            ("tag", self._streams[1], self._read_tags),
        ):
            threading.Thread(target=self._guard, args=(kind, stream, fn), daemon=True).start()

    def _guard(self, kind: str, stream: Any, fn: Callable[[Any], None]) -> None:
        try:
            fn(stream)
        except Exception as exc:  # noqa: BLE001 - recorded, asserted on
            self.errors.append(f"{kind}: {type(exc).__name__}: {exc}")
        self.ended[kind] = True

    def _read_images(self, stream: Any) -> None:
        for f in stream:
            with self.lock:
                self.images.append((time.monotonic(), bool(f.placeholder), f.width, f.height))

    def _read_tags(self, stream: Any) -> None:
        for f in stream:
            with self.lock:
                self.tags.append((time.monotonic(), bool(f.camera_absent)))

    def image_since(self, t: float, *, placeholder: bool) -> List[Tuple[float, bool, int, int]]:
        with self.lock:
            return [x for x in self.images if x[0] >= t and x[1] == placeholder]

    def tags_since(self, t: float, *, absent: bool) -> List[Tuple[float, bool]]:
        with self.lock:
            return [x for x in self.tags if x[0] >= t and x[1] == absent]

    def close(self) -> None:
        for s in self._streams:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass


def wait_for(pred: Callable[[], Any], timeout: float, step: float = 0.1) -> Optional[float]:
    """Seconds until *pred* is truthy, or None on timeout."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if pred():
            return time.monotonic() - start
        time.sleep(step)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", required=True, help="camera number or slug (the ONLY camera touched)")
    ap.add_argument("--daemon", default=None, help="daemon host (default: local daemon)")
    ap.add_argument("--ssh", default=None, help="ssh target for privileged host commands (default: run locally)")
    ap.add_argument("--usb-id", default=None, help="USB device path, e.g. 3-2.1.4 (default: auto-detect)")
    ap.add_argument("--product", default=None, help="substring of the v4l2 device name (auto-detect aid)")
    ap.add_argument("--width", type=int, default=None, help="expected frame width (default: from config)")
    ap.add_argument("--height", type=int, default=None, help="expected frame height (default: from config)")
    ap.add_argument("--unplug-s", type=float, default=2.0, help="max seconds for absent detection")
    ap.add_argument("--replug-s", type=float, default=5.0, help="max seconds for real frames after bind")
    ap.add_argument("--idle-close", action="store_true", help="also exercise idle close (needs systemd + root)")
    args = ap.parse_args()

    if not args.ssh and sys.platform != "linux":
        log("SKIP: not Linux and no --ssh target")
        return 0
    host = Host(args.ssh)
    if not args.ssh and os.geteuid() != 0 and host.sh("sudo -n true").returncode != 0:
        log("SKIP: not root and no passwordless sudo")
        return 0

    from aprilcam.client.discovery import Discovery  # noqa: PLC0415

    daemon = Discovery().connect(args.daemon)
    cams = {i: c for i, c in enumerate(daemon.list_cameras())}
    cli = ["aprilcam"] + (["-d", args.daemon] if args.daemon else [])
    if shutil_which("aprilcam") is None:
        cli = [sys.executable, "-m", "aprilcam"] + cli[1:]

    def run_cli(*a: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(cli + list(a), capture_output=True, text=True, timeout=timeout)

    def cam_row() -> str:
        out = run_cli("camera", "list").stdout
        for line in out.splitlines():
            if re.match(rf"\s*\[{re.escape(args.camera)}\]", line) or f" {args.camera} " in f" {line} ":
                return line
        return ""

    row0 = cam_row()
    if not row0:
        log(f"SKIP: camera {args.camera} not listed by the daemon")
        return 0
    slug = row0.split()[1]
    if "present" not in row0.split() or "absent" in row0.split():
        log(f"SKIP: camera {args.camera} is not present before the test: {row0.strip()}")
        return 0
    _ = cams

    # -- expected configuration (what must be re-applied) ---------------
    cfg = daemon.get_camera_config(slug)
    settings = dict(cfg.settings)
    want_w = args.width or settings.get("frame_width")
    want_h = args.height or settings.get("frame_height")
    check(bool(want_w and want_h), f"config pins a frame size ({want_w}x{want_h})")
    want_ctrl = {
        "auto_exposure": settings.get("auto_exposure"),
        "exposure_time_absolute": settings.get("exposure"),
    }
    log(f"config settings: {settings}")

    # -- locate the USB device and its video node ------------------------
    usb_id, node = detect_usb(host, args.usb_id, args.product or slug.split("-usb")[0].replace("-", " "))
    if not usb_id:
        log("SKIP: could not map the camera to a USB device (use --usb-id)")
        return 0
    log(f"camera {args.camera} ({slug}) usb-id={usb_id} node={node}")

    def v4l2_state() -> Tuple[Optional[str], dict]:
        fmt = host.sh(f"v4l2-ctl -d {node} --get-fmt-video").stdout
        m = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", fmt)
        size = f"{m.group(1)}x{m.group(2)}" if m else None
        ctl = host.sh(f"v4l2-ctl -d {node} -C auto_exposure,exposure_time_absolute").stdout
        vals = {k: int(v) for k, v in re.findall(r"(\w+): (\d+)", ctl)}
        return size, vals

    pid0 = host.sh("systemctl show aprilcamd -p MainPID --value").stdout.strip() if args.ssh or Path("/run/systemd").exists() else ""
    since = host.sh("date '+%Y-%m-%d %H:%M:%S'").stdout.strip()
    streams: Optional[Streams] = None
    bound = True
    dropin = False
    try:
        streams = Streams(daemon, slug)
        ok = wait_for(lambda: streams.image_since(T0, placeholder=False), 10)
        check(ok is not None, "baseline: real frames flowing before unplug")
        sz, ctl = v4l2_state()
        log(f"baseline v4l2: {sz} {ctl}")

        # 4a. probe while the camera is held by live subscribers
        p = run_cli("camera", "probe")
        row = next((l for l in p.stdout.splitlines() if slug in l), "")
        check(p.returncode == 0 and " present " in row + " " and " usable " in row + " ",
              f"probe while held (subscribed) reports present+usable: {row.strip()!r}")
        check("busy" not in p.stdout.lower() and "ebusy" not in p.stderr.lower(),
              "probe while held reported no EBUSY/busy error")

        # 1. unplug --------------------------------------------------------
        log(f"UNBIND {usb_id}")
        r = host.sh(f"echo {usb_id} > /sys/bus/usb/drivers/usb/unbind", sudo=True)
        t_unbind = time.monotonic()
        bound = False
        check(r.returncode == 0, f"unbind succeeded ({r.stderr.strip()})")
        dt = wait_for(lambda: " absent " in cam_row() + " ", args.unplug_s + 3, 0.2)
        log(f"camera list shows absent after {dt and dt + 0.0:.2f}s" if dt is not None else "never absent")
        check(dt is not None and time.monotonic() - t_unbind <= args.unplug_s + 0.7,
              f"camera list shows camera absent within ~{args.unplug_s:.0f}s ({time.monotonic() - t_unbind:.2f}s since unbind)")
        dt_img = wait_for(lambda: streams.image_since(t_unbind, placeholder=True), args.unplug_s + 2)
        check(dt_img is not None, f"image stream delivered a placeholder frame ({dt_img and round(dt_img, 2)}s after unbind)")
        dt_tag = wait_for(lambda: streams.tags_since(t_unbind, absent=True), args.unplug_s + 3)
        check(dt_tag is not None, f"tag stream delivered camera_absent heartbeat ({dt_tag and round(dt_tag, 2)}s after unbind)")
        # no silence: keep watching for 3 s, frames must keep arriving
        t_mark = time.monotonic()
        time.sleep(3.0)
        n_ph = len(streams.image_since(t_mark, placeholder=True))
        n_hb = len(streams.tags_since(t_mark, absent=True))
        check(n_ph >= 3, f"placeholder frames keep flowing while absent ({n_ph} in 3 s)")
        check(n_hb >= 2, f"camera_absent heartbeats keep flowing while absent ({n_hb} in 3 s)")
        check(not streams.image_since(t_unbind + args.unplug_s + 1, placeholder=False),
              "no real frames while unplugged")
        check(not streams.errors and not any(streams.ended.values()),
              f"streams stayed open without error while absent ({streams.errors})")

        # 2. camera image while absent --------------------------------------
        out = Path(tempfile.mkdtemp(prefix="hotplug-")) / "card.jpg"
        r = run_cli("camera", "image", slug, "-o", str(out))
        check(r.returncode == 3, f"`camera image` while absent exits 3 (got {r.returncode}; stderr={r.stderr.strip()!r})")
        check(out.exists() and out.stat().st_size > 1000, f"`camera image` wrote the card ({out.stat().st_size if out.exists() else 0} bytes)")

        # 3. re-plug ---------------------------------------------------------
        log(f"BIND {usb_id}")
        r = host.sh(f"echo {usb_id} > /sys/bus/usb/drivers/usb/bind", sudo=True)
        t_bind = time.monotonic()
        bound = True
        check(r.returncode == 0, f"bind succeeded ({r.stderr.strip()})")
        dt = wait_for(lambda: streams.image_since(t_bind, placeholder=False), args.replug_s + 3)
        check(dt is not None and dt <= args.replug_s,
              f"real frames resumed in the SAME image stream {dt and round(dt, 2)}s after bind (limit {args.replug_s}s)")
        real = streams.image_since(t_bind, placeholder=False)
        time.sleep(1.5)
        real = streams.image_since(t_bind, placeholder=False)
        sizes = {(w, h) for _, _, w, h in real}
        check(sizes == {(want_w, want_h)}, f"resumed frames are {want_w}x{want_h} (saw {sorted(sizes)})")
        dt = wait_for(lambda: streams.tags_since(t_bind, absent=False), 5)
        check(dt is not None, "tag stream resumed non-absent frames in the same stream")
        check(" absent " not in cam_row() + " " and " present " in cam_row() + " ", f"camera list shows present again: {cam_row().strip()!r}")
        sz, ctl = v4l2_state()
        log(f"after bind v4l2: {sz} {ctl}")
        check(sz == f"{want_w}x{want_h}", f"device format is {want_w}x{want_h} (v4l2: {sz})")
        for k, v in want_ctrl.items():
            if v is not None:
                check(ctl.get(k) == int(v), f"control {k} == {v} after replug (v4l2: {ctl.get(k)})")
        pid1 = host.sh("systemctl show aprilcamd -p MainPID --value").stdout.strip() if pid0 else ""
        check(pid0 == pid1, f"daemon not restarted (MainPID {pid0} -> {pid1})")
        check(not streams.errors and not any(streams.ended.values()), f"streams alive, no errors ({streams.errors})")

        # 4b. probe while held with NO subscribers --------------------------
        streams.close()
        streams = None
        time.sleep(1.0)
        p = run_cli("camera", "probe")
        row = next((l for l in p.stdout.splitlines() if slug in l), "")
        check(p.returncode == 0 and " present " in row + " " and " usable " in row + " ",
              f"probe while held idle reports present+usable: {row.strip()!r}")
        check("busy" not in p.stdout.lower() and "ebusy" not in p.stderr.lower(), "idle probe reported no EBUSY/busy error")

        # 5. explicit close + reopen ---------------------------------------
        r = run_cli("camera", "close", slug)
        check(r.returncode == 0, f"`camera close` ok ({r.stdout.strip()}{r.stderr.strip()})")
        check("closed" in cam_row(), f"camera list shows closed: {cam_row().strip()!r}")
        out2 = out.with_name("real.jpg")
        r = run_cli("camera", "image", slug, "-o", str(out2))
        check(r.returncode == 0 and out2.stat().st_size > 1000, f"`camera image` after close reopens it, exit 0 ({r.returncode})")
        check("closed" not in cam_row(), f"camera list no longer closed: {cam_row().strip()!r}")
        sz, ctl = v4l2_state()
        check(sz == f"{want_w}x{want_h}", f"reopen applied frame size {want_w}x{want_h} (v4l2: {sz})")
        for k, v in want_ctrl.items():
            if v is not None:
                check(ctl.get(k) == int(v), f"reopen applied {k} == {v} (v4l2: {ctl.get(k)})")

        # 5b. optional idle close ---------------------------------------------
        if args.idle_close:
            idle_s = 5
            log(f"setting APRILCAM_IDLE_CLOSE_S={idle_s} via temporary systemd drop-in")
            dropin = True
            host.sh(
                "mkdir -p /etc/systemd/system/aprilcamd.service.d && printf "
                f"'[Service]\\nEnvironment=APRILCAM_IDLE_CLOSE_S={idle_s}\\n' > "
                "/etc/systemd/system/aprilcamd.service.d/zz-hotplug-test.conf && systemctl daemon-reload && systemctl restart aprilcamd",
                sudo=True,
            )
            time.sleep(4)
            daemon = Discovery().connect(args.daemon)
            r = run_cli("camera", "image", slug, "-o", str(out2))
            check(r.returncode == 0, "camera image (opens the camera under short idle timeout)")
            dt = wait_for(lambda: "closed" in cam_row(), idle_s * 2 + 8, 0.5)
            check(dt is not None, f"idle close fired and camera list shows closed ({dt and round(dt, 1)}s)")
            r = run_cli("camera", "image", slug, "-o", str(out2))
            check(r.returncode == 0, "next request reopens an idle-closed camera")
    finally:
        if streams:
            streams.close()
        if not bound:
            r = host.sh(f"echo {usb_id} > /sys/bus/usb/drivers/usb/bind", sudo=True)
            log(f"FINALLY: re-bound {usb_id} (rc={r.returncode} {r.stderr.strip()})")
        if dropin:
            host.sh(
                "rm -f /etc/systemd/system/aprilcamd.service.d/zz-hotplug-test.conf; "
                "rmdir /etc/systemd/system/aprilcamd.service.d 2>/dev/null; "
                "systemctl daemon-reload && systemctl restart aprilcamd",
                sudo=True,
            )
            log("FINALLY: removed idle-close drop-in, restarted daemon")

    # -- journal: no error spam -----------------------------------------------
    j = host.sh(f"journalctl -u aprilcamd --since '{since}' --no-pager -o cat", sudo=True).stdout
    bad = [l for l in j.splitlines() if re.search(r"\b(ERROR|CRITICAL|Traceback|WARNING)\b", l)]
    log(f"journal: {len(j.splitlines())} lines, {len(bad)} warning/error lines")
    for l in bad[:15]:
        log(f"  journal> {l}")
    check(len(bad) <= 10, f"no error spam in the daemon journal ({len(bad)} warning/error lines)")
    log("ALL ASSERTIONS PASSED")
    return 0


def shutil_which(name: str) -> Optional[str]:
    import shutil  # noqa: PLC0415

    return shutil.which(name)


def detect_usb(host: Host, usb_id: Optional[str], product: str) -> Tuple[Optional[str], str]:
    """(usb device path, capture node) for the camera, from sysfs."""
    script = (
        "for d in /sys/class/video4linux/video*; do "
        'echo "$(basename $d)|$(cat $d/name)|$(cat $d/index)|$(basename $(readlink -f $d/device/..))"; done'
    )
    rows = [l.split("|") for l in host.sh(script).stdout.splitlines() if l.count("|") == 3]
    want = product.lower()
    for dev, name, index, usb in rows:
        if index != "0":
            continue
        if usb_id and usb != usb_id:
            continue
        if usb_id or want in name.lower() or name.lower().split(":")[0] in want:
            return usb, f"/dev/{dev}"
    return None, ""


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as exc:
        log(f"FAIL: {exc}")
        sys.exit(1)
