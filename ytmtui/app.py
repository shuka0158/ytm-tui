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

"""The Textual UI."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)
from textual.worker import Worker, get_current_worker
from textual_image.widget import Image

from . import art, config, cookies, local, lyrics, themes
from .disc import Disc, Visualizer
from .api import AuthMissing, Library
from .models import Collection, Playlist, Track, fmt_duration
from .player import MpvPlayer
from .queue import PlayQueue
from .resolver import JS_RUNTIME_HINT, Resolver, TrackBlocked, missing_js_runtime

BAR_WIDTH = 44
SPEED_PRESETS = (1.0, 1.25, 1.5, 1.75, 2.0, 0.75)

# (key, label). The first three give tracks; the rest give things you open.
SEARCH_FILTERS = (
    ("all", "All"),
    ("songs", "Songs"),
    ("videos", "Videos"),
    ("albums", "Albums"),
    ("artists", "Artists"),
    ("playlists", "Playlists"),
)
_TRACK_SEARCH_KINDS = {
    "all": ("songs", "videos"),
    "songs": ("songs",),
    "videos": ("videos",),
}

# (label, value): minutes, 0 to switch it off, or "track" to stop after this one.
SLEEP_CHOICES = (
    ("Off", 0),
    ("15 minutes", 15),
    ("30 minutes", 30),
    ("45 minutes", 45),
    ("1 hour", 60),
    ("90 minutes", 90),
    ("After this track", "track"),
)
SLEEP_FADE_SECONDS = 15
SESSION_SAVE_EVERY = 20.0


class SearchScreen(ModalScreen["tuple[str, str] | None"]):
    """Dismisses with (query, filter key), or None."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("tab", "next_filter", "Next filter"),
        ("shift+tab", "prev_filter", "Previous filter"),
    ]

    def __init__(self, filter_key: str) -> None:
        super().__init__()
        self.filter_index = next(
            (i for i, (key, _) in enumerate(SEARCH_FILTERS) if key == filter_key), 0
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="search-box"):
            yield Label("Search YouTube Music")
            yield Static("", id="search-filters")
            yield Input(placeholder="song, artist, album…", id="search-input")
            yield Static("[dim]tab filter   enter search   esc cancel[/dim]")

    def on_mount(self) -> None:
        self._draw_filters()
        self.query_one(Input).focus()

    def _draw_filters(self) -> None:
        chips = [
            f"[reverse] {label} [/reverse]" if i == self.filter_index else f" {label} "
            for i, (_, label) in enumerate(SEARCH_FILTERS)
        ]
        self.query_one("#search-filters", Static).update(" ".join(chips))

    def action_next_filter(self) -> None:
        self.filter_index = (self.filter_index + 1) % len(SEARCH_FILTERS)
        self._draw_filters()

    def action_prev_filter(self) -> None:
        self.filter_index = (self.filter_index - 1) % len(SEARCH_FILTERS)
        self._draw_filters()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        self.dismiss((query, SEARCH_FILTERS[self.filter_index][0]) if query else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SleepScreen(ModalScreen["int | str | None"]):
    """Sleep-timer picker. Dismisses with minutes, 0 for off, "track", or None."""

    BINDINGS = [("escape,q,z", "cancel", "Cancel")]

    def __init__(self, active: str) -> None:
        super().__init__()
        self.active = active

    def compose(self) -> ComposeResult:
        with Vertical(id="theme-box"):
            yield Label("Sleep timer" + (f"  [dim]{self.active}[/dim]" if self.active else ""))
            yield ListView(
                *(ListItem(Label(label)) for label, _ in SLEEP_CHOICES),
                id="theme-list",
            )
            yield Static("[dim]↑↓ choose   enter set   esc cancel[/dim]")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        self.dismiss(SLEEP_CHOICES[index][1] if index is not None else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class QueueScreen(ModalScreen[None]):
    """The play queue: jump, remove, reorder, clear what's coming."""

    BINDINGS = [
        ("escape,q,Q", "cancel", "Close"),
        ("d,delete", "remove", "Remove"),
        ("K,shift+up", "move_up", "Move up"),
        ("J,shift+down", "move_down", "Move down"),
        ("c", "clear", "Clear upcoming"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="queue-box"):
            yield Static("", id="queue-heading")
            yield DataTable(id="queue-table", cursor_type="row", zebra_stripes=True)
            yield Static(
                "[dim]enter play   d remove   K/J move up/down   c clear upcoming   esc close[/dim]"
            )

    def on_mount(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        table.add_column("", width=1)
        table.add_column("#", width=4)
        table.add_column("Title", width=40)
        table.add_column("Artist", width=24)
        table.add_column("Time", width=6)
        self.refresh_rows(cursor=self.app.queue.pos)
        table.focus()

    def refresh_rows(self, cursor: int | None = None) -> None:
        queue = self.app.queue
        table = self.query_one("#queue-table", DataTable)
        keep = table.cursor_row if cursor is None else cursor
        table.clear()
        for position, index in enumerate(queue.order):
            track = queue.tracks[index]
            table.add_row(
                "▶" if position == queue.pos else "",
                str(position + 1),
                self.app._clip(track.title, 40),
                self.app._clip(track.artist, 24),
                fmt_duration(track.duration),
            )
        if queue.order:
            table.move_cursor(row=max(0, min(keep, len(queue.order) - 1)))
        heading = f"[b]Queue[/b]  [dim]{len(queue.order)} tracks"
        if queue.source:
            heading += f"  ·  {queue.source}"
        self.query_one("#queue-heading", Static).update(heading + "[/dim]")

    def _row(self) -> int | None:
        table = self.query_one("#queue-table", DataTable)
        return table.cursor_row if table.row_count else None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.app.queue_play(event.cursor_row)
        self.refresh_rows(cursor=event.cursor_row)

    def action_remove(self) -> None:
        row = self._row()
        if row is not None and self.app.queue_remove(row):
            self.refresh_rows(cursor=row)

    def action_move_up(self) -> None:
        row = self._row()
        if row is not None:
            self.refresh_rows(cursor=self.app.queue_move(row, -1))

    def action_move_down(self) -> None:
        row = self._row()
        if row is not None:
            self.refresh_rows(cursor=self.app.queue_move(row, 1))

    def action_clear(self) -> None:
        self.app.queue_clear_upcoming()
        self.refresh_rows()

    def action_cancel(self) -> None:
        self.dismiss(None)


class DownloadScreen(ModalScreen[str | None]):
    """Asks where to save a track before downloading it."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, track_title: str, default_dir: str) -> None:
        super().__init__()
        self.track_title = track_title
        self.default_dir = default_dir

    def compose(self) -> ComposeResult:
        with Vertical(id="search-box"):
            yield Label(f'Download "{self.track_title}" to…')
            yield Input(value=self.default_dir, id="search-input")

    def on_mount(self) -> None:
        input_ = self.query_one(Input)
        input_.focus()
        input_.cursor_position = len(input_.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or self.default_dir)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NewPlaylistScreen(ModalScreen[str | None]):
    """Asks for the name of a playlist to create."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="search-box"):
            yield Label("New playlist name")
            yield Input(placeholder="My playlist", id="search-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlaylistPickScreen(ModalScreen["Playlist | str | None"]):
    """Choose a playlist to add to. Dismisses with a Playlist, "new", or None."""

    BINDINGS = [("escape,q", "cancel", "Cancel")]

    def __init__(self, track_title: str, playlists: list[Playlist]) -> None:
        super().__init__()
        self.track_title = track_title
        self.choices = playlists

    def compose(self) -> ComposeResult:
        with Vertical(id="theme-box"):
            yield Label(f'Add "{self.track_title}" to…')
            yield ListView(
                ListItem(Label("[b]＋ New playlist…[/b]")),
                *(ListItem(Label(p.title)) for p in self.choices),
                id="theme-list",
            )
            yield Static("[dim]↑↓ choose   enter select   esc cancel[/dim]")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is None:
            return
        self.dismiss("new" if index == 0 else self.choices[index - 1])

    def action_cancel(self) -> None:
        self.dismiss(None)


class ImportFolderScreen(ModalScreen[str | None]):
    """Asks for a folder path to add to the Local section."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="search-box"):
            yield Label("Import folder…")
            yield Input(placeholder="~/Music or /mnt/nas/songs", id="search-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Every key, in one scrollable box."""

    BINDINGS = [("escape,question_mark,q", "cancel", "Close")]

    HELP = """[dim]↑↓  pgup/pgdn  home/end  scroll          esc  close[/dim]

[b]Playback[/b]
  [cyan]enter[/cyan]         play the highlighted track
  [cyan]space[/cyan]         play / pause (also resumes last session)
  [cyan]n[/cyan]             next track
  [cyan]p[/cyan]             previous (restart it if past 5s)
  [cyan]←[/cyan] [cyan]→[/cyan]          seek 5s
  [cyan]shift+←[/cyan] [cyan]→[/cyan]    seek 30s
  [cyan]+[/cyan] [cyan]-[/cyan]          volume up / down
  [cyan]s[/cyan]             random on / off
  [cyan]r[/cyan]             repeat: off, all, one
  [cyan]v[/cyan]             speed: 1, 1.25, 1.5, 1.75, 2, 0.75x
  [cyan]x[/cyan]             stop (forgets the saved session)
  [cyan]z[/cyan]             sleep timer

[b]Queue[/b]
  [cyan]a[/cyan]             add highlighted track to the end
  [cyan]N[/cyan]             play highlighted track next
  [cyan]Q[/cyan]             open the queue editor

[b]Library[/b]
  [cyan]/[/cyan]             search YouTube Music
  [cyan]backspace[/cyan]     back to the search results (also [cyan]b[/cyan]);
                 after opening an album/artist/playlist
  [cyan]A[/cyan]             add highlighted track to a playlist,
                 or create a new one
  [cyan]d[/cyan]             download highlighted track
  [cyan]l[/cyan]             lyrics for the playing track (Genius)
  [cyan]F5[/cyan] [cyan]ctrl+r[/cyan]    reload playlists
  [cyan]t[/cyan]             theme picker
  [cyan]tab[/cyan]           move between panes
  [cyan]?[/cyan]             this help
  [cyan]q[/cyan] [cyan]ctrl+c[/cyan]     quit

[b]In the search box[/b]
  [cyan]tab[/cyan]           next filter: All, Songs, Videos,
                 Albums, Artists, Playlists
  [cyan]shift+tab[/cyan]     previous filter
  [cyan]enter[/cyan]         search
  [cyan]esc[/cyan]           cancel
  Albums, artists and playlists come back as rows:
  [cyan]enter[/cyan] opens one, [cyan]backspace[/cyan] returns to the results.

[b]In the queue editor[/b]
  [cyan]↑[/cyan] [cyan]↓[/cyan]          move the cursor
  [cyan]enter[/cyan]         play that row
  [cyan]d[/cyan] [cyan]delete[/cyan]     remove the row (not the playing one)
  [cyan]K[/cyan] [cyan]shift+↑[/cyan]    move the row up
  [cyan]J[/cyan] [cyan]shift+↓[/cyan]    move the row down
  [cyan]c[/cyan]             clear everything after the playing track
  [cyan]esc[/cyan] [cyan]q[/cyan] [cyan]Q[/cyan]     close
  Playback keys (space, n, p) still work while open.

[b]Sleep timer ([/b][cyan]z[/cyan][b])[/b]
  [cyan]↑[/cyan] [cyan]↓[/cyan]          choose: off, 15 / 30 / 45 / 60 / 90
                 minutes, or after this track
  [cyan]enter[/cyan]         set      [cyan]esc[/cyan] [cyan]z[/cyan]  cancel
  The volume fades over the last 15s, then it pauses.

[b]Theme picker ([/b][cyan]t[/cyan][b])[/b]
  [cyan]↑[/cyan] [cyan]↓[/cyan]          preview a theme
  [cyan]enter[/cyan]         keep it      [cyan]esc[/cyan]  put the old one back

[b]Local & Downloads[/b]
  [cyan]Local[/cyan]         audio files from your music folders.
                 [cyan]Import Folder…[/cyan] adds another one to scan.
  [cyan]Downloads[/cyan]     tracks saved with [cyan]d[/cyan], with cover art.

[b]Resume[/b]
  The queue and position are saved as you listen. Next
  start, the track is loaded paused: press [cyan]space[/cyan] to
  carry on. Turn it off with "resume": false in
  config/settings.json.

[dim]ytm-tui - Copyright (C) 2026 shuka0158 - GPLv3 or later.
Free software with ABSOLUTELY NO WARRANTY; you may change
and redistribute it. See the LICENSE file for details.[/dim]"""

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static(self.HELP), id="help-box")

    def on_mount(self) -> None:
        self.query_one("#help-box").focus()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ThemeScreen(ModalScreen[str | None]):
    """Theme picker. Moving the highlight previews; escape puts it back."""

    BINDINGS = [("escape,q", "cancel", "Cancel")]

    def __init__(self, current: str) -> None:
        super().__init__()
        self.names = themes.available()
        self.current = current
        try:
            self.start_index = self.names.index(current)
        except ValueError:
            self.start_index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="theme-box"):
            yield Label("Theme")
            yield ListView(
                *(ListItem(Label(themes.label(n))) for n in self.names),
                initial_index=self.start_index,
                id="theme-list",
            )
            yield Static("[dim]↑↓ preview   enter apply   esc cancel[/dim]")

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self.names):
            self.app.theme = self.names[index]

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        self.dismiss(self.names[index] if index is not None else None)

    def action_cancel(self) -> None:
        self.app.theme = self.current
        self.dismiss(None)


class LyricsScreen(ModalScreen[None]):
    """Lyrics for the current track, scraped from Genius, in a ~70% popup."""

    BINDINGS = [("escape,q,l", "cancel", "Close")]

    def __init__(self, heading: str) -> None:
        super().__init__()
        self.heading = heading

    def compose(self) -> ComposeResult:
        with Vertical(id="lyrics-box"):
            yield Static(self.heading, id="lyrics-heading")
            with VerticalScroll(id="lyrics-scroll"):
                yield Static("", id="lyrics-body")

    def set_text(self, heading: str, text: str) -> None:
        self.query_one("#lyrics-heading", Static).update(heading)
        self.query_one("#lyrics-body", Static).update(text)

    def action_cancel(self) -> None:
        self.dismiss(None)


class YtmTui(App[None]):
    CSS_PATH = "theme.tcss"
    TITLE = "ytm-tui"
    SUB_TITLE = "YouTube Music in the terminal"

    BINDINGS = [
        ("space", "play_pause", "Play/Pause"),
        ("n", "next", "Next"),
        ("p", "previous", "Prev"),
        ("right", "seek_forward", "＋5s"),
        ("left", "seek_back", "−5s"),
        ("shift+right", "seek_forward_big", ""),
        ("shift+left", "seek_back_big", ""),
        ("plus,equals_sign", "volume_up", "Vol+"),
        ("minus", "volume_down", "Vol−"),
        ("s", "random", "Random"),
        ("r", "repeat", "Repeat"),
        ("v", "speed", "Speed"),
        ("x", "stop", "Stop"),
        ("slash", "search", "Search"),
        ("a", "append", "+Queue"),
        ("N", "play_next", ""),
        ("Q", "queue", "Queue"),
        ("z", "sleep", "Sleep"),
        ("backspace,b", "back", "Back"),
        ("d", "download", "Download"),
        ("A", "add_to_playlist", "+Playlist"),
        ("l", "lyrics", "Lyrics"),
        ("f5,ctrl+r", "reload", "Reload"),
        ("t", "theme", "Theme"),
        ("question_mark", "help", "Help"),
        ("q,ctrl+c", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.settings = config.load_settings()
        self.library: Library | None = None
        self.player = MpvPlayer(on_eof=self._on_eof)
        cookie_browser, cookie_file = cookies.resolve_source(self.settings)
        self.resolver = Resolver(
            self.settings.get("format"),
            cookies_from_browser=cookie_browser,
            cookies_file=cookie_file,
        )
        self.queue = PlayQueue()
        self.queue.random = bool(self.settings.get("random", False))
        self.queue.repeat = self.settings.get("repeat", "off")
        self.volume = int(self.settings.get("volume", 80))
        self.speed = float(self.settings.get("speed", 1.0))
        self.playlists: list[Playlist] = []
        # Parallel to the "#playlists" ListView's children: what each row opens.
        # ("local", None) | ("downloads", None) | ("playlist", Playlist)
        self._sidebar_kinds: list[tuple[str, Playlist | None]] = []
        self.view_tracks: list[Track] = []
        # Set instead of view_tracks while the table lists albums, artists or
        # playlists from a search; Enter opens one rather than playing it.
        self.view_collections: list[Collection] = []
        self.view_title: str = ""
        # The last set of search collections, and whether the tracks on screen
        # were opened from them (so Backspace can return).
        self._last_collections: tuple[str, list[Collection]] | None = None
        self._can_go_back = False
        known = {key for key, _ in SEARCH_FILTERS}
        self.search_filter = self.settings.get("search_filter")
        if self.search_filter not in known:
            self.search_filter = "all"
        self.playing_row: int | None = None
        # Sleep timer: a monotonic deadline, or "end of this track".
        self._sleep_deadline: float | None = None
        self._sleep_after_track = False
        self._sleep_fading = False
        # Where to pick up the restored track; None once playback has started.
        self._resume_pos: float | None = None
        self._session_saved_at = 0.0
        # Skipping a failed track wraps around at the end of the queue, so a
        # queue where everything fails would skip forever. Bound it.
        self._failures_in_a_row = 0
        # Blocked track -> the upload we substituted, so a repeat play skips
        # the search and the failed resolves entirely.
        self._alternates: dict[str, Track] = {}
        self._rendered_width: int = 0
        self._col_keys: dict[str, object] = {}
        # Track title -> percent complete (or None while size is unknown),
        # for whichever downloads are in flight right now.
        self._downloads: dict[str, float | None] = {}

    # -- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        # The default "⭘" icon is the command-palette button; it renders as a
        # bare circle in most terminal fonts, so drop it.
        yield Header(show_clock=True, icon=" ")
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Static("Library", classes="pane-title")
                yield ListView(id="playlists")
            with Vertical(id="content"):
                with Horizontal(id="tracks-header", classes="pane-title"):
                    yield Static("", id="tracks-title")
                    yield Button("Import Folder…", id="import-folder-btn")
                yield Static("", id="download-status")
                yield DataTable(id="tracks", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="now"):
            yield Image(id="art")
            with Vertical(id="np"):
                yield Static("Nothing playing", id="np-title")
                yield Static("", id="np-artist")
                yield Static("", id="np-bar")
                yield Static("", id="np-status")
            yield Disc(id="disc")
            yield Visualizer(id="viz")
        # The palette's own footer button clashes with the key hints; ctrl+p
        # still opens it.
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        for theme in themes.CUSTOM_THEMES:
            self.register_theme(theme)
        self.theme = themes.normalise(self.settings.get("theme"))

        table = self.query_one("#tracks", DataTable)
        # Keep the ColumnKeys: auto-width columns never shrink again once a
        # wide value has been seen, so the text columns are sized explicitly.
        self._col_keys = {
            "mark": table.add_column("", key="mark", width=1),
            "num": table.add_column("#", key="num", width=4),
            "title": table.add_column("Title", key="title", width=40),
            "artist": table.add_column("Artist", key="artist", width=22),
            "album": table.add_column("Album", key="album", width=20),
            "time": table.add_column("Time", key="time", width=5),
        }

        self.query_one("#download-status", Static).display = False
        self.query_one("#import-folder-btn", Button).display = False

        try:
            self.player.start(volume=self.volume)
            self.player.set_speed(self.speed)
        except Exception as exc:
            self.notify(f"mpv failed to start: {exc}", severity="error", timeout=12)

        if self.resolver.cookie_warning:
            self.notify(self.resolver.cookie_warning, severity="warning", timeout=12)
        if missing_js_runtime():
            self.notify(JS_RUNTIME_HINT, severity="warning", timeout=15)

        self._apply_sidebar_width()
        self.set_interval(0.25, self._tick)
        # Local/Downloads don't need the network, so they show up immediately
        # rather than waiting on the library connection below.
        self._fill_playlists([])
        self._restore_session()
        self._connect_library()

    def on_unmount(self) -> None:
        self.settings.update(
            volume=self.volume,
            speed=self.speed,
            random=self.queue.random,
            repeat=self.queue.repeat,
            theme=self.theme,
            search_filter=self.search_filter,
        )
        config.save_settings(self.settings)
        self._save_session()
        self.resolver.shutdown()
        self.player.close()

    # -- library loading ---------------------------------------------------
    @work(thread=True, group="library", exclusive=True)
    def _connect_library(self) -> None:
        try:
            library = Library()
            playlists = library.playlists()
        except AuthMissing as exc:
            self.call_from_thread(
                self.notify, str(exc), severity="error", timeout=30
            )
            return
        except Exception as exc:
            self.call_from_thread(
                self.notify,
                f"Could not reach YouTube Music: {exc}",
                severity="error",
                timeout=20,
            )
            return
        self.library = library
        self.call_from_thread(self._fill_playlists, playlists)

    def _fill_playlists(self, playlists: list[Playlist]) -> None:
        self.playlists = playlists
        view = self.query_one("#playlists", ListView)
        had_selection = view.index is not None
        view.clear()
        self._sidebar_kinds = [("local", None), ("downloads", None)]
        view.append(ListItem(Label("[b]🖥  Local[/b]\n[dim]on-disk music[/dim]")))
        view.append(ListItem(Label("[b]⬇  Downloads[/b]\n[dim]saved from ytm-tui[/dim]")))
        for playlist in playlists:
            label = f"[b]{playlist.title}[/b]"
            if playlist.subtitle:
                label += f"\n[dim]{playlist.subtitle}[/dim]"
            view.append(ListItem(Label(label)))
            self._sidebar_kinds.append(("playlist", playlist))
        if playlists:
            view.index = 2  # first real playlist, past Local/Downloads
            self._open_playlist(playlists[0])
        elif not had_selection:
            view.index = 0

    def _open_playlist(self, playlist: Playlist) -> None:
        self.view_title = playlist.title
        self.query_one("#tracks-title", Static).update(f"{playlist.title}  [dim]loading…[/dim]")
        self._load_tracks(playlist)

    def _open_local(self) -> None:
        self.view_title = "Local"
        self.query_one("#tracks-title", Static).update("Local  [dim]scanning…[/dim]")
        self._scan_local_worker()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "import-folder-btn":
            self.push_screen(ImportFolderScreen(), callback=self._import_folder)

    def _import_folder(self, raw_path: str | None) -> None:
        if not raw_path:
            return
        path = Path(raw_path).expanduser()
        if not path.is_dir():
            self.notify(f"Not a folder: {path}", severity="error", timeout=5)
            return
        dirs = self.settings.setdefault("local_music_dirs", [])
        resolved = str(path.resolve())
        if resolved in (str(Path(d).expanduser().resolve()) for d in dirs):
            self.notify(f"Already imported: {resolved}", timeout=3)
            return
        dirs.append(resolved)
        config.save_settings(self.settings)
        self.notify(f"Imported: {resolved}", timeout=3)
        if self.view_title == "Local":
            self._open_local()

    @work(thread=True, group="tracks", exclusive=True)
    def _scan_local_worker(self) -> None:
        dirs = config.local_music_dirs(self.settings)
        tracks = local.scan(dirs)
        self.call_from_thread(self._show_tracks, tracks, "Local")
        if not dirs:
            self.call_from_thread(
                self.notify,
                "No local music folders found. Add paths to \"local_music_dirs\" "
                f"in {config.SETTINGS_FILE}, or drop files into ~/Music.",
                timeout=10,
            )

    def _open_downloads(self) -> None:
        self.view_title = "Downloads"
        self.query_one("#tracks-title", Static).update("Downloads  [dim]scanning…[/dim]")
        self._scan_downloads_worker()

    @work(thread=True, group="tracks", exclusive=True)
    def _scan_downloads_worker(self) -> None:
        tracks = local.scan([config.DOWNLOADS_DIR])
        self.call_from_thread(self._show_tracks, tracks, "Downloads")

    @work(thread=True, group="tracks", exclusive=True)
    def _load_tracks(self, playlist: Playlist) -> None:
        if self.library is None:
            return
        if playlist.loaded:  # already fetched this session
            self.call_from_thread(
                self._show_tracks, playlist.tracks, playlist.title
            )
            return
        worker = get_current_worker()
        try:
            tracks = self.library.tracks(playlist)
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"Failed to load {playlist.title}: {exc}", severity="error"
            )
            return
        if worker.is_cancelled:
            return
        playlist.tracks = tracks
        playlist.loaded = True
        self.call_from_thread(self._show_tracks, tracks, playlist.title)

    def _show_tracks(
        self, tracks: list[Track], title: str, from_collection: bool = False
    ) -> None:
        self.view_tracks = tracks
        self.view_collections = []
        self.view_title = title
        self._can_go_back = from_collection and self._last_collections is not None
        self._render_rows(cursor=0)
        self.query_one("#tracks-title", Static).update(
            f"{title}  [dim]{len(tracks)} tracks[/dim]"
        )
        self.query_one("#import-folder-btn", Button).display = title == "Local"
        self.screen.refresh_bindings()

    def _show_collections(self, collections: list[Collection], title: str) -> None:
        self.view_tracks = []
        self.view_collections = collections
        self.view_title = title
        self._can_go_back = False
        self._last_collections = (title, collections)
        self._render_rows(cursor=0)
        self.query_one("#tracks-title", Static).update(
            f"{title}  [dim]{len(collections)} results — enter to open[/dim]"
        )
        self.query_one("#import-folder-btn", Button).display = False
        self.screen.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Only offer "Back" when there is somewhere to go back to.
        if action == "back":
            return self._can_go_back
        return True

    def action_back(self) -> None:
        if self._can_go_back and self._last_collections is not None:
            title, collections = self._last_collections
            self._show_collections(collections, title)

    @staticmethod
    def _clip(text: str, width: int) -> str:
        if width <= 0:
            return ""
        return text if len(text) <= width else text[: width - 1] + "\u2026"

    def _apply_sidebar_width(self, screen_width: int | None = None) -> None:
        """Give the track table more room on narrow terminals."""
        screen_width = screen_width or self.size.width or 120
        target = 32 if screen_width >= 110 else (24 if screen_width >= 90 else 18)
        sidebar = self.query_one("#sidebar")
        if sidebar.styles.width is None or sidebar.styles.width.value != target:
            sidebar.styles.width = target

    def _text_budgets(self) -> tuple[int, int, int]:
        """Character budget for the title/artist/album columns at this width."""
        table = self.query_one("#tracks", DataTable)
        width = table.size.width or 100
        # fixed columns (1 + 4 + 5), one space of cell padding either side of
        # all six columns, and room for the vertical scrollbar
        flex = max(15, width - 10 - 12 - 2)
        title = max(6, int(flex * 0.45))
        artist = max(5, int(flex * 0.30))
        return title, artist, max(4, flex - title - artist)

    def _render_rows(self, cursor: int | None = None) -> None:
        table = self.query_one("#tracks", DataTable)
        keep = table.cursor_row if cursor is None else cursor
        title_w, artist_w, album_w = self._text_budgets()
        self._rendered_width = table.size.width
        for name, width in (("title", title_w), ("artist", artist_w), ("album", album_w)):
            column = table.columns[self._col_keys[name]]
            column.auto_width = False
            column.width = width
        table.clear()
        rows = self._view_rows()
        for i, (title, artist, album, duration) in enumerate(rows, start=1):
            table.add_row(
                "",
                str(i),
                self._clip(title, title_w),
                self._clip(artist, artist_w),
                self._clip(album, album_w),
                duration,
            )
        self.playing_row = self._current_view_row()
        self._mark_playing_row()
        if rows:
            table.move_cursor(row=max(0, min(keep, len(rows) - 1)))

    def _view_rows(self) -> list[tuple[str, str, str, str]]:
        """(title, artist, album, time) for whatever the table is showing."""
        if self.view_collections:
            return [(c.title, c.subtitle, c.detail, "") for c in self.view_collections]
        return [
            (t.title, t.artist, t.album, fmt_duration(t.duration)) for t in self.view_tracks
        ]

    def on_resize(self, event) -> None:
        # Child widgets still report the old size during this event, so use the
        # size the event carries and redraw once layout has settled. The width
        # check in _tick catches anything missed.
        self._apply_sidebar_width(event.size.width)
        if self.view_tracks or self.view_collections:
            self.call_after_refresh(self._render_rows)

    # -- events ------------------------------------------------------------
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = self.query_one("#playlists", ListView).index
        if index is None or not (0 <= index < len(self._sidebar_kinds)):
            return
        kind, payload = self._sidebar_kinds[index]
        if kind == "local":
            self._open_local()
        elif kind == "downloads":
            self._open_downloads()
        elif payload is not None:
            self._open_playlist(payload)
        self.query_one("#tracks", DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "tracks":
            return
        row = event.cursor_row
        if self.view_collections:
            if 0 <= row < len(self.view_collections):
                self._open_collection(self.view_collections[row])
            return
        self._play_from_view(row)

    def _open_collection(self, collection: Collection) -> None:
        self.query_one("#tracks-title", Static).update(
            f"{collection.title}  [dim]loading…[/dim]"
        )
        self._collection_worker(collection)

    @work(thread=True, group="tracks", exclusive=True)
    def _collection_worker(self, collection: Collection) -> None:
        if self.library is None:
            return
        try:
            tracks = self.library.collection_tracks(collection)
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"Failed to open {collection.title}: {exc}", severity="error"
            )
            return
        if not tracks:
            self.call_from_thread(
                self.notify, f"Nothing playable in {collection.title}.", timeout=4
            )
            return
        self.call_from_thread(self._show_tracks, tracks, collection.title, True)

    # -- playback ----------------------------------------------------------
    def _play_from_view(self, index: int) -> None:
        if not self.view_tracks or not (0 <= index < len(self.view_tracks)):
            return
        self.queue.load(self.view_tracks, start=index, source=self.view_title)
        self._failures_in_a_row = 0  # a deliberate choice deserves a fresh run
        self._start_current()

    def _start_current(self, start: float = 0.0) -> None:
        track = self.queue.current()
        if track is None:
            return
        self._resume_pos = None  # playback is starting; nothing left to resume
        # The old track's position must not be saved against the new one.
        self.player.time_pos = start
        self.player.duration = 0.0
        self._set_now_playing(track)
        if track.local_path:
            self._play_local(track, start)
        else:
            self._resolve_and_play(track, start)

    @work(thread=True, group="play", exclusive=True)
    def _play_local(self, track: Track, start: float = 0.0) -> None:
        try:
            self.player.play_url(track.local_path, start=start)
        except Exception as exc:
            self.call_from_thread(self.notify, f"mpv: {exc}", severity="error")
            return
        self._failures_in_a_row = 0
        upcoming = self.queue.peek_next()
        if upcoming is not None and not upcoming.local_path:
            self.resolver.prefetch(upcoming.video_id)

    @work(thread=True, group="play", exclusive=True)
    def _resolve_and_play(self, track: Track, start: float = 0.0) -> None:
        worker = get_current_worker()
        try:
            url, headers = self.resolver.resolve(track.video_id)
        except TrackBlocked as exc:
            # This upload is barred, but the song itself may exist elsewhere.
            swap = self._resolve_alternate(track, worker)
            if swap is None:
                self.call_from_thread(
                    self.notify, f'Cannot play "{track.title}": {exc}', severity="error"
                )
                self.call_from_thread(self._skip_after_failure)
                return
            alternate, url, headers = swap
            self.call_from_thread(
                self.notify,
                f'"{track.title}" is blocked - playing another upload: '
                f'"{alternate.title}"',
                severity="warning",
                timeout=8,
            )
        except Exception as exc:
            self.call_from_thread(
                self.notify, f'Cannot play "{track.title}": {exc}', severity="error"
            )
            self.call_from_thread(self._skip_after_failure)
            return
        if worker.is_cancelled:
            return
        self._failures_in_a_row = 0
        try:
            self.player.play_url(url, headers, start=start)
        except Exception as exc:
            self.call_from_thread(self.notify, f"mpv: {exc}", severity="error")
            return
        upcoming = self.queue.peek_next()
        if upcoming is not None and not upcoming.local_path:
            self.resolver.prefetch(upcoming.video_id)

    # How many other uploads to try before giving up on a blocked track. Each
    # costs a resolve, so keep it small enough to stay responsive.
    MAX_ALTERNATES = 3

    def _resolve_alternate(
        self, track: Track, worker: Worker
    ) -> tuple[Track, str, dict[str, str]] | None:
        """First playable re-upload of a blocked track, or None.

        Runs on the playback worker thread, so it may block.
        """
        known = self._alternates.get(track.video_id)
        if known is not None:
            try:
                url, headers = self.resolver.resolve(known.video_id)
            except Exception:
                self._alternates.pop(track.video_id, None)  # it died too
            else:
                return known, url, headers

        if self.library is None:
            return None
        try:
            candidates = self.library.alternates(track, limit=self.MAX_ALTERNATES)
        except Exception:
            return None
        if not candidates:
            return None

        # Resolve them at once rather than one after another: a blocked upload
        # costs a couple of seconds to find out about, and we may try several.
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            futures = {c.video_id: pool.submit(self.resolver.resolve, c.video_id)
                       for c in candidates}
            for candidate in candidates:  # keep best-match order, not finish order
                if worker.is_cancelled:
                    return None
                try:
                    url, headers = futures[candidate.video_id].result()
                except Exception:
                    continue  # blocked or dead as well; try the next one
                self._alternates[track.video_id] = candidate
                return candidate, url, headers
        return None

    # Give up once we have failed our way through the whole queue, but never
    # sit through more than a handful of errors before saying something.
    MAX_FAILURES_IN_A_ROW = 5

    def _skip_after_failure(self) -> None:
        self._failures_in_a_row += 1
        limit = min(len(self.queue.order) or 1, self.MAX_FAILURES_IN_A_ROW)
        if self._failures_in_a_row >= limit:
            self._failures_in_a_row = 0
            self.player.stop()
            self.notify(
                f"Stopped after {limit} tracks in a row failed to play.",
                severity="warning",
                timeout=10,
            )
            return
        if self.queue.advance(manual=True) is not None:
            self._start_current()

    def _set_now_playing(self, track: Track) -> None:
        self.query_one("#np-title", Static).update(f"[b]{track.title}[/b]")
        subtitle = track.artist + (f"  ·  {track.album}" if track.album else "")
        self.query_one("#np-artist", Static).update(f"[dim]{subtitle}[/dim]")
        self.playing_row = self._current_view_row()
        self._mark_playing_row()
        self._follow_playing_row()
        self._load_art(track.thumb)
        if isinstance(self.screen, QueueScreen):
            self.screen.refresh_rows(cursor=self.queue.pos)
        self._save_session()

    # -- session (resume) --------------------------------------------------
    def _save_session(self) -> None:
        """Write the queue and position so the next run can pick them up."""
        self._session_saved_at = time.monotonic()
        if not self.settings.get("resume", True):
            return
        snapshot = self.queue.snapshot()
        if snapshot is None:
            config.save_session(None)
            return
        if self._resume_pos is not None:
            position = self._resume_pos  # restored but not started: keep the old spot
        else:
            position = self.player.time_pos
            total = self.player.duration or snapshot["tracks"][snapshot["pos"]]["duration"]
            if total and position > total - 5:
                position = 0.0  # the track was effectively over
        snapshot["position"] = round(position, 1)
        config.save_session(snapshot)

    def _restore_session(self) -> None:
        """Bring back last run's queue, paused. Space picks it up from where it stopped."""
        if not self.settings.get("resume", True):
            return
        session = config.load_session()
        if session is None or not self.queue.restore(session):
            return
        track = self.queue.current()
        try:
            position = max(0.0, float(session.get("position", 0.0)))
        except (TypeError, ValueError):
            position = 0.0
        self._resume_pos = position
        self._set_now_playing(track)
        self.notify(
            f'Space resumes "{track.title}" at {fmt_duration(position) if position else "the start"}.',
            timeout=6,
        )

    @work(thread=True, group="art", exclusive=True)
    def _load_art(self, url: str) -> None:
        # Local/Downloads tracks carry a cover art *path* (see local.py's
        # scan), not a URL to fetch - use it directly instead of feeding it to
        # requests, which would just fail.
        if url and not url.startswith(("http://", "https://")):
            path = url if Path(url).is_file() else None
        else:
            path = art.fetch(url)
        if path:
            self.call_from_thread(self._apply_art, path)

    def _apply_art(self, path: str) -> None:
        try:
            self.query_one("#art", Image).image = path
        except Exception:
            pass

    def _current_view_row(self) -> int | None:
        """Row in the visible table showing the track that is playing, if any.

        The queue keeps its own copy of the track list, so this matches on video
        id rather than object identity.
        """
        track = self.queue.current()
        if track is None or self.view_title != self.queue.source:
            return None
        idx = self.queue.current_index()
        if (
            idx is not None
            and 0 <= idx < len(self.view_tracks)
            and self.view_tracks[idx].video_id == track.video_id
        ):
            return idx
        for row, candidate in enumerate(self.view_tracks):
            if candidate.video_id == track.video_id:
                return row
        return None

    def _follow_playing_row(self) -> None:
        """Keep the table's selection on whatever track is now playing.

        Only does this while the visible list is the one that's actually
        playing - if the user has browsed off to a different playlist, their
        cursor there is left alone.
        """
        if self.playing_row is None:
            return
        table = self.query_one("#tracks", DataTable)
        if 0 <= self.playing_row < table.row_count:
            table.move_cursor(row=self.playing_row)

    def _mark_playing_row(self) -> None:
        table = self.query_one("#tracks", DataTable)
        for row in range(table.row_count):
            marker = "▶" if row == self.playing_row else ""
            try:
                table.update_cell_at(Coordinate(row, 0), marker)
            except Exception:
                break

    def _on_eof(self) -> None:
        """Called from mpv's reader thread when a track ends by itself."""
        try:
            self.call_from_thread(self._advance_auto)
        except Exception:
            pass

    def _advance_auto(self) -> None:
        if self._sleep_after_track:
            self._sleep_after_track = False
            self.notify("Sleep timer: stopped after the track.", timeout=6)
            return
        if self.queue.advance(manual=False) is not None:
            self._start_current()
        else:
            self.notify("End of queue", timeout=3)

    # -- periodic UI refresh ----------------------------------------------
    def _tick(self) -> None:
        if self.view_tracks or self.view_collections:
            table = self.query_one("#tracks", DataTable)
            if table.size.width != self._rendered_width:
                self._render_rows()

        self._tick_sleep()
        if (
            self._resume_pos is None
            and not self.player.paused
            and time.monotonic() - self._session_saved_at >= SESSION_SAVE_EVERY
        ):
            self._save_session()

        track = self.queue.current()
        position = self._resume_pos if self._resume_pos is not None else self.player.time_pos
        total = self.player.duration or (track.duration if track else 0)

        ratio = position / total if total else 0.0
        ratio = max(0.0, min(1.0, ratio))
        filled = int(ratio * BAR_WIDTH)
        bar = "[cyan]" + "━" * filled + "[/cyan][dim]" + "─" * (BAR_WIDTH - filled) + "[/dim]"
        self.query_one("#np-bar", Static).update(
            f"{bar}  {fmt_duration(position)} / {fmt_duration(total)}"
        )

        if track is None:
            state = "idle"
        elif self._resume_pos is not None:
            state = "space to resume"
        elif self.player.paused:
            state = "paused"
        elif self.player.duration == 0:
            state = "buffering…"
        else:
            state = "playing"

        bits = [
            state,
            f"vol {self.volume}%",
            f"speed {self.speed:g}x",
            f"random {'on' if self.queue.random else 'off'}",
            f"repeat {self.queue.repeat}",
        ]
        if self.queue.tracks:
            bits.append(f"{self.queue.pos + 1}/{len(self.queue.order)}  {self.queue.source}")
        sleep = self._sleep_label()
        if sleep:
            bits.append(f"sleep {sleep}")
        self.query_one("#np-status", Static).update("[dim]" + "   ·   ".join(bits) + "[/dim]")

        disc = self.query_one("#disc", Disc)
        disc.display = track is not None
        disc.spin(state == "playing")
        viz = self.query_one("#viz", Visualizer)
        viz.display = track is not None
        viz.spin(state == "playing")

    # -- sleep timer -------------------------------------------------------
    def _sleep_label(self) -> str:
        """What the status line says about the timer, or "" when it is off."""
        if self._sleep_after_track:
            return "after track"
        if self._sleep_deadline is not None:
            minutes, seconds = divmod(max(0, int(self._sleep_deadline - time.monotonic())), 60)
            return f"{minutes}:{seconds:02d}"
        return ""

    def _tick_sleep(self) -> None:
        if self._sleep_deadline is None:
            return
        remaining = self._sleep_deadline - time.monotonic()
        if remaining <= 0:
            self._sleep_deadline = None
            self._sleep_fading = False
            if not self.player.paused:
                self.player.toggle_pause()
            self.player.set_volume(self.volume)  # ready for when you press play
            self.notify("Sleep timer: paused. Goodnight.", timeout=8)
        elif remaining <= SLEEP_FADE_SECONDS:
            self._sleep_fading = True
            self.player.set_volume(int(self.volume * remaining / SLEEP_FADE_SECONDS))

    def _cancel_sleep(self) -> None:
        if self._sleep_fading:
            self.player.set_volume(self.volume)
        self._sleep_deadline = None
        self._sleep_after_track = False
        self._sleep_fading = False

    def action_sleep(self) -> None:
        self.push_screen(SleepScreen(self._sleep_label()), callback=self._set_sleep)

    def _set_sleep(self, choice: "int | str | None") -> None:
        if choice is None:
            return
        self._cancel_sleep()
        if choice == "track":
            self._sleep_after_track = True
            self.notify("Sleep timer: stopping after this track.", timeout=3)
        elif choice:
            self._sleep_deadline = time.monotonic() + int(choice) * 60
            self.notify(f"Sleep timer: {choice} minutes.", timeout=3)
        else:
            self.notify("Sleep timer off.", timeout=2)

    # -- queue editing -----------------------------------------------------
    def action_queue(self) -> None:
        if not self.queue.order:
            self.notify("The queue is empty. Play something first.", timeout=3)
            return
        self.push_screen(QueueScreen())

    def queue_play(self, position: int) -> None:
        if self.queue.jump_to(position) is not None:
            self._failures_in_a_row = 0
            self._start_current()

    def queue_remove(self, position: int) -> bool:
        if position == self.queue.pos:
            self.notify("That one is playing. Skip it first.", timeout=3)
            return False
        return self.queue.remove(position)

    def queue_move(self, position: int, delta: int) -> int:
        return self.queue.move(position, delta)

    def queue_clear_upcoming(self) -> None:
        gone = self.queue.clear_upcoming()
        self.notify(f"Cleared {gone} upcoming track{'s' if gone != 1 else ''}.", timeout=3)

    def action_play_next(self) -> None:
        table = self.query_one("#tracks", DataTable)
        row = table.cursor_row
        if not self.view_tracks or not (0 <= row < len(self.view_tracks)):
            return
        track = self.view_tracks[row]
        self.queue.play_next(track)
        self.notify(f"Plays next: {track.title}", timeout=2)

    # -- actions -----------------------------------------------------------
    def action_play_pause(self) -> None:
        if self._resume_pos is not None and self.queue.current() is not None:
            self._start_current(self._resume_pos)
            return
        if self.queue.current() is None:
            table = self.query_one("#tracks", DataTable)
            self._play_from_view(table.cursor_row)
            return
        self.player.toggle_pause()

    def action_next(self) -> None:
        if self.queue.advance(manual=True) is not None:
            self._start_current()

    def action_previous(self) -> None:
        if self.player.time_pos > 5:
            self.player.seek_percent(0)
            return
        if self.queue.previous() is not None:
            self._start_current()

    def action_seek_forward(self) -> None:
        self.player.seek(5)

    def action_seek_back(self) -> None:
        self.player.seek(-5)

    def action_seek_forward_big(self) -> None:
        self.player.seek(30)

    def action_seek_back_big(self) -> None:
        self.player.seek(-30)

    def action_volume_up(self) -> None:
        self.volume = self.player.set_volume(self.volume + 5)

    def action_volume_down(self) -> None:
        self.volume = self.player.set_volume(self.volume - 5)

    def action_random(self) -> None:
        self.queue.set_random(not self.queue.random)
        self.notify(f"Random {'on' if self.queue.random else 'off'}", timeout=2)

    def action_repeat(self) -> None:
        self.notify(f"Repeat {self.queue.cycle_repeat()}", timeout=2)

    def action_speed(self) -> None:
        idx = SPEED_PRESETS.index(self.speed) if self.speed in SPEED_PRESETS else 0
        self.speed = SPEED_PRESETS[(idx + 1) % len(SPEED_PRESETS)]
        self.player.set_speed(self.speed)
        self.notify(f"Speed {self.speed:g}x", timeout=2)

    def action_stop(self) -> None:
        self.player.stop()
        self.queue.pos = -1
        self._resume_pos = None
        self.playing_row = None
        self._mark_playing_row()
        self.query_one("#np-title", Static).update("Nothing playing")
        self.query_one("#np-artist", Static).update("")

    def action_reload(self) -> None:
        self.notify("Reloading library…", timeout=2)
        self._connect_library()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_theme(self) -> None:
        self.push_screen(ThemeScreen(self.theme), callback=self._set_theme)

    def _set_theme(self, name: str | None) -> None:
        if not name:
            return
        self.theme = name
        self.settings["theme"] = name
        config.save_settings(self.settings)
        self.notify(f"Theme: {themes.label(name)}", timeout=2)

    def action_search(self) -> None:
        self.push_screen(SearchScreen(self.search_filter), callback=self._run_search)

    def _run_search(self, result: "tuple[str, str] | None") -> None:
        if not result:
            return
        query, kind = result
        self.search_filter = kind
        self.query_one("#tracks-title", Static).update(
            f"Search: {query}  [dim]searching…[/dim]"
        )
        self._search_worker(query, kind)

    @work(thread=True, group="tracks", exclusive=True)
    def _search_worker(self, query: str, kind: str) -> None:
        if self.library is None:
            return
        label = dict(SEARCH_FILTERS)[kind]
        title = f"Search: {query}" + ("" if kind == "all" else f" ({label})")
        try:
            if kind in _TRACK_SEARCH_KINDS:
                tracks = self.library.search(query, kinds=_TRACK_SEARCH_KINDS[kind])
                collections = None
            else:
                tracks = None
                collections = self.library.search_collections(query, kind)
        except Exception as exc:
            self.call_from_thread(self.notify, f"Search failed: {exc}", severity="error")
            return
        if collections is not None:
            self.call_from_thread(self._show_collections, collections, title)
        else:
            self.call_from_thread(self._show_tracks, tracks, title)

    def action_append(self) -> None:
        table = self.query_one("#tracks", DataTable)
        row = table.cursor_row
        if not self.view_tracks or not (0 <= row < len(self.view_tracks)):
            return
        track = self.view_tracks[row]
        self.queue.append(track)
        self.notify(f"Queued: {track.title}", timeout=2)

    def action_add_to_playlist(self) -> None:
        table = self.query_one("#tracks", DataTable)
        row = table.cursor_row
        if not self.view_tracks or not (0 <= row < len(self.view_tracks)):
            return
        track = self.view_tracks[row]
        if track.local_path or self.library is None:
            self.notify("Only YouTube Music tracks can be added to playlists.", timeout=3)
            return
        owned = [p for p in self.playlists if p.kind in ("playlist", "mix")]

        def chosen(choice: "Playlist | str | None") -> None:
            if choice == "new":
                self.push_screen(
                    NewPlaylistScreen(),
                    callback=lambda name: name and self._playlist_worker(track, None, name),
                )
            elif choice is not None:
                self._playlist_worker(track, choice, "")

        self.push_screen(PlaylistPickScreen(track.title, owned), callback=chosen)

    @work(thread=True, group="playlist")
    def _playlist_worker(self, track: Track, playlist: Playlist | None, name: str) -> None:
        try:
            if playlist is None:
                pid = self.library.yt.create_playlist(
                    name, "Created from ytm-tui", video_ids=[track.video_id]
                )
                if not isinstance(pid, str):
                    raise RuntimeError(str(pid))
                msg = f'Created "{name}" with "{track.title}".'
            else:
                self.library.yt.add_playlist_items(playlist.id, [track.video_id])
                playlist.loaded = False  # refetch next time it's opened
                msg = f'Added "{track.title}" to {playlist.title}.'
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"Add to playlist failed: {exc}", severity="error", timeout=8
            )
            return
        self.call_from_thread(self.notify, msg, timeout=4)
        if playlist is None:
            self.call_from_thread(self.action_reload)

    def action_download(self) -> None:
        table = self.query_one("#tracks", DataTable)
        row = table.cursor_row
        if not self.view_tracks or not (0 <= row < len(self.view_tracks)):
            return
        track = self.view_tracks[row]
        if track.local_path:
            self.notify("Already a local file.", timeout=2)
            return
        self.push_screen(
            DownloadScreen(track.title, str(config.DOWNLOADS_DIR)),
            callback=lambda dest: self._start_download(track, dest),
        )

    def _start_download(self, track: Track, dest: str | None) -> None:
        if not dest:
            return
        self._set_download_progress(track.title, None)
        self._download_worker(track, Path(dest).expanduser())

    @work(thread=True, group="download")
    def _download_worker(self, track: Track, dest_dir: Path) -> None:
        base = local.sanitize_filename(f"{track.artist} - {track.title}")
        last_sent = 0.0

        def on_progress(percent: float | None, speed: float | None) -> None:
            nonlocal last_sent
            # yt-dlp fires this many times a second; a UI refresh that often
            # is wasted work. 100% (or an unknown total) always gets through.
            now = time.monotonic()
            if percent is not None and 0 < percent < 100 and now - last_sent < 0.25:
                return
            last_sent = now
            self.call_from_thread(self._set_download_progress, track.title, percent)

        try:
            self.resolver.download(
                track.video_id, dest_dir, base, on_progress=on_progress, thumb_url=track.thumb
            )
        except Exception as exc:
            self.call_from_thread(self._clear_download_progress, track.title)
            self.call_from_thread(
                self.notify,
                f'Download failed for "{track.title}": {exc}',
                severity="error",
                timeout=10,
            )
            return
        self.call_from_thread(self._clear_download_progress, track.title)
        self.call_from_thread(
            self.notify, f'Saved "{track.title}" to {dest_dir}.', timeout=5
        )
        if self.view_title == "Downloads" and dest_dir.resolve() == config.DOWNLOADS_DIR.resolve():
            self.call_from_thread(self._scan_downloads_worker)

    def action_lyrics(self) -> None:
        track = self.queue.current()
        if track is None:
            self.notify("Nothing playing.", timeout=3)
            return
        screen = LyricsScreen(f"[b]{track.title}[/b]  [dim]{track.artist}[/dim]")
        self.push_screen(screen)
        self._lyrics_worker(track.artist, track.title, screen)

    @work(thread=True, group="lyrics", exclusive=True)
    def _lyrics_worker(self, artist: str, title: str, screen: LyricsScreen) -> None:
        worker = get_current_worker()
        try:
            text, display = lyrics.fetch(artist, title)
        except lyrics.LyricsError as exc:
            if worker.is_cancelled or not screen.is_attached:
                return
            self.call_from_thread(screen.set_text, f"[b]{title}[/b]", f"[dim]{exc}[/dim]")
            return
        except Exception as exc:
            if worker.is_cancelled or not screen.is_attached:
                return
            self.call_from_thread(
                screen.set_text, f"[b]{title}[/b]", f"[dim]Lyrics lookup failed: {exc}[/dim]"
            )
            return
        if worker.is_cancelled or not screen.is_attached:
            return
        self.call_from_thread(screen.set_text, f"[b]{display}[/b]", text)

    def _set_download_progress(self, title: str, percent: float | None) -> None:
        self._downloads[title] = percent
        self._render_download_status()

    def _clear_download_progress(self, title: str) -> None:
        self._downloads.pop(title, None)
        self._render_download_status()

    def _render_download_status(self) -> None:
        widget = self.query_one("#download-status", Static)
        if not self._downloads:
            widget.display = False
            return
        parts = []
        for title, percent in self._downloads.items():
            pct_text = f"{percent:4.0f}%" if percent is not None else " …  "
            parts.append(f"⬇ [b]{pct_text}[/b] {title}")
        widget.update("\n".join(parts))
        widget.display = True


def main() -> None:
    config.ensure_dirs()
    YtmTui().run()
