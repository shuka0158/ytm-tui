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

"""mpv driven over its JSON IPC socket.

We deliberately do not use libmpv bindings: mpv is spawned as a plain
subprocess with --input-ipc-server and talked to over a unix socket, so the
only runtime requirement is the mpv binary being on PATH.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable


class MpvError(RuntimeError):
    pass


# Centre frequencies (Hz) of the visualizer bands, roughly log-spaced.
BANDS = (60, 100, 160, 250, 400, 630, 1000, 1600, 2500, 4000, 6300, 10000)
_RMS_KEY = "lavfi.astats.Overall.RMS_level"


def _band_graph(paths: list[str]) -> str:
    """lavfi-complex graph: pass the audio through untouched and also feed a
    copy through one band-pass + level meter per band, each printing its RMS to
    its own FIFO. Measured before the volume, so the bars ignore the volume."""
    n = len(paths)
    outs = "".join(f"[b{i}]" for i in range(n))
    parts = [f"[aid1]asplit={n + 1}[ao]{outs}"]
    for i, (hz, path) in enumerate(zip(BANDS, paths)):
        parts.append(
            f"[b{i}]bandpass=f={hz}:width_type=o:w=1,asetnsamples=n=1024,"
            f"astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
            f"ametadata=print:key={_RMS_KEY}:file={path}:direct=1,anullsink"
        )
    return ";".join(parts)


class MpvPlayer:
    """Blocking-but-thread-safe controller for one mpv instance."""

    def __init__(self, on_eof: Callable[[], None] | None = None) -> None:
        self.on_eof = on_eof
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._path = os.path.join(
            tempfile.gettempdir(), f"ytmtui-mpv-{os.getpid()}.sock"
        )
        self._lock = threading.Lock()
        self._req_id = 0
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self._reader: threading.Thread | None = None
        self._closing = False

        # Latest values pushed by mpv's property observers. Read these from the
        # UI thread instead of doing IPC round-trips on every repaint.
        self.time_pos: float = 0.0
        self.duration: float = 0.0
        self.paused: bool = True
        self.idle: bool = True
        self.loading: bool = False
        self._start_at: float = 0.0

        # Audio levels for the visualizer: RMS in dB per frequency band, taken
        # from the same stream mpv is playing (see _band_graph). `analysis` is
        # False if this mpv could not run the analysis graph.
        self.analysis: bool = False
        self.levels: list[float] = [-90.0] * len(BANDS)
        self.levels_at: float = 0.0
        self._fifos: list[str] = []
        self._fifo_fds: list[tuple[int, int]] = []

    # -- lifecycle ---------------------------------------------------------
    def start(self, volume: int = 80) -> None:
        if shutil.which("mpv") is None:
            raise MpvError("mpv is not installed or not on PATH")

        if os.path.exists(self._path):
            os.unlink(self._path)

        self._open_fifos()
        args = self._mpv_args(volume, analysis=True)
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        # An mpv build without the analysis filters dies at once; retry plain.
        time.sleep(0.4)
        if self._proc.poll() is not None:
            self._close_fifos()
            self._proc = subprocess.Popen(
                self._mpv_args(volume, analysis=False),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        else:
            self.analysis = True
            threading.Thread(target=self._level_loop, daemon=True).start()
        self._connect()

    def _mpv_args(self, volume: int, analysis: bool) -> list[str]:
        args = [
                "mpv",
                "--idle=yes",
                "--no-video",
                "--no-terminal",
                "--really-quiet",
                "--audio-display=no",
                "--gapless-audio=yes",
                "--cache=yes",
                "--demuxer-max-bytes=64MiB",
                "--demuxer-readahead-secs=30",
                f"--volume={volume}",
                f"--input-ipc-server={self._path}",
        ]
        if analysis:
            args.append(f"--lavfi-complex={_band_graph(self._fifos)}")
        return args

    def _connect(self) -> None:
        deadline = time.time() + 10
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise MpvError("mpv exited immediately")
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self._path)
                self._sock = sock
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                time.sleep(0.05)
        else:
            raise MpvError("timed out waiting for the mpv IPC socket")

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        for pid, name in (
            (1, "time-pos"),
            (2, "duration"),
            (3, "pause"),
            (4, "core-idle"),
        ):
            self._send(["observe_property", pid, name], wait=False)

    # -- level analysis ----------------------------------------------------
    def _open_fifos(self) -> None:
        self._fifos = []
        self._fifo_fds = []
        try:
            for i in range(len(BANDS)):
                path = os.path.join(tempfile.gettempdir(), f"ytmtui-mpv-{os.getpid()}-b{i}")
                if os.path.exists(path):
                    os.unlink(path)
                os.mkfifo(path)
                self._fifos.append(path)
                # Hold both ends: the read end is ours, and keeping a write end
                # open stops the reader seeing EOF before mpv connects.
                rfd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                wfd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
                self._fifo_fds.append((rfd, wfd))
        except OSError:
            self._close_fifos()
            raise MpvError("could not create the level-meter pipes")

    def _close_fifos(self) -> None:
        for rfd, wfd in self._fifo_fds:
            for fd in (rfd, wfd):
                try:
                    os.close(fd)
                except OSError:
                    pass
        for path in self._fifos:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._fifos, self._fifo_fds = [], []

    def _level_loop(self) -> None:
        fds = {rfd: i for i, (rfd, _) in enumerate(self._fifo_fds)}
        while not self._closing:
            try:
                ready, _, _ = select.select(list(fds), [], [], 0.25)
            except (OSError, ValueError):
                return
            for fd in ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    continue
                for line in data.decode(errors="ignore").splitlines():
                    if line.startswith(_RMS_KEY + "="):
                        try:
                            db = float(line.split("=", 1)[1])
                        except ValueError:
                            db = -90.0
                        self.levels[fds[fd]] = max(-90.0, db)
                        self.levels_at = time.monotonic()

    def close(self) -> None:
        self._closing = True
        try:
            self._send(["quit"], wait=False)
        except Exception:
            pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if os.path.exists(self._path):
            try:
                os.unlink(self._path)
            except OSError:
                pass
        self._close_fifos()

    # -- IPC plumbing ------------------------------------------------------
    def _send(self, command: list[Any], wait: bool = True, timeout: float = 5.0) -> Any:
        if self._sock is None:
            raise MpvError("mpv is not running")

        with self._lock:
            self._req_id += 1
            req_id = self._req_id
            payload = json.dumps({"command": command, "request_id": req_id}) + "\n"
            if wait:
                self._pending[req_id] = (threading.Event(), [])
            try:
                self._sock.sendall(payload.encode())
            except OSError as exc:
                self._pending.pop(req_id, None)
                raise MpvError(f"mpv connection lost: {exc}") from exc

        if not wait:
            return None

        event, box = self._pending[req_id]
        if not event.wait(timeout):
            self._pending.pop(req_id, None)
            raise MpvError(f"mpv did not answer {command[0]!r} in time")
        self._pending.pop(req_id, None)
        return box[0] if box else None

    def _read_loop(self) -> None:
        buf = b""
        while not self._closing and self._sock is not None:
            try:
                chunk = self._sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        if "request_id" in msg:
            slot = self._pending.get(msg["request_id"])
            if slot is not None:
                event, box = slot
                box.append(msg.get("data"))
                event.set()
            return

        event = msg.get("event")
        if event == "property-change":
            name, value = msg.get("name"), msg.get("data")
            if name == "time-pos" and value is not None:
                self.time_pos = float(value)
            elif name == "duration" and value is not None:
                self.duration = float(value)
            elif name == "pause":
                self.paused = bool(value)
            elif name == "core-idle":
                self.idle = bool(value)
        elif event == "file-loaded":
            self.loading = False
            # Seeking once loaded works on every mpv; the loadfile options
            # argument moved between versions.
            if self._start_at > 0:
                start, self._start_at = self._start_at, 0.0
                self._send(["seek", start, "absolute"], wait=False)
        elif event == "end-file":
            # "stop" fires when we replace the file ourselves; only real
            # end-of-stream should advance the queue.
            if msg.get("reason") == "eof":
                self.time_pos = 0.0
                if self.on_eof is not None:
                    self.on_eof()

    # -- transport ---------------------------------------------------------
    def play_url(
        self, url: str, headers: dict[str, str] | None = None, start: float = 0.0
    ) -> None:
        """Load a file. `start` is a position to seek to once it has loaded."""
        self._start_at = start
        if headers:
            fields = [f"{k}: {v}" for k, v in headers.items() if k.lower() != "range"]
            self._send(["set_property", "http-header-fields", fields], wait=False)
        else:
            self._send(["set_property", "http-header-fields", []], wait=False)
        self.time_pos = start
        self.duration = 0.0
        self.loading = True
        self._send(["loadfile", url, "replace"], wait=False)
        self._send(["set_property", "pause", False], wait=False)
        self.paused = False

    def stop(self) -> None:
        self._send(["stop"], wait=False)
        self.time_pos = 0.0
        self.duration = 0.0
        self.paused = True

    def toggle_pause(self) -> bool:
        self.paused = not self.paused
        self._send(["set_property", "pause", self.paused], wait=False)
        return self.paused

    def seek(self, delta: float) -> None:
        self._send(["seek", delta, "relative"], wait=False)

    def seek_percent(self, percent: float) -> None:
        self._send(["seek", max(0.0, min(100.0, percent)), "absolute-percent"], wait=False)

    def set_volume(self, volume: int) -> int:
        volume = max(0, min(130, volume))
        self._send(["set_property", "volume", volume], wait=False)
        return volume

    def set_speed(self, speed: float) -> float:
        speed = max(0.25, min(3.0, speed))
        self._send(["set_property", "speed", speed], wait=False)
        return speed
