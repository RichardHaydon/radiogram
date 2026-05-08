"""Bluetooth speaker discovery + pairing via bluetoothctl, plus a
phone-streams-to-clockradio sink mode.

Mirrors WifiService's shape: a daemon thread, a lock-protected snapshot
(BluetoothStatus), and a command queue. Mutating commands (scan, pair,
forget, set_discoverable) flow through the queue so a 10–15 s pair
operation never blocks the UI thread.

bluetoothctl is invoked one-shot via `--timeout` so we never hold an
interactive session. The default bluez "Just Works" agent (NoInputNoOutput)
handles pairing for the modern A2DP speakers users actually own; PIN-prompt
devices fail with a clear error and we revisit with a custom D-Bus agent
later if needed.

Outbound (clock → speaker): on a successful pair the privileged helper
`/usr/local/sbin/clockradio-bt-output add MAC NAME` appends a
`bluealsa:DEV=MAC,PROFILE=a2dp` audio_output block to /etc/mpd.conf and
restarts mpd. The service then asks MPDService to switch to the new
output id — the speaker becomes the active sink without leaving the
BT scene.

Inbound (phone → clock): when the user enables Settings → BLUETOOTH
→ "Receive from phone", the controller is made `pairable +
discoverable` for ~5 minutes. A persistent `bt-agent NoInputNoOutput`
systemd service auto-accepts the pair, and `bluealsa-aplay` (routed
to the same ALSA device the radio uses) plays the incoming A2DP
stream out the clock's wired/USB speaker. While a phone is actively
streaming we pause MPD; on disconnect, MPD resumes if it was playing
before — the same behaviour as a car stereo.
"""
from __future__ import annotations

import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace


CMD_WAIT_S = 0.2
SLOW_POLL_S = 30.0
# Stream/discoverable status changes need to feel responsive in the UI
# (countdown ticks every second, MPD pause must follow within ~1 s of
# the phone hitting play). The sink-mode poll runs at this cadence
# whenever the radio is *currently* discoverable or streaming, falling
# back to SLOW_POLL_S when neither flag is set.
SINK_FAST_POLL_S = 1.0
BTCTL_TIMEOUT_S = 25
SCAN_DURATION_S = 12       # bluetoothctl --timeout for scan on
PAIR_TIMEOUT_S = 25        # pair + trust + connect each get this budget
HELPER_TIMEOUT_S = 20
DISCOVERABLE_DURATION_S = 300   # how long a "Receive from phone" tap stays open

# Names we hide in the "discovered" list. bluez fills the name field
# with the MAC (in either colon `AA:BB:..` or hyphen `AA-BB-..` form)
# when a device advertises no human-readable name — usually a phone,
# car, fitness band, or anonymised BLE peripheral that the user can't
# usefully pair to as a speaker. Filtering these keeps the discovered
# list small enough to see real audio devices at a glance.
_HIDDEN_NAME_PATTERNS = (
    re.compile(r"^[0-9A-F]{2}([-:][0-9A-F]{2}){5}$", re.IGNORECASE),
    re.compile(r"^Device [0-9A-F]{2}", re.IGNORECASE),
)


@dataclass(frozen=True)
class BluetoothDevice:
    mac: str
    name: str
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    is_audio: bool = False         # icon=audio-card / audio-headphones


@dataclass(frozen=True)
class BluetoothStatus:
    # Adapter present + powered. False when bluez isn't running or the
    # Pi has no BT radio (e.g. Pi 3 with disabled BT).
    available: bool = True
    discovering: bool = False       # a scan is in flight
    busy: bool = False              # a pair/forget is in flight
    last_error: str = ""
    last_action: str = ""           # short success message ("Paired Living Room")
    devices: tuple[BluetoothDevice, ...] = field(default_factory=tuple)
    # MAC of the currently-active BT speaker, derived from /etc/mpd.conf.
    # Empty if no BT output is configured.
    paired_mac: str = ""
    # Sink-mode (phone → clockradio) state. `discoverable` reflects the
    # bluez controller flag; the seconds_left field is what the UI
    # surfaces as a countdown next to the toggle. `streaming_from`
    # holds the human-readable name of the phone currently streaming
    # A2DP audio (empty when no stream is active). The UI uses this
    # to swap the toggle row label between "Receive from phone" and
    # "Receiving from <name>", and the service uses transitions on
    # this field to pause/resume MPD.
    discoverable: bool = False
    discoverable_seconds_left: int = 0
    streaming_from: str = ""


