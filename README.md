# Clock radio

A bedside clock radio for a Raspberry Pi 3 + the official 7" touchscreen B
+ a USB DAC. Renders directly to `/dev/fb0` (no SDL/pygame), reads touch
from raw `evdev`, plays internet radio via MPD, supports recurring +
one-shot alarms with skip-next, and exposes everything through a small
iOS-style gesture chrome.

## Hardware

- Raspberry Pi 3 Model B (rev 1.2 or later)
- Raspberry Pi official 7" touchscreen **B** (Goodix capacitive,
  ILI9881C panel, 720×1280 portrait native)
- A **USB DAC** or audio HAT — the Pi 3's onboard 3.5 mm jack is
  unusable on the current build (clicks, no audible audio)
- 5 V / 3 A+ power supply

## What's in the repo

```
app/                    Python source, deployed to /opt/clockradio/app
  clockradio.py         entry point; Display, Backlight, TouchReader, Compositor
  theme.py              Palette, Fonts, Theme (role-based; config-loadable later)
  widgets.py            Widget base + ClockWidget, TextWidget, Button, etc.
  scenes.py             Idle, Radio, AlarmFiring, Launcher, QuickPanel
  mpd_service.py        Threaded MPD wrapper + MPDStatus snapshot
  alarm.py              Alarm dataclass, persistence (atomic write)
  alarm_service.py      Scheduler + ramp-up volume thread

setup/
  01-bootstrap.sh       installs deps, copies app, enables systemd unit
  99-clockradio-touch.rules   udev: chgrp video + g+w on backlight

systemd/
  clockradio.service    runs the app as user `riha`, owns tty1

config/
  mpd.conf.example      template for /etc/mpd.conf (edit for your DAC)
```

## Bring-up from a blank Pi

1. **Flash Raspberry Pi OS Lite (64-bit, Trixie / Debian 13)** with
   `rpi-imager`. In advanced settings: enable SSH, set username `riha`
   and a password, configure wifi.

2. **Boot, SSH in.**

3. **Update firmware/kernel.** Older firmware (kernel 6.12.x and below)
   leaves the 7" touchscreen B blank at the KMS handoff. The 6.18.x
   firmware fixes the panel-regulator wiring on the Pi 3.
   ```sh
   sudo rpi-update
   sudo reboot
   ```

4. **Plug in the touchscreen and the USB DAC.** Confirm the splash + boot
   text are visible all the way to the login prompt. Re-seat the DSI
   ribbon at both ends if the panel goes blank during boot — that's the
   most common failure on this hardware.

5. **Clone this repo:**
   ```sh
   git clone https://github.com/RichardHaydon/clockradio.git ~/clockradio
   cd ~/clockradio
   ```

6. **Run the bootstrap script:**
   ```sh
   sudo bash setup/01-bootstrap.sh
   ```
   Installs runtime deps (PIL, numpy, mpd, mpc, python3-mpd, fonts,
   rpi-connect-lite), creates `/var/lib/clockradio` as the writable state
   dir, installs the systemd unit, sets up backlight permissions,
   appends the display rotation hint to `cmdline.txt`, and disables
   `getty@tty1` so the app can own that VT.

7. **Configure MPD for your USB DAC:**
   ```sh
   sudo cp config/mpd.conf.example /etc/mpd.conf
   ```
   Then edit the `audio_output` stanza. Use `aplay -l` to find your card
   name (e.g. `hw:CARD=Device`) and `amixer -c <card> scontrols` for the
   mixer control name (often `Speaker` or `PCM`). Restart MPD:
   ```sh
   sudo systemctl restart mpd
   ```

8. **Reboot.** The clock should appear within a few seconds of boot.
   ```sh
   sudo reboot
   ```

9. **Enable rpi-connect (optional, shell-only)** for remote SSH access
   without an inbound port:
   ```sh
   rpi-connect on
   rpi-connect signin
   ```

## Using the device

