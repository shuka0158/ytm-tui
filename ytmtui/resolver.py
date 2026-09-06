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
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
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

# The JS runtime is a subprocess, and yt-dlp fails the whole extraction if it
# writes anything at all to stderr. That is a transient condition, not a
# property of the track, so one immediate retry usually gets it.
RUNTIME_MARKERS = (
    "error running deno process",
    "error running node process",
    "error running bun process",
    "error running quickjs process",
)

# download() never wants a muxed video+audio fallback ("best" would give one
# when a client's format list has no real audio-only entry), unlike the
# streaming format in config.DEFAULTS which can tolerate that since mpv just
# discards the video track.
DOWNLOAD_AUDIO_FORMAT = "bestaudio[acodec=opus]/bestaudio"

BLOCKED_MESSAGE = (
    "age-restricted: YouTube refused this to the player even signed in. "
    "It normally still plays on youtube.com in a browser."
)


def _silence_helper_stderr() -> None:
    """Stop yt-dlp's helper processes from scribbling on the interface.

    Textual draws to stderr, and some yt-dlp spawns - the bgutil PO token
    script, most visibly - inherit ours instead of redirecting it, so a line
    like wgpu's "enumerate_adapters" complaint from deno lands on top of the
    UI. Anything that already asks for a specific stderr keeps it; the rest go
    to a log file we can read afterwards.
    """
    popen = yt_dlp.utils.Popen
    if getattr(popen, "_ytmtui_silenced", False):
        return

    original = popen.__init__

    def __init__(self, *args, **kwargs):
        # stderr is the sixth positional argument; only fill in the gap when
        # the caller left it out entirely.
        if len(args) < 6:
            kwargs.setdefault("stderr", _helper_stderr())
        original(self, *args, **kwargs)

    popen.__init__ = __init__
    popen._ytmtui_silenced = True


_helper_log = None


