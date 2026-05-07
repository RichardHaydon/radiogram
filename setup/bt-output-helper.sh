#!/bin/bash
# Privileged helper: add/remove a Bluetooth speaker as an MPD audio
# output. Invoked by BluetoothService via `sudo -n` after a successful
# pair (add) or forget (remove). Idempotent in both directions.
#
# Installed by 01-bootstrap.sh to /usr/local/sbin/clockradio-bt-output
# and granted NOPASSWD via /etc/sudoers.d/clockradio so the app user
# can shell out without a password.
#
# Usage:
#   sudo bash bt-output-helper.sh add     AA:BB:CC:DD:EE:FF "Living Room"
#   sudo bash bt-output-helper.sh remove  AA:BB:CC:DD:EE:FF
#   sudo bash bt-output-helper.sh unblock                    (soft-unblock BT radio)
#
# Implementation notes:
#   - MAC is validated against a strict regex *before* any substitution
#     into the mpd.conf block; anything else (including the friendly
#     name) is sanitised. The helper runs as root via sudo so input
#     validation matters even when the only caller is our own service.
#   - The mpd.conf section is delimited by a magic comment so we can
#     remove it cleanly without touching hand-edited blocks. Pre-existing
#     manually-added bluealsa blocks (from the original 02-add-bt-output.sh
#     workflow) are matched by their `bluealsa:DEV=MAC` device line.
#   - On any add or remove we restart mpd so the change takes effect
#     immediately. bluealsa.service is enabled the first time.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash $0 ..." >&2
    exit 1
fi

CONF="/etc/mpd.conf"
BEGIN_TAG="# clockradio-bt:"
END_TAG="# /clockradio-bt"

usage() {
    cat >&2 <<USAGE
Usage:
  $0 add     AA:BB:CC:DD:EE:FF "Friendly Name"
  $0 remove  AA:BB:CC:DD:EE:FF
  $0 unblock
USAGE
    exit 2
}

cmd_unblock() {
    # Pi 3 (and other devices first booted with BT disabled) keeps the
    # radio soft-blocked across reboots. bluetoothctl `power on` returns
    # org.bluez.Error.Failed in that state. Walk /sys/class/rfkill and
    # write 0 to soft for any bluetooth-typed entry — direct sysfs is
    # used because the rfkill CLI isn't installed on a stock image.
    # Safe to call any time; idempotent; returns 0 even if no rfkill
    # entries exist (e.g. on an adapter-less device).
    local d type changed=0
    for d in /sys/class/rfkill/rfkill*; do
        [ -e "$d" ] || continue
        type=$(cat "$d/type" 2>/dev/null || true)
        if [ "$type" = "bluetooth" ]; then
            if [ "$(cat "$d/soft" 2>/dev/null)" = "1" ]; then
                echo 0 > "$d/soft"
                changed=1
            fi
        fi
    done
    if [ "$changed" = "1" ]; then
        echo "Bluetooth radio soft-unblocked"
    fi
}

validate_mac() {
    local mac="$1"
    if ! [[ "$mac" =~ ^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$ ]]; then
        echo "Invalid MAC: $mac" >&2
        exit 3
    fi
}

# Strip everything outside [A-Za-z0-9 _-]. Cap to 32 chars. Empty input
# falls back to "Bluetooth" so MPD always has a non-empty `name`.
sanitise_name() {
    local raw="$1"
    local clean
    clean=$(printf '%s' "$raw" | tr -dc '[:alnum:] _-' | cut -c1-32)
    if [ -z "$clean" ]; then
        clean="Bluetooth"
    fi
    printf '%s' "$clean"
}

remove_existing_block() {
    local mac="$1"
    local mac_upper
    mac_upper=$(printf '%s' "$mac" | tr '[:lower:]' '[:upper:]')
    local mac_lower
    mac_lower=$(printf '%s' "$mac" | tr '[:upper:]' '[:lower:]')
    # Two passes: first the tagged blocks (our own), then any legacy
    # untagged block whose audio_output { ... } contains DEV=MAC. The
    # legacy pass uses awk to track brace depth so we only delete the
    # specific block, not every audio_output in the file.
    local tmp
    tmp=$(mktemp)
    # Pass 1: drop tagged blocks for this MAC.
    awk -v mac_u="$mac_upper" -v mac_l="$mac_lower" \
        -v begin="$BEGIN_TAG" -v end="$END_TAG" '
        BEGIN { skip = 0 }
        {
            if (skip == 0 && index($0, begin) > 0 \
                && (index($0, mac_u) > 0 || index($0, mac_l) > 0)) {
                skip = 1
                next
            }
            if (skip == 1) {
                if (index($0, end) > 0) {
                    skip = 0
                }
                next
            }
            print
        }' "$CONF" > "$tmp"
    mv "$tmp" "$CONF"
    # Pass 2: drop legacy untagged audio_output blocks whose body
    # references DEV=MAC. Brace tracking handles single-block deletion.
    tmp=$(mktemp)
    awk -v needle1="DEV=$mac_upper" -v needle2="DEV=$mac_lower" '
        BEGIN { in_block = 0; depth = 0; buf = ""; matched = 0 }
        {
            line = $0
            if (in_block == 0) {
                if (line ~ /^[[:space:]]*audio_output[[:space:]]*\{/) {
                    in_block = 1
                    depth = 1
                    buf = line "\n"
                    matched = 0
                    next
                }
                print line
                next
            }
            # Inside a block — accumulate, track braces.
            buf = buf line "\n"
            n = gsub(/\{/, "{", line); depth += n
            n = gsub(/\}/, "}", line); depth -= n
            if (index(line, needle1) > 0 || index(line, needle2) > 0) {
                matched = 1
            }
            if (depth <= 0) {
                if (matched == 0) {
                    printf "%s", buf
                }
                in_block = 0
                buf = ""
            }
        }
        END {
            if (in_block == 1) {
                # Unbalanced source — emit what we held to avoid loss.
                printf "%s", buf
            }
        }' "$CONF" > "$tmp"
    mv "$tmp" "$CONF"
}

cmd_add() {
    local mac="$1"
    local name="$2"
    validate_mac "$mac"
    name=$(sanitise_name "$name")
    local mac_upper
    mac_upper=$(printf '%s' "$mac" | tr '[:lower:]' '[:upper:]')

    # Replace any existing block for this MAC so the friendly name is
    # always up-to-date and we never end up with duplicates.
    remove_existing_block "$mac_upper"

    cat >> "$CONF" <<EOF

${BEGIN_TAG} ${mac_upper}
audio_output {
    type        "alsa"
    name        "${name}"
    device      "bluealsa:DEV=${mac_upper},PROFILE=a2dp"
    mixer_type  "software"
}
${END_TAG}
EOF
    echo "Added BT output \"${name}\" for ${mac_upper}"

    systemctl enable --now bluealsa.service 2>/dev/null || true
    systemctl restart mpd
}

cmd_remove() {
    local mac="$1"
    validate_mac "$mac"
    remove_existing_block "$mac"
    echo "Removed BT output for ${mac}"
    systemctl restart mpd
}

case "${1:-}" in
    add)
        if [ "$#" -ne 3 ]; then
            usage
        fi
        cmd_add "$2" "$3"
        ;;
    remove)
        if [ "$#" -ne 2 ]; then
            usage
        fi
        cmd_remove "$2"
        ;;
    unblock)
        if [ "$#" -ne 1 ]; then
            usage
        fi
        cmd_unblock
        ;;
    *)
        usage
        ;;
esac