def _is_useful_name(name: str) -> bool:
    if not name:
        return False
    n = name.strip()
    if not n:
        return False
    for pat in _HIDDEN_NAME_PATTERNS:
        if pat.match(n):
            return False
    return True


def _parse_device_lines(text: str) -> list[tuple[str, str]]:
    """Parse `bluetoothctl devices` output → [(mac, name), ...].

    Each line is `Device AA:BB:CC:DD:EE:FF Some Name`. Lines without a
    MAC token are ignored."""
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s.startswith("Device "):
            continue
        rest = s[len("Device "):]
        parts = rest.split(" ", 1)
        if not parts:
            continue
        mac = parts[0].strip().upper()
        name = parts[1].strip() if len(parts) > 1 else ""
        if len(mac.split(":")) != 6:
            continue
        out.append((mac, name))
    return out


def _parse_info(text: str) -> dict:
    """Parse `bluetoothctl info MAC` output → flat key/value dict.

    Whitespace-indented `Key: value` lines under the leading device line.
    Multi-value keys like UUID are kept as the *last* occurrence — we
    don't read them anyway."""
    info: dict = {}
    for raw in text.splitlines():
        s = raw.strip()
        if ":" not in s:
            continue
        k, _, v = s.partition(":")
        info[k.strip()] = v.strip()
    return info


