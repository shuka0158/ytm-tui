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

"""A round vinyl record that spins while music plays.

Drawn with braille characters (2x4 dots per cell, square dots), so the circle
stays round and smooth instead of blocky. The grooves are rings of dots; the
rotation shows as a light sheen sweeping across them and a notch on the label.
"""
from __future__ import annotations

import math
import random
import time

from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual.widgets import Static

COLS, ROWS = 10, 5
W, H = COLS * 2, ROWS * 4  # dot grid; 20x20 so it is a true circle
FPS = 12
REV_PER_SEC = 0.55  # roughly 33 rpm
LABEL_R = 0.36

# Braille bit for the dot at (x, y) inside a cell.
_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


def _mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _polar(x: float, y: float, phase: float) -> tuple[float, float]:
    dx, dy = x - W / 2 + 0.5, y - H / 2 + 0.5
    return math.hypot(dx, dy) / (W / 2), math.atan2(dy, dx) - phase


def _lit(x: int, y: int, phase: float) -> bool:
    r, angle = _polar(x, y, phase)
    if r > 1.0 or r < 0.07:  # outside the disc / spindle hole
        return False
    if r < LABEL_R:  # solid label with a notch that turns with the disc
        return not (r > 0.17 and abs(math.sin(angle)) < 0.25 and math.cos(angle) > 0)
    return int(r * 17) % 3 != 0  # grooves: rings of dots with gaps between


class Disc(Static):
    """Call `spin(True/False)`; the widget owns its own timer."""

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._phase = 0.0
        self._spinning = False

    def on_mount(self) -> None:
        self.set_interval(1 / FPS, self._frame)
        self._draw()

    def spin(self, on: bool) -> None:
        self._spinning = on

    def _frame(self) -> None:
        if not self._spinning:
            return
        self._phase = (self._phase + 2 * math.pi * REV_PER_SEC / FPS) % (2 * math.pi)
        self._draw()

    def _draw(self) -> None:
        try:
            c = Color.parse(self.app.current_theme.accent).get_truecolor()
            accent = (c.red, c.green, c.blue)
        except Exception:
            accent = (200, 60, 120)

        text = Text(no_wrap=True)
        for row in range(ROWS):
            for col in range(COLS):
                bits = 0
                for dx in range(2):
                    for dy in range(4):
                        if _lit(col * 2 + dx, row * 4 + dy, self._phase):
                            bits |= _BITS[dx][dy]
                if not bits:
                    text.append(" ")
                    continue
                r, angle = _polar(col * 2 + 1, row * 4 + 2, self._phase)
                if r < LABEL_R + 0.08:
                    rgb = accent
                else:
                    sheen = max(0.0, math.cos(2 * angle)) ** 6
                    rgb = _mix((92, 92, 106), (255, 255, 255), sheen)
                    if r > 0.88:  # bright rim
                        rgb = _mix(rgb, (255, 255, 255), 0.5)
                text.append(chr(0x2800 + bits), style=Style(color=Color.from_rgb(*rgb)))
            if row < ROWS - 1:
                text.append("\n")
        self.update(text)


def _accent(widget) -> tuple[int, int, int]:
    try:
        c = Color.parse(widget.app.current_theme.accent).get_truecolor()
        return (c.red, c.green, c.blue)
    except Exception:
        return (200, 60, 120)


class Visualizer(Static):
    """DJ-style level bars driven by the real audio: mpv splits the stream into
    frequency bands and reports each band's level (see player._band_graph).
    Falls back to a simulated spectrum only if mpv can't run that analysis."""

    BARS = 12
    LEVELS = " ▁▂▃▄▅▆▇█"
    FLOOR_DB, CEIL_DB = -60.0, 0.0
    STALE = 0.25  # seconds without new levels = silence (paused, stopped)

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._h = [0.0] * self.BARS
        self._t = 0.0
        self._playing = False

    def on_mount(self) -> None:
        self.set_interval(1 / 25, self._frame)

    def spin(self, on: bool) -> None:
        self._playing = on

    def _targets(self) -> list[float]:
        player = self.app.player
        if player.analysis:
            if not self._playing or time.monotonic() - player.levels_at > self.STALE:
                return [0.0] * self.BARS
            out = []
            for i, db in enumerate(player.levels[: self.BARS]):
                db += i * 0.6  # highs carry less energy; even the bars out
                x = max(0.0, min(1.0, (db - self.FLOOR_DB) / (self.CEIL_DB - self.FLOOR_DB)))
                out.append(x ** 1.6)  # curve keeps loud songs from pinning the bars
            return out
        if not self._playing:
            return [0.0] * self.BARS
        beat = max(0.0, math.cos(self._t * 2 * math.pi * 2.0)) ** 6
        out = []
        for i in range(self.BARS):
            wobble = 0.5 + 0.5 * math.sin(self._t * (2.1 + i * 0.37) + i * 1.7)
            weight = 1.0 - 0.55 * i / self.BARS
            t = weight * (0.25 + 0.45 * wobble + 0.35 * beat * (1 - i / self.BARS))
            out.append(min(1.0, t * random.uniform(0.75, 1.15)))
        return out

    def _frame(self) -> None:
        if not self._playing and not any(self._h):
            return
        self._t += 1 / 25
        for i, target in enumerate(self._targets()):
            # Quick attack, slow fall, like a real meter.
            self._h[i] += (target - self._h[i]) * (0.7 if target > self._h[i] else 0.2)
            if self._h[i] < 0.01:
                self._h[i] = 0.0
        self._draw()

    def _draw(self) -> None:
        accent = _accent(self)
        rows = 5
        text = Text(no_wrap=True)
        for row in range(rows):  # row 0 is the top
            base = (rows - 1 - row) * 8
            colour = _mix(accent, (255, 255, 255), 0.65 * (1 - row / (rows - 1)) ** 1.5)
            style = Style(color=Color.from_rgb(*colour))
            for h in self._h:
                level = int(max(0, min(8, h * rows * 8 - base)))
                text.append(self.LEVELS[level], style=style)
                text.append(" ")
            if row < rows - 1:
                text.append("\n")
        self.update(text)
