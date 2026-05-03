#!/bin/bash
# Add a Bluetooth speaker as an MPD audio output.
#
# Prerequisites:
#   1. bluez + bluealsa installed (done by 01-bootstrap.sh).
#   2. The speaker has been paired + trusted via bluetoothctl, e.g.:
#         bluetoothctl
#         > scan on
#         > pair AA:BB:CC:DD:EE:FF
#         > trust AA:BB:CC:DD:EE:FF
#         > connect AA:BB:CC:DD:EE:FF
#
# Usage: sudo bash setup/02-add-bt-output.sh AA:BB:CC:DD:EE:FF "Living Room"
#
# Idempotent — adds the output block once, no-op on subsequent runs.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash $0 ..." >&2
    exit 1
fi

if [ "$#" -lt 1 ]; then
    cat >&2 <<USAGE
Usage: sudo bash $0 <BT_MAC> [friendly-name]
Example: sudo bash $0 AA:BB:CC:DD:EE:FF "Living Room"
USAGE
    exit 1
fi

MAC="$1"
NAME="${2:-Bluetooth}"
CONF="/etc/mpd.conf"
DEV="bluealsa:DEV=${MAC},PROFILE=a2dp"

if grep -qF "$DEV" "$CONF"; then
    echo "BT output for $MAC already present in $CONF — skipping append"
else
    cat >> "$CONF" <<EOF

audio_output {
    type        "alsa"
    name        "${NAME}"
    device      "${DEV}"
    mixer_type  "software"
}
EOF
    echo "Appended BT output \"${NAME}\" to $CONF"
fi

# bluealsa.service exposes the BT audio device; without it the alsa
# device path won't resolve.
systemctl enable --now bluealsa.service 2>/dev/null || true
systemctl restart mpd
echo "MPD restarted. Open Settings → AUDIO in the app to switch outputs."
