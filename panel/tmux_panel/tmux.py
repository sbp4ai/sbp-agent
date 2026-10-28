"""tmux 访问层。

socket 解析逻辑移植自 example/tmux_ai_coder_panel_src/main.py 的 TmuxManager
（原作者处理了 ~/.tmux 与 /tmp 双 socket 目录、systemd 不 source shell rc 等坑），
这里在其上补了 pane 级信息，用来判断「这个 tmux 会话里是不是真的跑着 claude」。
"""
from __future__ import annotations

import os
import stat
import subprocess
from typing import Optional

PANE_FMT = "|".join([
    "#{session_name}", "#{pane_id}", "#{pane_pid}",
    "#{pane_current_command}", "#{pane_current_path}", "#{pane_active}",
    "#{pane_start_command}",
])

SESS_FMT = "|".join(["#{session_name}", "#{session_path}", "#{session_created}"])


def _run(argv: list[str], env: dict | None = None, timeout: float = 5.0) -> tuple[int, str]:
    try:
        # 服务端「真干活」的计数器：fork 一次 tmux 是最贵的原子操作，
        # 传输层对比 benchmark 就以它为准（见 tests/transport_bench.py）。
        Tmux.COUNTS["run"] += 1
        if "capture-pane" in argv:
            Tmux.COUNTS["capture"] += 1
    except Exception:
        pass
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env=env or os.environ.copy())
        return p.returncode, (p.stdout or "")
    except Exception:
        return 1, ""


class Tmux:
    """一个进程内复用的 tmux 句柄。所有操作都走 _cmd() 以兼容多 socket。"""

    COUNTS = {"run": 0, "capture": 0}

    # ---------------------------------------------------------------- socket
    @staticmethod
    def _is_socket(path: str) -> bool:
        try:
            return stat.S_ISSOCK(os.stat(path).st_mode)
        except OSError:
            return False

    @staticmethod
    def _socket_dirs() -> list[str]:
        home = os.path.expanduser("~")
        cands = [os.path.join(home, ".tmux"), "/tmp"]
        extra = os.environ.get("TMUX_TMPDIR")
        if extra and extra not in cands:
            cands.insert(0, extra)
        return cands

    @classmethod
    def _socket_names(cls, parent: str) -> list[str]:
        d = os.path.join(parent, f"tmux-{os.getuid()}")
        if not os.path.isdir(d):
            return []
        try:
            return [n for n in os.listdir(d) if cls._is_socket(os.path.join(d, n))]
        except OSError:
            return []

    @classmethod
    def _resolve(cls, session: str) -> Optional[tuple[str, str]]:
        """返回 (TMUX_TMPDIR, socket_name)；None 表示默认 socket 就够。"""
        rc, _ = _run(["tmux", "has-session", "-t", session], timeout=3)
        if rc == 0:
            return None
        for parent in cls._socket_dirs():
            for name in cls._socket_names(parent):
                env = os.environ.copy()
                env["TMUX_TMPDIR"] = parent
                rc, _ = _run(["tmux", "-L", name, "has-session", "-t", session], env=env, timeout=3)
                if rc == 0:
                    return parent, name
        return None

    @classmethod
    def _cmd(cls, session: str, args: list[str]) -> tuple[list[str], dict]:
        resolved = cls._resolve(session)
        if resolved is None:
            return ["tmux"] + args, os.environ.copy()
        parent, name = resolved
        env = os.environ.copy()
        env["TMUX_TMPDIR"] = parent
        return ["tmux", "-L", name] + args, env

    # ---------------------------------------------------------------- query
    @classmethod
    def list_sessions(cls) -> list[dict]:
        """枚举所有 socket 下的会话，附带 pane 信息。"""
        out: list[dict] = []
        seen: set[str] = set()
        for parent in cls._socket_dirs():
            for name in cls._socket_names(parent):
                env = os.environ.copy()
                env["TMUX_TMPDIR"] = parent
                rc, text = _run(["tmux", "-L", name, "list-sessions", "-F", SESS_FMT], env=env)
                if rc != 0:
                    continue
                for line in text.splitlines():
                    if not line.strip():
                        continue
                    parts = line.split("|")
                    sname = parts[0]
                    if sname in seen:
                        continue
                    seen.add(sname)
                    try:
                        created = int(parts[2])
                    except (IndexError, ValueError):
                        created = 0
                    out.append({
                        "name": sname,
                        "path": parts[1] if len(parts) > 1 else "",
                        "created": created,
                        "socket": name,
                        "panes": cls._panes(sname),
                    })
        return out

    @classmethod
    def _panes(cls, session: str) -> list[dict]:
        cmd, env = cls._cmd(session, ["list-panes", "-t", session, "-F", PANE_FMT])
        rc, text = _run(cmd, env=env)
        if rc != 0:
            return []
        panes = []
        for line in text.splitlines():
            p = line.split("|")
            if len(p) < 5:
                continue
            panes.append({
                "pane_id": p[1],
                "pid": p[2],
                "command": p[3],
                "cwd": p[4],
                "active": (p[5] == "1") if len(p) > 5 else False,
                # 脚本型 agent（dsh 是 shell 脚本时）comm 只会显示 bash，
                # 得靠启动命令行才能认出里面跑的是谁
                "start": (p[6] if len(p) > 6 else "").strip('"'),
            })
        return panes

    @classmethod
    def capture(cls, session: str, lines: int = 200) -> str:
        cmd, env = cls._cmd(session, ["capture-pane", "-p", "-t", session, "-S", f"-{int(lines)}"])
        rc, text = _run(cmd, env=env)
        return text if rc == 0 else ""

    @classmethod
    def alive(cls, session: str) -> bool:
        cmd, env = cls._cmd(session, ["has-session", "-t", session])
        rc, _ = _run(cmd, env=env, timeout=3)
        return rc == 0

    # ---------------------------------------------------------------- input
    KEYS = {
        "ENTER": "Enter", "ESC": "Escape", "TAB": "Tab", "SPACE": "Space",
        "UP": "Up", "DOWN": "Down", "LEFT": "Left", "RIGHT": "Right",
        "BS": "BSpace", "C-C": "C-c", "C-D": "C-d", "C-L": "C-l",
        "C-R": "C-r", "PGUP": "PageUp", "PGDN": "PageDown",
        "1": "1", "2": "2", "3": "3", "y": "y", "n": "n",
    }

    @classmethod
    def send_text(cls, session: str, text: str, submit: bool = True) -> bool:
        """-l 保证文本按字面发送，避免 tmux 把 'End' 之类当键名。"""
        cmd, env = cls._cmd(session, ["send-keys", "-t", session, "-l", "--", text])
        rc, _ = _run(cmd, env=env)
        if rc != 0:
            return False
        if submit:
            cmd, env = cls._cmd(session, ["send-keys", "-t", session, "Enter"])
            rc, _ = _run(cmd, env=env)
        return rc == 0

    @classmethod
    def send_key(cls, session: str, key: str) -> bool:
        name = cls.KEYS.get(key.upper(), key)
        cmd, env = cls._cmd(session, ["send-keys", "-t", session, name])
        rc, _ = _run(cmd, env=env)
        return rc == 0

    @classmethod
    def resize(cls, session: str, cols: int, rows: int) -> None:
        cmd, env = cls._cmd(session, ["resize-window", "-t", session, "-x", str(cols), "-y", str(rows)])
        _run(cmd, env=env)
