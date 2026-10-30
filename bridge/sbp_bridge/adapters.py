"""Adapter 加载与形式化映射执行（纯声明式，不允许脚本）。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


TEMPLATE_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _render(tmpl: Any, ctx: dict) -> str:
    if not isinstance(tmpl, str):
        return "" if tmpl is None else str(tmpl)
    return TEMPLATE_RE.sub(lambda m: str(ctx.get(m.group(1), "")), tmpl)


class Adapter:
    def __init__(self, data: dict, path: Path | None = None):
        self.data = data
        self.path = path
        self.name = data.get("adapter", "generic@0")
        self.agent = data.get("agent", "generic")
        self.caps: list[str] = list(data.get("caps") or [])
        self.transport: dict = data.get("transport") or {}
        self.semantics: dict = data.get("semantics") or {}

    # ---------------------------------------------------------- matching
    def score(self, cmd: str) -> int:
        match = self.data.get("match") or {}
        cmds = match.get("cmd") or []
        if not cmds:
            return -1
        base = Path(cmd).name
        if any(base == c or base.startswith(c) for c in cmds):
            return 100
        return -1

    # ---------------------------------------------------------- actions
    def supports(self, cap: str) -> bool:
        return cap in self.caps

    def action(self, kind: str, ctx: dict) -> list[str]:
        """把抽象动作翻成要写进 pty 的字节串序列。"""
        sem = self.semantics.get(kind)
        if not sem or sem.get("unsupported"):
            return []
        out: list[str] = []
        if kind == "input.key":
            key = str(ctx.get("key", "")).upper()
            table = sem.get("keys") or {}
            if key in table:
                out.append(_render(table[key], ctx))
            return out
        if kind == "input.approve":
            ctx = {**ctx, "yes_no": sem.get("yes", "y") if ctx.get("value") else sem.get("no", "n")}
        if "write" in sem:
            out.append(_render(sem["write"], ctx))
        if "submit" in sem:
            out.append(_render(sem["submit"], ctx))
        return out

    def spawn_argv(self, ctx: dict) -> list[str]:
        spawn = (self.transport.get("spawn") or {})
        if spawn.get("exec"):
            return ["/bin/sh", "-lc", _render(spawn["exec"], ctx)]
        shell = self.transport.get("shell") or ["/bin/zsh", "-i"]
        return [_render(s, ctx) for s in shell]

    def capture(self) -> dict:
        return self.transport.get("capture") or {"mode": "screen", "rows": 40, "cols": 100, "interval_ms": 500}

    def state_of(self, screen_text: str) -> str:
        state = self.data.get("state") or {}
        for name in ("waiting", "busy"):
            rx = (state.get(name) or {}).get("regex")
            if rx and re.search(rx, screen_text, re.M):
                return "waiting_input" if name == "waiting" else "running"
        return "idle"

    def parse_choices(self, screen_text: str) -> list[dict]:
        rule = (self.data.get("parse") or {}).get("choice") or {}
        rx = rule.get("regex")
        if not rx:
            return []
        gi = int(rule.get("group_index", 1))
        gl = int(rule.get("group_label", 2))
        found, seen = [], set()
        for m in re.finditer(rx, screen_text, re.M):
            try:
                idx = int(m.group(gi))
                label = m.group(gl).strip()
            except Exception:
                continue
            if idx in seen:
                continue
            seen.add(idx)
            found.append({"index": idx, "label": label})
        return found[:9]


def load_dir(path: str | Path) -> list[Adapter]:
    if yaml is None:
        raise RuntimeError("需要 pyyaml: pip install pyyaml")
    out = []
    for p in sorted(Path(path).glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out.append(Adapter(data, p))
        except Exception:
            continue
    return out


def load_dirs(paths) -> list[Adapter]:
    """按顺序合并多个 adapter 目录，同名只认第一次出现的（前者优先）。

    用于「本仓自带 + 同级 sbp-spec 仓」共存时避免同名 adapter 打架。
    """
    out: list[Adapter] = []
    seen: set[str] = set()
    for p in paths:
        for a in load_dir(p):
            if a.name in seen:
                continue
            seen.add(a.name)
            out.append(a)
    return out


# 一个 adapter 都没找到时的兜底：等价于 spec/adapters/generic.yaml。
# 没有它，pick() 对空列表会抛 IndexError（仓库拆分后最常见的炸法）。
_GENERIC_FALLBACK: dict = {
    "adapter": "generic@0",
    "agent": "generic",
    "display": "Generic Terminal",
    "match": {},
    "transport": {
        "type": "pty",
        "shell": ["{{shell}}", "-i"],
        "capture": {"mode": "screen", "rows": 40, "cols": 100, "interval_ms": 600},
        "env": {"TERM": "xterm-256color"},
    },
    "state": {"busy": {"regex": "(?!)"}, "idle": {"regex": ".*"}, "waiting": {"regex": "(?!)"}},
    "semantics": {
        "input.text": {"write": "{{text}}", "submit": "\r"},
        "input.key": {"keys": {"ENTER": "\r", "ESC": "\u001b"}},
        "input.choice": {"unsupported": True},
        "input.approve": {"unsupported": True},
        "input.interrupt": {"write": "\u0003"},
    },
    "caps": ["input.text", "input.key", "input.interrupt", "output.screen"],
}


def pick(adapters: list[Adapter], cmd: str, explicit: str | None = None) -> Adapter:
    if explicit:
        for a in adapters:
            if a.name == explicit or a.agent == explicit:
                return a
    scored = sorted(((a.score(cmd), a) for a in adapters), key=lambda t: t[0], reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][1]
    fallback = next((a for a in adapters if a.agent == "generic"), None)
    if fallback is not None:
        return fallback
    # 列表为空（adapter 目录缺失/没装 yaml）时也不能崩，退化成内置 generic。
    return Adapter(dict(_GENERIC_FALLBACK))
