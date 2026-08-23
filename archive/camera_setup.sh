#!/usr/bin/env bash
# Configure the Arducam OV9782 USB camera for AprilTag detection.
# Disables auto exposure / auto white balance, then sets a short exposure
# and moderate gain to keep tags sharp on a moving robot.
#
# Uses bin/uvc-util (macOS-native UVC CLI). uvcc does NOT work for UVC
# control on modern macOS — UVCAssistant claims the USB interface and
# every libusb control transfer stalls.
#
# Usage:
#   scripts/camera_setup.sh            # apply defaults below
#   scripts/camera_setup.sh 80 60      # exposure_ticks gain
#   scripts/camera_setup.sh --show     # print current values only
#   scripts/camera_setup.sh --save     # apply, then save profile to data/
#   scripts/camera_setup.sh --load     # restore from data/camera_apriltag.json
#
# Control ranges (OV9782, queried 2026-05-13):
#   exposure-time-abs : 1..5000  (units: 100µs ticks; default 157)
#   gain              : 0..100   (default 0)
#   auto-exposure-mode: 1=manual, 8=aperture-priority (default)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UVC="$ROOT_DIR/bin/uvc-util"
CAMERA_NAME="Arducam OV9782 USB Camera"
PROFILE="$ROOT_DIR/data/camera_apriltag.json"

EXPOSURE_DEFAULT=10  # 100 ticks = 10 ms
GAIN_DEFAULT=1       # mid-range; raise if image too dark

if [[ ! -x "$UVC" ]]; then
    echo "error: $UVC not found or not executable" >&2
    exit 1
fi

uvc() { "$UVC" -N "$CAMERA_NAME" "$@"; }

# Print the controls we care about, one per line: "name=value".
show() {
    for ctrl in auto-exposure-mode auto-exposure-priority \
                auto-white-balance-temp exposure-time-abs gain; do
        # -S output is multiline; pull out current-value.
        val=$(uvc -S "$ctrl" 2>/dev/null \
              | awk -F': *' '/current-value/ {print $2; exit}')
        printf '%s=%s\n' "$ctrl" "${val:-?}"
    done
}

# Save current values as a JSON profile.
save_profile() {
    mkdir -p "$(dirname "$PROFILE")"
    {
        echo "{"
        first=1
        while IFS='=' read -r k v; do
            [[ $first -eq 1 ]] || echo ","
            first=0
            printf '  "%s": "%s"' "$k" "$v"
        done < <(show)
        echo
        echo "}"
    } > "$PROFILE"
    echo "saved $PROFILE"
}

# Load a JSON profile and apply it. Expects the format produced by save_profile.
load_profile() {
    if [[ ! -f "$PROFILE" ]]; then
        echo "error: profile $PROFILE not found" >&2
        exit 1
    fi
    # Tiny ad-hoc parser: lines like '  "name": "value",'
    while IFS= read -r line; do
        if [[ "$line" =~ \"([a-z-]+)\":\ \"([^\"]+)\" ]]; then
            uvc -s "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
        fi
    done < "$PROFILE"
    echo "loaded $PROFILE"
}

case "${1:-}" in
    --show)  show; exit 0 ;;
    --load)  load_profile; show; exit 0 ;;
    --save)  SAVE=1; shift ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)       SAVE=0 ;;
esac

EXPOSURE="${1:-$EXPOSURE_DEFAULT}"
GAIN="${2:-$GAIN_DEFAULT}"

echo "configuring $CAMERA_NAME (exposure=$EXPOSURE, gain=$GAIN)..."

uvc -s auto-exposure-mode=1            # 1 = manual
uvc -s auto-white-balance-temp=false
uvc -s "exposure-time-abs=$EXPOSURE"
uvc -s "gain=$GAIN"

echo "done. current settings:"
show

[[ "$SAVE" == "1" ]] && save_profile
