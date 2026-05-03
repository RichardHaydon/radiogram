"""Station selection: which station is currently playing, plus next/prev.

Stateless wrt MPD — owns the in-memory station list and the current id,
delegates actual playback to MPDService via play_url commands.

Why a service (vs. dropping the logic into a scene): scenes are rebuilt
or hidden frequently; the "current station" needs to outlive that. And
multiple scenes (RadioScene transport row, StationListScene highlight,
QuickPanelScene later) need read access.
"""
from __future__ import annotations

import threading

from stations import Station, StationStore


class StationService:
    def __init__(self, store: StationStore, mpd_service):
        self._store = store
        self._mpd = mpd_service
        self._lock = threading.Lock()
        self._stations: list[Station] = store.load()
        self._current_id: str | None = None

    @property
    def stations(self) -> list[Station]:
        with self._lock:
            return list(self._stations)

    @property
    def current_id(self) -> str | None:
        return self._current_id

    def current(self) -> Station | None:
        cid = self._current_id
        if cid is None:
            return None
        with self._lock:
            for s in self._stations:
                if s.id == cid:
                    return s
        return None

    def play(self, station_id: str) -> None:
        with self._lock:
            for s in self._stations:
                if s.id == station_id:
                    self._current_id = s.id
                    self._mpd.command(("play_url", s.url))
                    return

    def next(self) -> None:
        self._cycle(1)

    def prev(self) -> None:
        self._cycle(-1)

    def _cycle(self, delta: int) -> None:
        with self._lock:
            if not self._stations:
                return
            ids = [s.id for s in self._stations]
            if self._current_id in ids:
                idx = ids.index(self._current_id)
                new_idx = (idx + delta) % len(self._stations)
            else:
                # Nothing selected yet — start at the natural end.
                new_idx = 0 if delta > 0 else len(self._stations) - 1
            s = self._stations[new_idx]
            self._current_id = s.id
            self._mpd.command(("play_url", s.url))
