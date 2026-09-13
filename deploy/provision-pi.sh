#!/usr/bin/env bash
# provision-pi.sh -- bring a fresh Raspberry Pi to the point where the
# deploy/ units (module config already active) can be installed
# (sprint 040 ticket 001, SUC-003). Idempotent: safe to re-run.
#
# What this script does NOT do (deliberately -- sprint.md Step 3's
# provision-pi.sh Boundary: "never installs or starts the units
# themselves, that stays a separate, explicit step per the runbook, so a
# re-provision never silently bounces a running daemon"):
#   - does not copy or enable any systemd unit
#   - does not build or install the aprilcam wheel
#   - does not build libcamera/libpisp from source -- vidar.local already
#     has the Pi 5 PiSP build done (archive/docs/knowledge/
#     raspberry-pi-camera-setup.md, "One-time system build"); a Pi that
#     does not have PiSP support yet needs that one-time build done by
#     hand first, following that doc -- see deploy/README.md.
#
# What it DOES do, all idempotently:
#   1. apt-get installs v4l-utils, gstreamer1.0-tools, pipx (if absent),
#      and the libcamera GStreamer plugin package -- but only as a
#      fallback when no working libcamera install is already on PATH
#      (installing the stock package over a from-source PiSP build like
#      vidar's would just be inert or, per v1's own setup script, is
#      explicitly retired in favour of the newer build).
#   2. Creates the dedicated `aprilcam` system user (no login shell, no
#      home directory created by useradd -M -- but a home *path* of
#      /var/lib/aprilcam is recorded in /etc/passwd, matching the
#      aprilcamd.service unit's own StateDirectory=aprilcam, because
#      pipx needs a writable $HOME to install into as this user; see
#      deploy/README.md step 4).
#   3. Adds `aprilcam`, and the invoking (sudo) user, to the `video`
#      group.
#   4. Installs the v4l2loopback module config
#      (deploy/modprobe.d/aprilcam.conf, deploy/modules-load.d/
#      aprilcam.conf) -- writing each file only if its content actually
#      changed -- and loads/reloads the module only when the config
#      changed or it was not already loaded, never unconditionally on
#      every run (a running bridge would lose its device out from under
#      it if this script blindly reloaded the module every time).
#
# Usage:
#   ./deploy/provision-pi.sh user@host      # from the dev machine, over SSH
#   sudo ./deploy/provision-pi.sh --local   # run directly on the target Pi
set -euo pipefail

