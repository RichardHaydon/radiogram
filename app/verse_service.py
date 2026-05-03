"""Verse-of-the-day service.

Two-step fetch:
    1. labs.bible.org/api/?passage=votd  → today's reference (e.g. "John 3:16")
    2. bible-api.com/<ref>?translation=<id>  → text in chosen translation

No API keys for either. We split it because labs.bible.org is the only
reliable free VOTD source but only serves NET text; bible-api.com gives
KJV/ASV/WEB/etc. but has no daily-verse endpoint.

The selected translation is persisted to disk so it survives reboot.
We also cache today's reference + text — same-day re-opens of the
verse scene don't re-hit either API.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path


HTTP_TIMEOUT_S = 6.0
USER_AGENT = "clockradio/1.0 (https://github.com/RichardHaydon/radiogram)"
VOTD_URL = "https://labs.bible.org/api/?passage=votd&type=json"
TEXT_URL = "https://bible-api.com/{ref}?translation={trans}"


# (id, short label) — id is bible-api.com's translation slug.
TRANSLATIONS: list[tuple[str, str]] = [
    ("kjv", "KJV"),
    ("web", "WEB"),
    ("asv", "ASV"),
    ("bbe", "BBE"),
    ("ylt", "YLT"),
    ("dra", "DRA"),
    ("oeb-cw", "OEB"),
]


def translation_label(slug: str) -> str:
    for t, lbl in TRANSLATIONS:
        if t == slug:
            return lbl
    return slug.upper()


@dataclass(frozen=True)
class VerseStatus:
    translation: str = "kjv"
    reference: str = ""
    text: str = ""
    last_fetch_date: str = ""   # local-date ISO of last successful fetch
    last_error: str = ""
    busy: bool = False


class VerseService:
    def __init__(self, store_path: Path):
        self._store_path = Path(store_path)
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._cmd_q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        # Defaults; _load() may overwrite from disk.
        self._translation = "kjv"
        self._cached_ref = ""
        self._cached_text = ""
        self._cached_date = ""
        self._status = VerseStatus(translation=self._translation)
        self._load()

    @property
    def status(self) -> VerseStatus:
        with self._lock:
            return self._status

    @property
    def translation(self) -> str:
        return self._translation

    def refresh(self) -> None:
        self._cmd_q.put(("refresh",))

    def cycle_translation(self) -> None:
        self._cmd_q.put(("cycle",))

    def set_translation(self, slug: str) -> None:
        self._cmd_q.put(("set", slug))

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="verse")
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
        # Don't auto-fetch at start — VerseScene.on_show triggers a
        # refresh on entry; cached value is used until then so we don't
        # burn API calls if the user never opens the scene.
        while not self._stop_evt.is_set():
            try:
                cmd = self._cmd_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._execute(cmd)
            except Exception as exc:
                print(f"verse cmd {cmd}: {exc}",
                      file=sys.stderr, flush=True)
                self._set(busy=False, last_error=str(exc))

    def _execute(self, cmd: tuple) -> None:
        kind = cmd[0]
        if kind == "refresh":
            self._fetch(force=False)
            return
        if kind == "cycle":
            ids = [t for t, _ in TRANSLATIONS]
            try:
                idx = ids.index(self._translation)
            except ValueError:
                idx = -1
            self._translation = ids[(idx + 1) % len(ids)]
            self._fetch(force=True)
            return
        if kind == "set":
            slug = cmd[1]
            if slug == self._translation:
                return
            self._translation = slug
            self._fetch(force=True)

    def _fetch(self, *, force: bool) -> None:
        today = datetime.now().date().isoformat()
        # Fast path: same day, same translation, have cached text.
        if (not force
                and self._cached_date == today
                and self._cached_text
                and self._status.translation == self._translation):
            self._set(
                translation=self._translation,
                reference=self._cached_ref,
                text=self._cached_text,
                last_fetch_date=today,
                last_error="",
                busy=False,
            )
            return
        self._set(busy=True, last_error="")
        # Step 1 — get today's reference (reuse cached if same day).
        if self._cached_date == today and self._cached_ref:
            ref = self._cached_ref
        else:
            ref = self._fetch_votd_reference()
            self._cached_ref = ref
            self._cached_date = today
        # Step 2 — fetch text in current translation.
        text = self._fetch_text(ref, self._translation)
        self._cached_text = text
        self._set(
            translation=self._translation,
            reference=ref,
            text=text,
            last_fetch_date=today,
            last_error="",
            busy=False,
        )
        self._save()

    def _fetch_votd_reference(self) -> str:
        data = self._fetch_json(VOTD_URL)
        if isinstance(data, list) and data:
            v = data[0]
            book = str(v.get("bookname", "")).strip()
            chap = str(v.get("chapter", "")).strip()
            verse = str(v.get("verse", "")).strip()
            if book and chap and verse:
                return f"{book} {chap}:{verse}"
        raise RuntimeError("VOTD lookup returned no reference")

    def _fetch_text(self, reference: str, translation: str) -> str:
        url = TEXT_URL.format(
            ref=urllib.parse.quote(reference),
            trans=urllib.parse.quote(translation),
        )
        data = self._fetch_json(url)
        text = str(data.get("text", "") or "").strip()
        # bible-api.com returns text with leading whitespace per verse;
        # collapse any internal whitespace runs to single spaces while
        # preserving paragraph breaks (\n\n).
        cleaned: list[str] = []
        for para in text.split("\n\n"):
            cleaned.append(" ".join(para.split()))
        return "\n\n".join(p for p in cleaned if p)

    @staticmethod
    def _fetch_json(url: str):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8"))

    # --- persistence ---------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._store_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        slug = data.get("translation")
        if isinstance(slug, str) and slug:
            self._translation = slug
        ref = str(data.get("reference", ""))
        text = str(data.get("text", ""))
        date = str(data.get("date", ""))
        if ref:
            self._cached_ref = ref
            self._cached_text = text
            self._cached_date = date
            self._status = VerseStatus(
                translation=self._translation,
                reference=ref,
                text=text,
                last_fetch_date=date,
            )
        else:
            self._status = VerseStatus(translation=self._translation)

    def _save(self) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "translation": self._translation,
                "reference": self._cached_ref,
                "text": self._cached_text,
                "date": self._cached_date,
            }, indent=2, ensure_ascii=False))
            import os
            os.replace(tmp, self._store_path)
        except OSError as exc:
            print(f"verse save: {exc}",
                  file=sys.stderr, flush=True)
