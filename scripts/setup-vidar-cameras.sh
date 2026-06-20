#!/bin/bash
#
# setup-vidar-cameras.sh — bring AprilCam up on a Raspberry Pi 5 with CSI cameras.
#
# Validated on vidar.local (Pi 5, Ubuntu 24.04, 2x IMX296). Idempotent: safe to
# re-run. See docs/knowledge/raspberry-pi-camera-setup.md for the rationale and
# every gotcha. To be replaced by Ansible; ad hoc for now.
#
# Architecture: a per-camera libcamerasrc->v4l2loopback bridge owns each CSI
# camera; the AprilCam daemon reads the loopback devices via plain V4L2. No
# GStreamer/libcamera runs inside the daemon (it segfaults there); the daemon
# venv uses pip opencv-contrib-python, not system cv2 (ABI clash).
#
# Usage:  bash scripts/setup-vidar-cameras.sh
# Run as a user with passwordless sudo, with the repo cloned (default ~/aprilcam).
set -euo pipefail

USER_NAME="${SUDO_USER:-$(id -un)}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
REPO="${APRILCAM_REPO:-$USER_HOME/aprilcam}"
VENV="$REPO/.venv"
LOOPBACK_NRS="70,71"           # /dev/video70, /dev/video71
LOOPBACK_LABELS="aprilcam-imx296-88000,aprilcam-imx296-80000"

echo "== AprilCam Pi camera setup ==  user=$USER_NAME repo=$REPO"

# --------------------------------------------------------------------------
# 1. System libcamera 0.5.x (PiSP) + GStreamer plugin -> /usr/local
#    Skipped if a working `cam` (with PiSP) is already on PATH.
# --------------------------------------------------------------------------
if command -v cam >/dev/null && cam -l 2>/dev/null | grep -q ':'; then
  echo "[1/6] libcamera: 'cam -l' already lists cameras — skipping build."
else
  echo "[1/6] Building libcamera (rpi fork) + libpisp -> /usr/local ..."
  sudo apt-get update
  sudo apt-get install -y git meson ninja-build pkg-config \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    python3-yaml python3-ply python3-jinja2 libyaml-dev libssl-dev libdw-dev libudev-dev
  sudo mkdir -p /opt/src && sudo chown "$USER_NAME" /opt/src
  [ -d /opt/src/libpisp ]   || git clone https://github.com/raspberrypi/libpisp.git   /opt/src/libpisp
  [ -d /opt/src/libcamera ] || git clone https://github.com/raspberrypi/libcamera.git /opt/src/libcamera
  meson setup /opt/src/libpisp/build /opt/src/libpisp --prefix=/usr/local || \
    meson configure /opt/src/libpisp/build -Dprefix=/usr/local
  sudo ninja -C /opt/src/libpisp/build install
  meson setup /opt/src/libcamera/build /opt/src/libcamera --prefix=/usr/local \
    -Dpipelines=rpi/vc4,rpi/pisp -Dipas=rpi/vc4,rpi/pisp \
    -Dgstreamer=enabled -Dcam=enabled -Dv4l2=enabled || \
    meson configure /opt/src/libcamera/build -Dprefix=/usr/local -Dgstreamer=enabled
  sudo ninja -C /opt/src/libcamera/build install
  sudo ldconfig
  sudo apt-get remove -y gstreamer1.0-libcamera || true   # retire stale 0.2.0 plugin
  sudo ln -sf /usr/local/lib/gstreamer-1.0/libgstlibcamera.so \
              /usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstlibcamera.so
fi

# --------------------------------------------------------------------------
# 2. v4l2loopback: dkms (+ matching kernel headers), persistent module + perms
# --------------------------------------------------------------------------
echo "[2/6] v4l2loopback ..."
sudo apt-get install -y "linux-headers-$(uname -r)" v4l2loopback-dkms v4l-utils
sudo dkms autoinstall 2>/dev/null || true
echo "v4l2loopback" | sudo tee /etc/modules-load.d/aprilcam.conf >/dev/null
echo "options v4l2loopback video_nr=${LOOPBACK_NRS} card_label=${LOOPBACK_LABELS} exclusive_caps=1,1 max_buffers=3" \
  | sudo tee /etc/modprobe.d/aprilcam.conf >/dev/null