- **Idle screen** — large clock, date, next alarm with skip-next button,
  always-available `VOL− | live % | VOL+` footer.
- **Radio** — when MPD is playing the layout switches automatically:
  clock at top, station + ICY title in the middle, transport row
  (PREV / PAUSE / NEXT) above the volume footer.
- **Alarm-firing** — full-screen `STOP` button, tap to silence.
- **Swipe up from the bottom edge** — Launcher (RADIO, ALARMS, WEATHER,
  VERSE, CAMERA, SETTINGS — most still placeholders).
- **Swipe down from the top edge** — Quick panel (context-aware actions:
  STOP RADIO, SKIP / UNSKIP NEXT ALARM, CLOSE).
- **All gesture chrome is blocked while an alarm is firing** — a
  3 a.m. sleeve-brush can't dismiss the alarm.

## State that lives outside the repo

The app reads / writes `/var/lib/clockradio/`:
- `alarms.json` — alarm list (atomic tmp+rename writes)
- `stations.json` — radio presets (atomic tmp+rename writes)

The `setup/01-bootstrap.sh` step creates this directory owned by the
`riha` user. If the root filesystem is later switched to overlayroot
(slice 4), this directory needs its own writable mount.

## Adding a new alarm before slice 3b ships the setter UI

```sh
sudo nano /var/lib/clockradio/alarms.json
sudo systemctl restart clockradio
```
Schema (fields all required):
```json
{
  "alarms": [
    {
      "id": "any-stable-id",
      "enabled": true,
      "hour": 7,
      "minute": 0,
      "days": 31,
      "skip_next": false
    }
  ]
}
```
`days` is a bitmask: bit 0 = Mon … bit 6 = Sun.
- `0` = one-shot (auto-disables after firing)
- `31` = Mon–Fri
- `96` = Sat–Sun
- `127` = every day

## Adding radio stations

The app seeds `stations.json` on first run with one approved entry. Add
more by SSH-editing the file:

```sh
sudo nano /var/lib/clockradio/stations.json
sudo systemctl restart clockradio
```

Schema:
```json
{
  "stations": [
    {
      "id": "any-stable-id",
      "name": "Display name",
      "url": "https://example.com/stream.m3u8"
    }
  ]
}
```

Per the household content policy, only broadly-Christian streams are
added. The launcher's `RADIO` tile opens this list; tap a row to play.
Once a station is playing, the radio scene's `PREV`/`NEXT` buttons cycle
through the list (MPD's playlist would otherwise stay on a single URL).

## Architecture notes

- The whole UI is built from `Widget` subclasses composed into `Scene`s.
  Adding a new applet (weather, verse-of-the-day, camera) = new
  `Widget` + drop into a `Scene`. Adding a new mode = new `Scene` and
  register with the compositor.
- `Theme` is a frozen dataclass; palette/fonts referenced by *role
  name* so a future config loader can override values without touching
  any widget code.
- Background data sources (MPD, alarms, future weather) follow a
  threaded-service pattern: snapshot via lock + start/stop lifecycle.
- `MPDClient` lives only on the MPD service thread; the UI pushes
  string/tuple commands through a queue. Never touch the client from the
  main thread.

## Why direct framebuffer (not pygame/SDL)

On Pi 3 + the new 7" touchscreen B, pygame on SDL2/KMSDRM consistently
breaks the panel: with DRM master grabbed the panel goes dark, without
master SDL renders to nothing visible, and Trixie's SDL2 doesn't compile
in the `fbdev` driver. We bypass SDL entirely:
- `KDSETMODE → KD_GRAPHICS` puts tty1 in graphics mode so `fbcon` stops
  drawing.
- PIL renders to a landscape canvas (1280×720), gets rotated 90° CCW to
  fit the panel's portrait native fb (720×1280).
- A vectorised numpy step packs RGB888 → RGB565 and writes to `/dev/fb0`.
- Touch is read directly from `/dev/input/eventN` via
  `struct.unpack` — no libinput.