# ---------------------------------------------------------------------
# The actual provisioning steps, as one function -- run either locally
# (sourced by --local, below) or remotely (serialized via `declare -f`
# and executed inside the SSH session), so there is exactly one copy of
# this logic rather than a second, independently-maintained remote copy.
# ---------------------------------------------------------------------
do_provision() {
    set -euo pipefail

    if [ "$(id -u)" -ne 0 ]; then
        echo "do_provision: must run as root (use sudo)" >&2
        exit 1
    fi
    INVOKING_USER="${SUDO_USER:-}"

    echo "--- [1/4] apt packages ---"
    apt-get update -qq
    apt-get install -y v4l-utils gstreamer1.0-tools pipx

    if command -v cam >/dev/null 2>&1 && cam -l 2>/dev/null | grep -q ':'; then
        echo "libcamera already working ('cam -l' lists cameras) -- not touching the libcamera GStreamer plugin package"
    else
        echo "no working libcamera found -- installing the packaged GStreamer plugin as a fallback"
        echo "NOTE: Raspberry Pi 5's PiSP pipeline needs a from-source libcamera+libpisp build;"
        echo "      the packaged plugin alone will not see a Pi 5 CSI camera. See"
        echo "      archive/docs/knowledge/raspberry-pi-camera-setup.md and deploy/README.md."
        apt-get install -y gstreamer1.0-libcamera || echo "WARNING: gstreamer1.0-libcamera not installable on this OS release -- see the note above"
    fi

    echo "--- [2/4] aprilcam system user ---"
    if ! getent passwd aprilcam >/dev/null; then
        useradd --system --no-create-home --home-dir /var/lib/aprilcam --shell /usr/sbin/nologin aprilcam
        echo "created system user aprilcam"
    else
        echo "user aprilcam already exists"
    fi
    # StateDirectory=aprilcam (aprilcamd.service) will also (re)create this
    # on every daemon start, but pipx needs it to exist *before* that, as
    # $HOME for `sudo -u aprilcam -H pipx install ...` (deploy/README.md).
    mkdir -p /var/lib/aprilcam
    chown aprilcam:aprilcam /var/lib/aprilcam
    chmod 750 /var/lib/aprilcam

    echo "--- [3/4] video group membership ---"
    usermod -aG video aprilcam
    if [ -n "$INVOKING_USER" ] && [ "$INVOKING_USER" != "aprilcam" ]; then
        usermod -aG video "$INVOKING_USER"
        echo "added video group to aprilcam and $INVOKING_USER"
    else
        echo "added video group to aprilcam (no separate invoking user to also add)"
    fi

    echo "--- [4/4] v4l2loopback module config ---"
    CONF_CHANGED=0
    install_conf_if_changed() {
        local content="$1" dst="$2"
        if [ -f "$dst" ] && printf '%s\n' "$content" | cmp -s - "$dst"; then
            echo "$dst already up to date"
            return
        fi
        printf '%s\n' "$content" > "$dst"
        chmod 0644 "$dst"
        CONF_CHANGED=1
        echo "installed $dst"
    }
    # Content must match deploy/modprobe.d/aprilcam.conf and
    # deploy/modules-load.d/aprilcam.conf exactly (tests/packaging/
    # test_deploy_assets.py asserts this) -- reproduces vidar.local's
    # already-working config; see those files' own comments for what
    # each option means and why max_buffers=16 matters.
    install_conf_if_changed \
        'options v4l2loopback video_nr=70,71 card_label=aprilcam-imx296-88000,aprilcam-imx296-80000 exclusive_caps=1,1 max_buffers=16' \
        /etc/modprobe.d/aprilcam.conf
    install_conf_if_changed 'v4l2loopback' /etc/modules-load.d/aprilcam.conf

    if [ "$CONF_CHANGED" -eq 1 ] || ! lsmod | grep -q '^v4l2loopback'; then
        echo "reloading v4l2loopback (config changed or module not loaded)"
        modprobe -r v4l2loopback 2>/dev/null || true
        modprobe v4l2loopback
    else
        echo "v4l2loopback already loaded with unchanged config -- not touching it (a running bridge would lose its device)"
    fi

    echo ""
    echo "Provisioning complete."
}

usage() {
    echo "Usage: $0 user@host   (provision a remote Pi over SSH)" >&2
    echo "       $0 --local     (provision this machine directly, needs sudo)" >&2
    exit 1
}

[ $# -eq 1 ] || usage

if [ "$1" = "--local" ]; then
    echo "==> Provisioning this host for AprilCam..."
    do_provision
else
    TARGET="$1"
    echo "==> Provisioning ${TARGET} for AprilCam..."
    # Ship the function definition itself over SSH and invoke it there --
    # one source of truth for the provisioning logic, run as root via sudo
    # on the far end (bash -s reads the script from stdin). Expansion is
    # deliberately local (unquoted EOF): $(declare -f do_provision) must
    # be evaluated here, not on the remote shell.
    # shellcheck disable=SC2087
    ssh "$TARGET" "sudo bash -s" <<EOF
$(declare -f do_provision)
do_provision
EOF
fi

echo "==> Done."
echo ""
echo "NEXT STEPS -- see deploy/README.md:"
echo "  1. Build the wheel on the dev machine, copy it over, pipx install as the aprilcam user."
echo "  2. Copy + enable deploy/aprilcam-camera-bridge and deploy/aprilcam-bridge@.service."
echo "  3. Copy + enable deploy/aprilcamd.service."
