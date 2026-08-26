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

"""Paths and persisted settings. Everything lives under the project root."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
CACHE_DIR = ROOT / "cache"
ART_DIR = CACHE_DIR / "art"
YTDLP_CACHE = CACHE_DIR / "ytdlp"
SUBPROCESS_LOG = CACHE_DIR / "subprocess.log"

BROWSER_AUTH = CONFIG_DIR / "browser.json"
OAUTH_AUTH = CONFIG_DIR / "oauth.json"
OAUTH_CLIENT = CONFIG_DIR / "oauth_client.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
# Generated from the credentials above; see ytmtui/cookies.py.
COOKIES_FILE = CONFIG_DIR / "cookies.txt"

DEFAULTS: dict[str, Any] = {
    "volume": 80,
    "shuffle": False,
    "repeat": "off",          # off | all | one
    "last_playlist": None,
    "format": "bestaudio[acodec=opus]/bestaudio/best",
    # Sign-in gated tracks need a logged-in session. Left null, we reuse the
    # one `ytm setup` already stored. Override with a browser profile
    # ("firefox", "chrome:Profile 1", ...) or a Netscape cookies.txt path.
    "cookies_from_browser": None,
    "cookies_file": None,
}

# yt-dlp accepts BROWSER[+KEYRING][:PROFILE][::CONTAINER] as one string.
SUPPORTED_BROWSERS = (
    "brave", "chrome", "chromium", "edge", "firefox",
    "opera", "safari", "vivaldi", "whale",
)


def parse_cookies_from_browser(spec: str) -> tuple[str, str | None, str | None, str | None]:
    """Turn a yt-dlp --cookies-from-browser string into its opts tuple.

    Raises ValueError if the browser name is not one yt-dlp knows.
    """
    rest, _, container = spec.partition("::")
    name, _, profile = rest.partition(":")
    browser, _, keyring = name.partition("+")
    browser = browser.strip().lower()
    if browser not in SUPPORTED_BROWSERS:
        raise ValueError(
            f"unknown browser {browser!r}; pick one of {', '.join(SUPPORTED_BROWSERS)}"
        )
    return (
        browser,
        profile.strip() or None,
        keyring.strip().upper() or None,
        container.strip() or None,
    )


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, CACHE_DIR, ART_DIR, YTDLP_CACHE):
        d.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict[str, Any]:
    settings = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            settings.update(json.loads(SETTINGS_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    ensure_dirs()
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except OSError:
        pass


def auth_file() -> Path | None:
    """Whichever credential file is present, OAuth taking priority."""
    if OAUTH_AUTH.exists():
        return OAUTH_AUTH
    if BROWSER_AUTH.exists():
        return BROWSER_AUTH
    return None
