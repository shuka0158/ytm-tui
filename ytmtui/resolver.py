"""Turns a YouTube video id into a direct audio stream URL via yt-dlp,
with an expiry-aware cache and background prefetching of the next track."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import yt_dlp

from . import config

# googlevideo URLs are signed and time-limited; keep our own margin.
FALLBACK_TTL = 3 * 3600
EXPIRY_MARGIN = 300


class ResolveError(RuntimeError):
    pass


def _expiry_of(url: str) -> float:
    try:
        expire = parse_qs(urlparse(url).query).get("expire", [None])[0]
        if expire:
            return float(expire)
    except (ValueError, TypeError):
        pass
    return time.time() + FALLBACK_TTL


class Resolver:
    def __init__(self, fmt: str | None = None) -> None:
        config.ensure_dirs()
        self._opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "format": fmt or config.DEFAULTS["format"],
            "cachedir": str(config.YTDLP_CACHE),
        }
        self._cache: dict[str, tuple[str, dict[str, str], float]] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="resolve")
        self._inflight: set[str] = set()

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

    def resolve(self, video_id: str) -> tuple[str, dict[str, str]]:
        """Blocking. Returns (stream_url, http_headers)."""
        hit = self._lookup(video_id)
        if hit is not None:
            return hit

        watch_url = f"https://music.youtube.com/watch?v={video_id}"
        try:
            with yt_dlp.YoutubeDL(self._opts) as ydl:
                info = ydl.extract_info(watch_url, download=False)
        except Exception as exc:  # yt-dlp raises a zoo of exception types
            raise ResolveError(str(exc).strip().splitlines()[-1] if str(exc) else "yt-dlp failed") from exc

        url = info.get("url")
        headers = info.get("http_headers") or {}
        if not url:
            requested = info.get("requested_formats") or []
            if requested:
                url = requested[0].get("url")
                headers = requested[0].get("http_headers") or headers
        if not url:
            raise ResolveError("yt-dlp returned no playable audio stream")

        with self._lock:
            self._cache[video_id] = (url, headers, _expiry_of(url))
        return url, headers

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
