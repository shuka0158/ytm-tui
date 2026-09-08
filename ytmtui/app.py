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
from textual.containers import Horizontal, Vertical
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

from . import art, config, cookies, local, themes
from .api import AuthMissing, Library
from .models import Playlist, Track, fmt_duration
from .player import MpvPlayer
from .queue import PlayQueue
from .resolver import JS_RUNTIME_HINT, Resolver, TrackBlocked, missing_js_runtime

BAR_WIDTH = 44


class SearchScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="search-box"):
            yield Label("Search YouTube Music")
            yield Input(placeholder="song, artist, album…", id="search-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

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
            yield Label(f"Download “{self.track_title}” to…")
            yield Input(value=self.default_dir, id="search-input")

    def on_mount(self) -> None:
        input_ = self.query_one(Input)
        input_.focus()
        input_.cursor_position = len(input_.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or self.default_dir)

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
    BINDINGS = [("escape,question_mark,q", "cancel", "Close")]

    HELP = """[b]Playback[/b]
  [cyan]enter[/cyan]      play highlighted track
  [cyan]space[/cyan]      play / pause
  [cyan]n[/cyan] / [cyan]p[/cyan]      next / previous track
  [cyan]←[/cyan] / [cyan]→[/cyan]      seek 5s      [cyan]shift+←/→[/cyan] seek 30s
  [cyan]+[/cyan] / [cyan]-[/cyan]      volume
  [cyan]s[/cyan]          shuffle       [cyan]r[/cyan]  repeat off/all/one
  [cyan]x[/cyan]          stop

[b]Library[/b]
  [cyan]/[/cyan]          search YouTube Music
  [cyan]a[/cyan]          append highlighted track to the queue
  [cyan]d[/cyan]          download highlighted track (asks for a folder)
  [cyan]F5[/cyan]         reload playlists
  [cyan]t[/cyan]          theme picker
  [cyan]tab[/cyan]        move between panes

[b]Local & Downloads[/b]
  [cyan]Local[/cyan]      browse audio files from your local music folders
                [cyan]Import Folder…[/cyan] button adds another folder to scan
  [cyan]Downloads[/cyan]  tracks saved from ytm-tui with [cyan]d[/cyan]

  [cyan]?[/cyan] help     [cyan]q[/cyan] quit

[dim]ytm-tui — Copyright (C) 2026 shuka0158 — GPLv3 or later.
This is free software with ABSOLUTELY NO WARRANTY; you are free to
change and redistribute it. See the LICENSE file for details.[/dim]"""

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(self.HELP)

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
        ("s", "shuffle", "Shuffle"),
        ("r", "repeat", "Repeat"),
        ("x", "stop", "Stop"),
        ("slash", "search", "Search"),
        ("a", "append", "Queue"),
        ("d", "download", "Download"),
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
        self.queue.shuffle = bool(self.settings.get("shuffle", False))
        self.queue.repeat = self.settings.get("repeat", "off")
        self.volume = int(self.settings.get("volume", 80))
        self.playlists: list[Playlist] = []
        # Parallel to the "#playlists" ListView's children: what each row opens.
        # ("local", None) | ("downloads", None) | ("playlist", Playlist)
        self._sidebar_kinds: list[tuple[str, Playlist | None]] = []
        self.view_tracks: list[Track] = []
        self.view_title: str = ""
        self.playing_row: int | None = None
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
        self._connect_library()

    def on_unmount(self) -> None:
        self.settings.update(
            volume=self.volume,
            shuffle=self.queue.shuffle,
            repeat=self.queue.repeat,
            theme=self.theme,
        )
        config.save_settings(self.settings)
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

    def _show_tracks(self, tracks: list[Track], title: str) -> None:
        self.view_tracks = tracks
        self.view_title = title
        self._render_rows(cursor=0)
        self.query_one("#tracks-title", Static).update(
            f"{title}  [dim]{len(tracks)} tracks[/dim]"
        )
        self.query_one("#import-folder-btn", Button).display = title == "Local"

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
        for i, track in enumerate(self.view_tracks, start=1):
            table.add_row(
                "",
                str(i),
                self._clip(track.title, title_w),
                self._clip(track.artist, artist_w),
                self._clip(track.album, album_w),
                fmt_duration(track.duration),
            )
        self.playing_row = self._current_view_row()
        self._mark_playing_row()
        if self.view_tracks:
            table.move_cursor(row=max(0, min(keep, len(self.view_tracks) - 1)))

    def on_resize(self, event) -> None:
        # Child widgets still report the old size during this event, so use the
        # size the event carries and redraw once layout has settled. The width
        # check in _tick catches anything missed.
        self._apply_sidebar_width(event.size.width)
        if self.view_tracks:
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
        self._play_from_view(event.cursor_row)

    # -- playback ----------------------------------------------------------
    def _play_from_view(self, index: int) -> None:
        if not self.view_tracks or not (0 <= index < len(self.view_tracks)):
            return
        self.queue.load(self.view_tracks, start=index, source=self.view_title)
        self._failures_in_a_row = 0  # a deliberate choice deserves a fresh run
        self._start_current()

    def _start_current(self) -> None:
        track = self.queue.current()
        if track is None:
            return
        self._set_now_playing(track)
        if track.local_path:
            self._play_local(track)
        else:
            self._resolve_and_play(track)

    @work(thread=True, group="play", exclusive=True)
    def _play_local(self, track: Track) -> None:
        try:
            self.player.play_url(track.local_path)
        except Exception as exc:
            self.call_from_thread(self.notify, f"mpv: {exc}", severity="error")
            return
        self._failures_in_a_row = 0
        upcoming = self.queue.peek_next()
        if upcoming is not None and not upcoming.local_path:
            self.resolver.prefetch(upcoming.video_id)

    @work(thread=True, group="play", exclusive=True)
    def _resolve_and_play(self, track: Track) -> None:
        worker = get_current_worker()
        try:
            url, headers = self.resolver.resolve(track.video_id)
        except TrackBlocked as exc:
            # This upload is barred, but the song itself may exist elsewhere.
            swap = self._resolve_alternate(track, worker)
            if swap is None:
                self.call_from_thread(
                    self.notify, f"Cannot play “{track.title}”: {exc}", severity="error"
                )
                self.call_from_thread(self._skip_after_failure)
                return
            alternate, url, headers = swap
            self.call_from_thread(
                self.notify,
                f"“{track.title}” is blocked - playing another upload: "
                f"“{alternate.title}”",
                severity="warning",
                timeout=8,
            )
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"Cannot play “{track.title}”: {exc}", severity="error"
            )
            self.call_from_thread(self._skip_after_failure)
            return
        if worker.is_cancelled:
            return
        self._failures_in_a_row = 0
        try:
            self.player.play_url(url, headers)
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

    @work(thread=True, group="art", exclusive=True)
    def _load_art(self, url: str) -> None:
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
        if self.queue.advance(manual=False) is not None:
            self._start_current()
        else:
            self.notify("End of queue", timeout=3)

    # -- periodic UI refresh ----------------------------------------------
    def _tick(self) -> None:
        if self.view_tracks:
            table = self.query_one("#tracks", DataTable)
            if table.size.width != self._rendered_width:
                self._render_rows()

        track = self.queue.current()
        position = self.player.time_pos
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
        elif self.player.paused:
            state = "paused"
        elif self.player.duration == 0:
            state = "buffering…"
        else:
            state = "playing"

        bits = [
            state,
            f"vol {self.volume}%",
            f"shuffle {'on' if self.queue.shuffle else 'off'}",
            f"repeat {self.queue.repeat}",
        ]
        if self.queue.tracks:
            bits.append(f"{self.queue.pos + 1}/{len(self.queue.order)}  {self.queue.source}")
        self.query_one("#np-status", Static).update("[dim]" + "   ·   ".join(bits) + "[/dim]")

    # -- actions -----------------------------------------------------------
    def action_play_pause(self) -> None:
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

    def action_shuffle(self) -> None:
        self.queue.set_shuffle(not self.queue.shuffle)
        self.notify(f"Shuffle {'on' if self.queue.shuffle else 'off'}", timeout=2)

    def action_repeat(self) -> None:
        self.notify(f"Repeat {self.queue.cycle_repeat()}", timeout=2)

    def action_stop(self) -> None:
        self.player.stop()
        self.queue.pos = -1
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
        self.push_screen(SearchScreen(), callback=self._run_search)

    def _run_search(self, query: str | None) -> None:
        if query:
            self.query_one("#tracks-title", Static).update(
                f"Search: {query}  [dim]searching…[/dim]"
            )
            self._search_worker(query)

    @work(thread=True, group="tracks", exclusive=True)
    def _search_worker(self, query: str) -> None:
        if self.library is None:
            return
        try:
            tracks = self.library.search(query)
        except Exception as exc:
            self.call_from_thread(self.notify, f"Search failed: {exc}", severity="error")
            return
        self.call_from_thread(self._show_tracks, tracks, f"Search: {query}")

    def action_append(self) -> None:
        table = self.query_one("#tracks", DataTable)
        row = table.cursor_row
        if not self.view_tracks or not (0 <= row < len(self.view_tracks)):
            return
        track = self.view_tracks[row]
        self.queue.append(track)
        self.notify(f"Queued: {track.title}", timeout=2)

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
            self.resolver.download(track.video_id, dest_dir, base, on_progress=on_progress)
        except Exception as exc:
            self.call_from_thread(self._clear_download_progress, track.title)
            self.call_from_thread(
                self.notify,
                f"Download failed for “{track.title}”: {exc}",
                severity="error",
                timeout=10,
            )
            return
        self.call_from_thread(self._clear_download_progress, track.title)
        self.call_from_thread(
            self.notify, f"Saved “{track.title}” to {dest_dir}.", timeout=5
        )
        if self.view_title == "Downloads" and dest_dir.resolve() == config.DOWNLOADS_DIR.resolve():
            self.call_from_thread(self._scan_downloads_worker)

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
