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

"""Turns a YouTube video id into a direct audio stream URL via yt-dlp,
with an expiry-aware cache and background prefetching of the next track.

Cookies are a per-track fallback, never the default. YouTube serves anonymous
requests happily but answers cookie-authenticated ones with storyboards and
"The page needs to be reloaded", so sending a session on every track would break
the tracks that currently work. Instead we resolve anonymously and only retry
with the signed-in session when YouTube specifically asks us to sign in.
"""
from __future__ import annotations

import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp

from . import config

# googlevideo URLs are signed and time-limited; keep our own margin.
FALLBACK_TTL = 3 * 3600
EXPIRY_MARGIN = 300

# YouTube's stream URLs are signed by player JavaScript, so yt-dlp needs a JS
# engine to solve them. Only deno is enabled by default; accept anything we can
# get, highest priority first, or formats silently go missing.
JS_RUNTIMES = {"deno": {}, "node": {}, "quickjs": {}, "bun": {}}

# yt-dlp phrasings that mean "this needs a signed-in session", worth a retry.
SIGNIN_MARKERS = (
    "sign in to confirm your age",
    "sign in to confirm you",
    "this video is available to this channel",
    "members-only",
    "private video",
    "login required",
)

# ... and the ones a session does not fix. YouTube refuses these to yt-dlp even
# with a signed-in, age-verified account, because it wants the attestation a
# real browser produces. Such a track usually still plays on youtube.com.
BLOCKED_MARKERS = (
    "age-verification",
    "content is age-restricted",
)

BLOCKED_MESSAGE = (
    "age-restricted: YouTube refused this to the player even signed in. "
    "It normally still plays on youtube.com in a browser."
)


class ResolveError(RuntimeError):
    pass


class TrackBlocked(ResolveError):
    """YouTube refuses this particular upload.

    Nothing is wrong with the track itself, so a different upload of the same
    song stands a good chance of playing.
    """


def missing_js_runtime() -> bool:
    """True when no JS engine is installed, which costs us formats."""
    return not any(shutil.which(name) for name in JS_RUNTIMES)


JS_RUNTIME_HINT = (
    "No JavaScript runtime found (" + ", ".join(JS_RUNTIMES) + "). YouTube signs "
    "its stream URLs with player JS, so some tracks will fail. Install one, "
    "e.g. `sudo apt install nodejs`."
)


# yt-dlp colours its errors, and those escapes render as literal junk in a
# Textual notification.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _clean(text: str) -> str:
    return _ANSI.sub("", text).strip()


# "ERROR: [youtube] dQw4w9WgXcQ: Video unavailable" -> "Video unavailable".
# The user is looking at the track name already.
_PREFIX = re.compile(r"^ERROR:\s*(?:\[[^\]]+\]\s*)?(?:[\w-]{11}:\s*)?")


def _last_line(text: str) -> str:
    cleaned = _clean(text)
    if not cleaned:
        return "yt-dlp failed"
    return _PREFIX.sub("", cleaned.splitlines()[-1]).strip() or "yt-dlp failed"


def _expiry_of(url: str) -> float:
    try:
        expire = parse_qs(urlparse(url).query).get("expire", [None])[0]
        if expire:
            return float(expire)
    except (ValueError, TypeError):
        pass
    return time.time() + FALLBACK_TTL


