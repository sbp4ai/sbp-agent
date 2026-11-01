"""非 claude agent 的会话历史源（qwen code 等）。

背景：panel 以前只认一处历史文件 —— ``~/.claude/projects/<slug>/<uuid>.jsonl``。
于是 qwen code 这类「其实也把对话落盘成 jsonl」的 agent 被一律当成纯终端会话，
历史一页都翻不到（前端只能显示「纯终端会话 · 没有历史文件」）。

现在把它做成**适配器声明驱动**的通用能力：``adapters/*.yaml`` 里加一段 ``history:``，
声明历史文件在哪、怎么把「正在跑的进程」绑到具体某个文件。没声明的 agent
保持原样（纯终端 + 实时屏幕），不会误伤。

qwen code 的实测事实（v0.26.0，2026-11-01 在 8.216.44.155 上逐条核对）：

  历史文件   ~/.qwen/projects/<slug>/chats/<uuid>.jsonl
  slug 规则  与 claude 完全一致：cwd 里非字母数字全替换成 '-'
              （/tridu33 -> -tridu33，和 ~/.claude/projects/-tridu33 同名）
  绑定方式 1 进程命令行里带 --resume <uuid>
  绑定方式 2 chats/<uuid>.runtime.json 里有 {"pid":…, "session_id":"…"}
  兜底       取 cwd 对应目录下 mtime 最新的那个 jsonl
  记录格式   type: user / assistant / system，正文在 message.parts（不是 content）

声明示例（见 sbp-spec/spec/adapters/qwen-code.yaml）：

  history:
    root: "~/.qwen/projects"
    glob: "*/chats/*.jsonl"
    cwd_dir: "{slug}/chats"
    id_from_cmdline: '--resume\\s+([0-9a-fA-F][0-9a-fA-F-]{7,})'
    runtime: {glob: "*/chats/*.runtime.json", pid: pid, sid: session_id}
    format: parts
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from . import claude
from . import live

# 绑定结果缓存：一个 pane 在这么久内重复问，不再扫盘
RESOLVE_TTL = 20.0
# runtime.json / glob 扫描缓存
SCAN_TTL = 60.0
MAX_SCAN = 200

_resolve_cache: dict[tuple, tuple[float, Optional[str]]] = {}
_scan_cache: dict[str, tuple[float, list]] = {}

# 内置兜底：sbp-spec 不在或适配器没写 history 段时用它，保证开箱能认 qwen
DEFAULT_SPECS: dict[str, dict] = {
    "qwen-code": {
        "root": "~/.qwen/projects",
        "glob": "*/chats/*.jsonl",
        "cwd_dir": "{slug}/chats",
        "id_from_cmdline": r"--resume\s+([0-9a-fA-F][0-9a-fA-F-]{7,})",
        "runtime": {"glob": "*/chats/*.runtime.json", "pid": "pid", "sid": "session_id"},
        "format": "parts",
    },
}


# ------------------------------------------------------------------ specs
def specs() -> dict[str, dict]:
    """agent id -> history 声明。适配器声明优先于内置兜底。"""
    out = dict(DEFAULT_SPECS)
    for agent, data in (live.adapter_data() or {}).items():
        h = (data or {}).get("history")
        if isinstance(h, dict) and h.get("root"):
            out[agent] = h
    return out


def root_of(spec: dict) -> Path:
    root = str(spec.get("root") or "")
    return Path(os.path.expandvars(os.path.expanduser(root)))


def known(agent: str) -> bool:
    return bool(specs().get(agent))


# ------------------------------------------------------------------ bind
def _pids_of(pane: dict) -> list[int]:
    try:
        pid = int(pane.get("pid") or 0)
    except (TypeError, ValueError):
        return []
    if pid <= 1:
        return []
    return live._tree([pid])


def _sid_from_cmdline(spec: dict, pane: dict) -> str:
    """从进程命令行里抠出 session id（qwen --resume <uuid>）。"""
    rx = spec.get("id_from_cmdline")
    if not rx:
        return ""
    cmds: list[str] = []
    pids = _pids_of(pane)
    if pids:
        cmds += [c for c in live._cmd_map(pids).values() if c]
    # tmux 自报的启动命令行也带上：/proc 被沙箱挡住时它是唯一线索
    cmds.append(pane.get("start") or "")
    cmds.append(pane.get("command") or "")
    try:
        pat = re.compile(rx)
    except re.error:
        return ""
    sid = ""
    for c in cmds:
        m = pat.search(c or "")
        if m:
            sid = m.group(1)
    return sid


def _scan(spec: dict, glob: str) -> list[tuple[float, str]]:
    """按 mtime 倒序列出匹配项，带 TTL 缓存（避免每次建索引都全盘 glob）。"""
    root = root_of(spec)
    key = f"{root}|{glob}"
    now = time.time()
    hit = _scan_cache.get(key)
    if hit and now - hit[0] < SCAN_TTL:
        return hit[1]
    out: list[tuple[float, str]] = []
    try:
        for p in root.glob(glob):
            try:
                out.append((p.stat().st_mtime, str(p)))
            except OSError:
                continue
    except OSError:
        pass
    out.sort(reverse=True)
    _scan_cache[key] = (now, out)
    return out


def _sid_from_runtime(spec: dict, pane: dict) -> str:
    """runtime.json 里有 {pid, session_id}：用进程树里的 pid 反查会话文件。"""
    rt = spec.get("runtime") or {}
    if not rt.get("glob"):
        return ""
    pids = set(_pids_of(pane))
    if not pids:
        return ""
    pid_key = str(rt.get("pid") or "pid")
    sid_key = str(rt.get("sid") or "session_id")
    for _mt, p in _scan(spec, rt["glob"])[:MAX_SCAN]:
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8") or "{}")
        except Exception:
            continue
        try:
            rp = int(d.get(pid_key) or 0)
        except (TypeError, ValueError):
            continue
        if rp and rp in pids:
            return str(d.get(sid_key) or "")
    return ""


def _by_sid(spec: dict, sid: str) -> Optional[str]:
    if not sid:
        return None
    pat = spec.get("by_sid")
    if not pat:
        # 由 glob 推出：*/chats/*.jsonl -> */chats/<sid>.jsonl
        base = str(spec.get("glob") or "*.jsonl")
        pat = base.rsplit("*", 1)[0] + sid + ".jsonl" if "*" in base else base
    for p in root_of(spec).glob(pat.replace("{sid}", sid)):
        if p.is_file():
            return str(p)
    return None


def _newest_in_cwd(spec: dict, cwd: str) -> Optional[str]:
    """兜底：该 cwd 对应目录里 mtime 最新的会话文件。"""
    if not cwd:
        return None
    tmpl = str(spec.get("cwd_dir") or "")
    if not tmpl:
        return None
    d = root_of(spec) / tmpl.replace("{slug}", claude.slug_for(cwd))
    if not d.is_dir():
        return None
    files = [p for p in d.glob("*.jsonl") if p.is_file()]
    if not files:
        return None
    return str(max(files, key=lambda p: p.stat().st_mtime))


def resolve(agent: str, pane: dict, cwd: str = "") -> Optional[str]:
    """这个 pane 里 agent 正在写的那个会话文件；找不到返回 None（= 纯终端）。"""
    spec = specs().get(agent)
    if not spec:
        return None
    root = root_of(spec)
    if not root.is_dir():
        return None
    key = (agent, str(pane.get("pane_id") or ""), str(pane.get("pid") or ""), cwd)
    now = time.time()
    hit = _resolve_cache.get(key)
    if hit and now - hit[0] < RESOLVE_TTL:
        return hit[1]

    path: Optional[str] = None
    sid = _sid_from_cmdline(spec, pane) or _sid_from_runtime(spec, pane)
    if sid:
        path = _by_sid(spec, sid)
    if not path:
        path = _newest_in_cwd(spec, cwd or (pane.get("cwd") or ""))
    if path and not os.path.isfile(path):
        path = None
    _resolve_cache[key] = (now, path)
    return path


# ------------------------------------------------------------------ listing
def recent(limit: int = 8, exclude: set[str] | None = None) -> list[dict]:
    """所有声明了 history 的 agent 的近期会话文件（给「其他会话」兜底组用）。"""
    exclude = exclude or set()
    found: list[tuple[float, str, str]] = []
    for agent, spec in specs().items():
        root = root_of(spec)
        if not root.is_dir():
            continue
        for mt, p in _scan(spec, str(spec.get("glob") or "*.jsonl"))[:MAX_SCAN]:
            if p in exclude:
                continue
            found.append((mt, p, agent))
    found.sort(reverse=True)
    out: list[dict] = []
    for mt, p, agent in found[:limit]:
        try:
            it = claude.entry(Path(p).stem, p)
        except OSError:
            continue
        it["agent"] = agent
        it["kind"] = "chat"
        out.append(it)
    return out
