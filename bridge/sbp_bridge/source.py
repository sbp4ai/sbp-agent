"""会话源：pty（本地进程）与 tmux（抓已存在的会话）。"""
from __future__ import annotations

import asyncio
import os
import pty
import select
import signal
import subprocess
import sys
from typing import Callable

OnData = Callable[[bytes], None]


class PtySource:
    """在 pty 里跑一个进程，把字节流回调出来。"""

    kind = "pty"

    def __init__(self, argv: list[str], on_data: OnData, cwd: str | None = None,
                 env: dict | None = None, cols: int = 100, rows: int = 40):
        self.argv = argv
        self.on_data = on_data
        self.cwd = cwd or os.getcwd()
        self.env = {**os.environ, **(env or {}), "COLUMNS": str(cols), "LINES": str(rows)}
        self.cols, self.rows = cols, rows
        self.pid: int | None = None
        self.fd: int | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(self.cwd)
                os.execvpe(self.argv[0], self.argv, self.env)
            except Exception:
                os._exit(127)
        self.pid, self.fd = pid, fd
        import fcntl
        import struct
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))
        loop.add_reader(fd, self._readable)

    def _readable(self) -> None:
        try:
            data = os.read(self.fd, 65536)  # type: ignore[arg-type]
        except OSError:
            self.close()
            return
        if not data:
            self.close()
            return
        self.on_data(data)

    def write(self, text: str) -> None:
        if self.fd is None:
            return
        try:
            os.write(self.fd, text.encode())
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        if self.fd is None:
            return
        import fcntl
        import struct
        import termios
        self.cols, self.rows = cols, rows
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.loop and self.fd is not None:
            try:
                self.loop.remove_reader(self.fd)
            except Exception:
                pass
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGHUP)
            except OSError:
                pass
            self.pid = None

    @property
    def alive(self) -> bool:
        return not self._closed


class TmuxSource:
    """轮询 tmux capture-pane，适合接管已经存在的 agent 会话。"""

    kind = "tmux"

    def __init__(self, target: str, on_data: OnData, rows: int = 40, interval_ms: int = 500):
        self.target = target
        self.on_data = on_data
        self.rows = rows
        self.interval = interval_ms / 1000
        self._task: asyncio.Task | None = None
        self._last = ""
        self._closed = False

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._task = loop.create_task(self._poll())

    async def _poll(self) -> None:
        while not self._closed:
            try:
                out = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-t", self.target, "-S", f"-{self.rows}"],
                    capture_output=True, timeout=3,
                )
                text = out.stdout.decode("utf-8", "replace")
                if text != self._last:
                    self._last = text
                    self.on_data(("\r" + text).encode())
            except Exception:
                pass
            await asyncio.sleep(self.interval)

    def write(self, text: str) -> None:
        subprocess.run(["tmux", "send-keys", "-t", self.target, "-l", text], capture_output=True)
        if text.endswith("\r"):
            subprocess.run(["tmux", "send-keys", "-t", self.target, "Enter"], capture_output=True)

    def resize(self, cols: int, rows: int) -> None:
        self.rows = rows
        subprocess.run(["tmux", "resize-window", "-t", self.target, "-x", str(cols), "-y", str(rows)],
                       capture_output=True)

    def close(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            self._task = None

    @property
    def alive(self) -> bool:
        return not self._closed
