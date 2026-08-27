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

"""The theme list.

Most of these are Textual's own; the two high-contrast ones and the terminal
palette are defined here. The stylesheet only ever names design tokens
($surface, $accent, ...), so a theme swap needs no CSS of its own.
"""
from __future__ import annotations

from textual.theme import BUILTIN_THEMES, Theme

# Pure black behind white text, and only colours that stay legible against it:
# every accent here clears 7:1 on #000000, which is WCAG AAA for body text.
HIGH_CONTRAST = Theme(
    name="high-contrast",
    primary="#00E5FF",
    secondary="#FFD400",
    accent="#FF7AF5",
    warning="#FFB000",
    error="#FF5F5F",
    success="#4BFF88",
    foreground="#FFFFFF",
    background="#000000",
    surface="#000000",
    panel="#141414",
    dark=True,
    # No dimming: the point of this theme is that nothing is dimmed.
    text_alpha=1.0,
    luminosity_spread=0.3,
    variables={
        "block-cursor-text-style": "bold",
        "footer-key-foreground": "#00E5FF",
        "input-selection-background": "#00E5FF 35%",
    },
)

# The same idea inverted, for daylight and for e-ink-ish terminals.
HIGH_CONTRAST_LIGHT = Theme(
    name="high-contrast-light",
    primary="#0000C4",
    secondary="#7A0071",
    accent="#A30000",
    warning="#6B4A00",
    error="#B00000",
    success="#005C24",
    foreground="#000000",
    background="#FFFFFF",
    surface="#FFFFFF",
    panel="#E4E4E4",
    dark=False,
    text_alpha=1.0,
    luminosity_spread=0.3,
    variables={
        "block-cursor-text-style": "bold",
        "footer-key-foreground": "#0000C4",
        "input-selection-background": "#0000C4 30%",
    },
)

CUSTOM_THEMES = (HIGH_CONTRAST, HIGH_CONTRAST_LIGHT)

# Ordered as the picker shows them: dark first, since that is what a music
# player at midnight is for. "terminal" is Textual's ansi theme, which paints
# with the 16 colours the user's own terminal is configured with.
THEME_ORDER = (
    "tokyo-night",
    "catppuccin-mocha",
    "gruvbox",
    "nord",
    "dracula",
    "monokai",
    "rose-pine",
    "solarized-dark",
    "textual-dark",
    "high-contrast",
    "ansi-dark",
    "catppuccin-latte",
    "solarized-light",
    "textual-light",
    "high-contrast-light",
    "ansi-light",
)

LABELS = {
    "tokyo-night": "Tokyo Night",
    "catppuccin-mocha": "Catppuccin Mocha",
    "gruvbox": "Gruvbox",
    "nord": "Nord",
    "dracula": "Dracula",
    "monokai": "Monokai",
    "rose-pine": "Rosé Pine",
    "solarized-dark": "Solarized Dark",
    "textual-dark": "Textual Dark",
    "high-contrast": "High Contrast",
    "ansi-dark": "Terminal (dark ANSI)",
    "catppuccin-latte": "Catppuccin Latte",
    "solarized-light": "Solarized Light",
    "textual-light": "Textual Light",
    "high-contrast-light": "High Contrast Light",
    "ansi-light": "Terminal (light ANSI)",
}

DEFAULT_THEME = "tokyo-night"


def available() -> list[str]:
    """The themes in THEME_ORDER that this Textual actually provides."""
    known = set(BUILTIN_THEMES) | {t.name for t in CUSTOM_THEMES}
    return [name for name in THEME_ORDER if name in known]


def label(name: str) -> str:
    return LABELS.get(name, name.replace("-", " ").title())


def normalise(name: str | None) -> str:
    """A stored theme name, or the default if it is unknown or missing."""
    names = available()
    if name in names:
        return name
    return DEFAULT_THEME if DEFAULT_THEME in names else names[0]


def step(name: str, delta: int) -> str:
    """The next/previous theme, wrapping around."""
    names = available()
    try:
        index = names.index(name)
    except ValueError:
        index = 0
    return names[(index + delta) % len(names)]