class Resolver:
    def __init__(
        self,
        fmt: str | None = None,
        cookies_from_browser: str | None = None,
        cookies_file: str | None = None,
    ) -> None:
        config.ensure_dirs()
        self._opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "format": fmt or config.DEFAULTS["format"],
            "cachedir": str(config.YTDLP_CACHE),
            "js_runtimes": JS_RUNTIMES,
            "no_color": True,
        }
        self.cookie_warning: str | None = None
        self._auth_opts: dict | None = self._build_auth_opts(
            cookies_from_browser, cookies_file
        )
        self._cache: dict[str, tuple[str, dict[str, str], float]] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="resolve")
        self._inflight: set[str] = set()
        # Finding out a track is blocked costs two round trips, and the answer
        # does not change within a session. Remember it.
        self._blocked: dict[str, str] = {}

    def _build_auth_opts(
        self, cookies_from_browser: str | None, cookies_file: str | None
    ) -> dict | None:
        """The same options plus a cookie source, or None if we have no session."""
        if cookies_from_browser:
            try:
                source = {
                    "cookiesfrombrowser": config.parse_cookies_from_browser(
                        cookies_from_browser
                    )
                }
            except ValueError as exc:
                self.cookie_warning = f"cookies ignored: {exc}"
                return None
        elif cookies_file:
            path = Path(cookies_file).expanduser()
            if not path.is_file():
                self.cookie_warning = f"cookies file not found: {path}"
                return None
            source = {"cookiefile": str(path)}
        else:
            return None
        return {**self._opts, **source}

    @property
    def has_session(self) -> bool:
        return self._auth_opts is not None

    def _lookup(self, video_id: str) -> tuple[str, dict[str, str]] | None:
        with self._lock:
            hit = self._cache.get(video_id)
        if hit is None:
            return None
        url, headers, expiry = hit
        if expiry - EXPIRY_MARGIN <= time.time():
            with self._lock:
                self._cache.pop(video_id, None)
            return None
        return url, headers

    def _extract(self, watch_url: str, opts: dict) -> dict:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(watch_url, download=False)
        if not info:
            # yt-dlp returns None rather than raising for a few skip paths.
            raise ResolveError("yt-dlp returned nothing for this track")
        return info

    @staticmethod
    def _stream_of(info: dict) -> tuple[str | None, dict[str, str]]:
        url = info.get("url")
        headers = info.get("http_headers") or {}
        if not url:
            requested = info.get("requested_formats") or []
            if requested:
                url = requested[0].get("url")
                headers = requested[0].get("http_headers") or headers
        return url, headers

    def resolve(self, video_id: str) -> tuple[str, dict[str, str]]:
        """Blocking. Returns (stream_url, http_headers)."""
        hit = self._lookup(video_id)
        if hit is not None:
            return hit

        with self._lock:
            blocked = self._blocked.get(video_id)
        if blocked is not None:
            raise TrackBlocked(blocked)

        watch_url = f"https://music.youtube.com/watch?v={video_id}"
        try:
            info = self._extract(watch_url, self._opts)
        except TrackBlocked as exc:
            self._remember_blocked(video_id, exc)
            raise
        except Exception as exc:
            try:
                info = self._retry_signed_in(watch_url, exc)
            except TrackBlocked as blocked_exc:
                self._remember_blocked(video_id, blocked_exc)
                raise

        url, headers = self._stream_of(info)
        if not url:
            raise ResolveError("yt-dlp returned no playable audio stream")

        with self._lock:
            self._cache[video_id] = (url, headers, _expiry_of(url))
        return url, headers

    def _remember_blocked(self, video_id: str, exc: TrackBlocked) -> None:
        with self._lock:
            self._blocked[video_id] = str(exc)

    def _retry_signed_in(self, watch_url: str, exc: Exception) -> dict:
        """Second attempt with cookies, for the errors a session can actually fix.

        Always raises ResolveError unless the retry succeeds.
        """
        text = _clean(str(exc))
        lowered = text.lower()

        if any(marker in lowered for marker in BLOCKED_MARKERS):
            raise TrackBlocked(BLOCKED_MESSAGE) from exc

        needs_session = any(marker in lowered for marker in SIGNIN_MARKERS)
        if not needs_session:
            raise ResolveError(_last_line(text)) from exc

        if not self.has_session:
            raise TrackBlocked(
                "this track needs a signed-in session - run `ytm setup`, "
                "or see `ytm cookies`"
            ) from exc

        try:
            return self._extract(watch_url, self._auth_opts)
        except Exception as retry_exc:
            retry_text = _clean(str(retry_exc)).lower()
            if any(marker in retry_text for marker in BLOCKED_MARKERS):
                raise TrackBlocked(BLOCKED_MESSAGE) from retry_exc
            raise TrackBlocked(
                f"{_last_line(text)} (retry with your saved session failed too)"
            ) from retry_exc

    def prefetch(self, video_id: str) -> None:
        """Warm the cache for a track we are about to need. Never raises."""
        if not video_id or self._lookup(video_id) is not None:
            return
        with self._lock:
            if video_id in self._inflight:
                return
            self._inflight.add(video_id)

        def run() -> None:
            try:
                self.resolve(video_id)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._inflight.discard(video_id)

        self._pool.submit(run)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
