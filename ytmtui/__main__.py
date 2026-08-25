"""Entry point: `python -m ytmtui [setup|verify|logout]`."""
from __future__ import annotations

import sys

from . import config

USAGE = """ytm - YouTube Music in the terminal

  ytm             start the player
  ytm setup       connect a Google account
  ytm verify      check the stored credentials
  ytm logout      delete the stored credentials
  ytm --help      this text
"""


def main() -> int:
    args = sys.argv[1:]
    command = args[0] if args else ""

    if command in ("-h", "--help", "help"):
        print(USAGE)
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