echo 'SUBSYSTEM=="video4linux", ATTR{name}=="aprilcam-*", GROUP="video", MODE="0660"' \
  | sudo tee /etc/udev/rules.d/99-aprilcam.rules >/dev/null
sudo modprobe -r v4l2loopback 2>/dev/null || true
sudo modprobe v4l2loopback
sudo udevadm control --reload-rules && sudo udevadm trigger
sleep 1

# --------------------------------------------------------------------------
# 3. AprilCam venv — pip opencv-contrib-python (NOT system cv2; ABI clash)
# --------------------------------------------------------------------------
echo "[3/6] AprilCam venv ..."
[ -d "$REPO/.git" ] || git clone https://github.com/League-Robotics/aprilcam.git "$REPO"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
# [daemon] pulls opencv-contrib-python + mss/websockets/msgpack/grpcio-reflection/...
"$VENV/bin/pip" install -q -e "$REPO[daemon]"

# --------------------------------------------------------------------------
# 4. Bridge launcher (libcamerasrc -> v4l2sink), one per camera position
# --------------------------------------------------------------------------
echo "[4/6] bridge launcher ..."
sudo tee /usr/local/bin/aprilcam-camera-bridge >/dev/null <<'SH'
#!/bin/bash
# args: <position 0-based> <loopback /dev/videoN>
POS="$1"; DEV="$2"
CID="$(cam -l 2>/dev/null | sed -n "s/^$((POS+1)): .*(\(.*\))$/\1/p")"
[ -z "$CID" ] && { echo "no libcamera camera at position $POS" >&2; exit 1; }
exec gst-launch-1.0 -q libcamerasrc camera-name="$CID" \
  ! video/x-raw,format=NV12,width=1280,height=720,framerate=30/1 \
  ! videoconvert ! video/x-raw,format=YUY2 ! v4l2sink device="$DEV" sync=false
SH
sudo chmod +x /usr/local/bin/aprilcam-camera-bridge

# --------------------------------------------------------------------------
# 5. systemd SYSTEM services (SupplementaryGroups=video needs system, not user)
# --------------------------------------------------------------------------
echo "[5/6] systemd services ..."
sudo tee /etc/systemd/system/aprilcam-bridge@.service >/dev/null <<SVC
[Unit]
Description=AprilCam libcamera->v4l2loopback bridge (camera %i)
After=systemd-modules-load.service
[Service]
User=${USER_NAME}
SupplementaryGroups=video
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/local/bin/aprilcam-camera-bridge %i /dev/video7%i
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
SVC

sudo tee /etc/systemd/system/aprilcamd.service >/dev/null <<SVC
[Unit]
Description=AprilCam daemon
After=network.target aprilcam-bridge@0.service aprilcam-bridge@1.service
Wants=aprilcam-bridge@0.service aprilcam-bridge@1.service
[Service]
User=${USER_NAME}
SupplementaryGroups=video
Environment=APRILCAM_CAMERA_BACKEND=libcamera
Environment=GRPC_ENABLE_FORK_SUPPORT=0
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=${VENV}/bin/python -m aprilcam.daemon
Restart=on-failure
RestartSec=3
[Install]
WantedBy=multi-user.target
SVC

# --------------------------------------------------------------------------
# 6. Enable + start
# --------------------------------------------------------------------------
echo "[6/6] enable + start ..."
sudo systemctl daemon-reload
sudo systemctl enable --now aprilcam-bridge@0.service aprilcam-bridge@1.service
sleep 5
sudo systemctl enable --now aprilcamd.service
sleep 5

echo "== status =="
systemctl is-active aprilcam-bridge@0 aprilcam-bridge@1 aprilcamd
echo "== cameras =="
APRILCAM_DAEMON_HOST=127.0.0.1 "$VENV/bin/aprilcam" cameras || true
echo "done — expect exactly two cameras above."
