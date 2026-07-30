#!/usr/bin/env bash
# provision-pi.sh — Provision an Ubuntu 24.04 aarch64 Raspberry Pi for AprilCam.
#
# Usage:
#   ./provision-pi.sh user@host
#
# Example:
#   ./provision-pi.sh eric@vali.local
#
# What this script does (over SSH):
#   1. Installs apt packages required by aprilcam[daemon] and the camera stack.
#   2. Adds the user to the 'video' group (camera device access).
#   3. Runs 'pipx ensurepath' so ~/.local/bin is on PATH.
#   4. Creates the data directory structure ~/aprilcam-data/{cameras,playfields}.
#
# MANUAL STEPS (not automated — must be done before running this script):
#   Build wheel on your dev machine:
#     uv build --wheel            # or: python -m build --wheel
#   Copy wheel to the Pi:
#     scp dist/aprilcam-*.whl eric@vali.local:~/wheels/
#   Install with pipx (run on the Pi after provisioning):
#     pipx install "aprilcam[daemon]==<version>" \
#         --pip-args "--find-links ~/wheels --extra-index-url https://www.piwheels.org/simple"
#   Install systemd service (run on the Pi after pipx install):
#     scp deploy/aprilcamd.service eric@vali.local:~/
#     ssh eric@vali.local "sudo mv ~/aprilcamd.service /etc/systemd/system/ && \
#         sudo systemctl daemon-reload && \
#         sudo systemctl enable aprilcamd && \
#         sudo systemctl start aprilcamd"
#
# After provisioning, log out and back in for the 'video' group to take effect.

set -euo pipefail

TARGET="${1:?Usage: $0 user@host}"

echo "==> Provisioning ${TARGET} for AprilCam..."

ssh "$TARGET" bash -s << 'PROVISION'
set -euo pipefail

echo "--- Installing apt packages ---"
sudo apt-get update -qq
sudo apt-get install -y \
    python3-venv \
    python3-pip \
    pipx \
    v4l-utils \
    libgl1 \
    libglib2.0-0 \
    avahi-daemon

echo "--- Adding ${USER} to video group ---"
sudo usermod -aG video "$USER"
echo "NOTE: Log out and back in for the video group change to take effect."

echo "--- Running pipx ensurepath ---"
pipx ensurepath

echo "--- Creating AprilCam data directories ---"
mkdir -p ~/aprilcam-data/cameras ~/aprilcam-data/playfields

echo ""
echo "Provision complete."
echo ""
echo "NEXT STEPS (run manually on this machine):"
echo "  1. Copy the wheel: scp dist/aprilcam-*.whl ${USER}@$(hostname):~/wheels/"
echo "  2. pipx install 'aprilcam[daemon]==<version>' \\"
echo "         --pip-args \"--find-links ~/wheels --extra-index-url https://www.piwheels.org/simple\""
echo "  3. Install service: sudo cp ~/aprilcamd.service /etc/systemd/system/"
echo "  4. sudo systemctl daemon-reload && sudo systemctl enable --now aprilcamd"
echo "  5. Log out and back in for the 'video' group to take effect."
PROVISION

echo "==> Done. Provisioned ${TARGET}."
