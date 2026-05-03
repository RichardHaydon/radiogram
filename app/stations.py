"""Station data model + JSON persistence.

Schema mirrors alarms.json: stable ids, atomic tmp+rename writes, tolerant
load that returns [] on parse failure.

Stream URLs are NEVER invented — content policy requires every station be
explicitly approved by the user. The seed list contains only what the user
has already provided. New stations are added by SSH-editing
/var/lib/clockradio/stations.json (see README).
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Station:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    url: str = ""


# Bundled seed — only stations the user has explicitly provided.
DEFAULT_SEED: list[Station] = [
    Station(id="myfaithradio",
            name="MyFaith Radio",
            url="https://nwm.streamguys1.com/faith/playlist.m3u8"),
]


class StationStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[Station]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            # First boot or corrupt file — seed and return.
            seed = [Station(id=s.id, name=s.name, url=s.url)
                    for s in DEFAULT_SEED]
            self.save(seed)
            return seed
        stations: list[Station] = []
        for d in data.get("stations", []):
            try:
                stations.append(Station(
                    id=d.get("id") or uuid.uuid4().hex,
                    name=str(d.get("name", "")),
                    url=str(d.get("url", "")),
                ))
            except (TypeError, ValueError):
                continue
        return stations

    def save(self, stations: list[Station]) -> None:
        data = {"stations": [asdict(s) for s in stations]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)
