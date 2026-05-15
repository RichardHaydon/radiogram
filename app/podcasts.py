"""Podcast subscription + episode model with JSON persistence.

Mirrors the station model (stations.py): stable ids, atomic tmp+rename
writes, tolerant load that returns [] on parse failure.

Episodes are cached from the last successful feed fetch so the user
can see something the next time they open a podcast page even if the
feed host is briefly unreachable. Refresh re-fetches the feed.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class PodcastEpisode:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    title: str = ""
    audio_url: str = ""
    pub_date: str = ""    # ISO-ish string verbatim from <pubDate>
    duration_s: int = 0    # 0 if the feed didn't expose duration


@dataclass
class Podcast:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    title: str = ""
    feed_url: str = ""
    episodes: list[PodcastEpisode] = field(default_factory=list)
    last_fetched_at: float = 0.0


@dataclass
class SearchResult:
    """One hit from the iTunes podcast search API. Not persisted —
    these are session-scoped objects the search UI displays until the
    user picks one (which then turns into a full Podcast via subscribe)
    or backs out."""
    title: str = ""        # collectionName
    author: str = ""        # artistName
    feed_url: str = ""


def stable_episode_id(podcast_id: str, guid: str, audio_url: str) -> str:
    """A short stable hash so the same episode keeps the same id across
    refreshes — letting "currently playing" highlighting survive a feed
    re-fetch. Falls back to audio_url when guid is missing."""
    key = (guid or audio_url or "").strip()
    digest = hashlib.sha1(f"{podcast_id}:{key}".encode("utf-8")).hexdigest()
    return digest[:16]


class PodcastStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[Podcast]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        pods: list[Podcast] = []
        for d in data.get("podcasts", []):
            try:
                eps = [PodcastEpisode(
                    id=str(e.get("id") or uuid.uuid4().hex),
                    title=str(e.get("title", "")),
                    audio_url=str(e.get("audio_url", "")),
                    pub_date=str(e.get("pub_date", "")),
                    duration_s=int(e.get("duration_s", 0) or 0),
                ) for e in d.get("episodes", [])]
                pods.append(Podcast(
                    id=str(d.get("id") or uuid.uuid4().hex),
                    title=str(d.get("title", "")),
                    feed_url=str(d.get("feed_url", "")),
                    episodes=eps,
                    last_fetched_at=float(d.get("last_fetched_at", 0.0) or 0.0),
                ))
            except (TypeError, ValueError):
                continue
        return pods

    def save(self, podcasts: list[Podcast]) -> None:
        data = {"podcasts": [asdict(p) for p in podcasts]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)
