#!/bin/bash
# Bootstrap a fresh Raspberry Pi OS Lite (Trixie) for the clock radio app.
# Idempotent — safe to re-run.
#
# Usage: sudo bash setup/01-bootstrap.sh
#
# Slice 2a scope: clock + idle dim + touch wake + MPD now-playing split layout.
# Adds: PIL, numpy, MPD + python-mpd, fonts, rpi-connect-lite, screen rotation,
#       backlight permission, systemd unit owning tty1.
# Defers: alarms, station picker (slice 2b+).

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash $0" >&2
    exit 1
fi

APP_DIR="/opt/clockradio"
DATA_DIR="/var/lib/clockradio"
APP_USER="riha"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CMDLINE="/boot/firmware/cmdline.txt"

echo "==> 1/7  apt: install runtime deps"
apt-get update
# Bluetooth packages are installed unconditionally — they're harmless when
# unused and let the audio output picker route MPD to a paired BT speaker
# without re-running setup. Pairing itself is a one-time SSH step
# (bluetoothctl), then add an audio_output { device "bluealsa:..." }
# block to /etc/mpd.conf using the helper at setup/02-add-bt-output.sh.
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3-pil \
    python3-numpy \
    python3-mpd \
    mpd \
    mpc \
    fonts-dejavu-core \
    rpi-connect-lite \
    bluez \
    bluez-tools \
    bluealsa

echo "==> 2/7  Persistent state dir at $DATA_DIR"
install -d -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"

echo "==> 3/7  udev rules: backlight perms + touch rotation"
install -m 0644 "$SRC_DIR/setup/99-clockradio-touch.rules" \
    /etc/udev/rules.d/99-clockradio-touch.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=backlight
udevadm trigger --subsystem-match=input

echo "==> 4/7  Display rotation: 90 deg CW via $CMDLINE"
if [ -f "$CMDLINE" ] && ! grep -q "video=DSI-1:" "$CMDLINE"; then
    # cmdline.txt must remain a single line. Append before the trailing newline.
    sed -i 's|$| video=DSI-1:panel_orientation=right_side_up|' "$CMDLINE"
    echo "    (appended; reboot required)"
else
    echo "    (already present or no cmdline.txt — skipping)"
fi

echo "==> 5/7  Install app to $APP_DIR"
install -d "$APP_DIR/app"
install -m 0644 "$SRC_DIR/app/"*.py "$APP_DIR/app/"
# Bundled assets (e.g. world map land mask). Optional — only copy if
# the source dir exists so older trees still bootstrap cleanly.
if [ -d "$SRC_DIR/app/data" ]; then
    install -d "$APP_DIR/app/data"
    # Glob only PNGs — the data dir also caches large build-time sources
    # (Natural Earth geojson, shaded-relief zip) that are several MB each
    # and not used at runtime. Keeping them in the repo for build
    # reproducibility but skipping them on install.
    for f in "$SRC_DIR/app/data/"*.png; do
        [ -f "$f" ] && install -m 0644 "$f" "$APP_DIR/app/data/"
    done
fi

echo "==> 6/8  systemd unit + tty1 ownership"
install -m 0644 "$SRC_DIR/systemd/clockradio.service" \
    /etc/systemd/system/clockradio.service
systemctl daemon-reload
systemctl disable getty@tty1.service 2>/dev/null || true
systemctl enable clockradio.service

echo "==> 7/8  sudoers: $APP_USER may manage wifi + restart mpd"
# Two narrow rules:
#   - nmcli: WifiScene needs to rescan / connect / forget (NetworkManager
#     mutations require root).
#   - systemctl restart mpd: MPD watchdog. When MPD wedges on a stalled
#     stream, the python-mpd client times out and can't recover without
#     a daemon restart, so MPDService asks systemd to bounce it.
SUDOERS_FILE="/etc/sudoers.d/clockradio"
cat > "$SUDOERS_FILE" <<EOF
$APP_USER ALL=(root) NOPASSWD: /usr/bin/nmcli
$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart mpd
EOF
chmod 0440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE" >/dev/null
# Old single-purpose file from earlier slices — superseded.
rm -f /etc/sudoers.d/clockradio-nmcli

echo "==> 8/8  rpi-connect-lite: enable shell access (run as $APP_USER)"
echo "    After reboot, run as $APP_USER:"
echo "        rpi-connect on"
echo "        rpi-connect signin"

echo
echo "Bootstrap complete. Reboot to start the clock:"
echo "    sudo reboot"
