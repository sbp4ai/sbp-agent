"""会话目录（tmux 分组）与实时推送。

传输层无关：这里只负责「把状态算出来 + 缓存住」。
WebSocket 客户端通过 attach() 订阅并被动收推送；
REST 轮询客户端通过 touch() 登记、再读同一份缓存。
两种传输共享同一次 capture-pane / 同一次建索引，所以 N 个轮询者
不会把成本放大 N 倍——这是「省服务器资源」的关键。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import Optional

from . import claude
from . import live
from . import prompt as promptmod
from .tmux import Tmux

# dsh 这类 pty agent 会把当前任务打在屏幕上（见 spec/adapters/dsh.yaml 的 parse.title），
# 拿它当副标题，侧栏才能分清「哪个 dsh 在干啥」。
_TITLE_RES = [re.compile(r"^task:\s*(.+)$"), re.compile(r"^[#>]\s*(.{4,60})$")]

INDEX_TTL = 6.0
# 侧栏「哪个会话在跑」是会变的（claude 启动/退出/切会话），
# 定期重算索引，变了才广播。建一次索引要 fork 60+ 个子进程，是全站最贵的操作，
# 所以让它跟 tick 对齐：tick 负责重建，HTTP 轮询者直接读缓存，
# 这样 N 个轮询者不会把成本放大 N 倍。
PUSH_TTL = 6.0
# 抓屏缓存：同 1 个 pane 在这么久内的重复请求共用一次 capture-pane。
# 一次 capture-pane = 4 次 fork（见 tests/transport_bench.py），
# 这是全站最贵的原子操作，缓存住它比换协议更省。
SNAP_TTL = 1.0
# REST 轮询者断线多久后回收它的 watch（避免进程常驻抓屏）
POLL_TTL = 90.0
# 没人订阅时的空转周期
IDLE_TICK = 3.0


def _screen_title(pane_id: str, lines: int = 18) -> str:
    """从 pane 屏幕尾部捞一行「当前在干什么」。

    dsh 这类没有会话文件的 agent，这是唯一能区分「哪条 dsh」的信息
    （spec/adapters/dsh.yaml 里 parse.title 就是 `^task:` 那行）。
    """
    if not pane_id:
        return ""
    txt = Tmux.capture(pane_id, lines=lines)
    if not txt:
        return ""
    for line in reversed([x for x in txt.splitlines() if x.strip()]):
        s = line.strip()
        for rx in _TITLE_RES:
            m = rx.match(s)
            if m:
                return m.group(1).strip()[:60]
    return ""


def _slim(it: dict) -> dict:
    """索引只留 UI 要用的字段：路径、正文、模型都不进侧栏。"""
    return {
        "sid": it["sid"], "short": it["short"], "title": it["title"],
        "subtitle": it["subtitle"], "custom": it["custom"],
        "count": it["count"], "updated_at": it["updated_at"],
        "branch": it["branch"], "tmux": it.get("tmux", ""),
        "kind": it.get("kind", "claude"),
        "agent": it.get("agent", "claude-code"),
        # 侧栏配色：True = 这个 tmux 里当前挂着它的那个会话（深色）
        "active": bool(it.get("active")),
    }


def build_index() -> dict:
    """产出一份「tmux 会话分组 -> claude 会话索引」的树。

    侧栏只需要这些字段，永远不带消息正文。
    """
    groups: list[dict] = []
    covered_cwd: set[str] = set()
    sid_to_tmux: dict[str, str] = {}
    sid_to_agent: dict[str, str] = {}
    now = int(time.time() * 1000)

    for s in Tmux.list_sessions():
        panes = s.get("panes") or []
        active = next((p for p in panes if p.get("active")), panes[0] if panes else {})
        cwd = (active or {}).get("cwd") or s.get("path") or ""

        # 每个 pane 单独看两件事：
        #   sa = 启动过什么（pane_start_command，dsh 退出后痕迹还在）
        #   ra = 此刻还活着什么（进程树，dsh 还在跑才算「激活」）
        pane_state: list[tuple[dict, str, str]] = []
        for p in panes:
            toks: list[str] = [p.get("command") or ""]
            toks += re.split(r"[\s'\"]+", p.get("start") or "")
            pane_state.append((p, live.agent_of(toks), live.running_agent(p)))
        agent = next((ra or sa for _, sa, ra in pane_state if (ra or sa)), "")
        any_running = any(ra for _, _, ra in pane_state)

        items = claude.sessions_in(cwd, limit=12) if cwd else []
        for it in items:
            sid_to_tmux.setdefault(it["sid"], s["name"])
            sid_to_agent.setdefault(it["sid"], "claude-code")
            a = claude.anno.get(it["sid"])
            it["title"] = a.get("title") or it["short"]
            it["subtitle"] = a.get("subtitle") or it["ai_title"] or it["last_prompt"][:80] or ""
            it["custom"] = bool(a.get("title") or a.get("subtitle"))
            it["tmux"] = s["name"]

        # 有没有 claude 真的在跑：dsh 在跑不代表 claude 在跑，别把 claude 会话点亮
        pids = [int(p["pid"]) for p in panes if str(p.get("pid") or "").isdigit()]
        held = live.held_sids(pids) if pids else set()
        claude_live = bool(held) or any(ra == "claude" for _, _, ra in pane_state)

        # 每个挂着 agent 的 pane，只要没被上面的 claude 列表覆盖，就列成终端会话
        # （dsh / codex / aider 这些没有 jsonl，只有一条实时屏幕）
        n_term = 0
        for p, sa, ra in pane_state:
            ag = ra or sa
            if not ag:
                continue                      # 纯 shell 的 pane 不列
            if ag == "claude" and items and (p.get("cwd") or "") == cwd:
                continue                      # 已经在上面的 claude 会话列表里
            pane_id = p.get("pane_id") or ""
            # 单 pane 保持老 id（term:<tmux>），多 pane 才加后缀，别动已有客户端
            sid = f"term:{s['name']}" if n_term == 0 else f"term:{s['name']}#{pane_id}"
            n_term += 1
            sub = f"终端 · {ag}"
            if ra:
                t = _screen_title(pane_id)
                if t:
                    sub = f"{ag} · {t}"
            tinfo = {
                "sid": sid,
                "short": (s["name"] if n_term == 1 else f"{s['name']}/{pane_id.lstrip('%')}")[:8],
                "title": s["name"] if n_term == 1 else f"{s['name']} · {pane_id.lstrip('%')}",
                "subtitle": sub,
                # 用 tmux 会话的创建时间，别用 now：否则索引指纹每次都变，
                # ETag / If-None-Match 永远命中不了 304，轮询白白重传整份索引
                "custom": False, "count": 0, "updated_at": int(s.get("created") or 0),
                "branch": "", "tmux": s["name"],
                "kind": "term", "agent": ag,
                # 只有「此刻还在跑」才是深色；只剩启动痕迹 -> 灰
                "active": bool(ra),
            }
            items.append(tinfo)
            sid_to_tmux.setdefault(sid, s["name"])
            sid_to_agent.setdefault(sid, ag)

        # 这个 tmux 里「正在被 claude 用着」的那个会话：只有它深色，其余灰色
        act, src = live.active_sid(panes, [i for i in items if i.get("kind") != "term"], claude_live)
        for it in items:
            if it.get("kind") == "term":
                continue                      # term 项上面已经按「进程是否活着」定过色
            it["active"] = (it["sid"] == act)
        if act:
            # 进程抱着 jsonl 但命令行没被认出来（自定义包装脚本等）：组头也点亮
            agent = agent or "claude"

        groups_items = [_slim(x) for x in items]
        covered_cwd.add(cwd)
        groups.append({
            "kind": "tmux",
            "name": s["name"],
            "cwd": cwd,
            # 组头绿点 = 这个 tmux 里有东西在跑（claude 或 dsh 都算）
            "live": bool(claude_live or any_running),
            "active_sid": act,
            "active_src": src,
            "panes": [{"command": p.get("command", ""), "cwd": p.get("cwd", ""),
                       "active": p.get("active", False)} for p in panes],
            "items": groups_items,
            "count": len(items),
        })

    # 兜底：不在任何 tmux 里的历史会话，单独放一组，避免"找不到昨天那段"
    orphans: list[dict] = []
    seen: set[str] = set()
    if claude.PROJECTS.is_dir():
        for p in sorted(claude.PROJECTS.glob("*/*.jsonl"),
                        key=lambda x: x.stat().st_mtime, reverse=True)[:40]:
            try:
                it = claude.entry(p.stem, str(p))
            except OSError:
                continue
            if it["cwd"] in covered_cwd or it["sid"] in seen:
                continue
            if it["sid"] in sid_to_tmux:
                continue
            a = claude.anno.get(it["sid"])
            it["title"] = a.get("title") or it["short"]
            it["subtitle"] = a.get("subtitle") or it["ai_title"] or it["last_prompt"][:80] or it["cwd"]
            it["custom"] = bool(a.get("title") or a.get("subtitle"))
            it["tmux"] = ""
            seen.add(it["sid"])
            orphans.append(_slim(it))
            if len(orphans) >= 8:
                break
    if orphans:
        groups.append({
            "kind": "orphan", "name": "其他会话", "cwd": "",
            "live": False, "active_sid": "", "active_src": "none",
            "panes": [], "items": orphans, "count": len(orphans),
        })

    return {"ts": now, "groups": groups, "sid_to_tmux": sid_to_tmux,
            "sid_to_agent": sid_to_agent}


class Watch:
    """一个被 attach 的会话：跟踪 jsonl 增长 + tmux 屏幕变化 + 交互提示。

    path 为空表示纯终端会话（dsh/codex 这类没有 jsonl 的），只有屏幕流。
    """

    def __init__(self, sid: str, path: str, tmux_name: str = "", agent: str = "claude-code"):
        self.sid = sid
        self.path = path
        self.tmux_name = tmux_name
        self.agent = agent
        self.last_seq = claude.LineIndex.count(path) if path else 0
        self.last_screen = ""
        self.last_state = ""
        self.last_pid = ""       # 上一轮的提示指纹
        self.prompt = None       # 当前 open 的 PromptSpec

    def poll(self) -> tuple[list[dict], str, dict | None]:
        new_msgs: list[dict] = []
        if self.path:
            new_msgs = claude.since(self.path, self.last_seq)
            if new_msgs:
                self.last_seq = max(m["seq"] for m in new_msgs)
        screen = ""
        if self.tmux_name:
            screen = Tmux.capture(self.tmux_name, lines=40)
        # 屏幕没变就复用上次解析结果，省一次正则
        if screen == self.last_screen:
            return new_msgs, screen, self.prompt
        spec = promptmod.extract(screen, self.agent)
        return new_msgs, screen, spec


class Hub:
    def __init__(self) -> None:
        self.clients: set = set()
        self.watches: dict[str, set] = {}     # sid -> {client}  WebSocket 订阅者
        self._index_cache: tuple[float, dict] | None = None
        self._watch_state: dict[str, Watch] = {}
        self._task: Optional[asyncio.Task] = None
        self._sig: str = ""
        self._pushed: float = 0.0
        self._snap: dict[str, tuple[float, str]] = {}        # pane -> (ts, screen)
        self._pollers: dict[str, tuple[float, float]] = {}   # sid -> (last_seen, 轮询间隔s)
        self._ticks: int = 0                                 # 诊断计数

    # ------------------------------------------------------------ shared work
    async def screen(self, key: str, lines: int = 120, ttl: float = SNAP_TTL) -> str:
        """带 TTL 的抓屏缓存。

        一次 capture-pane 要 fork 4 个子进程；3 个手机各 5 秒轮询一次，
        如果每次都真抓就是 36 次/分钟，缓存后退化成 ≤60 次/分钟且与人数无关。
        """
        if not key:
            return ""
        now = time.time()
        hit = self._snap.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        loop = asyncio.get_running_loop()
        s = await loop.run_in_executor(None, lambda: Tmux.capture(key, lines))
        self._snap[key] = (now, s)
        return s

    @staticmethod
    def etag(s: str) -> str:
        """内容指纹，给 HTTP 的 ETag / If-None-Match 用（没变就回 304，不带 body）。"""
        return '"%d-%s"' % (len(s), hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:16])

    def index_etag(self, data: dict) -> str:
        """索引的指纹只算「影响侧栏显示的字段」。

        直接对整份 JSON 取哈希是不行的：里面有个 ts 每次都变，
        ETag 就永远对不上，304 一次也命中不了。
        """
        return self.etag(_index_sig(data))

    async def _fanout(self, clients, frame: dict) -> None:
        """序列化一次，发给所有人。

        之前是每人各 dumps 一遍：100 个连接 × 16KB 索引 = 100 次 JSON 编码。
        扇出是 O(N) 的写，但编码必须只做一次。

        每个发送都带超时并顺手清掉僵尸：send_text 对一个半开连接（对端断电、
        没 FIN）会永远阻塞，一个僵尸就能把后面所有人的推送卡死。
        """
        if not clients:
            return
        try:
            blob = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return
        for c in list(clients):
            try:
                await asyncio.wait_for(c.send_raw(blob), timeout=3.0)
            except Exception:
                self.drop(c)

    def drop(self, c) -> None:
        """把一个失联/卡死的客户端从所有订阅里清出去。"""
        self.clients.discard(c)
        self.detach_all(c)

    def touch(self, sid: str, interval: float = 5.0) -> Optional[Watch]:
        """REST 侧登记「有人在盯这个会话」。

        只建 watch、不进 WS 扇出列表：tick 会照常刷新它的 screen/prompt，
        轮询者再来读缓存即可。代价与人数无关。
        """
        w = self._ensure_watch(sid)
        if not w:
            return None
        self._pollers[sid] = (time.time(), max(0.5, float(interval)))
        return w

    def poll_state(self, sid: str) -> dict:
        """REST 一次拿全：屏幕 + 提示卡 + 状态。只读缓存，不 fork。"""
        w = self._watch_state.get(sid)
        if not w:
            return {}
        screen = w.last_screen or ""
        return {
            "sid": sid,
            "tmux": w.tmux_name,
            "lines": screen.splitlines()[-120:],
            "prompt": w.prompt,
            "status": w.last_state or "idle",
        }

    def _ensure_watch(self, sid: str) -> Optional[Watch]:
        """按 sid 建/取 watch，逻辑与 attach 一致（抽出来给 REST 复用）。"""
        if sid.startswith("term:"):
            rest = sid[5:]
            pane = ""
            if "#" in rest:
                rest, pane = rest.split("#", 1)
            path, tmux_name = None, (pane or rest)
        else:
            path = claude.find_by_sid(sid)
            if not path:
                return None
            tmux_name = ""
            if self._index_cache:
                tmux_name = (self._index_cache[1].get("sid_to_tmux") or {}).get(sid, "")
        agent = "claude-code"
        if self._index_cache:
            agent = (self._index_cache[1].get("sid_to_agent") or {}).get(sid, agent)
        w = self._watch_state.get(sid)
        if not w:
            w = Watch(sid, path, tmux_name, agent)
            self._watch_state[sid] = w
        return w

    # ---------------------------------------------------------------- index
    async def index(self, force: bool = False) -> dict:
        if not force and self._index_cache and time.time() - self._index_cache[0] < INDEX_TTL:
            return self._index_cache[1]
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, build_index)
        self._index_cache = (time.time(), data)
        return data

    def invalidate(self) -> None:
        self._index_cache = None

    async def broadcast_index(self) -> None:
        data = await self.index(force=True)
        self._sig = _index_sig(data)
        self._pushed = time.time()
        await self._fanout(list(self.clients), {"v": "sbp/0.1", "type": "evt",
                                                "name": "sessions.index", "payload": data})

    async def push_if_changed(self) -> None:
        """侧栏只在「有东西变了」时才推：谁在跑、多了少了会话、最新一条动了。

        REST 轮询者也算订阅者：索引由这一处统一重建，轮询者来读缓存就行，
        否则每个轮询者各自触发一次 60+ fork 的重建。
        """
        if not (self.clients or self._pollers):
            return
        if time.time() - self._pushed < PUSH_TTL:
            return
        data = await self.index(force=True)
        sig = _index_sig(data)
        self._pushed = time.time()
        if sig == self._sig:
            return
        self._sig = sig
        await self._fanout(list(self.clients), {"v": "sbp/0.1", "type": "evt",
                                                "name": "sessions.index", "payload": data})

    # ---------------------------------------------------------------- watch
    def attach(self, client, sid: str) -> Optional[Watch]:
        w = self._ensure_watch(sid)
        if not w:
            return None
        # 文件可能被别的进程追加过，重新对齐尾部
        w.last_seq = max(0, w.last_seq)
        self.watches.setdefault(sid, set()).add(client)
        return w

    def detach(self, client, sid: str) -> None:
        subs = self.watches.get(sid)
        if subs:
            subs.discard(client)
            if not subs:
                self.watches.pop(sid, None)

    def detach_all(self, client) -> None:
        for sid in list(self.watches):
            self.detach(client, sid)

    def _period(self) -> float:
        """下一轮等多久：取所有观察者里最急的那个。

        只有 REST 轮询者时，按「轮询间隔的一半」抓屏就够了（采样定理），
        5s 轮询 → 2.5s 抓一次，比固定 1.2s 省一半 fork。
        有 WS 订阅者时降到 1.2s，因为人家要的是实时。
        """
        p = IDLE_TICK
        if self.watches:
            p = min(p, 1.2)
        for _sid, (_ts, iv) in self._pollers.items():
            p = min(p, max(0.6, iv / 2.0))
        return p

    def _reap(self) -> None:
        """轮询者失联超过 POLL_TTL 就回收它的 watch，别让抓屏常驻。"""
        now = time.time()
        for sid, (ts, _iv) in list(self._pollers.items()):
            if now - ts > POLL_TTL:
                self._pollers.pop(sid, None)
                if not self.watches.get(sid):
                    self._watch_state.pop(sid, None)
                    self._snap.pop(sid, None)

    async def tick_loop(self) -> None:
        """一轮：jsonl 增量 -> session.message；tmux 屏幕变化 -> output.delta。

        WS 订阅者和 REST 轮询者共用这一份刷新结果，所以成本是 O(会话数)，
        不是 O(会话数 × 人数)。
        """
        while True:
            await asyncio.sleep(self._period())
            self._ticks += 1
            try:
                await self.push_if_changed()
            except Exception:
                pass
            self._reap()
            sids = set(self.watches) | set(self._pollers)
            if not sids:
                continue
            loop = asyncio.get_running_loop()
            for sid in list(sids):
                w = self._watch_state.get(sid)
                if not w:
                    continue
                try:
                    msgs, screen, spec = await loop.run_in_executor(None, w.poll)
                except Exception:
                    continue
                clients = list(self.watches.get(sid) or ())
                for m in msgs:
                    await self._fanout(clients, {"v": "sbp/0.1", "type": "evt",
                                                 "name": "session.message", "sid": sid,
                                                 "payload": {"sid": sid, "message": m}})
                if screen and screen != w.last_screen:
                    w.last_screen = screen
                    state = "waiting" if spec else ("running" if _looks_busy(screen) else "idle")
                    await self._fanout(clients, {"v": "sbp/0.1", "type": "evt", "name": "output.delta",
                                                 "sid": sid, "payload": {"sid": sid, "mode": "screen",
                                                                        "lines": screen.splitlines()[-40:]}})
                    if state != w.last_state:
                        await self._fanout(clients, {"v": "sbp/0.1", "type": "evt", "name": "session.state",
                                                     "sid": sid, "payload": {"sid": sid, "status": state}})
                    w.last_state = state
                    # SBP-0.2：提示出现 / 被替换 / 消失
                    new_pid = spec["pid"] if spec else ""
                    if new_pid != w.last_pid:
                        if w.last_pid and w.prompt:
                            await self._fanout(clients, {"v": "sbp/0.1", "type": "evt", "name": "session.prompt",
                                                         "sid": sid, "payload": {"sid": sid, "pid": w.last_pid,
                                                                                 "state": "gone"}})
                        if spec:
                            await self._fanout(clients, {"v": "sbp/0.1", "type": "evt", "name": "session.prompt",
                                                         "sid": sid, "payload": {"sid": sid, **spec}})
                        w.last_pid = new_pid
                        w.prompt = spec


def _index_sig(data: dict) -> str:
    """索引指纹：只在影响侧栏显示的字段上算。"""
    parts = []
    for g in data.get("groups") or []:
        parts.append("|".join([
            g.get("name", ""), g.get("active_sid", ""), g.get("active_src", ""),
            str(g.get("live")), str(g.get("count")),
        ]))
        for it in g.get("items") or []:
            parts.append(f'{it.get("sid")}:{it.get("updated_at")}:{it.get("subtitle","")}:{it.get("title","")}')
    return "\n".join(parts)


def _looks_busy(screen: str) -> bool:
    tail = "\n".join(screen.splitlines()[-6:])
    return any(k in tail for k in ("esc to interrupt", "Thinking", "running", "✻", "· ·"))
