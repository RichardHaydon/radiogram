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

echo "==> 1/9  apt: install runtime deps"
apt-get update
# Bluetooth packages are installed unconditionally — they're harmless when
# unused and let the audio output picker route MPD to a paired BT speaker
# without re-running setup. Pairing is now handled in-app via
# Settings → BLUETOOTH (BluetoothService shells out to the privileged
# helper installed below); manual `bluetoothctl` is no longer required.
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
    bluez-alsa-utils \
    libasound2-plugin-bluez

echo "==> 2/9  Persistent state dir at $DATA_DIR"
install -d -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"

echo "==> 3/9  udev rules: backlight perms + touch rotation"
install -m 0644 "$SRC_DIR/setup/99-clockradio-touch.rules" \
    /etc/udev/rules.d/99-clockradio-touch.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=backlight
udevadm trigger --subsystem-match=input

echo "==> 4/9  Display rotation: 90 deg CW via $CMDLINE"
if [ -f "$CMDLINE" ] && ! grep -q "video=DSI-1:" "$CMDLINE"; then
    # cmdline.txt must remain a single line. Append before the trailing newline.
    sed -i 's|$| video=DSI-1:panel_orientation=right_side_up|' "$CMDLINE"
    echo "    (appended; reboot required)"
else
    echo "    (already present or no cmdline.txt — skipping)"
fi

echo "==> 5/9  Install app to $APP_DIR"
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

echo "==> 6/9  systemd unit + tty1 ownership"
install -m 0644 "$SRC_DIR/systemd/clockradio.service" \
    /etc/systemd/system/clockradio.service
systemctl daemon-reload
systemctl disable getty@tty1.service 2>/dev/null || true
systemctl enable clockradio.service

echo "==> 7a/9  MPD auto-config helper (USB DAC self-healing)"
# Re-derives /etc/mpd.conf's audio_output stanza on every boot from the
# live ALSA card name + mixer control. A different USB DAC plugged in
# next boot will have a different card-name string ("Device" for
# C-Media, "Audio" for Realtek, etc.) — without this, MPD would fail
# to open hw:CARD=<previous_name> and the radio would be silent.
install -m 0755 -o root -g root \
    "$SRC_DIR/setup/mpd-autoconfig" \
    /usr/local/sbin/clockradio-mpd-autoconfig
install -m 0644 "$SRC_DIR/systemd/clockradio-mpd-autoconfig.service" \
    /etc/systemd/system/clockradio-mpd-autoconfig.service
# Hot-plug rule: re-run autoconfig when a USB sound card appears or
# disappears so swapping the DAC on a running system Just Works
# instead of requiring a reboot.
install -m 0644 "$SRC_DIR/setup/99-clockradio-audio.rules" \
    /etc/udev/rules.d/99-clockradio-audio.rules
udevadm control --reload-rules
systemctl daemon-reload
systemctl enable clockradio-mpd-autoconfig.service

echo "==> 7/9  Bluetooth helper + pair-accept agent"
# Privileged shim that BluetoothService shells out to via `sudo -n`.
# Adds/removes the bluealsa MPD output block + retargets bluealsa-aplay
# at the active ALSA device + restarts the relevant services. Lives
# under /usr/local/sbin so the sudoers rule below pins to a stable
# absolute path, not to a path inside the user-writable repo checkout.
install -m 0755 -o root -g root \
    "$SRC_DIR/setup/bt-output-helper.sh" \
    /usr/local/sbin/clockradio-bt-output

# bt-agent runs as a system service so phones doing "Just Works"
# pairing find an agent waiting on the system D-Bus. Always-on; gated
# in practice by the app toggling discoverable + pairable for a
# bounded window when the user enables "Receive from phone".
install -m 0644 "$SRC_DIR/setup/clockradio-bt-agent.service" \
    /etc/systemd/system/clockradio-bt-agent.service
systemctl daemon-reload
systemctl enable --now clockradio-bt-agent.service

echo "==> 8/9  sudoers: $APP_USER may manage wifi + bluetooth + restart mpd"
# Narrow rules — each pinned to a single absolute command:
#   - nmcli: WifiScene needs to rescan / connect / forget (NetworkManager
#     mutations require root).
#   - bluetoothctl: BluetoothService scans/pairs/forgets BT speakers.
#   - clockradio-bt-output: privileged helper that edits mpd.conf +
#     restarts mpd after a successful pair/forget.
#   - systemctl restart mpd: MPD watchdog. When MPD wedges on a stalled
#     stream, the python-mpd client times out and can't recover without
#     a daemon restart, so MPDService asks systemd to bounce it.
SUDOERS_FILE="/etc/sudoers.d/clockradio"
cat > "$SUDOERS_FILE" <<EOF
$APP_USER ALL=(root) NOPASSWD: /usr/bin/nmcli
$APP_USER ALL=(root) NOPASSWD: /usr/bin/bluetoothctl
$APP_USER ALL=(root) NOPASSWD: /usr/local/sbin/clockradio-bt-output
$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart mpd
EOF
chmod 0440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE" >/dev/null
# Old single-purpose file from earlier slices — superseded.
rm -f /etc/sudoers.d/clockradio-nmcli

echo "==> 9/9  rpi-connect-lite: enable shell access (run as $APP_USER)"
echo "    After reboot, run as $APP_USER:"
echo "        rpi-connect on"
echo "        rpi-connect signin"

echo
echo "Bootstrap complete. Reboot to start the clock:"
echo "    sudo reboot"
