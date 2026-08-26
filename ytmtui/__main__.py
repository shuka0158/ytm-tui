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

from . import __version__, config

NOTICE = f"""ytm-tui {__version__}
Copyright (C) 2026 shuka0158
License GPLv3+: GNU GPL version 3 or later <https://gnu.org/licenses/gpl.html>.
This is free software: you are free to change and redistribute it.
There is NO WARRANTY, to the extent permitted by law."""

USAGE = f"""ytm - YouTube Music in the terminal

  ytm             start the player
  ytm setup       connect a Google account
  ytm verify      check the stored credentials
  ytm logout      delete the stored credentials
  ytm license     licensing and warranty information
  ytm --help      this text

{NOTICE}
"""


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

    if command == "verify":
        from .auth import verify
        return verify()

    if command == "logout":
        removed = []
        for path in (config.BROWSER_AUTH, config.OAUTH_AUTH, config.OAUTH_CLIENT):
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
