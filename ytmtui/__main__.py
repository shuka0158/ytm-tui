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

"""Entry point: `python -m ytmtui [setup|verify|logout|license]`."""
from __future__ import annotations

import sys
from pathlib import Path

from . import __version__, config, cookies

NOTICE = f"""ytm-tui {__version__}
Copyright (C) 2026 shuka0158
License GPLv3+: GNU GPL version 3 or later <https://gnu.org/licenses/gpl.html>.
This is free software: you are free to change and redistribute it.
There is NO WARRANTY, to the extent permitted by law."""

USAGE = f"""ytm - YouTube Music in the terminal

  ytm             start the player
  ytm setup       connect a Google account
  ytm cookies     inspect or override the session used for gated tracks
  ytm doctor      check everything playback depends on
  ytm verify      check the stored credentials
  ytm logout      delete the stored credentials
  ytm license     licensing and warranty information
  ytm --help      this text

{NOTICE}
"""


COOKIES_USAGE = f"""ytm cookies - the signed-in session used for gated tracks

Normal tracks play anonymously; YouTube refuses to serve streams to a
cookie-authenticated request. A session is only sent as a second attempt, for
tracks that answer "sign in to confirm your age".

  ytm cookies                 show what would be used for that retry
  ytm cookies auto            reuse the session from `ytm setup` (the default)
  ytm cookies login           open the YouTube sign-in page, then re-run setup
  ytm cookies detect          list browser profiles signed in to YouTube
  ytm cookies <browser>       read cookies from a browser profile instead
  ytm cookies file <path>     read cookies from a Netscape cookies.txt
  ytm cookies off             same as auto; kept for symmetry

<browser> is {', '.join(config.SUPPORTED_BROWSERS)}, optionally with a profile
and container as BROWSER[+KEYRING][:PROFILE][::CONTAINER], e.g. "chrome:Profile 1".
Chromium-based browsers encrypt their cookie store, which needs a running
keyring; Firefox does not.
"""


def _cookies_status() -> int:
    settings = config.load_settings()
    browser = settings.get("cookies_from_browser")
    path = settings.get("cookies_file")

    if browser:
        print(f"Source: browser profile {browser!r} (explicitly configured)")
        return 0
    if path:
        print(f"Source: cookies file {path} (explicitly configured)")
        return 0

    generated = cookies.sync_from_auth()
    if generated:
        print(f"Source: the session saved by `ytm setup`, exported to {generated}")
        return 0

    print("Source: none - requests are anonymous, so gated tracks will not play.")
    if config.auth_file() is None:
        print("Run `ytm setup` to connect an account.")
    else:
        print("The saved credentials carry no login cookie. Re-run `ytm setup`,")
        print("or pick a browser with `ytm cookies detect`.")
    return 0


def _cookies(args: list[str]) -> int:
    if not args:
        return _cookies_status()

    command = args[0]

    if command in ("-h", "--help", "help"):
        print(COOKIES_USAGE)
        return 0

    if command in ("auto", "off"):
        settings = config.load_settings()
        settings["cookies_from_browser"] = None
        settings["cookies_file"] = None
        config.save_settings(settings)
        print("Back to the session from `ytm setup`.")
        return _cookies_status()

    if command == "login":
        if cookies.open_login_page():
            print("Opened the YouTube sign-in page in your browser.")
        else:
            print(f"Could not open a browser. Go to {cookies.LOGIN_URL} yourself.")
        print("Sign in there, then run `ytm setup` to capture the session.")
        return 0

    if command == "detect":
        found = cookies.detect_browsers()
        if not found:
            print("No browser profile on this machine is signed in to YouTube.")
            print("Run `ytm cookies login` to sign in, or use `ytm setup`.")
            return 1
        print("Browser profiles signed in to YouTube:")
        for spec in found:
            print(f"  ytm cookies {spec!r}" if " " in spec else f"  ytm cookies {spec}")
        return 0

    if command == "file":
        if len(args) < 2:
            print("Usage: ytm cookies file <path>")
            return 2
        path = Path(args[1]).expanduser()
        if not path.is_file():
            print(f"No such file: {path}")
            return 1
        settings = config.load_settings()
        settings["cookies_file"] = str(path)
        settings["cookies_from_browser"] = None
        config.save_settings(settings)
        print(f"Using cookies file: {path}")
        return 0

    try:
        config.parse_cookies_from_browser(command)
    except ValueError as exc:
        print(f"{exc}\n")
        print(COOKIES_USAGE)
        return 2
    settings = config.load_settings()
    settings["cookies_from_browser"] = command
    settings["cookies_file"] = None
    config.save_settings(settings)
    print(f"Using cookies from browser: {command}")
    print("That profile must be signed in to YouTube. Check with `ytm cookies detect`.")
    return 0


def _doctor() -> int:
    """One place to look when playback misbehaves."""
    import shutil

    from .resolver import JS_RUNTIME_HINT, JS_RUNTIMES, missing_js_runtime

    ok = True

    if shutil.which("mpv"):
        print("mpv                 found")
    else:
        print("mpv                 MISSING - install it, nothing plays without it")
        ok = False

    runtimes = [name for name in JS_RUNTIMES if shutil.which(name)]
    if missing_js_runtime():
        print(f"JavaScript runtime  MISSING - {JS_RUNTIME_HINT}")
        ok = False
    else:
        print(f"JavaScript runtime  found ({', '.join(runtimes)})")

    auth = config.auth_file()
    if auth:
        print(f"account             {auth.name}")
    else:
        print("account             MISSING - run `ytm setup`")
        ok = False

    settings = config.load_settings()
    browser, path = cookies.resolve_source(settings)
    if browser:
        print(f"gated-track session browser profile {browser}")
    elif path:
        print(f"gated-track session {path}")
    else:
        print("gated-track session none - tracks needing sign-in will not play")

    print("\nAll good." if ok else "\nFix the MISSING lines above.")
    return 0 if ok else 1


def main() -> int:
    args = sys.argv[1:]
    command = args[0] if args else ""

    if command in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    if command in ("license", "version", "--version", "-V"):
        print(NOTICE)
        return 0

    if command == "setup":
        from .auth import wizard
        return wizard()

    if command == "doctor":
        return _doctor()

    if command == "cookies":
        return _cookies(args[1:])

    if command == "verify":
        from .auth import verify
        return verify()

    if command == "logout":
        removed = []
        for path in (
            config.BROWSER_AUTH,
            config.OAUTH_AUTH,
            config.OAUTH_CLIENT,
            config.COOKIES_FILE,  # generated from the above; must go with them
        ):
            if path.exists():
                path.unlink()
                removed.append(path.name)
        print("Removed: " + (", ".join(removed) if removed else "nothing to remove"))
        return 0

    if command:
        print(f"Unknown command: {command}\n")
        print(USAGE)
        return 2

    if config.auth_file() is None:
        print("No account connected yet - starting setup.\n")
        from .auth import wizard
        if wizard() != 0:
            return 1
        print("\nStarting the player…\n")

    from .app import main as run_app
    run_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
