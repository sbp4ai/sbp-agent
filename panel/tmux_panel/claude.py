"""Claude Code 会话仓：发现、索引、倒序分页、增量读取。

数据来源：~/.claude/projects/<slug>/<session-id>.jsonl
  slug  = cwd 里非字母数字全替换成 '-'
  记录类型：user / assistant / ai-title / last-prompt / mode / permission-mode /
            file-history-snapshot / queue-operation / attachment ...
只有 user / assistant 是消息，ai-title 与 last-prompt 拿来当标题和副标题。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable, Optional

CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
PROJECTS = CLAUDE_HOME / "projects"
STATE_DIR = Path(os.environ.get("PANEL_STATE") or (Path.home() / ".tmux-claude-panel"))
ANNO_FILE = STATE_DIR / "annotations.json"

TAIL_BYTES = 192 * 1024
MAX_TEXT = 6000
SKIP_TYPES = {"mode", "permission-mode", "file-history-snapshot", "queue-operation", "attachment"}


def slug_for(path: str) -> str:
    p = (path or "").rstrip("/")
    return re.sub(r"[^A-Za-z0-9]", "-", p)


def project_dir(cwd: str) -> Path:
    return PROJECTS / slug_for(cwd)


# ------------------------------------------------------------------ line index
class LineIndex:
    """字节偏移索引：让「倒序翻页」与「增量」都变成 O(1) 定位。

    同一文件的索引按 (size, mtime) 缓存；文件被追加时自动重建。
    """

    _cache: dict[str, tuple[int, float, list[int]]] = {}

    @classmethod
    def offsets(cls, path: str, force: bool = False) -> list[int]:
        try:
            st = os.stat(path)
        except OSError:
            return []
        hit = cls._cache.get(path)
        if hit and not force and hit[0] == st.st_size and abs(hit[1] - st.st_mtime) < 1e-6:
            return hit[2]
        offs = [0]
        pos = 0
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                start = 0
                while True:
                    i = chunk.find(b"\n", start)
                    if i == -1:
                        break
                    offs.append(pos + i + 1)
                    start = i + 1
                pos += len(chunk)
        if len(offs) > 1 and offs[-1] >= pos:
            offs.pop()          # 文件以 \n 结尾时多算了一个空行
        cls._cache[path] = (st.st_size, st.st_mtime, offs)
        return offs

    @classmethod
    def count(cls, path: str) -> int:
        return len(cls.offsets(path))


def _read_lines(path: str, offsets: list[int], start: int, stop: int) -> list[str]:
    """按行号 [start, stop) 读取（1-based，含 start 不含 stop）。"""
    out: list[str] = []
    if not offsets:
        return out
    start = max(1, start)
    stop = min(len(offsets) + 1, stop)
    if start >= stop:
        return out
    with open(path, "rb") as fh:
        for i in range(start - 1, stop - 1):
            fh.seek(offsets[i])
            raw = fh.readline()
            if raw:
                out.append(raw.decode("utf-8", "replace"))
    return out


# ------------------------------------------------------------------ normalize
def _text_of(content) -> tuple[str, list[str]]:
    """把 message.content 拍平成文本 + 工具名列表。"""
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []
    parts: list[str] = []
    tools: list[str] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            txt = (b.get("text") or "").strip()
            if txt:
                parts.append(txt)
        elif t == "thinking":
            pass                      # 默认不展示思考，避免刷屏
        elif t == "tool_use":
            name = b.get("name") or "tool"
            tools.append(name)
            inp = b.get("input") or {}
            hint = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("url") or ""
            hint = str(hint).replace("\n", " ")[:160]
            parts.append(f"⚙ {name}" + (f" · {hint}" if hint else ""))
        elif t == "tool_result":
            body = b.get("content")
            if isinstance(body, list):
                body = "\n".join(x.get("text", "") for x in body if isinstance(x, dict))
            body = str(body or "").strip()
            if body:
                parts.append("↳ " + body[:800])
        elif t == "image":
            parts.append("[图片]")
    return "\n\n".join(p for p in parts if p), tools


def normalize(rec: dict, seq: int) -> Optional[dict]:
    """jsonl 记录 -> 前端可渲染的消息。返回 None 表示这条不展示。"""
    typ = rec.get("type")
    if typ in SKIP_TYPES or typ not in ("user", "assistant"):
        return None
    if rec.get("isSidechain"):
        return None
    msg = rec.get("message") or {}
    content = msg.get("content")
    text, tools = _text_of(content)
    if not text:
        return None
    truncated = len(text) > MAX_TEXT
    return {
        "seq": seq,
        "role": "user" if typ == "user" else "assistant",
        "kind": "tool" if (tools and len(text) < 400) else "text",
        "text": text[:MAX_TEXT],
        "truncated": truncated,
        "ts": rec.get("timestamp") or "",
        "model": (msg.get("model") or "") if typ == "assistant" else "",
        "tools": tools[:6],
        "side": False,
    }


# ------------------------------------------------------------------ index entry
def _tail_meta(path: str) -> dict:
    """只读文件尾部，拿到 aiTitle / lastPrompt / 最后一条消息摘要。"""
    meta = {"ai_title": "", "last_prompt": "", "preview": "", "model": "", "branch": "", "cwd": ""}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
                fh.readline()           # 丢掉半行
            blob = fh.read().decode("utf-8", "replace")
    except OSError:
        return meta
    for line in reversed(blob.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        t = rec.get("type")
        if t == "ai-title" and not meta["ai_title"]:
            meta["ai_title"] = (rec.get("aiTitle") or "").strip()
        elif t == "last-prompt" and not meta["last_prompt"]:
            meta["last_prompt"] = (rec.get("lastPrompt") or "").strip()
        elif t in ("user", "assistant") and not rec.get("isSidechain") and not meta["preview"]:
            txt, _ = _text_of((rec.get("message") or {}).get("content"))
            meta["preview"] = re.sub(r"\s+", " ", txt)[:160]
            meta["model"] = (rec.get("message") or {}).get("model") or ""
            meta["branch"] = rec.get("gitBranch") or ""
            meta["cwd"] = rec.get("cwd") or ""
        if meta["ai_title"] and meta["last_prompt"] and meta["preview"]:
            break
    return meta


def entry(session_id: str, path: str, cwd_hint: str = "") -> dict:
    st = os.stat(path)
    meta = _tail_meta(path)
    count = LineIndex.count(path) if st.st_size < 8 * 1024 * 1024 else None
    return {
        "sid": session_id,
        "short": session_id[:8],
        "file": path,
        "cwd": meta["cwd"] or cwd_hint,
        "ai_title": meta["ai_title"],
        "last_prompt": meta["last_prompt"],
        "preview": meta["preview"],
        "model": meta["model"],
        "branch": meta["branch"],
        "updated_at": int(st.st_mtime * 1000),
        "size": st.st_size,
        "count": count,
    }


# ------------------------------------------------------------------ annotations
class Annotations:
    """用户自定义标题/副标题。key = claude session id。"""

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            self._data = json.loads(ANNO_FILE.read_text())
        except Exception:
            self._data = {}

    def _save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ANNO_FILE.write_text(json.dumps(self._data, ensure_ascii=False, indent=1))

    def get(self, sid: str) -> dict:
        return self._data.get(sid, {})

    def set(self, sid: str, title: str | None = None, subtitle: str | None = None) -> dict:
        cur = dict(self._data.get(sid, {}))
        if title is not None:
            cur["title"] = title.strip()
        if subtitle is not None:
            cur["subtitle"] = subtitle.strip()
        if cur.get("title") or cur.get("subtitle"):
            self._data[sid] = cur
        else:
            self._data.pop(sid, None)
        self._save()
        return cur

    def all(self) -> dict:
        return dict(self._data)


anno = Annotations()


# ------------------------------------------------------------------ session list
def sessions_in(cwd: str, limit: int = 12) -> list[dict]:
    """某目录下的 claude 会话，按 mtime 倒序。"""
    d = project_dir(cwd)
    if not d.is_dir():
        return []
    files = [p for p in d.glob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:limit]:
        try:
            out.append(entry(p.stem, str(p), cwd))
        except OSError:
            continue
    return out


# ------------------------------------------------------------------ paging
def page(path: str, before: int | None = None, limit: int = 20) -> dict:
    offs = LineIndex.offsets(path)
    total = len(offs)
    if total == 0:
        return {"items": [], "has_more": False, "next_before": None, "total": 0}
    limit = max(1, min(int(limit or 20), 100))
    upper = int(before) if before else total + 1
    start = max(1, upper - limit)
    raw = _read_lines(path, offs, start, upper)
    items = []
    for i, line in enumerate(raw):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        m = normalize(rec, start + i)
        if m:
            items.append(m)
    return {
        "items": items,
        "has_more": start > 1,
        "next_before": start if start > 1 else None,
        "total": total,
        "from": start,
        "to": upper - 1,
    }


def since(path: str, after: int) -> list[dict]:
    offs = LineIndex.offsets(path)
    total = len(offs)
    if after >= total:
        return []
    raw = _read_lines(path, offs, after + 1, total + 1)
    out = []
    for i, line in enumerate(raw):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        m = normalize(rec, after + 1 + i)
        if m:
            out.append(m)
    return out


def find_by_sid(sid: str) -> Optional[str]:
    """按 session id 反查文件路径（不依赖是否在 tmux 里）。"""
    if not PROJECTS.is_dir():
        return None
    for p in PROJECTS.glob(f"*/{sid}.jsonl"):
        return str(p)
    return None