def _helper_stderr():
    """Append-mode handle on the helper log, or DEVNULL if it will not open."""
    global _helper_log
    if _helper_log is None:
        try:
            _helper_log = open(config.SUBPROCESS_LOG, "a", buffering=1)  # noqa: SIM115
        except OSError:
            _helper_log = subprocess.DEVNULL
    return _helper_log


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
        _silence_helper_stderr()
        self._opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "format": fmt or config.DEFAULTS["format"],
            "cachedir": str(config.YTDLP_CACHE),
            "js_runtimes": JS_RUNTIMES,
            "no_color": True,
            # The web client needs a JS engine to solve YouTube's signature
            # cipher, which costs several seconds per track. The android
            # client ships pre-signed URLs and skips that entirely - about
            # 3x faster on a cold resolve. _fallback_opts (plain web, built
            # lazily) covers the rare video android can't serve.
            "extractor_args": {"youtube": {"player_client": ["android"]}},
            # yt-dlp now solves the web client's signature/n challenges with a
            # downloadable script rather than bundled code. Without this, the
            # web client - which is what the signed-in retry uses - resolves
            # no formats at all and looks identical to a hard block. Cached
            # after the first fetch, so this costs nothing on later resolves.
            "remote_components": ["ejs:github"],
        }
        self._fallback_opts = {k: v for k, v in self._opts.items() if k != "extractor_args"}
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

        # A fresh YoutubeDL() per resolve throws away its connection pool and
        # re-runs extractor init every time; reusing one instance per option
        # set turns a warm resolve into ~1-2s instead of ~5s. Concurrent
        # extract_info() calls on a shared instance are not officially
        # documented as safe, but hammer-tested fine here - yt-dlp's HTTP
        # layer pools per-thread and extract_info keeps no call-scoped state
        # of its own.
        self._ydl_lock = threading.Lock()
        self._ydl_cache: dict[int, yt_dlp.YoutubeDL] = {}

    def _ydl_for(self, opts: dict) -> yt_dlp.YoutubeDL:
        key = id(opts)
        with self._ydl_lock:
            ydl = self._ydl_cache.get(key)
            if ydl is None:
                ydl = yt_dlp.YoutubeDL(opts)
                self._ydl_cache[key] = ydl
            return ydl

    def _build_auth_opts(
        self, cookies_from_browser: str | None, cookies_file: str | None
    ) -> dict | None:
        """The fallback (web) options plus a cookie source, or None with no session.

        Built on _fallback_opts rather than _opts: cookies are a web-session
        concept, and the android client we default to for speed ignores them
        entirely, so an age-gated retry on android opts sends the session to a
        client that ignores it and fails exactly the same way as an anonymous
        request. Only the web client actually consumes the signed-in cookies.
        """
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
        return {**self._fallback_opts, **source}

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
        ydl = self._ydl_for(opts)
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
            if any(marker in _clean(str(exc)).lower() for marker in RUNTIME_MARKERS):
                try:
                    info = self._extract(watch_url, self._opts)
                except Exception as retry_exc:
                    exc = retry_exc
                else:
                    return self._cache_stream(video_id, info)

            lowered = _clean(str(exc)).lower()
            if not any(m in lowered for m in SIGNIN_MARKERS + BLOCKED_MARKERS):
                # Not a sign-in or age gate - likely just a gap in the fast
                # android client's format list. Fall back to the slower but
                # more complete web client once before giving up.
                try:
                    info = self._extract(watch_url, self._fallback_opts)
                except Exception as fallback_exc:
                    exc = fallback_exc
                else:
                    return self._cache_stream(video_id, info)

            try:
                info = self._retry_signed_in(watch_url, exc)
            except TrackBlocked as blocked_exc:
                self._remember_blocked(video_id, blocked_exc)
                raise

        return self._cache_stream(video_id, info)

    def _cache_stream(self, video_id: str, info: dict) -> tuple[str, dict[str, str]]:
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

    def download(
        self,
        video_id: str,
        dest_dir: Path,
        filename_base: str,
        on_progress: Callable[[float | None, float | None], None] | None = None,
    ) -> str:
        """Download the best audio for `video_id` into `dest_dir`. Returns the
        saved file's path.

        Blocking - run on a worker thread. Reuses resolve()'s sign-in / age-gate
        ladder (android client first, web-client fallback, then a signed-in
        retry) but with skip_download turned off, since none of that logic
        cares whether the extraction actually downloads or not.

        `on_progress`, if given, is called from this same thread as
        (percent complete or None if unknown, bytes/sec or None).
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        watch_url = f"https://music.youtube.com/watch?v={video_id}"
        outtmpl = str(dest_dir / f"{filename_base}.%(ext)s")

        def _hook(d: dict) -> None:
            if on_progress is None:
                return
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes") or 0
                percent = (downloaded / total * 100) if total else None
                on_progress(percent, d.get("speed"))
            elif status == "finished":
                on_progress(100.0, None)

        def _run(base_opts: dict) -> dict:
            opts = {
                **base_opts,
                # The configured streaming format ends in "/best", which for
                # some videos the android client only serves as a muxed
                # video+audio file - fine for mpv (it just ignores the video
                # track), but wrong for a saved file. Force audio-only here,
                # even if that means falling through to a client with a
                # fuller format list instead of silently saving a video.
                "format": DOWNLOAD_AUDIO_FORMAT,
                "skip_download": False,
                "outtmpl": outtmpl,
                "overwrites": True,
                "progress_hooks": [_hook],
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(watch_url, download=True)
            if not info:
                raise ResolveError("yt-dlp returned nothing for this track")
            return info

        try:
            info = _run(self._opts)
        except Exception as exc:
            lowered = _clean(str(exc)).lower()
            if any(m in lowered for m in BLOCKED_MARKERS):
                raise TrackBlocked(BLOCKED_MESSAGE) from exc
            if any(m in lowered for m in SIGNIN_MARKERS):
                if not self.has_session:
                    raise TrackBlocked(
                        "this track needs a signed-in session - run `ytm setup`, "
                        "or see `ytm cookies`"
                    ) from exc
                try:
                    info = _run(self._auth_opts)
                except Exception as retry_exc:
                    retry_text = _clean(str(retry_exc)).lower()
                    if any(m in retry_text for m in BLOCKED_MARKERS):
                        raise TrackBlocked(BLOCKED_MESSAGE) from retry_exc
                    raise ResolveError(_last_line(str(retry_exc))) from retry_exc
            else:
                # Likely just a gap in the fast android client's format list.
                try:
                    info = _run(self._fallback_opts)
                except Exception as fallback_exc:
                    raise ResolveError(_last_line(str(fallback_exc))) from fallback_exc

        path = info.get("filepath")
        if not path:
            requested = info.get("requested_downloads") or []
            if requested:
                path = requested[0].get("filepath")
        if not path:
            with yt_dlp.YoutubeDL({**self._opts, "outtmpl": outtmpl}) as ydl:
                path = ydl.prepare_filename(info)
        return path

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
        with self._ydl_lock:
            for ydl in self._ydl_cache.values():
                try:
                    ydl.close()
                except Exception:
                    pass
            self._ydl_cache.clear()
