"""Alarm data model + JSON persistence.

Alarm shape
-----------
    id          uuid (string), stable identifier across restarts
    enabled     master on/off
    hour, min   24h time
    days        bitmask, bit i set = fire on weekday i (Mon=0..Sun=6).
                days == 0 means one-shot — auto-disables after firing.
    skip_next   silently skip the next firing, then auto-clear.

Persistence: /var/lib/clockradio/alarms.json. Atomic write (tmp+rename)
so a power-yank during save doesn't corrupt the file.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path


DAY_NAMES_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_KEYS_SHORT = ("day.short.mon", "day.short.tue", "day.short.wed",
                  "day.short.thu", "day.short.fri", "day.short.sat",
                  "day.short.sun")
ALL_WEEKDAYS = 0b0011111  # Mon..Fri
ALL_WEEKEND = 0b1100000   # Sat..Sun
ALL_DAYS = 0b1111111


@dataclass
class Alarm:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    enabled: bool = True
    hour: int = 7
    minute: int = 0
    days: int = 0
    skip_next: bool = False


def days_label(days: int) -> str:
    """Localised label for an alarm's day mask. Resolved at call-time
    so the string follows the active language."""
    # Late import to avoid a circular dependency: scenes imports alarm,
    # i18n_service imports nothing app-side, but the module-level _t
    # helper lives in scenes (it carries the I18nService handle wired
    # at startup).
    try:
        from scenes import _t
    except Exception:
        # Fallback path used during unit imports — return English.
        from translations import EN
        if days == 0:
            return EN["days.once"]
        if days == ALL_WEEKDAYS:
            return EN["days.weekdays"]
        if days == ALL_WEEKEND:
            return EN["days.weekend"]
        if days == ALL_DAYS:
            return EN["days.every_day"]
        return " ".join(EN[DAY_KEYS_SHORT[i]] for i in range(7)
                        if days & (1 << i))
    if days == 0:
        return _t("days.once")
    if days == ALL_WEEKDAYS:
        return _t("days.weekdays")
    if days == ALL_WEEKEND:
        return _t("days.weekend")
    if days == ALL_DAYS:
        return _t("days.every_day")
    return " ".join(_t(DAY_KEYS_SHORT[i]) for i in range(7)
                    if days & (1 << i))


def next_fire(alarm: Alarm, now: datetime, *,
              tolerance_seconds: float = 0) -> datetime | None:
    """When does this alarm next fire? None if disabled.

    tolerance_seconds: scheduled moments up to this many seconds in the
    past are still treated as 'next' — lets a scheduler with non-instant
    ticks catch a moment it just missed. Default 0 = strict future-only,
    suitable for "show next alarm" UI.
    """
    if not alarm.enabled:
        return None
    if alarm.days == 0:
        cand = now.replace(hour=alarm.hour, minute=alarm.minute,
                           second=0, microsecond=0)
        if (cand - now).total_seconds() < -tolerance_seconds:
            cand += timedelta(days=1)
        return cand
    for delta in range(8):
        d = now + timedelta(days=delta)
        if alarm.days & (1 << d.weekday()):
            cand = d.replace(hour=alarm.hour, minute=alarm.minute,
                             second=0, microsecond=0)
            if (cand - now).total_seconds() >= -tolerance_seconds:
                return cand
    return None


class AlarmStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[Alarm]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        alarms: list[Alarm] = []
        for d in data.get("alarms", []):
            try:
                alarms.append(Alarm(
                    id=d.get("id") or uuid.uuid4().hex,
                    enabled=bool(d.get("enabled", True)),
                    hour=int(d.get("hour", 7)),
                    minute=int(d.get("minute", 0)),
                    days=int(d.get("days", 0)),
                    skip_next=bool(d.get("skip_next", False)),
                ))
            except (TypeError, ValueError):
                continue
        return alarms

    def save(self, alarms: list[Alarm]) -> None:
        data = {"alarms": [asdict(a) for a in alarms]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)