def _read_paired_bt_mac(mpd_conf: str = "/etc/mpd.conf") -> str:
    """Best-effort: scan /etc/mpd.conf for a `bluealsa:DEV=MAC,...` line
    and return the first MAC found. Returns "" if no BT output present."""
    try:
        with open(mpd_conf, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    m = re.search(r"bluealsa:DEV=([0-9A-Fa-f:]{17})", text)
    return m.group(1).upper() if m else ""


class BluetoothService:
    def __init__(self,
                 *,
                 helper_path: str = "/usr/local/sbin/clockradio-bt-output",
                 mpd_conf_path: str = "/etc/mpd.conf",
                 mpd_service=None):
        # mpd_service is optional — if provided, a successful pair will
        # enqueue ("set_output", id) on it once the new output appears
        # so the speaker becomes the active sink immediately. The same
        # handle drives the sink-mode pause/resume orchestration: when
        # a phone starts streaming we send `pause`; when the stream
        # ends we send `play` if MPD was playing before the interrupt.
        self._helper_path = helper_path
        self._mpd_conf_path = mpd_conf_path
        self._mpd = mpd_service
        self._lock = threading.Lock()
        self._status = BluetoothStatus(
            paired_mac=_read_paired_bt_mac(mpd_conf_path))
        self._stop_evt = threading.Event()
        self._cmd_q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        # Sink-mode internal state. `_sink_route` tracks the ALSA
        # device we last asked bluealsa-aplay to play to, so we only
        # invoke the helper (which restarts the service, dropping any
        # active phone stream) when it actually changes. `_was_playing`
        # captures MPD's state at the moment a phone stream starts so
        # we can resume the user's radio after they hang up. The
        # `_streaming_addr` field holds the MAC of the phone currently
        # streaming, used to map the bluealsa PCM path back to a name
        # for the UI.
        self._sink_route: str = ""
        self._was_playing: bool = False
        self._streaming_addr: str = ""

    # --- public API ----------------------------------------------------

    @property
    def status(self) -> BluetoothStatus:
        with self._lock:
            return self._status

    def scan(self) -> None:
        self._cmd_q.put(("scan",))

    def pair(self, mac: str, name: str = "") -> None:
        self._cmd_q.put(("pair", mac, name))

    def forget(self, mac: str) -> None:
        self._cmd_q.put(("forget", mac))

    def set_discoverable(self, enabled: bool) -> None:
        """Enable/disable sink-mode (phone → clockradio).

        Enabled: makes the controller pairable + discoverable for
        DISCOVERABLE_DURATION_S seconds (bluez auto-disables on
        timeout). Disabled: turns both flags off immediately. Idempotent."""
        self._cmd_q.put(("set_discoverable", bool(enabled)))

    def refresh_now(self) -> None:
        self._cmd_q.put(("refresh",))

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="bt-poll")
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    # --- thread internals ---------------------------------------------

    def _set(self, **kwargs) -> None:
        with self._lock:
            self._status = replace(self._status, **kwargs)

    def _run(self) -> None:
        # Power on the adapter at startup. Pi 3 (and any device first
        # booted with BT off) keeps the radio soft-blocked at the
        # rfkill layer; bluetoothctl `power on` returns
        # org.bluez.Error.Failed in that state. Try a soft-unblock via
        # the privileged helper first — it touches /sys/class/rfkill
        # which we can't write to directly. Best-effort throughout.
        try:
            try:
                subprocess.run(
                    ["sudo", "-n", self._helper_path, "unblock"],
                    capture_output=True, text=True,
                    timeout=HELPER_TIMEOUT_S, check=False)
            except (subprocess.SubprocessError, FileNotFoundError) as exc:
                print(f"bt unblock: {exc}",
                      file=sys.stderr, flush=True)
            r = self._btctl(["show"])
            if "Powered: no" in r:
                try:
                    self._btctl(["power", "on"])
                except subprocess.CalledProcessError as exc:
                    # Adapter still refuses — could be hardware-blocked
                    # or absent. Surface but stay running; the periodic
                    # refresh re-checks availability in case the user
                    # toggles the radio externally.
                    msg = (exc.stderr or exc.stdout or "").strip()
                    print(f"bt power on: {msg}",
                          file=sys.stderr, flush=True)
                    self._set(available=False, last_error=msg)
            else:
                self._set(available=True)
        except Exception as exc:
            print(f"bt init: {exc}", file=sys.stderr, flush=True)
            self._set(available=False, last_error=str(exc))

        try:
            self._refresh()
        except Exception as exc:
            print(f"bt initial refresh: {exc}",
                  file=sys.stderr, flush=True)

        # First-pass sink route — point bluealsa-aplay at whatever ALSA
        # device MPD is currently using so a phone-pair can stream the
        # moment the user enables sink mode without an extra reload.
        # Best-effort: failures here are logged but don't block startup.
        self._sync_sink_route()

        last_poll = time.monotonic()
        last_sink_poll = 0.0
        while not self._stop_evt.is_set():
            try:
                cmd = self._cmd_q.get(timeout=CMD_WAIT_S)
            except queue.Empty:
                cmd = None
            if cmd is not None:
                try:
                    self._execute(cmd)
                except Exception as exc:
                    print(f"bt cmd {cmd}: {exc}",
                          file=sys.stderr, flush=True)
                    self._set(busy=False, discovering=False,
                              last_error=str(exc))
                last_poll = time.monotonic()
                last_sink_poll = last_poll
                continue
            now = time.monotonic()
            # Fast sink poll runs only when there's actually something
            # to track (we're discoverable, or a phone is currently
            # streaming). Outside that window we save the bluealsa-cli
            # + bluetoothctl round-trips and let the slow poll catch
            # state drift. Pause/resume orchestration runs inside this
            # poll so it sees streaming transitions within ~1 s.
            need_fast = (self._status.discoverable
                         or self._status.streaming_from)
            sink_due = now - last_sink_poll >= (
                SINK_FAST_POLL_S if need_fast else SLOW_POLL_S)
            if sink_due:
                try:
                    self._refresh_sink_state()
                except Exception as exc:
                    print(f"bt sink poll: {exc}",
                          file=sys.stderr, flush=True)
                last_sink_poll = now
            if now - last_poll >= SLOW_POLL_S:
                try:
                    self._refresh()
                    self._sync_sink_route()
                except Exception as exc:
                    print(f"bt poll: {exc}",
                          file=sys.stderr, flush=True)
                last_poll = now

    def _execute(self, cmd: tuple) -> None:
        kind = cmd[0]
        if kind == "refresh":
            self._refresh()
            return
        if kind == "scan":
            self._set(discovering=True, last_error="")
            try:
                # --timeout makes bluetoothctl auto-stop scanning, so we
                # don't have to remember to send `scan off` if the user
                # navigates away mid-scan. Blocks for SCAN_DURATION_S +
                # a small startup overhead.
                self._btctl(["--timeout", str(SCAN_DURATION_S),
                             "scan", "on"],
                            timeout=SCAN_DURATION_S + 8)
            except subprocess.TimeoutExpired:
                # Some adapters don't honour --timeout cleanly; force-stop.
                try:
                    self._btctl(["scan", "off"])
                except Exception:
                    pass
            except subprocess.CalledProcessError as exc:
                msg = (exc.stderr or exc.stdout or "").strip()
                self._set(last_error=msg or "scan failed")
            self._refresh()
            self._set(discovering=False)
            return
        if kind == "pair":
            mac = str(cmd[1]).upper()
            name = str(cmd[2]) if len(cmd) > 2 else ""
            self._do_pair(mac, name)
            return
        if kind == "forget":
            mac = str(cmd[1]).upper()
            self._do_forget(mac)
            return
        if kind == "set_discoverable":
            self._do_set_discoverable(bool(cmd[1]))
            return

    # --- sink mode (phone → clockradio) --------------------------------

    def _do_set_discoverable(self, enabled: bool) -> None:
        """Toggle the controller's pairable+discoverable flags."""
        self._set(last_error="", last_action="")
        try:
            if enabled:
                # Ensure the route is current before opening the door —
                # if MPD switched outputs since the last refresh, the
                # phone audio would otherwise go to the old device.
                self._sync_sink_route()
                # Order matters: pairable first so an incoming pair
                # request during the discoverable window can complete.
                self._btctl(["pairable", "on"], timeout=10)
                self._btctl(["discoverable-timeout",
                             str(DISCOVERABLE_DURATION_S)], timeout=10)
                self._btctl(["discoverable", "on"], timeout=10)
            else:
                # Closing time. Existing connections stay alive — only
                # new pair attempts get refused. This means a phone
                # currently streaming continues uninterrupted.
                self._btctl(["discoverable", "off"], timeout=10)
                self._btctl(["pairable", "off"], timeout=10)
            self._refresh_sink_state()
        except subprocess.CalledProcessError as exc:
            msg = (exc.stderr or exc.stdout or "").strip()
            self._set(last_error=msg or "discoverable toggle failed")

    def _refresh_sink_state(self) -> None:
        """Re-read discoverable + streaming state from the system and
        fire MPD pause/resume on transitions. Cheap to call (one
        bluetoothctl + one bluealsa-cli round-trip), so the fast poll
        runs it every second when sink mode is engaged."""
        # 1. Discoverable flag + remaining timer from bluetoothctl.
        discoverable = False
        seconds_left = 0
        try:
            show = self._btctl(["show"], timeout=8)
            for line in show.splitlines():
                s = line.strip()
                if s.startswith("Discoverable:"):
                    discoverable = s.split(":", 1)[1].strip().lower() == "yes"
                elif s.startswith("DiscoverableTimeout:"):
                    # Format: "DiscoverableTimeout: 0x000000b4 (180)"
                    m = re.search(r"\((\d+)\)", s)
                    if m:
                        seconds_left = int(m.group(1))
        except subprocess.CalledProcessError:
            # Adapter may have gone away; leave previous state and let
            # the slow refresh re-establish availability.
            return

        # 2. Active A2DP source PCMs from bluealsa-cli — each entry
        # like /org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsrc/source
        # represents a phone currently piping audio at us. We only
        # care that *one* is present and which device it belongs to.
        streaming_addr = ""
        try:
            pcms = subprocess.run(
                ["bluealsa-cli", "list-pcms"],
                capture_output=True, text=True,
                timeout=5, check=False).stdout
            for path in pcms.splitlines():
                if "/a2dpsrc/" in path or "a2dp-source" in path.lower():
                    m = re.search(r"dev_([0-9A-F_]{17})", path)
                    if m:
                        streaming_addr = m.group(1).replace("_", ":")
                        break
        except (subprocess.SubprocessError, FileNotFoundError):
            streaming_addr = ""

        streaming_name = ""
        if streaming_addr:
            streaming_name = self._device_name(streaming_addr) \
                or streaming_addr

        # 3. Pause/resume orchestration. We only mutate MPD on edge
        # transitions to avoid spamming toggle every poll. The flag
        # captures whether MPD was *playing* (not paused, not stopped)
        # so we don't accidentally start playing on a phone disconnect
        # if the user had explicitly stopped the radio earlier.
        # MPDService only exposes a single `toggle` string command for
        # play/pause; we use it in both directions, gated by was_playing.
        prev_streaming = self._status.streaming_from
        if streaming_name and not prev_streaming:
            self._was_playing = self._mpd_is_playing()
            if self._was_playing:
                self._mpd_command("toggle")   # play → pause
        elif prev_streaming and not streaming_name:
            if self._was_playing:
                self._mpd_command("toggle")   # pause → play
            self._was_playing = False

        self._streaming_addr = streaming_addr
        self._set(discoverable=discoverable,
                  discoverable_seconds_left=(seconds_left
                                             if discoverable else 0),
                  streaming_from=streaming_name)

    def _sync_sink_route(self) -> None:
        """Compute the desired ALSA device for bluealsa-aplay from the
        currently-active MPD output and ask the helper to apply it.
        Idempotent: skips the (expensive — restarts a service) helper
        call when the route is already up to date."""
        device = self._desired_sink_device()
        if not device:
            return
        if device == self._sink_route:
            return
        # Don't disturb an active phone stream. The helper restart
        # would drop bluealsa-aplay's current PCM. Defer until the
        # phone hangs up and the next refresh sees streaming_from
        # cleared, at which point this branch fires.
        if self._status.streaming_from:
            return
        try:
            subprocess.run(
                ["sudo", "-n", self._helper_path,
                 "set-sink-route", device],
                capture_output=True, text=True,
                timeout=HELPER_TIMEOUT_S, check=True)
            self._sink_route = device
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            print(f"bt set-sink-route: {exc}",
                  file=sys.stderr, flush=True)

    def _desired_sink_device(self) -> str:
        """Pick the ALSA `device` line from MPD's currently-enabled
        audio_output. If no output is enabled (rare) or the file is
        unreadable, return "" — the caller skips the route update."""
        cfg = self._read_mpd_conf_outputs()
        if not cfg:
            return ""
        active_name = ""
        if self._mpd is not None:
            try:
                outs = self._mpd.status.outputs
            except Exception:
                outs = ()
            for o in outs:
                if getattr(o, "enabled", False):
                    active_name = o.name
                    break
        if active_name and active_name in cfg:
            return cfg[active_name].get("device", "")
        # Fallback: first non-bluealsa output. Phone-into-its-own-
        # bluealsa-loop would just feedback into itself and the user
        # almost certainly didn't mean to do that.
        for name, kv in cfg.items():
            dev = kv.get("device", "")
            if dev and not dev.startswith("bluealsa:"):
                return dev
        return ""

    def _mpd_is_playing(self) -> bool:
        if self._mpd is None:
            return False
        try:
            return getattr(self._mpd.status, "state", "") == "play"
        except Exception:
            return False

    def _mpd_command(self, cmd: str) -> None:
        if self._mpd is None:
            return
        try:
            self._mpd.command(cmd)
        except Exception:
            pass

    # --- pair / forget --------------------------------------------------

    def _do_pair(self, mac: str, fallback_name: str) -> None:
        self._set(busy=True, last_error="", last_action="")
        try:
            # Trust first so bluez auto-accepts the pairing request and
            # auto-reconnects on boot. `trust` is idempotent.
            self._btctl(["trust", mac], timeout=10)
            try:
                self._btctl(["pair", mac], timeout=PAIR_TIMEOUT_S)
            except subprocess.CalledProcessError as exc:
                msg = (exc.stderr or exc.stdout or "").lower()
                # AlreadyExists is fine — device was paired previously
                # but the MPD output never got added (e.g. helper missing).
                if "alreadyexists" not in msg and "already paired" not in msg:
                    raise
            try:
                self._btctl(["connect", mac], timeout=PAIR_TIMEOUT_S)
            except subprocess.CalledProcessError as exc:
                msg = (exc.stderr or exc.stdout or "").lower()
                if "alreadyconnected" not in msg \
                        and "already connected" not in msg:
                    raise

            # Resolve a friendly name from bluez for the MPD output,
            # falling back to whatever the scene passed in (which is the
            # name from the discovery list).
            name = self._device_name(mac) or fallback_name or "Bluetooth"
            self._add_mpd_output(mac, name)

            # Ask MPD to switch to the new output. We can't know the
            # outputid synchronously — MPDService polls it every ~2 s —
            # but we can poll briefly here on the worker thread (we're
            # off the UI thread already, so a few seconds is fine).
            self._route_mpd_to(mac)

            self._set(paired_mac=mac, last_action=f"Connected: {name}")
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or "pair failed").strip()
            self._set(last_error=_short_pair_error(err))
        except subprocess.TimeoutExpired:
            self._set(last_error="Timed out — put speaker in pairing mode")
        except Exception as exc:
            self._set(last_error=str(exc))
        finally:
            self._refresh()
            self._set(busy=False)

    def _do_forget(self, mac: str) -> None:
        self._set(busy=True, last_error="", last_action="")
        try:
            try:
                self._btctl(["disconnect", mac], timeout=10)
            except subprocess.CalledProcessError:
                # Disconnect can fail if the device is already gone;
                # that's not a forget failure.
                pass
            try:
                self._btctl(["untrust", mac], timeout=10)
            except subprocess.CalledProcessError:
                pass
            try:
                self._btctl(["remove", mac], timeout=10)
            except subprocess.CalledProcessError as exc:
                msg = (exc.stderr or exc.stdout or "").lower()
                if "doesnotexist" not in msg and "not available" not in msg:
                    raise
            self._remove_mpd_output(mac)
            self._set(paired_mac="", last_action="Forgotten")
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or "forget failed").strip()
            self._set(last_error=err)
        except Exception as exc:
            self._set(last_error=str(exc))
        finally:
            self._refresh()
            self._set(busy=False)

    # --- MPD output integration ----------------------------------------

    def _add_mpd_output(self, mac: str, name: str) -> None:
        """Run the privileged helper to append a bluealsa audio_output
        block. The helper is idempotent — re-pairing the same MAC after
        an earlier forget is safe."""
        subprocess.run(
            ["sudo", "-n", self._helper_path, "add", mac, name],
            capture_output=True, text=True,
            timeout=HELPER_TIMEOUT_S, check=True)

    def _remove_mpd_output(self, mac: str) -> None:
        subprocess.run(
            ["sudo", "-n", self._helper_path, "remove", mac],
            capture_output=True, text=True,
            timeout=HELPER_TIMEOUT_S, check=True)

    def _route_mpd_to(self, mac: str) -> None:
        """Wait briefly for MPDService to pick up the new bluealsa
        output block, then ask it to switch. Returns silently if the
        output never appears (helper failed to restart MPD, etc.)."""
        if self._mpd is None:
            return
        deadline = time.monotonic() + 8.0
        target_token = f"DEV={mac}"
        while time.monotonic() < deadline:
            outs = getattr(self._mpd, "status", None)
            outs = outs.outputs if outs is not None else ()
            for o in outs:
                # MPD's outputs() doesn't expose `device`, only `name` +
                # `plugin`. We added the block with NAME=friendly name,
                # so match by name first; if that fails, the bluealsa
                # plugin will be unique enough.
                if o.plugin in ("alsa", "bluealsa") and o.name:
                    # Cross-check via /etc/mpd.conf to confirm this row
                    # is in fact the new MAC's block.
                    cfg = self._read_mpd_conf_outputs()
                    dev = cfg.get(o.name, {}).get("device", "")
                    if target_token in dev:
                        try:
                            self._mpd.command(("set_output", o.id))
                        except Exception:
                            pass
                        return
            time.sleep(0.5)

    def _read_mpd_conf_outputs(self) -> dict:
        try:
            with open(self._mpd_conf_path, "r",
                      encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return {}
        out: dict = {}
        for m in re.finditer(r"audio_output\s*\{([^}]*)\}", text, re.S):
            block = m.group(1)
            kv: dict = {}
            for line in block.splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                mm = re.match(r'(\w+)\s+"([^"]*)"', s)
                if mm:
                    kv[mm.group(1)] = mm.group(2)
            nm = kv.get("name", "").strip()
            if nm:
                out[nm] = kv
        return out

    # --- snapshot refresh ---------------------------------------------

    def _refresh(self) -> None:
        try:
            devices_text = self._btctl(["devices"])
        except subprocess.CalledProcessError as exc:
            self._set(available=False,
                      last_error=(exc.stderr or "bluetoothctl failed").strip())
            return
        except FileNotFoundError:
            self._set(available=False, last_error="bluetoothctl not installed")
            return

        rows: dict[str, BluetoothDevice] = {}
        for mac, name in _parse_device_lines(devices_text):
            if not _is_useful_name(name):
                continue
            rows[mac] = BluetoothDevice(mac=mac, name=name)

        # Pull richer state from `info` for the small number we expect to
        # be paired/connected — it's a per-device CLI invocation, so
        # avoid the storm by querying only Paired + Connected sets plus
        # whatever's already in `rows`.
        try:
            paired_text = self._btctl(["devices", "Paired"])
        except subprocess.CalledProcessError:
            paired_text = ""
        try:
            conn_text = self._btctl(["devices", "Connected"])
        except subprocess.CalledProcessError:
            conn_text = ""
        paired_macs = {m for m, _ in _parse_device_lines(paired_text)}
        connected_macs = {m for m, _ in _parse_device_lines(conn_text)}

        # Hydrate full info for the small set the user actually cares
        # about (paired + connected). Discovered-but-not-paired devices
        # don't need an info call — we only show their name + signal.
        hydrate_macs = paired_macs | connected_macs
        for mac in list(rows.keys()):
            if mac not in hydrate_macs:
                continue
            try:
                info_text = self._btctl(["info", mac], timeout=8)
            except subprocess.CalledProcessError:
                continue
            info = _parse_info(info_text)
            icon = info.get("Icon", "").lower()
            name = info.get("Alias") or info.get("Name") or rows[mac].name
            rows[mac] = BluetoothDevice(
                mac=mac, name=name,
                paired=info.get("Paired", "").lower() == "yes",
                trusted=info.get("Trusted", "").lower() == "yes",
                connected=info.get("Connected", "").lower() == "yes",
                is_audio=("audio" in icon or "headphone" in icon
                          or "headset" in icon),
            )

        # For non-hydrated devices fall back to the membership sets so a
        # newly-paired device still shows the green tick before the next
        # full refresh.
        for mac, dev in rows.items():
            if mac in hydrate_macs:
                continue
            if mac in paired_macs or mac in connected_macs:
                rows[mac] = replace(
                    dev,
                    paired=mac in paired_macs,
                    connected=mac in connected_macs,
                )

        # Sort: connected first, then paired, then alphabetical by name.
        items = list(rows.values())
        items.sort(key=lambda d: (
            not d.connected, not d.paired, d.name.lower()))
        self._set(devices=tuple(items),
                  paired_mac=_read_paired_bt_mac(self._mpd_conf_path),
                  available=True)

    def _device_name(self, mac: str) -> str:
        try:
            text = self._btctl(["info", mac], timeout=8)
        except subprocess.CalledProcessError:
            return ""
        info = _parse_info(text)
        return info.get("Alias") or info.get("Name") or ""

    # --- subprocess wrapper -------------------------------------------

    @staticmethod
    def _btctl(args: list[str], *, timeout: int = BTCTL_TIMEOUT_S) -> str:
        # bluetoothctl one-shot mode: runs the command and exits, no
        # interactive prompt. Returns stdout as the canonical surface;
        # we still inspect stderr in error paths via CalledProcessError.
        #
        # Wrapped in `sudo -n` for the same reason WifiService wraps
        # nmcli — the app user isn't in the `bluetooth` group on a fresh
        # bootstrap, and polkit auth would block interactively. The
        # bootstrap installs a NOPASSWD rule pinning to /usr/bin/bluetoothctl
        # so this never prompts; if the rule is missing, sudo exits
        # non-zero immediately rather than hanging.
        r = subprocess.run(
            ["sudo", "-n", "bluetoothctl"] + args,
            capture_output=True, text=True,
            timeout=timeout, check=True)
        return r.stdout


def _short_pair_error(raw: str) -> str:
    """Map common bluez error strings to short, user-readable hints.
    Falls back to the raw message (truncated) when no rule matches —
    diagnostic info still reaches the screen, just less prominently."""
    low = raw.lower()
    if "br-connection-profile-unavailable" in low:
        return "Speaker doesn't support music (A2DP)"
    if "page-timeout" in low or "page timeout" in low:
        return "Speaker not responding — check pairing mode"
    if "authentication" in low or "auth-failed" in low:
        return "Pairing rejected by speaker"
    if "in-progress" in low:
        return "Already busy — wait a moment"
    if "not available" in low:
        return "Speaker is out of range"
    # Single-line, capped length so it fits the status row.
    return raw.splitlines()[0][:64] if raw else "Pair failed"
