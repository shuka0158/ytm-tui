# ytm-tui - a terminal player for your YouTube Music account.
# Copyright (C) 2026 shuka0158
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Keep kitty-protocol images sharp under a translucent modal.

Kitty images are drawn with Unicode placeholder cells whose *foreground colour
is the image id*. Textual dims the screen below a modal by blending every
foreground colour, which corrupts the id and makes the image vanish. This
patch leaves the foreground of placeholder cells alone (the background is still
tinted), so the rest of the screen blurs exactly as before.
"""
from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.color import Color
from textual.renderables.background_screen import BackgroundScreen

_PLACEHOLDER = "\U0010eeee"
_original = BackgroundScreen.process_segments.__func__


def _process_segments(cls, segments, color):
    def generate():
        for segment in segments:
            text, style, control = segment
            if (
                not control
                and style is not None
                and style.color is not None
                and _PLACEHOLDER in text
            ):
                style = style.clear_meta_and_links()
                if style.bgcolor is not None and color.a:
                    style = style + Style(
                        bgcolor=(Color.from_rich_color(style.bgcolor) + color).rich_color
                    )
                yield Segment(text, style, control)
            else:
                yield from _original(cls, (segment,), color)

    return generate()


def apply() -> None:
    if BackgroundScreen.process_segments.__func__ is not _process_segments:
        BackgroundScreen.process_segments = classmethod(_process_segments)
