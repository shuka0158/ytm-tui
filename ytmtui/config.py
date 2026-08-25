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

BROWSER_AUTH = CONFIG_DIR / "browser.json"
OAUTH_AUTH = CONFIG_DIR / "oauth.json"
OAUTH_CLIENT = CONFIG_DIR / "oauth_client.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

DEFAULTS: dict[str, Any] = {
    "volume": 80,
    "shuffle": False,
    "repeat": "off",          # off | all | one
    "last_playlist": None,
    "format": "bestaudio[acodec=opus]/bestaudio/best",
}


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
