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

"""Playback queue: play order, random and repeat."""
from __future__ import annotations

import random

from .models import Track

REPEAT_MODES = ("off", "all", "one")


class PlayQueue:
    def __init__(self) -> None:
        self.tracks: list[Track] = []
        self.order: list[int] = []
        self.pos: int = -1
        self.random: bool = False
        self.repeat: str = "off"
        self.source: str = ""

    # -- population --------------------------------------------------------
    def load(self, tracks: list[Track], start: int = 0, source: str = "") -> None:
        self.tracks = list(tracks)
        self.source = source
        self._rebuild_order(keep=start if 0 <= start < len(tracks) else None)
        if not self.tracks:
            self.pos = -1
            return
        target = start if 0 <= start < len(self.tracks) else 0
        self.pos = self.order.index(target)

    def _rebuild_order(self, keep: int | None = None) -> None:
        self.order = list(range(len(self.tracks)))
        if self.random and self.order:
            random.shuffle(self.order)
            if keep is not None and keep in self.order:
                self.order.remove(keep)
                self.order.insert(0, keep)

    def set_random(self, enabled: bool) -> None:
        current = self.current_index()
        self.random = enabled
        self._rebuild_order(keep=current)
        if current is not None and current in self.order:
            self.pos = self.order.index(current)

    def cycle_repeat(self) -> str:
        self.repeat = REPEAT_MODES[(REPEAT_MODES.index(self.repeat) + 1) % len(REPEAT_MODES)]
        return self.repeat

    # -- navigation --------------------------------------------------------
    def current_index(self) -> int | None:
        if 0 <= self.pos < len(self.order):
            return self.order[self.pos]
        return None

    def current(self) -> Track | None:
        idx = self.current_index()
        return self.tracks[idx] if idx is not None else None

    def jump(self, track_index: int) -> Track | None:
        if not (0 <= track_index < len(self.tracks)):
            return None
        if track_index not in self.order:
            self._rebuild_order(keep=track_index)
        self.pos = self.order.index(track_index)
        return self.current()

    def advance(self, manual: bool = False) -> Track | None:
        """Step forward. `manual` ignores repeat-one, as a skip should skip."""
        if not self.order:
            return None
        if self.repeat == "one" and not manual:
            return self.current()
        if self.pos + 1 < len(self.order):
            self.pos += 1
        elif self.repeat == "all" or (manual and self.repeat != "off"):
            self.pos = 0
        elif manual:
            self.pos = 0
        else:
            return None
        return self.current()

    def previous(self) -> Track | None:
        if not self.order:
            return None
        self.pos = self.pos - 1 if self.pos > 0 else len(self.order) - 1
        return self.current()

    def peek_next(self) -> Track | None:
        if not self.order:
            return None
        if self.repeat == "one":
            return self.current()
        if self.pos + 1 < len(self.order):
            return self.tracks[self.order[self.pos + 1]]
        if self.repeat == "all":
            return self.tracks[self.order[0]]
        return None

    def append(self, track: Track) -> None:
        self.tracks.append(track)
        self.order.append(len(self.tracks) - 1)

    def extend(self, tracks: list[Track]) -> None:
        for track in tracks:
            self.append(track)
