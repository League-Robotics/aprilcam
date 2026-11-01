#!/bin/bash
# Build and install uvcvideo with the max_payload bandwidth cap via DKMS.
# Run on the daemon host (Ubuntu). See
# docs/knowledge/2026-10-31-linux-uvc-daemon-host-buzzkill.md, section 3.
#
#   bash install.sh [CAP]     # CAP defaults to 1600 bytes per microframe
set -euo pipefail

CAP=${1:-1600}
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=uvcvideo-bwcap
VER=1.0
SRC=/usr/src/$NAME-$VER

# Upstream version of the running Ubuntu kernel, e.g. "7.0.14".
UPSTREAM=$(awk '{print $NF}' /proc/version_signature)
BASE="https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain/drivers/media/usb/uvc"

sudo apt-get install -y dkms "linux-headers-$(uname -r)"

work=$(mktemp -d)
for f in Makefile Kconfig uvc_ctrl.c uvc_debugfs.c uvc_driver.c uvc_entity.c \
         uvc_isight.c uvc_metadata.c uvc_queue.c uvc_status.c uvc_v4l2.c \
         uvc_video.c uvcvideo.h; do
  curl -sfo "$work/$f" "$BASE/$f?h=v$UPSTREAM"
done
patch -d "$work" -p5 < "$HERE/max_payload.patch"

if dkms status "$NAME/$VER" | grep -q .; then
  sudo dkms remove "$NAME/$VER" --all
fi
sudo rm -rf "$SRC"
sudo mkdir -p "$SRC"
sudo cp "$work"/* "$HERE/dkms.conf" "$SRC/"
rm -rf "$work"

echo "options uvcvideo max_payload=$CAP" | sudo tee /etc/modprobe.d/$NAME.conf
sudo dkms install "$NAME/$VER"

# Swap the running driver (stop the daemon so nothing holds a camera).
sudo systemctl stop aprilcamd || true
sudo rmmod uvcvideo || true
sudo modprobe uvcvideo
echo "max_payload=$(cat /sys/module/uvcvideo/parameters/max_payload)"
sudo systemctl start aprilcamd || true
