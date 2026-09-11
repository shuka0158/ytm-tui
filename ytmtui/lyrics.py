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

"""Lyrics: look a track up on Genius and scrape the lyrics off its page.

Genius' public search endpoint (the one genius.com's own search box calls)
needs no API key, and the lyrics themselves are server-rendered straight into
the song page inside one or more `data-lyrics-container` divs - no login,
no token, just two plain HTTP requests. Results are cached on disk since the
text never changes once a song is published.
"""
from __future__ import annotations

import hashlib
import html
import re
import threading

import requests

from . import config

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
_SEARCH_URL = "https://genius.com/api/search/multi"
_lock = threading.Lock()

_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_CONTAINER_RE = re.compile(r'<div[^>]*data-lyrics-container="true"[^>]*>')
# Inside the container, Genius also nests a credits/translations header (and
# occasionally other asides) marked like this - not part of the lyrics.
_EXCLUDED_DIV_RE = re.compile(r'<div\b[^>]*data-exclude-from-selection="true"[^>]*>')
_DIV_TAG_RE = re.compile(r"<div\b[^>]*>|</div>")


def _strip_balanced(text: str, opener_re: "re.Pattern[str]") -> str:
    """Remove each <div ...> matched by opener_re along with its own </div>."""
    out: list[str] = []
    pos = 0
    for match in opener_re.finditer(text):
        if match.start() < pos:
            continue  # inside a block we already dropped
        out.append(text[pos:match.start()])
        depth = 1
        end = len(text)
        for tag in _DIV_TAG_RE.finditer(text, match.end()):
            depth += -1 if tag.group().startswith("</div") else 1
            if depth == 0:
                end = tag.end()
                break
        pos = end
    out.append(text[pos:])
    return "".join(out)


# Collapse the odd triple-blank-line gap Genius leaves between sections.
# [Verse 1], [Chorus] etc. section headers are kept - they read fine as-is.
_WS_RE = re.compile(r"\n{3,}")


class LyricsError(Exception):
    """No lyrics could be found or fetched."""


def _cache_path(artist: str, title: str):
    key = hashlib.sha1(f"{artist}\x00{title}".lower().encode()).hexdigest()
    return config.LYRICS_DIR / f"{key}.txt"


def _clean_query(text: str) -> str:
    # Strip parentheticals like "(Official Video)" / "[Lyrics]" and a
    # trailing "feat. X" - they hurt the search more than they help it.
    text = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", text)
    text = re.sub(r"\bfeat\.?.*$", "", text, flags=re.I)
    return " ".join(text.split())


def _find_song_url(artist: str, title: str) -> tuple[str, str] | None:
    """Best-matching Genius song URL and display title, or None."""
    query = _clean_query(f"{artist} {title}").strip() or title
    try:
        response = requests.get(
            _SEARCH_URL, params={"q": query}, headers=_HEADERS, timeout=10
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return None

    hits = []
    for section in data.get("response", {}).get("sections", []):
        if section.get("type") == "song":
            hits = section.get("hits", [])
            break

    if not hits:
        return None

    artist_lower = artist.lower()
    best = None
    for hit in hits:
        result = hit.get("result") or {}
        url = result.get("url")
        if not url:
            continue
        if best is None:
            best = result
        primary = ((result.get("primary_artist") or {}).get("name") or "").lower()
        if primary and (primary in artist_lower or artist_lower in primary):
            best = result
            break

    if best is None:
        return None
    display = f"{(best.get('primary_artist') or {}).get('name', artist)} - {best.get('title', title)}"
    return best["url"], display


def _extract_lyrics(page_html: str) -> str | None:
    """Pull the text out of every data-lyrics-container div on the page."""
    chunks: list[str] = []
    for match in _CONTAINER_RE.finditer(page_html):
        start = match.end()
        depth = 1
        pos = start
        # Walk forward counting nested <div ...> / </div> tags to find the
        # matching close, since containers can nest a translation div etc.
        for tag in re.finditer(r"<div\b[^>]*>|</div>", page_html[start:]):
            if tag.group().startswith("</div"):
                depth -= 1
            else:
                depth += 1
            if depth == 0:
                pos = start + tag.start()
                break
        else:
            continue
        inner = page_html[start:pos]
        inner = _strip_balanced(inner, _EXCLUDED_DIV_RE)
        inner = _BR_RE.sub("\n", inner)
        inner = _TAG_RE.sub("", inner)
        chunks.append(html.unescape(inner))

    if not chunks:
        return None
    text = "\n".join(chunks).strip()
    text = _WS_RE.sub("\n\n", text)
    return text or None


def fetch(artist: str, title: str) -> tuple[str, str]:
    """Lyrics text and the matched "Artist - Title" credit line.

    Raises LyricsError if nothing could be found. Blocking - call off the UI
    thread.
    """
    cache_file = _cache_path(artist, title)
    if cache_file.exists():
        try:
            cached = cache_file.read_text(encoding="utf-8")
            heading, _, body = cached.partition("\n\n")
            if heading.startswith("# ") and body:
                return body, heading[2:]
        except OSError:
            pass

    found = _find_song_url(artist, title)
    if found is None:
        raise LyricsError(f"No Genius match for “{title}”.")
    url, display = found

    try:
        response = requests.get(url, headers=_HEADERS, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise LyricsError(f"Could not reach Genius: {exc}") from exc

    text = _extract_lyrics(response.text)
    if not text:
        raise LyricsError(f"Genius page for “{display}” had no lyrics container.")

    with _lock:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".part")
        tmp.write_text(f"# {display}\n\n{text}", encoding="utf-8")
        tmp.replace(cache_file)

    return text, display
