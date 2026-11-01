#!/usr/bin/env bash
# provision-uvc-host.sh -- bring a fresh Ubuntu x86 host with USB (UVC)
# cameras to the point where deploy/aprilcamd.service can be installed.
# The UVC-host counterpart of provision-pi.sh (buzzkill, 2026-10-31).
# Idempotent: safe to re-run, and re-run it after a kernel upgrade if
# the cameras start failing with "Not enough bandwidth" in dmesg.
#
# Same boundary as provision-pi.sh: it never installs the wheel, the
# daemon unit, or /etc/aprilcam config, and never bounces a running
# daemon -- if aprilcamd holds the cameras, a driver swap is left for a
# reboot (or the commands it prints).
#
# What it does, all idempotently:
#   1. apt-get installs v4l-utils, pipx, dkms, build tools, and the
#      running kernel's headers.
#   2. Creates the `aprilcam` system user (home /var/lib/aprilcam, as
#      aprilcamd.service's StateDirectory) and adds it, and the invoking
#      sudo user, to the `video` group.
#   3. Builds uvcvideo with the max_payload bandwidth cap
#      (deploy/uvcvideo-bwcap/) through DKMS: fetches the uvc driver
#      source for the running kernel's upstream version from kernel.org,
#      applies max_payload.patch, and installs it as uvcvideo-bwcap/1.0
#      -- rebuilding only when that source differs from what is
#      installed or the running kernel has no build yet.
#   4. Installs deploy/modprobe.d/uvcvideo-bwcap.conf.
#   5. Loads the patched driver if the stock one is running and no daemon
#      holds the cameras; if only the cap value changed, sets it live.
#
# See docs/knowledge/2026-10-31-linux-uvc-daemon-host-buzzkill.md for why.
#
# Usage:
#   ./deploy/provision-uvc-host.sh user@host   # from the dev machine, over SSH
#   sudo ./deploy/provision-uvc-host.sh --local  # on the target host
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)

