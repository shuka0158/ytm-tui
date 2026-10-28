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
from .models import Collection, Playlist, Track

# YouTube's personalised mixes all share this playlist-id prefix.
_MIX_ID_PREFIX = "RDTMAK5uy_"

# YouTube answers with ~200 mix tracks however few we ask for, and paging
# past that buys nothing anyone will scroll to.
_MIX_TRACK_LIMIT = 100

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


def _artist_names(artists: object) -> str:
    return ", ".join(
        a.get("name", "") for a in (artists or []) if isinstance(a, dict) and a.get("name")
    )


def _collection_from_item(item: dict) -> Collection | None:
    """Turn an album/artist/playlist search hit into a Collection."""
    kind = item.get("resultType")
    browse_id = item.get("browseId")
    if kind not in ("album", "artist", "playlist") or not browse_id:
        return None
    thumbs = item.get("thumbnails") or []
    thumb = thumbs[-1].get("url", "") if thumbs else ""

    if kind == "artist":
        return Collection(
            kind, browse_id, item.get("artist") or item.get("title") or "Unknown artist",
            "", "Artist", thumb,
        )
    if kind == "album":
        detail = " · ".join(
            part for part in (item.get("type") or "Album", item.get("year")) if part
        )
        return Collection(
            kind, browse_id, item.get("title") or "Unknown album",
            _artist_names(item.get("artists")), detail, thumb,
        )
    count = item.get("itemCount")
    detail = f"Playlist · {count} songs" if count else "Playlist"
    return Collection(
        kind, browse_id, item.get("title") or "Untitled",
        item.get("author") or "", detail, thumb,
    )


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
    def mixes(self) -> list[Playlist]:
        """YouTube's own mixes for this account — Supermix, My Mix N, Discover…

        Picked out by playlist id rather than by the home section's heading,
        which comes back in the account's own language.
        """
        try:
            home = self.yt.get_home(limit=10)
        except Exception:
            return []

        out: list[Playlist] = []
        seen: set[str] = set()
        for section in home or []:
            for item in section.get("contents") or []:
                pid = item.get("playlistId") or ""
                if not pid.startswith(_MIX_ID_PREFIX) or pid in seen:
                    continue
                seen.add(pid)
                thumbs = item.get("thumbnails") or []
                out.append(
                    Playlist(
                        id=pid,
                        title=item.get("title") or "Mix",
                        subtitle=item.get("description") or "mix",
                        thumb=thumbs[-1].get("url", "") if thumbs else "",
                        kind="mix",
                    )
                )
        return out

    def playlists(self) -> list[Playlist]:
        out: list[Playlist] = [
            Playlist(id="LM", title="Liked Music", subtitle="your likes", kind="liked"),
            Playlist(id="__library__", title="Library Songs", subtitle="saved songs", kind="library"),
        ]
        out.extend(self.mixes())
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
                    kind="playlist",
                )
            )
        return out

    def tracks(self, playlist: Playlist) -> list[Track]:
        if playlist.kind == "liked":
            raw = (self.yt.get_liked_songs(limit=5000) or {}).get("tracks", [])
        elif playlist.kind == "library":
            raw = self.yt.get_library_songs(limit=5000, order="recently_added")
        else:
            # A mix has no end, so asking for everything just pages forever —
            # 37s and 1400 tracks where the first response already holds 200.
            limit = _MIX_TRACK_LIMIT if playlist.kind == "mix" else 5000
            raw = (self.yt.get_playlist(playlist.id, limit=limit) or {}).get("tracks", [])

        tracks = []
        for item in raw or []:
            track = Track.from_item(item)
            if track is not None:
                tracks.append(track)
        return tracks

    # -- search ------------------------------------------------------------
    def search(
        self, query: str, limit: int = 40, kinds: tuple[str, ...] = ("songs", "videos")
    ) -> list[Track]:
        """Songs and videos, interleaved.

        Plenty of material — covers, remixes, anything a label never uploaded —
        exists only as a video. Searching songs alone hides it, and simply
        appending videos lets songs fill the limit first, so alternate between
        the two lists and let each keep half the room. `kinds` narrows it to one
        of them.
        """
        groups: list[list[Track]] = []
        for filt in kinds:
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

    def search_collections(self, query: str, kind: str, limit: int = 30) -> list[Collection]:
        """Albums, artists or playlists matching a query. `kind` is the plural
        filter name: "albums", "artists" or "playlists"."""
        results = self.yt.search(query, filter=kind, limit=limit)
        out: list[Collection] = []
        for item in results or []:
            collection = _collection_from_item(item)
            if collection is not None:
                out.append(collection)
        return out

    def collection_tracks(self, collection: Collection) -> list[Track]:
        """The tracks inside an album, artist or playlist hit."""
        if collection.kind == "album":
            data = self.yt.get_album(collection.browse_id) or {}
            raw = data.get("tracks") or []
            album_thumbs = data.get("thumbnails") or []
            fallback_thumb = album_thumbs[-1].get("url", "") if album_thumbs else ""
            fallback_artist = _artist_names(data.get("artists"))
            tracks = []
            for item in raw:
                track = Track.from_item(item)
                if track is None:
                    continue
                # Album pages leave these off the individual tracks.
                track.album = track.album or data.get("title") or collection.title
                track.thumb = track.thumb or fallback_thumb
                if track.artist == "Unknown artist" and fallback_artist:
                    track.artist = fallback_artist
                tracks.append(track)
            return tracks

        if collection.kind == "artist":
            data = self.yt.get_artist(collection.browse_id) or {}
            songs = data.get("songs") or {}
            raw = []
            if songs.get("browseId"):
                # The artist page only shows a handful; this is the full list.
                try:
                    raw = (self.yt.get_playlist(songs["browseId"], limit=200) or {}).get("tracks", [])
                except Exception:
                    raw = []
            raw = raw or songs.get("results") or []
        else:
            limit = _MIX_TRACK_LIMIT if collection.browse_id.startswith("RD") else 5000
            raw = (self.yt.get_playlist(collection.browse_id, limit=limit) or {}).get("tracks", [])

        tracks = []
        for item in raw or []:
            track = Track.from_item(item)
            if track is not None:
                tracks.append(track)
        return tracks

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
