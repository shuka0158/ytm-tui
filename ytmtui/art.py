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

"""Album art: fetch a thumbnail once, keep it on disk, hand back a path."""
from __future__ import annotations

import hashlib
import re
import threading

import requests

from . import config

_SIZE_RE = re.compile(r"=w\d+-h\d+")
_lock = threading.Lock()


def upscale(url: str, size: int = 544) -> str:
    """YouTube thumbnail URLs carry their dimensions in the query suffix, so we
    can ask for a bigger version of the same image for free."""
    if not url:
        return url
    if _SIZE_RE.search(url):
        return _SIZE_RE.sub(f"=w{size}-h{size}", url)
    return url


def fetch(url: str, size: int = 544) -> str | None:
    """Blocking download into the cache. Returns a local path, or None."""
    if not url:
        return None
    config.ensure_dirs()
    url = upscale(url, size)
    name = hashlib.sha1(url.encode()).hexdigest() + ".jpg"
    path = config.ART_DIR / name
    if path.exists() and path.stat().st_size > 0:
        return str(path)

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.content
    except requests.RequestException:
        return None
    if not data:
        return None

    with _lock:
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    return str(path)
