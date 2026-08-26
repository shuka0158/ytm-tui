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

"""Interactive account setup, run outside the TUI."""
from __future__ import annotations

import json
import sys

from ytmusicapi import setup as setup_browser_headers
from ytmusicapi import setup_oauth

from . import config

BROWSER_STEPS = """
  1. Open  https://music.youtube.com  and sign in to the account whose playlists
     you want.  TIP: use an Incognito/Private window — see the note at the end.
  2. Press F12 to open DevTools, go to the  Network  tab.
  3. Type  browse  into the filter box, then click anything in the page so a
     request appears. Click one POST request to music.youtube.com.
  4. Copy its request headers as raw text:
       Firefox  – right-click the request > Copy Value > Copy Request Headers
       Chrome   – Headers tab > Request Headers > "Raw" toggle > select+copy
  5. Paste the whole block below, then press Ctrl-D on an empty line.

  These cookies survive reboots and closing your browser. To ALSO survive a
  browser logout, do steps 1-4 in an Incognito window and then just CLOSE it
  (do NOT click "sign out"): that leaves the session alive for this player while
  keeping it independent of your everyday browsing.
"""

OAUTH_STEPS = """
  1. Go to  https://console.cloud.google.com/  and create (or pick) a project.
  2. APIs & Services > Library > enable  "YouTube Data API v3".
  3. APIs & Services > OAuth consent screen > External, fill in app name + email,
     then PUBLISH the app (set publishing status to "In production").
     Skipping this makes your login expire after 7 days. Ignore the
     "unverified app" warning; as the owner you can still use it.
  4. APIs & Services > Credentials > Create Credentials > OAuth client ID.
     Application type must be  "TVs and Limited Input devices".
  5. Copy the client ID and client secret and paste them below.
"""


def _read_block(prompt: str) -> str:
    print(prompt)
    return sys.stdin.read()


def setup_browser() -> int:
    config.ensure_dirs()
    print("\n=== Browser-header sign-in ===")
    print(BROWSER_STEPS)
    raw = _read_block("Paste request headers now:\n")
    if not raw.strip():
        print("\nNothing pasted. Aborted.")
        return 1
    try:
        setup_browser_headers(filepath=str(config.BROWSER_AUTH), headers_raw=raw)
    except Exception as exc:
        print(f"\nSetup failed: {exc}")
        print("Make sure you copied the *request* headers, cookies included.")
        return 1
    if config.OAUTH_AUTH.exists():
        config.OAUTH_AUTH.unlink()
    print(f"\nSaved credentials to {config.BROWSER_AUTH}")
    return 0


def setup_oauth_flow() -> int:
    config.ensure_dirs()
    print("\n=== OAuth sign-in ===")
    print(OAUTH_STEPS)
    client_id = input("Client ID: ").strip()
    client_secret = input("Client secret: ").strip()
    if not client_id or not client_secret:
        print("\nBoth values are required. Aborted.")
        return 1
    try:
        setup_oauth(
            client_id=client_id,
            client_secret=client_secret,
            filepath=str(config.OAUTH_AUTH),
            open_browser=False,
        )
    except KeyboardInterrupt:
        print("\nAborted.")
        return 1
    except Exception as exc:
        print(f"\nSetup failed: {exc}")
        return 1
    config.OAUTH_CLIENT.write_text(
        json.dumps({"client_id": client_id, "client_secret": client_secret}, indent=2)
    )
    if config.BROWSER_AUTH.exists():
        config.BROWSER_AUTH.unlink()
    print(f"\nSaved credentials to {config.OAUTH_AUTH}")
    return 0


def verify() -> int:
    from .api import Library

    try:
        library = Library()
        playlists = library.playlists()
    except Exception as exc:
        print(f"Connection failed: {exc}")
        return 1
    print(f"Connected. {len(playlists)} playlists visible:")
    for playlist in playlists[:15]:
        print(f"  - {playlist.title}  ({playlist.subtitle or playlist.id})")
    if len(playlists) > 15:
        print(f"  … and {len(playlists) - 15} more")
    return 0


def wizard() -> int:
    print("How do you want to connect your YouTube account?\n")
    print("  1) Browser headers  - the only method that works (recommended)")
    print("  2) OAuth            - BROKEN: Google 400s device-OAuth on every call. Don't.")
    choice = input("\nChoice [1]: ").strip() or "1"
    if choice == "1":
        code = setup_browser()
    elif choice == "2":
        code = setup_oauth_flow()
    else:
        print("Unknown choice.")
        return 1
    if code == 0:
        print("\nVerifying…")
        code = verify()
    return code
