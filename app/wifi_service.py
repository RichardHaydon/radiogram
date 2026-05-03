"""Wifi state + control via NetworkManager (nmcli).

Mirrors MPDService's shape: a daemon thread, a lock-protected snapshot
(WifiStatus), and a command queue. Mutating commands (rescan, connect,
forget) flow through the queue so a 5–15 s `nmcli connect` never
blocks the UI thread.

A slow background poll (~30 s) keeps the current SSID / signal / IP
fresh; the expensive `wifi list` is run on enter to the wifi scene and
on explicit rescan/connect — wifi scans are throttled by NM anyway.

`nmcli` mutations need root. The bootstrap installs a sudoers drop-in
granting NOPASSWD nmcli to the app user; we shell out via `sudo -n`
which fails fast if the rule isn't present.
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace


CMD_WAIT_S = 0.2
SLOW_POLL_S = 30.0
NMCLI_TIMEOUT_S = 20


@dataclass(frozen=True)
class Network:
    ssid: str
    signal: int = 0
    security: str = ""   # "WPA2", "WPA2 WPA3", "" for open
    in_use: bool = False


@dataclass(frozen=True)
class WifiStatus:
    state: str = "unknown"        # connected/disconnected/connecting/...
    ssid: str = ""                # current
    signal: int = 0
    ip: str = ""
    last_error: str = ""
    busy: bool = False            # a connect/rescan is in-flight
    networks: tuple[Network, ...] = field(default_factory=tuple)
    # Names of saved wifi profiles. Used by the UI to skip the password
    # prompt when reconnecting to a known network.
    saved: tuple[str, ...] = field(default_factory=tuple)


def _split_terse(line: str) -> list[str]:
    """Parse one line of `nmcli -t` output. Fields are colon-separated;
    nmcli escapes literal colons inside fields with a backslash."""
    fields: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields


def _parse_wifi_list(text: str) -> list[Network]:
    """Parse `nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list`.
    De-dups by SSID — keeping the strongest signal — and drops hidden
    (empty SSID) entries since we can't connect to them anyway."""
    best: dict[str, Network] = {}
    for raw in text.splitlines():
        if not raw:
            continue
        f = _split_terse(raw)
        if len(f) < 4:
            continue
        in_use = f[0].strip() == "*"
        ssid = f[1]
        if not ssid:
            continue
        try:
            signal = int(f[2])
        except ValueError:
            signal = 0
        security = f[3]
        existing = best.get(ssid)
        if existing is None or signal > existing.signal:
            best[ssid] = Network(ssid=ssid, signal=signal,
                                 security=security, in_use=in_use)
        elif in_use:
            best[ssid] = replace(existing, in_use=True)
    nets = list(best.values())
    nets.sort(key=lambda n: (not n.in_use, -n.signal))
    return nets


