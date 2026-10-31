"""判断「这个 tmux 会话里正在跑的是哪个会话」。

侧栏要的效果：一个 tmux 组里只有当前被 agent 用着的那个会话是深色，其余都是灰色。
claude 靠 jsonl（有文件可查），dsh / codex 这类 pty 型 agent 靠进程（看谁还活着）。

claude 会话的三级证据，从硬到软：
  L2 held    pane 的进程树里，有进程正打开着 <sid>.jsonl（lsof / /proc/pid/fd）
  L1 recent  pane 里跑着 agent，且最新那个 jsonl 在 ACTIVE_TTL 内被写过
  L0 guess   pane 里跑着 agent，但文件已经冷了 —— 仍然认最新那个（claude 默认续最新会话）
  否则 none  这个 tmux 里没有 agent，整组都是历史会话，全灰

dsh 这类没有会话文件的：pane 进程树里现在真有 agent 进程 -> 深色；
只有「启动过」（pane_start_command）而没有活进程 -> 灰色。

命中不了的代价只是「多灰一个」，不会误把历史会话点亮，所以宁可保守。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import claude

# ------------------------------------------------------------------ 统一适配器声明
# SBP 是多种 CLI 的统一会话协议：每种 agent 的识别（命令名）与展示名都声明在
# sbp-spec/spec/adapters/*.yaml 里。这里把它们加载进来，作为 agent 识别的唯一真相源，
# 避免 panel 与 bridge 各维护一份硬编码列表（改了 adapter 忘了改这里就会“只认 claude”）。
try:
    _BRIDGE = Path(__file__).resolve().parents[2] / "bridge"
    if str(_BRIDGE) not in sys.path:
        sys.path.insert(0, str(_BRIDGE))
    from sbp_bridge.adapters import load_dirs          # noqa: E402
    from sbp_bridge.client import ADAPTER_DIRS         # noqa: E402
    _ADAPTERS = load_dirs(ADAPTER_DIRS)
    CMD_TO_AGENT: dict[str, str] = {}
    AGENT_DISPLAY: dict[str, str] = {}
    for _a in _ADAPTERS:
        for _c in (_a.data.get("match") or {}).get("cmd") or []:
            CMD_TO_AGENT.setdefault(_c, _a.agent)
        AGENT_DISPLAY[_a.agent] = _a.data.get("display", _a.agent)
    _ADAPTER_OK = True
except Exception:                                      # bridge 不在 / 没装 yaml：退回内置清单
    _ADAPTER_OK = False
    CMD_TO_AGENT = {}
    AGENT_DISPLAY = {}

if not CMD_TO_AGENT:                                   # 兜底：保证至少认识最常见的几种
    CMD_TO_AGENT = {
        "claude": "claude-code", "claude-code": "claude-code",
        "qwen": "qwen-code", "qwen-code": "qwen-code", "qodercli": "qodercli", "qoder": "qodercli",
        "codex": "codex", "dsh": "dsh", "dsh-agent": "dsh",
        "aider": "aider", "opencode": "opencode",
        "hermes": "hermes", "openclaw": "openclaw", "workbuddy": "workbuddy",
    }
    AGENT_DISPLAY = {
        "claude-code": "Claude Code", "qwen-code": "Qwen Code", "qodercli": "Qoder CLI",
        "codex": "OpenAI Codex", "dsh": "DSH Agent", "aider": "Aider", "opencode": "OpenCode",
        "hermes": "Hermes", "openclaw": "OpenClaw", "workbuddy": "WorkBuddy",
    }


def display_of(agent_id: str) -> str:
    """agent id（如 claude-code）-> 展示名（如 Claude Code）。"""
    return AGENT_DISPLAY.get(agent_id, (agent_id or "").replace("-", " ").title())


ACTIVE_TTL = 180.0      # jsonl 在这么多秒内被写过，就算「刚活跃」
_PS_TTL = 2.0           # ps 快照缓存
_FD_TTL = 5.0           # 单个 pid 的打开文件缓存

_ps_cache: tuple[float, dict] = (0.0, {})
_fd_cache: dict[int, tuple[float, list]] = {}
_cmd_cache: dict[int, tuple[float, str]] = {}

# 解释器/壳不算证据，否则每个终端都会被当成 agent
_WEAK = {"node", "bun", "bash", "sh", "zsh", "fish", "python", "python3", ""}


def agent_of(tokens: list[str]) -> str:
    """从一串命令 token 里认出 agent id（如 claude-code / qwen-code / aider）；认不出返回 ''。

    识别依据全部来自 sbp-spec/spec/adapters/*.yaml 的 match.cmd（见文件顶部加载逻辑）。
    dsh 这类 agent 常常是个 shell 脚本，tmux 的 pane_current_command 只显示
    解释器（bash），必须看启动命令行 / ps 的完整命令行才知道里面跑的是谁。
    """
    for t in tokens:
        b = os.path.basename((t or "").strip().strip('"').lower())
        if b in _WEAK or not b:
            continue
        if b in CMD_TO_AGENT:                       # 精确命中优先
            return CMD_TO_AGENT[b]
        for hint in CMD_TO_AGENT:                   # 前缀命中（claude-code-foo 之类）
            if b.startswith(hint):
                return CMD_TO_AGENT[hint]
    return ""


# ------------------------------------------------------------------ process
def _ppid_map() -> dict[int, int] | None:
    """pid -> ppid 全表；拿不到返回 None（沙箱里 ps 可能被禁）。

    Linux 直接扫 /proc，零外部命令；macOS 走 ps -eo，失败再退化到 pgrep。
    """
    global _ps_cache
    now = time.time()
    if now - _ps_cache[0] < _PS_TTL:
        return _ps_cache[1] or None

    m: dict[int, int] = {}
    if os.path.isdir("/proc"):
        try:
            for e in os.listdir("/proc"):
                if not e.isdigit():
                    continue
                with open(f"/proc/{e}/stat", "rb") as fh:
                    raw = fh.read()
                rp = raw.rfind(b")")
                if rp == -1:
                    continue
                f = raw[rp + 2:].split()
                if len(f) >= 2:
                    m[int(e)] = int(f[1])          # state(3) 之后就是 ppid
            if m:
                _ps_cache = (now, m)
                return m
        except OSError:
            pass

    try:
        p = subprocess.run(["ps", "-eo", "pid=,ppid="],
                           capture_output=True, text=True, timeout=5)
        for line in (p.stdout or "").splitlines():
            f = line.split()
            if len(f) >= 2:
                try:
                    m[int(f[0])] = int(f[1])
                except ValueError:
                    pass
    except Exception:
        pass
    _ps_cache = (now, m)
    return m or None


def _kids_of(pid: int) -> list[int]:
    """ps 不可用时的兜底：pgrep -P 只要直接子进程。"""
    hit = _fd_cache.get(-pid)
    if hit and time.time() - hit[0] < _PS_TTL:
        return hit[1]
    out: list[int] = []
    try:
        p = subprocess.run(["pgrep", "-P", str(pid)],
                           capture_output=True, text=True, timeout=5)
        for line in (p.stdout or "").split():
            if line.isdigit():
                out.append(int(line))
    except Exception:
        pass
    _fd_cache[-pid] = (time.time(), out)
    return out


def _tree(roots: list[int]) -> list[int]:
    """pane pid 的整棵子进程树（claude 通常是 pane 的孙进程）。"""
    m = _ppid_map()
    kids: dict[int, list[int]] = {}
    if m:
        for c, p in m.items():
            kids.setdefault(p, []).append(c)
    out: list[int] = []
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        out.append(cur)
        stack.extend(kids.get(cur) if m else _kids_of(cur))
    return out


def _open_files(pid: int) -> list[str]:
    """进程打开的文件路径。Linux 走 /proc，macOS 走 lsof。"""
    now = time.time()
    hit = _fd_cache.get(pid)
    if hit and now - hit[0] < _FD_TTL:
        return hit[1]
    files: list[str] = []
    d = f"/proc/{pid}/fd"
    if os.path.isdir(d):
        try:
            for fd in os.listdir(d):
                try:
                    files.append(os.readlink(os.path.join(d, fd)))
                except OSError:
                    pass
        except OSError:
            pass
    else:
        try:
            p = subprocess.run(["lsof", "-p", str(pid), "-Fn"],
                               capture_output=True, text=True, timeout=5)
            for line in (p.stdout or "").splitlines():
                if line.startswith("n/"):
                    files.append(line[1:])
        except Exception:
            pass
    _fd_cache[pid] = (now, files)
    if len(_fd_cache) > 400:
        _fd_cache.clear()
    return files


def _cmd_map(pids: list[int]) -> dict[int, str]:
    """pid -> 完整命令行。Linux 读 /proc，macOS 一次 ps 批量取。

    拿不到（沙箱禁 ps）就返回空表，调用方退回 tmux 自报的 pane 信息。
    """
    now = time.time()
    out: dict[int, str] = {}
    todo: list[int] = []
    for pid in pids:
        hit = _cmd_cache.get(pid)
        if hit and now - hit[0] < _PS_TTL:
            out[pid] = hit[1]
        else:
            todo.append(pid)
    if not todo:
        return out

    got: dict[int, str] = {}
    if os.path.isdir("/proc"):
        for pid in todo:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    got[pid] = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                pass
    else:
        try:
            p = subprocess.run(
                ["ps", "-o", "pid=,command=", "-p", ",".join(str(x) for x in todo)],
                capture_output=True, text=True, timeout=5)
            for line in (p.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                head, _, cmd = line.partition(" ")
                if head.isdigit():
                    got[int(head)] = cmd
        except Exception:
            pass
    for pid in todo:
        _cmd_cache[pid] = (now, got.get(pid, ""))
    out.update({p: got.get(p, "") for p in todo})
    if len(_cmd_cache) > 400:
        _cmd_cache.clear()
    return out


def running_agent(pane: dict) -> str:
    """这个 pane 里「此刻」跑着的 agent 名；没有/已退出返回 ''。

    只看 pane 的进程树：启动命令行（pane_start_command）是历史痕迹，
    dsh 退出回到 shell 之后它还在，靠它判断会把已经死的会话点亮。
    """
    try:
        pid = int(pane.get("pid") or 0)
    except (TypeError, ValueError):
        return ""
    if pid <= 1:
        return ""
    tree = _tree([pid])
    cmds = _cmd_map(tree)
    if not any(cmds.values()):
        # ps / /proc 都取不到：退回 pane 自报的 command + start（老行为，宁可信其有）
        toks: list[str] = [pane.get("command") or ""]
        toks += re.split(r"[\s'\"]+", pane.get("start") or "")
        return agent_of(toks)
    toks = []
    for p in tree:
        toks += re.split(r"[\s'\"]+", cmds.get(p) or "")
    return agent_of(toks)


def held_sids(pids: list[int]) -> set[str]:
    """进程树里正被打开的 claude 会话文件 -> sid 集合。"""
    # lsof 报的是 realpath（macOS 上 /tmp 会变成 /private/tmp），两边都得解析再比
    root = os.path.realpath(str(claude.PROJECTS))
    out: set[str] = set()
    for pid in _tree(pids):
        for f in _open_files(pid):
            if not f.endswith(".jsonl"):
                continue
            rp = os.path.realpath(f)
            if not (rp.startswith(root + os.sep) or "/.claude/projects/" in rp):
                continue
            sid = Path(f).stem
            if len(sid) >= 8:
                out.add(sid)
    return out


# ------------------------------------------------------------------ decision
def active_sid(panes: list[dict], items: list[dict], agent_live: bool) -> tuple[str, str]:
    """返回 (sid, level)。level ∈ held | recent | guess | none。

    items 必须已按 mtime 倒序（claude.sessions_in 的返回值就是）。
    """
    pids: list[int] = []
    for p in panes or []:
        try:
            v = int(p.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if v > 1:
            pids.append(v)

    if pids:
        held = held_sids(pids)
        if held:
            for it in items:                       # 多个就取最新的那个
                if it["sid"] in held:
                    return it["sid"], "held"
            return sorted(held)[0], "held"

    if not items:
        return "", "none"
    newest = items[0]
    if not agent_live:
        return "", "none"
    fresh = (time.time() * 1000 - (newest.get("updated_at") or 0)) < ACTIVE_TTL * 1000
    return newest["sid"], "recent" if fresh else "guess"
