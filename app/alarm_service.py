"""Alarm scheduler + ramp-up volume.

Scheduler thread polls every SCHEDULER_TICK_S. When current time crosses
an alarm's next-fire moment (within tolerance for missed ticks):
    - one-shot alarms (days == 0) auto-disable
    - if skip_next is set, the moment is consumed silently (flag clears)
    - else: alarm fires — MPD plays the configured URL at vol 0, and a
      ramp thread steps the volume from 0 → target over RAMP_DURATION_S.

When the user taps STOP on the firing scene, stop_firing() cancels the
ramp, stops MPD, and restores volume to target so the radio behaves
normally afterwards.
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta

from alarm import Alarm, AlarmStore, next_fire


RAMP_DURATION_S = 30.0
RAMP_STEPS = 30
SCHEDULER_TICK_S = 5.0
DEFAULT_TARGET_VOL = 70
# Hard cap so an unattended alarm doesn't play forever — 30 min is the
# common bedside-clock convention.
MAX_FIRE_DURATION_S = 1800.0
# Classic clock-radio snooze interval.
SNOOZE_MINUTES = 9


class AlarmService:
    def __init__(self, store: AlarmStore, mpd_service,
                 alarm_url: str = "",
                 target_volume: int = DEFAULT_TARGET_VOL):
        self._store = store
        self._mpd = mpd_service
        self._alarm_url = alarm_url
        self._target_volume = target_volume
        self._lock = threading.Lock()
        self._alarms: list[Alarm] = store.load()
        self._firing: Alarm | None = None
        self._firing_started_at: datetime | None = None
        # Maps alarm.id -> the scheduled-moment datetime we last fired (or
        # consumed via skip_next). Prevents the scheduler from re-firing the
        # same moment repeatedly during the tolerance window.
        self._last_fired: dict[str, "datetime"] = {}
        # Snooze: when set, the scheduler re-fires _snooze_alarm at
        # _snooze_until (typical bedside clock-radio behaviour).
        self._snooze_until: datetime | None = None
        self._snooze_alarm: Alarm | None = None
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._ramp_stop = threading.Event()
        self._ramp_thread: threading.Thread | None = None

    # --- public read API ---------------------------------------------

    @property
    def alarms(self) -> list[Alarm]:
        with self._lock:
            return list(self._alarms)

    @property
    def firing(self) -> bool:
        return self._firing is not None

    @property
    def firing_alarm(self) -> Alarm | None:
        return self._firing

    def next_to_fire(self) -> tuple[Alarm, datetime] | None:
        """Snapshot of the next-to-fire (alarm, fire_time), regardless of
        skip_next. Returns None if no enabled alarm has a next firing."""
        now = datetime.now()
        candidates = []
        for a in self.alarms:
            t = next_fire(a, now)
            if t is not None:
                candidates.append((a, t))
        if not candidates:
            return None
        return min(candidates, key=lambda x: x[1])

    # --- public mutate API -------------------------------------------

    def toggle_skip_next(self) -> bool:
        """Toggle skip_next on the next-to-fire alarm. Returns the new
        flag value, or False if no alarm is scheduled."""
        nf = self.next_to_fire()
        if nf is None:
            return False
        target_id = nf[0].id
        new_flag = False
        with self._lock:
            for a in self._alarms:
                if a.id == target_id:
                    a.skip_next = not a.skip_next
                    new_flag = a.skip_next
            self._store.save(self._alarms)
        return new_flag

    def upsert_alarm(self, a: Alarm) -> None:
        """Insert a new alarm or replace an existing one (matched by id).
        Clears any stale last_fired entry so an edited time is not blocked
        by the de-dupe window."""
        with self._lock:
            for i, existing in enumerate(self._alarms):
                if existing.id == a.id:
                    self._alarms[i] = a
                    self._last_fired.pop(a.id, None)
                    self._store.save(self._alarms)
                    return
            self._alarms.append(a)
            self._store.save(self._alarms)

    def delete_alarm(self, alarm_id: str) -> None:
        with self._lock:
            self._alarms = [a for a in self._alarms if a.id != alarm_id]
            self._last_fired.pop(alarm_id, None)
            self._store.save(self._alarms)

    def stop_firing(self) -> None:
        if self._firing is None:
            return
        # Flip the firing flag first so the scene switches immediately.
        # The ramp thread sees _ramp_stop on its next wait and exits;
        # we don't join here — joining from the UI thread would freeze
        # the panel for up to a second.
        self._firing = None
        self._firing_started_at = None
        # STOP is final — clear any pending snooze too.
        self._snooze_until = None
        self._snooze_alarm = None
        self._ramp_stop.set()
        self._mpd.command(("stop_alarm",))
        self._mpd.command(("setvol", self._target_volume))

    def snooze(self) -> None:
        """Silence the firing alarm for SNOOZE_MINUTES, then re-fire.
        No-op if nothing is firing."""
        if self._firing is None:
            return
        self._snooze_alarm = self._firing
        self._snooze_until = (datetime.now()
                              + timedelta(minutes=SNOOZE_MINUTES))
        self._firing = None
        self._firing_started_at = None
        self._ramp_stop.set()
        self._mpd.command(("stop_alarm",))
        self._mpd.command(("setvol", self._target_volume))
        print(f"alarm snoozed until {self._snooze_until}",
              file=sys.stderr, flush=True)

    @property
    def snoozed_until(self) -> datetime | None:
        return self._snooze_until

    # --- lifecycle ---------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="alarm-sched")
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._ramp_stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if self._ramp_thread is not None:
            self._ramp_thread.join(timeout=1.0)

    # --- scheduler ---------------------------------------------------

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._tick()
            except Exception as exc:
                print(f"alarm tick error: {exc}",
                      file=sys.stderr, flush=True)
            if self._stop_evt.wait(SCHEDULER_TICK_S):
                return

    def _tick(self) -> None:
        now = datetime.now()
        # Hard cap: an alarm that's been firing past MAX_FIRE_DURATION_S
        # gets force-stopped so an unattended panel doesn't play all day.
        if self._firing is not None:
            started = self._firing_started_at
            if (started is not None
                    and (now - started).total_seconds()
                    >= MAX_FIRE_DURATION_S):
                print(f"alarm hard cap reached ({MAX_FIRE_DURATION_S:.0f}s)"
                      f" — auto-stopping",
                      file=sys.stderr, flush=True)
                self.stop_firing()
            return
        # Snooze re-fire: if we've passed _snooze_until, re-arm the same
        # alarm. Cleared first so a second snooze can be set after re-fire.
        if (self._snooze_until is not None
                and now >= self._snooze_until):
            a = self._snooze_alarm
            self._snooze_until = None
            self._snooze_alarm = None
            if a is not None:
                with self._lock:
                    self._fire_locked(a)
            return
        # Tolerance window: alarm time within last 2*tick seconds is still
        # "current" — handles a missed tick. Older than that = stale.
        tolerance_s = SCHEDULER_TICK_S * 2
        with self._lock:
            for a in self._alarms:
                if not a.enabled:
                    continue
                t = next_fire(a, now, tolerance_seconds=tolerance_s)
                if t is None:
                    continue
                if not (t <= now and (now - t).total_seconds() < tolerance_s):
                    continue
                if self._last_fired.get(a.id) == t:
                    continue  # already handled this scheduled moment
                if a.skip_next:
                    a.skip_next = False
                    self._last_fired[a.id] = t
                    self._store.save(self._alarms)
                    print(f"alarm {a.id} skipped at {t}",
                          file=sys.stderr, flush=True)
                    continue
                self._last_fired[a.id] = t
                self._fire_locked(a)
                return

    def _fire_locked(self, a: Alarm) -> None:
        # Caller holds self._lock.
        print(f"alarm {a.id} firing at {datetime.now()} "
              f"({a.hour:02d}:{a.minute:02d})",
              file=sys.stderr, flush=True)
        self._firing = a
        self._firing_started_at = datetime.now()
        if a.days == 0:
            a.enabled = False  # one-shot: auto-disable after firing
            self._store.save(self._alarms)
        if not self._alarm_url:
            print("alarm: no URL configured — alarm scene will show but "
                  "no audio will play", file=sys.stderr, flush=True)
            return
        # Start audio at 0, then ramp.
        self._mpd.command(("setvol", 0))
        self._mpd.command(("play_url", self._alarm_url))
        self._ramp_stop.clear()
        self._ramp_thread = threading.Thread(
            target=self._ramp, daemon=True, name="alarm-ramp")
        self._ramp_thread.start()

    def _ramp(self) -> None:
        target = self._target_volume
        for i in range(1, RAMP_STEPS + 1):
            if self._ramp_stop.is_set():
                return
            v = int(round(target * i / RAMP_STEPS))
            self._mpd.command(("setvol", v))
            if self._ramp_stop.wait(RAMP_DURATION_S / RAMP_STEPS):
                return