class WifiService:
    def __init__(self):
        self._lock = threading.Lock()
        self._status = WifiStatus()
        self._stop_evt = threading.Event()
        self._cmd_q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    @property
    def status(self) -> WifiStatus:
        with self._lock:
            return self._status

    def rescan(self) -> None:
        self._cmd_q.put(("rescan",))

    def connect(self, ssid: str, password: str | None = None) -> None:
        self._cmd_q.put(("connect", ssid, password))

    def forget(self, ssid: str) -> None:
        self._cmd_q.put(("forget", ssid))

    def refresh_now(self) -> None:
        """Ask the worker thread to refresh the snapshot ASAP. Cheap —
        no scan, just current-state queries."""
        self._cmd_q.put(("refresh",))

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="wifi-poll")
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    # --- thread internals ------------------------------------------------

    def _set(self, **kwargs) -> None:
        with self._lock:
            self._status = replace(self._status, **kwargs)

    def _run(self) -> None:
        try:
            self._refresh(scan=False)
        except Exception as exc:
            print(f"wifi initial refresh: {exc}",
                  file=sys.stderr, flush=True)
        last_poll = time.monotonic()
        while not self._stop_evt.is_set():
            try:
                cmd = self._cmd_q.get(timeout=CMD_WAIT_S)
            except queue.Empty:
                cmd = None
            if cmd is not None:
                try:
                    self._execute(cmd)
                except Exception as exc:
                    print(f"wifi cmd {cmd}: {exc}",
                          file=sys.stderr, flush=True)
                last_poll = time.monotonic()
                continue
            now = time.monotonic()
            if now - last_poll >= SLOW_POLL_S:
                try:
                    self._refresh(scan=False)
                except Exception as exc:
                    print(f"wifi poll: {exc}",
                          file=sys.stderr, flush=True)
                last_poll = now

    def _execute(self, cmd: tuple) -> None:
        kind = cmd[0]
        if kind == "refresh":
            self._refresh(scan=False)
            return
        if kind == "rescan":
            self._set(busy=True, last_error="")
            try:
                self._sudo_nmcli(["device", "wifi", "rescan"])
            except subprocess.CalledProcessError as exc:
                # nmcli rate-limits scans; that's not user-visible failure.
                msg = (exc.stderr or "").strip()
                if "wait" not in msg.lower():
                    self._set(last_error=msg or "rescan failed")
            self._refresh(scan=True)
            self._set(busy=False)
            return
        if kind == "connect":
            ssid = cmd[1]
            password = cmd[2]
            args = ["device", "wifi", "connect", ssid]
            if password:
                args += ["password", password]
            self._set(busy=True, last_error="")
            try:
                self._sudo_nmcli(args)
                self._set(last_error="")
            except subprocess.CalledProcessError as exc:
                self._set(last_error=(exc.stderr or "connect failed").strip())
            self._refresh(scan=False)
            self._set(busy=False)
            return
        if kind == "forget":
            ssid = cmd[1]
            self._set(busy=True, last_error="")
            try:
                self._sudo_nmcli(["connection", "delete", ssid])
            except subprocess.CalledProcessError as exc:
                self._set(last_error=(exc.stderr or "forget failed").strip())
            self._refresh(scan=False)
            self._set(busy=False)

    def _refresh(self, *, scan: bool) -> None:
        # Overall state from `nmcli general status`
        general = self._nmcli(["-t", "-f", "STATE,WIFI", "general"])
        state = "unknown"
        if general:
            f = _split_terse(general.splitlines()[0])
            if f:
                state = f[0]
        # Active SSID + signal
        active = self._nmcli([
            "-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi"])
        ssid = ""
        signal = 0
        for line in active.splitlines():
            f = _split_terse(line)
            if len(f) >= 3 and f[0] == "yes":
                ssid = f[1]
                try:
                    signal = int(f[2])
                except ValueError:
                    signal = 0
                break
        # IP — first non-loopback IPv4 from `hostname -I`.
        ip_out = self._run_text(["hostname", "-I"])
        toks = ip_out.split()
        ip = toks[0] if toks else ""
        # Network list. With scan=False this returns nmcli's last cached
        # list (cheap, no airtime); scan=True forces a rescan.
        list_args = ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
                     "device", "wifi", "list"]
        if not scan:
            list_args += ["--rescan", "no"]
        list_out = self._nmcli(list_args)
        networks = tuple(_parse_wifi_list(list_out))
        # Saved wifi profiles. nmcli reports type as "802-11-wireless".
        saved_out = self._nmcli([
            "-t", "-f", "NAME,TYPE", "connection", "show"])
        saved: list[str] = []
        for line in saved_out.splitlines():
            f = _split_terse(line)
            if len(f) >= 2 and "wireless" in f[1].lower():
                saved.append(f[0])
        self._set(state=state, ssid=ssid, signal=signal, ip=ip,
                  networks=networks, saved=tuple(saved))

    @staticmethod
    def _nmcli(args: list[str]) -> str:
        try:
            r = subprocess.run(
                ["nmcli"] + args,
                capture_output=True, text=True,
                timeout=NMCLI_TIMEOUT_S, check=False)
            return r.stdout
        except subprocess.SubprocessError as exc:
            print(f"nmcli {args}: {exc}", file=sys.stderr, flush=True)
            return ""

    @staticmethod
    def _sudo_nmcli(args: list[str]) -> None:
        # -n: never prompt; if the sudoers rule isn't installed this
        # exits non-zero immediately rather than hanging.
        subprocess.run(
            ["sudo", "-n", "nmcli"] + args,
            capture_output=True, text=True,
            timeout=NMCLI_TIMEOUT_S, check=True)

    @staticmethod
    def _run_text(cmdline: list[str]) -> str:
        try:
            r = subprocess.run(cmdline, capture_output=True, text=True,
                               timeout=5, check=False)
            return r.stdout
        except subprocess.SubprocessError:
            return ""
