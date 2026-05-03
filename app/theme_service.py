"""Theme selection: persisted choice + a proxy that quacks like Theme.

Why a proxy: scenes are constructed once at startup with `theme=...`
captured into self.theme. We want runtime theme switching without
rebuilding scenes, so we hand each scene a ThemeProxy whose attribute
access (palette / fonts / clock_style / button_style) always reflects
the *current* selection. Existing helpers like `color(theme, role)`
keep working unchanged.

Theme switches need to trigger a repaint. The compositor reads
`.version` (a counter that bumps on every set()) and folds it into its
repaint key, so the next tick draws with the new colors.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from theme import Theme


class ThemeService:
    def __init__(self, store_path: Path, themes: list[Theme]):
        self._store_path = Path(store_path)
        self._themes = list(themes)
        self._current = self._themes[0]
        self._lock = threading.Lock()
        self._version = 0
        self._load()

    @property
    def current(self) -> Theme:
        with self._lock:
            return self._current

    @property
    def version(self) -> int:
        # Bumped each set(); compositor uses this to know when to repaint.
        return self._version

    @property
    def themes(self) -> list[Theme]:
        return list(self._themes)

    def set(self, name: str) -> bool:
        for t in self._themes:
            if t.name == name:
                with self._lock:
                    if self._current.name == name:
                        return True
                    self._current = t
                    self._version += 1
                self._save()
                return True
        return False

    def _load(self) -> None:
        try:
            data = json.loads(self._store_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        name = data.get("theme", "")
        for t in self._themes:
            if t.name == name:
                self._current = t
                return

    def _save(self) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"theme": self._current.name}, indent=2))
            import os
            os.replace(tmp, self._store_path)
        except OSError as exc:
            print(f"theme save: {exc}",
                  file=sys.stderr, flush=True)


class ThemeProxy:
    """Drop-in for Theme that always reflects the current selection.

    Each property delegates to ThemeService.current, so scenes that
    captured `self.theme = proxy` at construction will still see live
    palette / fonts / style values after a theme change.
    """

    def __init__(self, service: ThemeService):
        self._service = service

    @property
    def name(self) -> str:
        return self._service.current.name

    @property
    def palette(self):
        return self._service.current.palette

    @property
    def fonts(self):
        return self._service.current.fonts

    @property
    def clock_style(self) -> str:
        return self._service.current.clock_style

    @property
    def button_style(self) -> str:
        return self._service.current.button_style