do_provision() {
    set -euo pipefail
    local assets="$1"

    if [ "$(id -u)" -ne 0 ]; then
        echo "do_provision: must run as root (use sudo)" >&2
        exit 1
    fi
    local invoking_user="${SUDO_USER:-}"
    local kernel
    kernel=$(uname -r)

    echo "--- [1/5] apt packages ---"
    apt-get update -qq
    apt-get install -y v4l-utils pipx dkms build-essential curl patch "linux-headers-$kernel"

    echo "--- [2/5] aprilcam system user and video group ---"
    if ! getent passwd aprilcam >/dev/null; then
        useradd --system --no-create-home --home-dir /var/lib/aprilcam --shell /usr/sbin/nologin aprilcam
        echo "created system user aprilcam"
    else
        echo "user aprilcam already exists"
    fi
    mkdir -p /var/lib/aprilcam
    chown aprilcam:aprilcam /var/lib/aprilcam
    chmod 750 /var/lib/aprilcam
    usermod -aG video aprilcam
    if [ -n "$invoking_user" ] && [ "$invoking_user" != "aprilcam" ]; then
        usermod -aG video "$invoking_user"
    fi

    echo "--- [3/5] uvcvideo-bwcap (DKMS) ---"
    local name=uvcvideo-bwcap ver=1.0
    local src=/usr/src/$name-$ver
    # Ubuntu's signature ends with the upstream version, e.g. "7.0.14".
    local upstream
    upstream=$(awk '{print $NF}' /proc/version_signature)
    local base="https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain/drivers/media/usb/uvc"
    local work
    work=$(mktemp -d)
    local f
    for f in Makefile Kconfig uvc_ctrl.c uvc_debugfs.c uvc_driver.c uvc_entity.c \
             uvc_isight.c uvc_metadata.c uvc_queue.c uvc_status.c uvc_v4l2.c \
             uvc_video.c uvcvideo.h; do
        curl -sfo "$work/$f" "$base/$f?h=v$upstream"
    done
    patch -s -d "$work" -p5 < "$assets/uvcvideo-bwcap/max_payload.patch"
    cp "$assets/uvcvideo-bwcap/dkms.conf" "$work/"

    # Capture dkms status before matching: `dkms status | grep -q` under
    # pipefail fails whenever grep exits early (SIGPIPE), the trap
    # provision-pi.sh documents for lsmod.
    local status here_status
    status=$(dkms status "$name/$ver" 2>/dev/null || true)
    here_status=$(dkms status "$name/$ver" -k "$kernel" 2>/dev/null || true)
    local rebuilt=0
    if diff -rq "$work" "$src" >/dev/null 2>&1 && [[ "$here_status" == *installed* ]]; then
        echo "$name/$ver already built from v$upstream source for $kernel"
    else
        echo "building $name/$ver from upstream v$upstream for $kernel"
        if [ -n "$status" ]; then
            dkms remove "$name/$ver" --all
        fi
        rm -rf "$src"
        mkdir -p "$src"
        cp "$work"/* "$src/"
        # Every installed kernel with headers, not just the running one: a
        # newer kernel already installed (pending reboot) would otherwise
        # boot with the stock driver.
        local k
        for k in /lib/modules/*/build; do
            k=$(basename "$(dirname "$k")")
            dkms install "$name/$ver" -k "$k"
        done
        rebuilt=1
    fi
    rm -rf "$work"

    echo "--- [4/5] modprobe config ---"
    local conf=/etc/modprobe.d/uvcvideo-bwcap.conf
    if cmp -s "$assets/modprobe.d/uvcvideo-bwcap.conf" "$conf"; then
        echo "$conf already up to date"
    else
        install -m 0644 "$assets/modprobe.d/uvcvideo-bwcap.conf" "$conf"
        echo "installed $conf"
    fi
    local cap
    cap=$(sed -n 's/^options uvcvideo .*max_payload=\([0-9]*\).*/\1/p' "$conf")

    echo "--- [5/5] load the patched driver ---"
    local param=/sys/module/uvcvideo/parameters/max_payload
    if ! systemctl is-active --quiet aprilcamd && { [ ! -e "$param" ] || [ "$rebuilt" -eq 1 ]; }; then
        # modprobe first so its dependencies (videobuf2, uvc) are loaded;
        # rmmod then removes only uvcvideo itself.
        modprobe uvcvideo
        rmmod uvcvideo
        modprobe uvcvideo
        echo "loaded patched uvcvideo, max_payload=$(cat "$param")"
    elif [ -e "$param" ]; then
        if [ "$(cat "$param")" != "$cap" ]; then
            echo "$cap" > "$param"
            echo "set max_payload=$cap live (takes effect on each camera's next stream start)"
        else
            echo "patched uvcvideo loaded, max_payload=$cap"
        fi
        if [ "$rebuilt" -eq 1 ]; then
            echo "(the rebuilt module replaces the loaded one at the next reboot)"
        fi
    else
        echo "aprilcamd is running and holds the cameras -- not swapping the driver under it."
        echo "Reboot, or: sudo systemctl stop aprilcamd && sudo rmmod uvcvideo && sudo modprobe uvcvideo && sudo systemctl start aprilcamd"
    fi

    echo ""
    echo "Provisioning complete."
}

usage() {
    echo "Usage: $0 user@host   (provision a remote host over SSH)" >&2
    echo "       $0 --local     (provision this machine directly, needs sudo)" >&2
    exit 1
}

[ $# -eq 1 ] || usage

if [ "$1" = "--local" ]; then
    echo "==> Provisioning this host for AprilCam..."
    do_provision "$HERE"
else
    TARGET="$1"
    echo "==> Provisioning ${TARGET} for AprilCam..."
    # Ship the assets the function reads, then the function itself -- one
    # copy of the logic, run as root on the far end.
    REMOTE_DIR=$(ssh "$TARGET" mktemp -d)
    COPYFILE_DISABLE=1 tar --no-xattrs -C "$HERE" -cf - uvcvideo-bwcap/max_payload.patch uvcvideo-bwcap/dkms.conf \
        modprobe.d/uvcvideo-bwcap.conf | ssh "$TARGET" tar -C "$REMOTE_DIR" -xf -
    # shellcheck disable=SC2087
    ssh "$TARGET" "sudo bash -s" <<EOF
$(declare -f do_provision)
do_provision "$REMOTE_DIR"
rm -rf "$REMOTE_DIR"
EOF
fi

echo "==> Done."
echo ""
echo "NEXT STEPS -- see deploy/README.md:"
echo "  1. Build the wheel on the dev machine, copy it over, pipx install as the aprilcam user."
echo "  2. Copy config/aprilcam/ to /etc/aprilcam (owned by aprilcam)."
echo "  3. Copy + enable deploy/aprilcamd.service."
