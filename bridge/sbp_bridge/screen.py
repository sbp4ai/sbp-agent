"""极简终端行模型：把 pty 字节流变成手机可读的行，并做增量 diff。

不做完整 VT100（v0.1 用不上），只处理 ANSI 清洗 + CR/LF/BS + 自动换行。
需要真彩/光标定位时切 output.stream 模式交给 xterm.js。
"""
from __future__ import annotations

import re
import unicodedata

ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"      # CSI
    r"|\x1b\][^\x07\x1b]*(\x07|\x1b\\)"  # OSC
    r"|\x1b[()][0-9A-B]"              # charset
    r"|\x1b[=>NOM]"                   # misc
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f]"  # 其余控制符
)

MAX_LINES = 800


def _width(ch: str) -> int:
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


class Screen:
    def __init__(self, rows: int = 40, cols: int = 100):
        self.rows = rows
        self.cols = cols
        self.lines: list[str] = [""]
        self.col = 0

    # ------------------------------------------------------------ feed
    def feed(self, data: bytes) -> None:
        text = data.decode("utf-8", errors="replace")
        text = ANSI_RE.sub("", text)
        for ch in text:
            if ch == "\n":
                self.lines.append("")
                self.col = 0
            elif ch == "\r":
                self.col = 0
            elif ch == "\b":
                self.col = max(0, self.col - 1)
            else:
                cur = self.lines[-1]
                w = _width(ch)
                if self.col < len(cur):
                    cur_list = list(cur)
                    cur_list[self.col:self.col + w] = [ch] + [""] * (w - 1)
                    self.lines[-1] = "".join(cur_list)
                else:
                    pad = " " * max(0, self.col - len(cur))
                    self.lines[-1] = cur + pad + ch
                self.col += w
        if len(self.lines) > MAX_LINES:
            self.lines = self.lines[-MAX_LINES:]

    def feed_text(self, text: str) -> None:
        self.feed(text.encode())

    # ------------------------------------------------------------ read
    def snapshot(self) -> list[str]:
        tail = self.lines[-self.rows:]
        return [ln.rstrip() for ln in tail]

    def tail_text(self, since_index: int) -> str:
        chunk = self.lines[since_index:] if since_index < len(self.lines) else []
        return "\n".join(ln.rstrip() for ln in chunk).strip()

    @property
    def mark(self) -> int:
        return len(self.lines)

    # ------------------------------------------------------------ diff
    def diff(self, prev: list[str] | None) -> dict:
        cur = self.snapshot()
        if prev is None or prev == cur:
            return {"full": True, "lines": cur, "changed": prev != cur}
        if len(prev) != len(cur):
            # 滚动：整体重发（一屏 < 2KB，手机侧无感）
            return {"full": True, "lines": cur, "changed": True}
        start, patch = None, []
        for i, (a, b) in enumerate(zip(prev, cur)):
            if a != b:
                if start is None:
                    start = i
                patch.append(b)
            elif start is not None:
                break
        if start is None:
            return {"full": False, "lines": [], "changed": False}
        return {"full": False, "start": start, "lines": patch, "changed": True}
