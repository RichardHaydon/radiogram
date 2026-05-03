"""Threaded MPD wrapper.

Owns a single MPDClient on a daemon thread. The UI reads .status (a
frozen MPDStatus snapshot) and pushes commands via .command(name).
Reconnects on socket loss.

This is the template for any future background data source. A
WeatherService, VerseService, or CameraService should follow the same
shape: thread + lock-protected snapshot + start()/stop() lifecycle.
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

try:
    from mpd import MPDClient
    from mpd.base import ConnectionError as MPDConnectionError
    from mpd.base import CommandError as MPDCommandError
    AVAILABLE = True
except ImportError:
    MPDClient = None  # type: ignore
    MPDConnectionError = Exception  # type: ignore
    MPDCommandError = Exception  # type: ignore
    AVAILABLE = False


POLL_INTERVAL_S = 2.0
CMD_WAIT_S = 0.15
VOL_STEP = 10
VOL_WRAP_FROM = 100
VOL_WRAP_TO = 10
# Watchdog: when MPD wedges (TCP open but never answers), the client
# socket times out and we keep retrying. After this many consecutive
# connect failures (≈ 8 s of dead silence) we ask systemd to restart
# MPD via sudo. Min interval prevents a flapping MPD from looping
# the restart cycle every few seconds.
CONNECT_FAILS_BEFORE_RESTART = 4
MIN_RESTART_INTERVAL_S = 60.0


@dataclass(frozen=True)
class AudioOutput:
    id: int
    name: str
    enabled: bool
    plugin: str = ""   # e.g. "alsa", "bluealsa" — useful in the picker


@dataclass(frozen=True)
class MPDStatus:
    state: str = "stop"
    station: str = ""
    title: str = ""
    volume: int = 0
    # Stream info — empty when no stream is active.
    audio: str = ""           # e.g. "44100:16:2" (Hz : bits : channels)
    bitrate: int = 0          # kbps
    # Configured MPD outputs (snapshot per poll).
    outputs: tuple = ()

    @property
    def active(self) -> bool:
        return self.state in ("play", "pause")


class MPDService:
    def __init__(self, host: str = "localhost", port: int = 6600):
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._status = MPDStatus()
        self._stop_evt = threading.Event()
        self._cmd_q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._connect_failures = 0
        self._last_restart_t = 0.0

    @property
    def status(self) -> MPDStatus:
        with self._lock:
            return self._status

    def command(self, cmd: str) -> None:
        self._cmd_q.put(cmd)

    def start(self) -> None:
        if not AVAILABLE:
            print("mpd: library missing", file=sys.stderr, flush=True)
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="mpd-poll")
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    # --- thread-only helpers ---------------------------------------

    def _set(self, status: MPDStatus) -> None:
        with self._lock:
            self._status = status

    def _maybe_restart_mpd(self) -> None:
        """If MPD has been unreachable long enough, ask systemd to bounce
        it. Requires a sudoers drop-in granting NOPASSWD systemctl
        restart mpd to the app user — installed by setup/01-bootstrap.sh."""
        if self._connect_failures < CONNECT_FAILS_BEFORE_RESTART:
            return
        now = time.monotonic()
        if now - self._last_restart_t < MIN_RESTART_INTERVAL_S:
            return
        self._last_restart_t = now
        self._connect_failures = 0
        print("mpd watchdog: restarting mpd.service",
              file=sys.stderr, flush=True)
        try:
            r = subprocess.run(
                ["sudo", "-n", "systemctl", "restart", "mpd"],
                capture_output=True, text=True,
                timeout=15, check=False)
            if r.returncode != 0:
                msg = (r.stderr or r.stdout or "").strip()
                print(f"mpd watchdog: restart failed: {msg}",
                      file=sys.stderr, flush=True)
        except subprocess.SubprocessError as exc:
            print(f"mpd watchdog: restart subprocess: {exc}",
                  file=sys.stderr, flush=True)

    def _connect(self):
        c = MPDClient()
        # Keep this generous: when MPD is busy reconnecting to a stalled
        # stream it can take a few seconds to answer a new control
        # request. A short timeout flips us into a reconnect loop that
        # ends only when the user manually restarts the service.
        c.timeout = 5
        c.connect(self.host, self.port)
        # Single-stream playlists otherwise stop on `next`; repeat is
        # also the right default UX for a radio.
        try:
            c.repeat(1)
        except (OSError, MPDConnectionError) as exc:
            print(f"mpd repeat(1) failed: {exc}",
                  file=sys.stderr, flush=True)
        return c

    def _refresh(self, client) -> None:
        st = client.status()
        state = st.get("state", "stop")
        try:
            volume = int(st.get("volume", 0))
        except (TypeError, ValueError):
            volume = 0
        if state in ("play", "pause"):
            song = client.currentsong()
            station = song.get("name") or song.get("album") or ""
            title = song.get("title") or song.get("file") or ""
        else:
            station = ""
            title = ""
        # Stream format + bitrate are blank when stopped; that's fine,
        # the UI only renders them when a stream is active.
        audio = st.get("audio", "") or ""
        try:
            bitrate = int(st.get("bitrate", 0))
        except (TypeError, ValueError):
            bitrate = 0
        # Outputs change rarely (config + user toggle) but the cost of
        # asking each poll is one extra MPD command — negligible.
        try:
            outs_raw = client.outputs()
        except (OSError, MPDConnectionError):
            outs_raw = []
        outputs = tuple(
            AudioOutput(
                id=int(o.get("outputid", -1)),
                name=str(o.get("outputname", "")),
                enabled=str(o.get("outputenabled", "0")) == "1",
                plugin=str(o.get("plugin", "")),
            )
            for o in outs_raw
        )
        self._set(MPDStatus(state=state, station=station,
                            title=title, volume=volume,
                            audio=audio, bitrate=bitrate,
                            outputs=outputs))

    def _execute(self, client, cmd) -> None:
        # Tuple commands: ("setvol", N), ("play_url", url), ("stop_alarm",),
        # ("set_output", id).
        if isinstance(cmd, tuple):
            name = cmd[0]
            if name == "setvol":
                client.setvol(int(cmd[1]))
            elif name == "play_url":
                client.clear()
                client.add(str(cmd[1]))
                client.play()
            elif name == "stop_alarm":
                client.stop()
                client.clear()
            elif name == "set_output":
                # Switch outputs exclusively — enable target, disable
                # all others. Lets the UI present an audio-output picker
                # instead of toggle-each-row semantics.
                target_id = int(cmd[1])
                for o in client.outputs():
                    oid = int(o.get("outputid", -1))
                    if oid == target_id:
                        client.enableoutput(oid)
                    else:
                        client.disableoutput(oid)
            return
        # String commands.
        if cmd == "toggle":
            cur = client.status().get("state")
            if cur == "play":
                client.pause(1)
            elif cur == "pause":
                client.pause(0)
            else:
                client.play()
        elif cmd == "next":
            client.next()
        elif cmd == "prev":
            client.previous()
        elif cmd == "vol_step":
            cur = int(client.status().get("volume", 50))
            new = cur + VOL_STEP
            if new > VOL_WRAP_FROM:
                new = VOL_WRAP_TO
            client.setvol(new)
        elif cmd == "vol_up":
            cur = int(client.status().get("volume", 50))
            client.setvol(min(100, cur + VOL_STEP))
        elif cmd == "vol_down":
            cur = int(client.status().get("volume", 50))
            client.setvol(max(0, cur - VOL_STEP))

    def _run(self) -> None:
        client = None
        last_poll = 0.0
        while not self._stop_evt.is_set():
            if client is None:
                try:
                    client = self._connect()
                    self._connect_failures = 0
                    last_poll = 0.0
                except (OSError, MPDConnectionError) as exc:
                    print(f"mpd connect failed: {exc}",
                          file=sys.stderr, flush=True)
                    self._set(MPDStatus())
                    self._connect_failures += 1
                    self._maybe_restart_mpd()
                    if self._stop_evt.wait(POLL_INTERVAL_S):
                        return
                    continue

            cmds: list[str] = []
            try:
                cmds.append(self._cmd_q.get(timeout=CMD_WAIT_S))
                while True:
                    try:
                        cmds.append(self._cmd_q.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                pass

            if self._stop_evt.is_set():
                break

            try:
                for cmd in cmds:
                    # CommandError = MPD rejected the command (bad mixer
                    # control, missing playlist, etc). Connection is
                    # still good — log and keep going so one bad command
                    # doesn't kill volume control until the next reboot.
                    try:
                        self._execute(client, cmd)
                    except MPDCommandError as exc:
                        print(f"mpd command {cmd!r} failed: {exc}",
                              file=sys.stderr, flush=True)
                now = time.monotonic()
                if cmds or now - last_poll >= POLL_INTERVAL_S:
                    try:
                        self._refresh(client)
                    except MPDCommandError as exc:
                        print(f"mpd refresh failed: {exc}",
                              file=sys.stderr, flush=True)
                    last_poll = now
            except (OSError, MPDConnectionError) as exc:
                print(f"mpd lost: {exc}", file=sys.stderr, flush=True)
                try:
                    client.disconnect()
                except Exception:
                    pass
                client = None
                self._set(MPDStatus())

        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
