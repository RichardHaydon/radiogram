"""Podcast subscription + playback service.

Mirrors StationService: owns the in-memory podcast list, the current
(podcast, episode) selection, and delegates audio playback to MPD via
play_url commands.

Feed fetches happen on a background thread so the touch loop never
stalls on a slow host. The result is delivered via a callback the UI
can stash on the calling scene; while in flight, `is_fetching(feed_url)`
returns True so the scene can show a "Fetching…" placeholder.

XML parsing uses stdlib `xml.etree` — RSS 2.0 has a stable shape, and
podcast feeds in particular reach a small subset (channel/title +
item/title/enclosure[url,length,type]/pubDate/itunes:duration). No
third-party dependency for that.
"""
from __future__ import annotations

import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict
from typing import Callable

from podcasts import Podcast, PodcastEpisode, PodcastStore, stable_episode_id


# Hard cap on episodes we keep per feed — most podcasts publish hundreds
# of historical episodes but the user only ever scrolls a handful.
MAX_EPISODES_PER_FEED = 60
FETCH_TIMEOUT_S = 15
USER_AGENT = "ClockRadio/1.0 (podcast feed reader)"
_ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


class PodcastService:
    def __init__(self, store: PodcastStore, mpd_service):
        self._store = store
        self._mpd = mpd_service
        self._lock = threading.Lock()
        self._podcasts: list[Podcast] = store.load()
        self._current_podcast_id: str | None = None
        self._current_episode_id: str | None = None
        # Feed URLs currently being fetched — keyed by url so the same
        # subscribe attempt isn't dispatched twice.
        self._in_flight: set[str] = set()

    # --- read API ----------------------------------------------------

    @property
    def podcasts(self) -> list[Podcast]:
        with self._lock:
            return list(self._podcasts)

    def get(self, podcast_id: str) -> Podcast | None:
        with self._lock:
            for p in self._podcasts:
                if p.id == podcast_id:
                    return p
        return None

    @property
    def current_episode_id(self) -> str | None:
        return self._current_episode_id

    @property
    def current_podcast_id(self) -> str | None:
        return self._current_podcast_id

    def current(self) -> tuple[Podcast, PodcastEpisode] | None:
        pid = self._current_podcast_id
        eid = self._current_episode_id
        if not pid or not eid:
            return None
        with self._lock:
            for p in self._podcasts:
                if p.id != pid:
                    continue
                for e in p.episodes:
                    if e.id == eid:
                        return (p, e)
        return None

    def is_fetching(self, feed_url: str = "") -> bool:
        with self._lock:
            if not feed_url:
                return bool(self._in_flight)
            return feed_url in self._in_flight

    # --- mutate API --------------------------------------------------

    def subscribe(self, feed_url: str,
                  on_done: Callable[[bool, str], None] | None = None) -> None:
        """Fetch + parse `feed_url`, then add (or update) a Podcast.
        Runs on a background thread; calls `on_done(success, message)`
        when finished. Safe to call again with the same URL — the second
        call no-ops while the first is still in flight."""
        url = feed_url.strip()
        if not url:
            if on_done:
                on_done(False, "empty url")
            return
        with self._lock:
            if url in self._in_flight:
                return
            self._in_flight.add(url)
        t = threading.Thread(
            target=self._subscribe_worker, args=(url, on_done),
            daemon=True, name=f"podcast-fetch-{url[:24]}")
        t.start()

    def refresh(self, podcast_id: str,
                on_done: Callable[[bool, str], None] | None = None) -> None:
        """Re-fetch the feed for an existing subscription."""
        p = self.get(podcast_id)
        if p is None:
            if on_done:
                on_done(False, "unknown podcast")
            return
        self.subscribe(p.feed_url, on_done=on_done)

    def unsubscribe(self, podcast_id: str) -> None:
        with self._lock:
            self._podcasts = [p for p in self._podcasts if p.id != podcast_id]
            if self._current_podcast_id == podcast_id:
                self._current_podcast_id = None
                self._current_episode_id = None
            self._store.save(self._podcasts)

    def play_episode(self, podcast_id: str, episode_id: str) -> None:
        with self._lock:
            for p in self._podcasts:
                if p.id != podcast_id:
                    continue
                for e in p.episodes:
                    if e.id != episode_id:
                        continue
                    self._current_podcast_id = p.id
                    self._current_episode_id = e.id
                    self._mpd.command(("play_url", e.audio_url))
                    return

    # --- worker / parsing -------------------------------------------

    def _subscribe_worker(self, url: str,
                          on_done: Callable[[bool, str], None] | None) -> None:
        try:
            title, episodes = self._fetch_and_parse(url)
        except Exception as exc:
            print(f"podcast fetch failed for {url}: {exc}",
                  file=sys.stderr, flush=True)
            with self._lock:
                self._in_flight.discard(url)
            if on_done:
                on_done(False, str(exc))
            return
        with self._lock:
            existing = next((p for p in self._podcasts
                             if p.feed_url == url), None)
            if existing is None:
                new_pod = Podcast(title=title or url, feed_url=url)
                # Assign episode ids deterministically so they survive
                # the next refresh.
                new_pod.episodes = [
                    PodcastEpisode(
                        id=stable_episode_id(new_pod.id, ep.id, ep.audio_url),
                        title=ep.title,
                        audio_url=ep.audio_url,
                        pub_date=ep.pub_date,
                        duration_s=ep.duration_s)
                    for ep in episodes[:MAX_EPISODES_PER_FEED]
                ]
                new_pod.last_fetched_at = time.time()
                self._podcasts.append(new_pod)
            else:
                existing.title = title or existing.title or url
                existing.episodes = [
                    PodcastEpisode(
                        id=stable_episode_id(existing.id, ep.id, ep.audio_url),
                        title=ep.title,
                        audio_url=ep.audio_url,
                        pub_date=ep.pub_date,
                        duration_s=ep.duration_s)
                    for ep in episodes[:MAX_EPISODES_PER_FEED]
                ]
                existing.last_fetched_at = time.time()
            self._store.save(self._podcasts)
            self._in_flight.discard(url)
        if on_done:
            on_done(True, "")

    def _fetch_and_parse(self, url: str) -> tuple[str, list[PodcastEpisode]]:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        # RSS 2.0: <rss><channel>...</channel></rss>
        channel = root.find("channel")
        if channel is None:
            raise ValueError("no <channel> element — not an RSS feed?")
        title = (channel.findtext("title") or "").strip()
        eps: list[PodcastEpisode] = []
        for item in channel.findall("item"):
            ep_title = (item.findtext("title") or "").strip()
            enclosure = item.find("enclosure")
            if enclosure is None:
                continue
            audio_url = (enclosure.attrib.get("url") or "").strip()
            if not audio_url:
                continue
            guid = (item.findtext("guid") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            duration_s = _parse_duration(
                item.findtext(f"{_ITUNES_NS}duration"))
            eps.append(PodcastEpisode(
                id=guid or audio_url,   # placeholder; replaced with stable id
                title=ep_title,
                audio_url=audio_url,
                pub_date=pub_date,
                duration_s=duration_s,
            ))
        return title, eps


def _parse_duration(s: str | None) -> int:
    """itunes:duration is either seconds, or HH:MM:SS / MM:SS. Tolerant
    parser — anything unrecognised returns 0."""
    if not s:
        return 0
    s = s.strip()
    if s.isdigit():
        return int(s)
    parts = s.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return 0
    return 0
