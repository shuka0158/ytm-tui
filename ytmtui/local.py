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

"""On-disk audio: the "Local" and "Downloads" sections both browse a set of
folders the same way, they just point at different directories."""
from __future__ import annotations

import re
from pathlib import Path

from .models import Track

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".webm",
}
# Cover art saved alongside a track (see resolver.download) as "<stem>.jpg";
# checked in this order for whichever an audio file happens to sit next to.
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

_INVALID = re.compile(r'[\\/:*?"<>|]+')


def sanitize_filename(name: str) -> str:
    """Strip characters no filesystem likes, for a downloaded track's name."""
    name = _INVALID.sub("_", name).strip(" .")
    return name or "track"


def _cover_for(path: Path) -> str:
    """A same-named image file next to `path`, if there is one."""
    for ext in IMAGE_EXTENSIONS:
        candidate = path.with_suffix(ext)
        if candidate.is_file():
            return str(candidate)
    return ""


def scan(directories: list[Path]) -> list[Track]:
    """Recursively find audio files under `directories`.

    Each file becomes a Track with its containing folder (relative to the
    scanned root) as the "album" column, so the list doubles as a browser of
    where things live on disk. video_id is a synthetic "local:<path>" id —
    unique, and enough for the UI to mark the currently-playing row.
    """
    tracks: list[Track] = []
    seen: set[str] = set()
    for base in directories:
        base = Path(base).expanduser()
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)

            try:
                folder = str(path.parent.relative_to(base))
            except ValueError:
                folder = str(path.parent)
            if folder == ".":
                folder = base.name

            tracks.append(
                Track(
                    video_id=f"local:{key}",
                    title=path.stem,
                    artist="Local file",
                    album=folder,
                    duration=0,
                    thumb=_cover_for(path),
                    local_path=key,
                )
            )
    return tracks
