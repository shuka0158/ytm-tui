# ytm-tui - a terminal player for your YouTube Music account.
# Copyright (C) 2026 shuka0158
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Plain data holders shared between the API layer and the UI."""
from __future__ import annotations

from dataclasses import dataclass, field


def _parse_duration(text: str | None) -> int:
    if not text:
        return 0
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def fmt_duration(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@dataclass(slots=True)
class Track:
    video_id: str
    title: str
    artist: str
    album: str = ""
    duration: int = 0
    thumb: str = ""

    @classmethod
    def from_item(cls, item: dict) -> "Track | None":
        """Build a Track from a ytmusicapi track dict, or None if unplayable."""
        video_id = item.get("videoId")
        if not video_id:
            return None

        artists = item.get("artists") or []
        artist = ", ".join(a.get("name", "") for a in artists if a.get("name"))
        if not artist:
            artist = (item.get("author") or {}).get("name", "") if isinstance(item.get("author"), dict) else ""

        album = ""
        raw_album = item.get("album")
        if isinstance(raw_album, dict):
            album = raw_album.get("name") or ""
        elif isinstance(raw_album, str):
            album = raw_album

        duration = item.get("duration_seconds") or _parse_duration(item.get("duration"))

        thumbs = item.get("thumbnails") or []
        thumb = thumbs[-1].get("url", "") if thumbs else ""

        return cls(
            video_id=video_id,
            title=item.get("title") or "Unknown",
            artist=artist or "Unknown artist",
            album=album,
            duration=int(duration or 0),
            thumb=thumb,
        )


@dataclass(slots=True)
class Playlist:
    id: str
    title: str
    subtitle: str = ""
    thumb: str = ""
    kind: str = "playlist"          # playlist | liked | library | search
    tracks: list[Track] = field(default_factory=list)
    loaded: bool = False
