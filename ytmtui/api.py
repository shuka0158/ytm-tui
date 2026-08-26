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

"""Thin wrapper over ytmusicapi: connects with whatever credentials exist and
turns the raw dicts into Track/Playlist objects."""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from itertools import zip_longest

from ytmusicapi import OAuthCredentials, YTMusic

from . import config
from .models import Playlist, Track

# How alike two titles must be before we treat them as the same song.
_MIN_TITLE_SIMILARITY = 0.55

# Snippets and clips share a title with the real thing; length gives them away.
_DURATION_TOLERANCE = 0.25

_NOISE = re.compile(
    r"\b(official|video|audio|lyrics?|hd|hq|4k|remaster(ed)?|full|clip|snippet)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def _normalise(title: str) -> str:
    text = _NOISE.sub(" ", title.casefold())
    return " ".join(_PUNCT.sub(" ", text).split())


def _title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalise(a), _normalise(b)).ratio()


def _duration_fits(want: int, got: int) -> bool:
    """Reject obvious snippets. Unknown durations get the benefit of the doubt."""
    if not want or not got:
        return True
    return abs(got - want) <= want * _DURATION_TOLERANCE


class AuthMissing(RuntimeError):
    pass


def connect() -> YTMusic:
    """Return an authenticated YTMusic client."""
    auth = config.auth_file()
    if auth is None:
        raise AuthMissing(
            "No credentials found. Run './ytm setup' to connect your Google account."
        )

    if auth == config.OAUTH_AUTH:
        if not config.OAUTH_CLIENT.exists():
            raise AuthMissing(
                "oauth.json exists but oauth_client.json is missing. Re-run './ytm setup'."
            )
        client = json.loads(config.OAUTH_CLIENT.read_text())
        creds = OAuthCredentials(
            client_id=client["client_id"], client_secret=client["client_secret"]
        )
        return YTMusic(str(auth), oauth_credentials=creds)

    return YTMusic(str(auth))


class Library:
    """All network calls live here. Every method is blocking — the UI runs them
    in worker threads."""

    def __init__(self) -> None:
        self.yt = connect()

    # -- playlists ---------------------------------------------------------
    def playlists(self) -> list[Playlist]:
        out: list[Playlist] = [
            Playlist(id="LM", title="Liked Music", subtitle="your likes", kind="liked"),
            Playlist(id="__library__", title="Library Songs", subtitle="saved songs", kind="library"),
        ]
        try:
            raw = self.yt.get_library_playlists(limit=200)
        except Exception:
            raw = []

        for item in raw:
            pid = item.get("playlistId")
            if not pid or pid == "LM":
                continue
            thumbs = item.get("thumbnails") or []
            count = item.get("count")
            out.append(
                Playlist(
                    id=pid,
                    title=item.get("title") or "Untitled",
                    subtitle=f"{count} tracks" if count else "",
                    thumb=thumbs[-1].get("url", "") if thumbs else "",
                )
            )
        return out

    def tracks(self, playlist: Playlist) -> list[Track]:
        if playlist.kind == "liked":
            raw = (self.yt.get_liked_songs(limit=5000) or {}).get("tracks", [])
        elif playlist.kind == "library":
            raw = self.yt.get_library_songs(limit=5000, order="recently_added")
        else:
            raw = (self.yt.get_playlist(playlist.id, limit=5000) or {}).get("tracks", [])

        tracks = []
        for item in raw or []:
            track = Track.from_item(item)
            if track is not None:
                tracks.append(track)
        return tracks

    # -- search ------------------------------------------------------------
    def search(self, query: str, limit: int = 40) -> list[Track]:
        """Songs and videos, interleaved.

        Plenty of material — covers, remixes, anything a label never uploaded —
        exists only as a video. Searching songs alone hides it, and simply
        appending videos lets songs fill the limit first, so alternate between
        the two lists and let each keep half the room.
        """
        groups: list[list[Track]] = []
        for filt in ("songs", "videos"):
            try:
                results = self.yt.search(query, filter=filt, limit=limit)
            except Exception:
                continue  # one filter failing should not lose the other's hits
            group = []
            for item in results or []:
                track = Track.from_item(item)
                if track is not None:
                    group.append(track)
            groups.append(group)

        tracks: list[Track] = []
        seen: set[str] = set()
        for row in zip_longest(*groups):
            for track in row:
                if track is None or track.video_id in seen:
                    continue
                seen.add(track.video_id)
                tracks.append(track)
        return tracks[:limit]

    # -- alternates --------------------------------------------------------
    def alternates(self, track: Track, limit: int = 5) -> list[Track]:
        """Other uploads of the same song, best match first.

        Used when YouTube refuses one upload: a cover or re-upload of the same
        thing will often play when the original will not. Matching is
        deliberately strict, because playing the wrong song is worse than
        playing nothing.
        """
        query = f"{track.title} {track.artist}".strip()
        if not query:
            return []
        try:
            results = self.search(query, limit=limit * 4)
        except Exception:
            return []

        scored: list[tuple[float, Track]] = []
        for other in results:
            if other.video_id == track.video_id:
                continue
            if not _duration_fits(track.duration, other.duration):
                continue
            score = _title_similarity(track.title, other.title)
            if score < _MIN_TITLE_SIMILARITY:
                continue
            scored.append((score, other))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [other for _, other in scored[:limit]]

    # -- radio / autoplay --------------------------------------------------
    def radio(self, video_id: str, limit: int = 40) -> list[Track]:
        """Tracks YouTube Music would play next after this one."""
        watch = self.yt.get_watch_playlist(videoId=video_id, radio=True, limit=limit)
        tracks = []
        for item in (watch or {}).get("tracks") or []:
            track = Track.from_item(item)
            if track is not None:
                tracks.append(track)
        return tracks
