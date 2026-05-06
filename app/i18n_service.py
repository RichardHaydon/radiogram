"""Language selection: persisted choice + a service that hands out
translated strings.

Why a service rather than a module-level dict
---------------------------------------------
Same shape as ThemeService — runtime switching needs cache invalidation
and a `version` counter the compositor folds into its repaint key, so
flipping languages from the picker repaints every scene with the new
strings without rebuilding scene objects.

Lookup is `t(key)` (returns the translated string) or `t(key, **fmt)`
(formats with `.format(**fmt)` after lookup). Missing keys fall back
to the English string; missing translations for a non-English locale
fall back to English so a partial translation still ships safely.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from translations import LANGUAGES, TRANSLATIONS


VALID_LANG_CODES = tuple(code for code, _ in LANGUAGES)
DEFAULT_LANG = "en"


class I18nService:
    def __init__(self, store_path: Path):
        self._store_path = Path(store_path)
        self._lang = DEFAULT_LANG
        self._lock = threading.Lock()
        self._version = 0
        self._load()

    @property
    def lang(self) -> str:
        with self._lock:
            return self._lang

    @property
    def version(self) -> int:
        return self._version

    @property
    def languages(self) -> list[tuple[str, str]]:
        """[(code, native_name), ...] in display order."""
        return list(LANGUAGES)

    def native_name(self, code: str | None = None) -> str:
        c = code or self._lang
        for lc, name in LANGUAGES:
            if lc == c:
                return name
        return c

    def set(self, code: str) -> bool:
        if code not in VALID_LANG_CODES:
            return False
        with self._lock:
            if self._lang == code:
                return True
            self._lang = code
            self._version += 1
        self._save()
        return True

    def t(self, key: str, **fmt) -> str:
        """Translate `key`. Falls back through:
        active language → English → the key itself.
        Format placeholders are applied after lookup."""
        with self._lock:
            lang = self._lang
        table = TRANSLATIONS.get(lang) or {}
        s = table.get(key)
        if s is None:
            s = TRANSLATIONS["en"].get(key, key)
        if fmt:
            try:
                return s.format(**fmt)
            except (KeyError, IndexError, ValueError):
                # Translation accidentally dropped a placeholder — fall
                # back to English so the substitution still works.
                en = TRANSLATIONS["en"].get(key, key)
                try:
                    return en.format(**fmt)
                except Exception:
                    return en
        return s

    def _load(self) -> None:
        try:
            data = json.loads(self._store_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        code = data.get("lang", "")
        if code in VALID_LANG_CODES:
            self._lang = code

    def _save(self) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"lang": self._lang}, indent=2))
            os.replace(tmp, self._store_path)
        except OSError as exc:
            print(f"i18n save: {exc}", file=sys.stderr, flush=True)
