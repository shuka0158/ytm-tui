"""The Textual UI."""
from __future__ import annotations

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)
from textual.worker import get_current_worker
from textual_image.widget import Image

from . import art, config
from .api import AuthMissing, Library
from .models import Playlist, Track, fmt_duration
from .player import MpvPlayer
from .queue import PlayQueue
from .resolver import Resolver

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
  [cyan]R[/cyan]          start a radio from the highlighted track
  [cyan]a[/cyan]          append highlighted track to the queue
  [cyan]F5[/cyan]         reload playlists
  [cyan]tab[/cyan]        move between panes

  [cyan]?[/cyan] help     [cyan]q[/cyan] quit"""

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(self.HELP)

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
        ("s", "shuffle", "Shuffle"),
        ("r", "repeat", "Repeat"),
        ("x", "stop", "Stop"),
        ("slash", "search", "Search"),
        ("R", "radio", "Radio"),
        ("a", "append", "Queue"),
        ("f5,ctrl+r", "reload", "Reload"),
        ("question_mark", "help", "Help"),
        ("q,ctrl+c", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.settings = config.load_settings()
        self.library: Library | None = None
        self.player = MpvPlayer(on_eof=self._on_eof)
        self.resolver = Resolver(self.settings.get("format"))
        self.queue = PlayQueue()
        self.queue.shuffle = bool(self.settings.get("shuffle", False))
        self.queue.repeat = self.settings.get("repeat", "off")
        self.volume = int(self.settings.get("volume", 80))
        self.playlists: list[Playlist] = []
        self.view_tracks: list[Track] = []
        self.view_title: str = ""
        self.playing_row: int | None = None
        self._rendered_width: int = 0
        self._col_keys: dict[str, object] = {}

    # -- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Static("Playlists", classes="pane-title")
                yield ListView(id="playlists")
            with Vertical(id="content"):
                yield Static("", id="tracks-title", classes="pane-title")
                yield DataTable(id="tracks", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="now"):
            yield Image(id="art")
            with Vertical(id="np"):
                yield Static("Nothing playing", id="np-title")
                yield Static("", id="np-artist")
                yield Static("", id="np-bar")
                yield Static("", id="np-status")
        yield Footer()

    def on_mount(self) -> None:
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

        try:
            self.player.start(volume=self.volume)
        except Exception as exc:
            self.notify(f"mpv failed to start: {exc}", severity="error", timeout=12)

        self._apply_sidebar_width()
        self.set_interval(0.25, self._tick)
        self._connect_library()

    def on_unmount(self) -> None:
        self.settings.update(
            volume=self.volume,
            shuffle=self.queue.shuffle,
            repeat=self.queue.repeat,
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
        view.clear()
        for playlist in playlists:
            label = f"[b]{playlist.title}[/b]"
            if playlist.subtitle:
                label += f"\n[dim]{playlist.subtitle}[/dim]"
            view.append(ListItem(Label(label)))
        if playlists:
            view.index = 0
            self._open_playlist(playlists[0])

    def _open_playlist(self, playlist: Playlist) -> None:
        self.view_title = playlist.title
        self.query_one("#tracks-title", Static).update(f"{playlist.title}  [dim]loading…[/dim]")
        self._load_tracks(playlist)

    @work(thread=True, group="tracks", exclusive=True)
    def _load_tracks(self, playlist: Playlist) -> None:
        if self.library is None:
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
        self.call_from_thread(self._show_tracks, tracks, playlist.title)

    def _show_tracks(self, tracks: list[Track], title: str) -> None:
        self.view_tracks = tracks
        self.view_title = title
        self._render_rows(cursor=0)
        self.query_one("#tracks-title", Static).update(
            f"{title}  [dim]{len(tracks)} tracks[/dim]"
        )

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
        if index is not None and 0 <= index < len(self.playlists):
            self._open_playlist(self.playlists[index])
            self.query_one("#tracks", DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._play_from_view(event.cursor_row)

    # -- playback ----------------------------------------------------------
    def _play_from_view(self, index: int) -> None:
        if not self.view_tracks or not (0 <= index < len(self.view_tracks)):
            return
        self.queue.load(self.view_tracks, start=index, source=self.view_title)
        self._start_current()

    def _start_current(self) -> None:
        track = self.queue.current()
        if track is None:
            return
        self._set_now_playing(track)
        self._resolve_and_play(track)

    @work(thread=True, group="play", exclusive=True)
    def _resolve_and_play(self, track: Track) -> None:
        worker = get_current_worker()
        try:
            url, headers = self.resolver.resolve(track.video_id)
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"Cannot play “{track.title}”: {exc}", severity="error"
            )
            self.call_from_thread(self._skip_after_failure)
            return
        if worker.is_cancelled:
            return
        try:
            self.player.play_url(url, headers)
        except Exception as exc:
            self.call_from_thread(self.notify, f"mpv: {exc}", severity="error")
            return
        upcoming = self.queue.peek_next()
        if upcoming is not None:
            self.resolver.prefetch(upcoming.video_id)

    def _skip_after_failure(self) -> None:
        if self.queue.advance(manual=True) is not None:
            self._start_current()

    def _set_now_playing(self, track: Track) -> None:
        self.query_one("#np-title", Static).update(f"[b]{track.title}[/b]")
        subtitle = track.artist + (f"  ·  {track.album}" if track.album else "")
        self.query_one("#np-artist", Static).update(f"[dim]{subtitle}[/dim]")
        self.playing_row = self._current_view_row()
        self._mark_playing_row()
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

    def action_radio(self) -> None:
        table = self.query_one("#tracks", DataTable)
        row = table.cursor_row
        if not self.view_tracks or not (0 <= row < len(self.view_tracks)):
            return
        track = self.view_tracks[row]
        self.query_one("#tracks-title", Static).update(
            f"Radio: {track.title}  [dim]building…[/dim]"
        )
        self._radio_worker(track)

    @work(thread=True, group="tracks", exclusive=True)
    def _radio_worker(self, track: Track) -> None:
        if self.library is None:
            return
        try:
            tracks = self.library.radio(track.video_id)
        except Exception as exc:
            self.call_from_thread(self.notify, f"Radio failed: {exc}", severity="error")
            return
        self.call_from_thread(self._show_tracks, tracks, f"Radio: {track.title}")
        self.call_from_thread(self._play_from_view, 0)

    def action_append(self) -> None:
        table = self.query_one("#tracks", DataTable)
        row = table.cursor_row
        if not self.view_tracks or not (0 <= row < len(self.view_tracks)):
            return
        track = self.view_tracks[row]
        self.queue.append(track)
        self.notify(f"Queued: {track.title}", timeout=2)


def main() -> None:
    config.ensure_dirs()
    YtmTui().run()
