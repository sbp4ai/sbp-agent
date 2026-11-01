"""面板作为 SBP device，反向长连接到本机的 sbp-server。

为什么要这一层
--------------
面板原本只服务本机的浏览器：谁要看会话，就得给这台机器开一个网址。
两台机器就是两个网址，切一次换一个登录态。

加上这条反向连接后：

    panel --bridge ws://127.0.0.1:8787/ws  ──►  本机 sbp-server  ◄── 一个客户端

面板把本机 tmux / CLI 会话上报成一台 device，客户端只要配「服务器地址 + 签名」
就能在下拉里切机器。与 SBP 的整体约束一致：**面板只往外连**，本机 8787 不需要
对外开端口（要不要把它暴露出去，是 nginx 那层的事）。

帧的翻译
--------
hub 的扇出是「一次序列化、发给所有人」，所以这里实现的是 send_raw(blob)：
收到面板自己那份帧，翻成 SBP 帧再发往服务器。面板侧的帧名与服务端略有差异
（sessions.index vs session.index、message 的字段），差异都收在这个文件里。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Optional

from . import claude
from . import prompt as promptmod
from .tmux import Tmux

VERSION = "sbp/0.1"
SUB_PROTO = "sbp.v0.1"
STATE_DIR = Path(os.environ.get("SBP_HOME") or (Path.home() / ".sbp"))
#: 与 sbp-bridge 的 device.json 分开存：两个进程可以同时连同一台 server
DEVICE_FILE = STATE_DIR / "panel-device.json"

CAPS = ["session.index", "session.list", "session.history", "session.attach",
        "session.detach", "session.input", "session.answer", "pane.snapshot",
        "session.subtitle", "ping"]


def now_ms() -> int:
    return int(time.time() * 1000)


def _aiohttp():
    try:
        import aiohttp  # noqa: WPS433
    except ImportError:  # pragma: no cover
        raise RuntimeError("bridge 模式需要 aiohttp：pip install 'tmux-ai-coder-panel[bridge]'")
    return aiohttp


def http_base_of(ws_url: str) -> str:
    """ws://127.0.0.1:8787/ws -> http://127.0.0.1:8787"""
    u = (ws_url or "").strip()
    if u.startswith("wss://"):
        u = "https://" + u[6:]
    elif u.startswith("ws://"):
        u = "http://" + u[5:]
    # 从 "://" 之后开始找，否则会撞上 "http://" 里那两个斜杠，把基址截成 "http:"
    i = u.find("/", u.find("://") + 3)
    return u[:i] if i > 0 else u


class Sink:
    """hub 的扇出目标。

    hub._fanout 调的是 send_raw(预序列化好的字符串)，超时会 drop(c)。
    这里把面板帧翻译成服务端帧；drop 只会把它从订阅里摘掉，不影响重连。
    """

    def __init__(self, bridge: "Bridge"):
        self.b = bridge

    async def send_raw(self, blob: str) -> None:
        try:
            frame = json.loads(blob)
        except Exception:
            return
        await self.b.on_panel_frame(frame)

    async def send(self, obj: dict) -> None:
        await self.b.send(obj)

    async def send_evt(self, name: str, payload: dict, sid: str | None = None) -> None:
        f = {"v": VERSION, "type": "evt", "name": name, "payload": payload}
        if sid:
            f["sid"] = sid
        await self.b.send(f)

    async def close(self, code: int = 1000) -> None:
        pass


class Bridge:
    def __init__(self, ws_url: str, hub, name: str = ""):
        self.ws_url = ws_url
        self.http_base = http_base_of(ws_url)
        self.hub = hub
        self.name = name or socket.gethostname()
        self.sink = Sink(self)
        self.ws = None
        self.device: dict = {}
        self._stop = False

    # ------------------------------------------------------------ 设备身份
    async def _register(self, session) -> None:
        """服务端按 fingerprint 幂等：重连只轮换 secret，不会攒出一堆设备。"""
        fp = hashlib.sha1(f"panel|{socket.gethostname()}|{os.environ.get('USER', '')}".encode()).hexdigest()[:16]
        async with session.post(f"{self.http_base}/api/device/register",
                                json={"name": self.name, "agent": "sbp-panel", "fingerprint": fp}) as r:
            data = await r.json()
        self.device = {"device_id": data["device_id"], "secret": data["device_secret"]}
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        DEVICE_FILE.write_text(json.dumps({"server": self.http_base, **self.device}, indent=2))
        os.chmod(DEVICE_FILE, 0o600)
        print(f"  [bridge] 设备 {data['device_id']} 已注册到 {self.http_base}", file=sys.stderr, flush=True)

    # ------------------------------------------------------------ 出方向
    async def send(self, obj: dict) -> None:
        if self.ws is not None and not self.ws.closed:
            try:
                await self.ws.send_json(obj)
            except Exception:
                pass

    async def _push_index(self, force: bool = False) -> None:
        """把本机会话列表推给服务端。"""
        data = await self.hub.index(force=force)
        sessions = []
        for g in data.get("groups") or []:
            for it in g.get("items") or []:
                sessions.append({
                    "sid": it.get("sid", ""),
                    "title": it.get("title") or it.get("short") or it.get("sid", ""),
                    "subtitle": it.get("subtitle") or "",
                    "kind": it.get("kind") or "claude",
                    "agent": it.get("agent") or "claude-code",
                    "count": it.get("count", 0),
                    "updated_at": it.get("updated_at", 0),
                    "tmux": it.get("tmux", ""),
                    "has_history": bool(it.get("has_history")),
                    "status": "running" if it.get("active") else "idle",
                })
        await self.send({"v": VERSION, "type": "evt", "name": "session.index", "ts": now_ms(),
                         "payload": {"sessions": sessions, "device": self.name}})

    async def on_panel_frame(self, frame: dict) -> None:
        """面板帧 -> SBP 帧。"""
        name = frame.get("name")
        p = frame.get("payload") or {}

        if name == "sessions.index":
            await self._push_index(force=True)
            return

        if name == "session.message":
            m = p.get("message") or {}
            await self.send({
                "v": VERSION, "type": "evt", "name": "session.message",
                "sid": frame.get("sid") or p.get("sid") or "", "ts": now_ms(),
                "payload": {"role": m.get("role", "agent"), "kind": m.get("kind", "text"),
                            "text": m.get("text", ""), "ts": m.get("ts") or now_ms(),
                            "meta": {"seq": m.get("seq"), "tools": (m.get("tools") or [])[:4],
                                     "model": m.get("model", "")}},
            })
            return

        if name == "output.delta":
            await self.send({"v": VERSION, "type": "evt", "name": "output.delta",
                             "sid": frame.get("sid") or p.get("sid") or "", "ts": now_ms(),
                             "payload": {"mode": p.get("mode", "screen"), "lines": p.get("lines") or []}})
            return

        if name == "session.prompt":
            await self.send({"v": VERSION, "type": "evt", "name": "session.prompt",
                             "sid": frame.get("sid") or p.get("sid") or "", "ts": now_ms(),
                             "payload": {k: v for k, v in p.items() if k != "sid"}})
            return

        if name == "session.state":
            await self.send({"v": VERSION, "type": "evt", "name": "session.state",
                             "sid": frame.get("sid") or p.get("sid") or "", "ts": now_ms(),
                             "payload": {"status": p.get("status", "idle")}})
            return

    # ------------------------------------------------------------ 入方向
    async def _on_req(self, frame: dict) -> None:
        name = frame.get("name")
        p = dict(frame.get("payload") or {})
        p.pop("_from", None)
        p.pop("_caps", None)
        sid = frame.get("sid") or p.get("sid") or ""

        def res(ok: bool, data: dict | None = None, err: dict | None = None) -> dict:
            out = {"v": VERSION, "type": "res", "ref": frame.get("id"), "name": name, "ok": ok, "ts": now_ms()}
            if data is not None:
                out["payload"] = data
            if err:
                out["err"] = err
            return out

        if name == "ping":
            await self.send(res(True, {"t": p.get("t"), "ts": now_ms()}))
            return

        if name == "session.attach":
            await self.hub.index()
            w = self.hub.attach(self.sink, sid)
            if not w:
                await self.send(res(False, None, {"code": "E_NO_SESSION", "msg": "会话不存在"}))
                return
            screen = Tmux.capture(w.tmux_name, lines=40) if w.tmux_name else ""
            spec = promptmod.extract(screen, w.agent) if screen else None
            w.last_screen, w.prompt, w.last_pid = screen, spec, (spec["pid"] if spec else "")
            await self.send(res(True, {"sid": sid, "tmux": w.tmux_name, "last_seq": w.last_seq,
                                       "kind": "term" if not w.path else "claude",
                                       "editable": bool(w.tmux_name), "prompt": spec}))
            if screen:
                await self.send({"v": VERSION, "type": "evt", "name": "output.delta", "sid": sid,
                                 "ts": now_ms(), "payload": {"mode": "screen", "lines": screen.splitlines()[-40:]}})
            if spec:
                await self.send({"v": VERSION, "type": "evt", "name": "session.prompt", "sid": sid,
                                 "ts": now_ms(), "payload": {k: v for k, v in spec.items() if k != "sid"}})
            return

        if name == "session.detach":
            self.hub.detach(self.sink, sid)
            await self.send(res(True, {"sid": sid}))
            return

        if name == "session.history":
            await self.hub.index()
            path = self.hub.resolve_path(sid)
            if not path:
                await self.send(res(True, {"sid": sid, "items": [], "has_more": False,
                                           "next_before": None, "total": 0}))
                return
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, lambda: claude.page(path, p.get("before"), int(p.get("limit") or 20)))
            await self.send(res(True, {"sid": sid, **data}))
            return

        if name == "pane.snapshot":
            w = self.hub._watch_state.get(sid)
            tmux_name = (w.tmux_name if w else "") or (
                (self.hub._index_cache or (0, {}))[1].get("sid_to_tmux") or {}).get(sid, "")
            if not tmux_name:
                await self.send(res(False, None, {"code": "E_NO_TMUX", "msg": "该会话不在 tmux 中"}))
                return
            screen = await self.hub.screen(tmux_name, int(p.get("lines") or 120))
            spec = promptmod.extract(screen, (w.agent if w else "claude-code")) if screen else None
            await self.send(res(True, {"sid": sid, "tmux": tmux_name,
                                       "lines": screen.splitlines(), "prompt": spec}))
            return

        if name == "session.input":
            w = self.hub._watch_state.get(sid)
            tmux_name = (w.tmux_name if w else "") or (
                (self.hub._index_cache or (0, {}))[1].get("sid_to_tmux") or {}).get(sid, "")
            if not tmux_name:
                await self.send(res(False, None, {"code": "E_NO_TMUX", "msg": "该会话不在 tmux 中，无法输入"}))
                return
            loop = asyncio.get_running_loop()
            if (p.get("kind") or "text") == "key":
                ok = await loop.run_in_executor(None, lambda: Tmux.send_key(tmux_name, str(p.get("key") or "ENTER")))
            else:
                ok = await loop.run_in_executor(
                    None, lambda: Tmux.send_text(tmux_name, str(p.get("text") or ""), bool(p.get("submit", True))))
            await self.send(res(ok, {"sid": sid}, None if ok else {"code": "E_INTERNAL", "msg": "tmux 写入失败"}))
            return

        if name == "session.answer":
            await self._answer(frame, res)
            return

        if name == "session.subtitle":
            claude.anno.set(sid, title=p.get("title"), subtitle=p.get("subtitle"))
            self.hub.invalidate()
            await self.send(res(True, {"sid": sid, "anno": claude.anno.get(sid)}))
            await self._push_index(force=True)
            return

        await self.send(res(False, None, {"code": "E_UNKNOWN_NAME", "msg": f"未知请求 {name}"}))

    async def _answer(self, frame: dict, res) -> None:
        """SBP-0.2：把一次选项点击翻译成终端写入。逻辑与面板自己的 _dispatch 一致。"""
        p = frame.get("payload") or {}
        sid = frame.get("sid") or p.get("sid") or ""
        w = self.hub._watch_state.get(sid)
        spec = w.prompt if w else None
        pid = p.get("pid") or ""
        if not spec or (pid and spec["pid"] != pid):
            await self.send(res(False, None, {"code": "E_PROMPT_STALE", "msg": "该选项已过期，请按屏幕最新内容操作"}))
            return
        tmux_name = (w.tmux_name if w else "") or (
            (self.hub._index_cache or (0, {}))[1].get("sid_to_tmux") or {}).get(sid, "")
        if not tmux_name:
            await self.send(res(False, None, {"code": "E_NO_TMUX", "msg": "会话不可写"}))
            return
        writes = promptmod.answer_writes(spec, p)
        if not writes:
            await self.send(res(True, {"sid": sid, "pid": spec["pid"], "state": "answered", "noop": True}))
            return
        loop = asyncio.get_running_loop()
        for i, (text, submit) in enumerate(writes):
            if i:
                await asyncio.sleep(0.3)
            ok = await loop.run_in_executor(None, lambda t=text, s=submit: Tmux.send_text(tmux_name, t, s))
            if not ok:
                await self.send(res(False, None, {"code": "E_INTERNAL", "msg": "tmux 写入失败"}))
                return
        if w:
            # 记住 pid：下一轮抓到「作答回显」那屏时会解析出同一个 pid，
            # 不清掉它就会把刚收起的卡片又弹回来
            w.prompt = None
            w.last_pid = spec["pid"]
        await self.send(res(True, {"sid": sid, "pid": spec["pid"], "state": "answered"}))
        await self.on_panel_frame({"v": VERSION, "type": "evt", "name": "session.prompt", "sid": sid,
                                   "payload": {"sid": sid, "pid": spec["pid"], "state": "answered"}})

    # ------------------------------------------------------------ 主循环
    async def run(self) -> None:
        aiohttp = _aiohttp()
        backoff = 1.0
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                try:
                    await self._register(session)
                    async with session.ws_connect(self.ws_url, protocols=(SUB_PROTO,), heartbeat=25) as ws:
                        self.ws = ws
                        await ws.send_json({
                            "v": VERSION, "id": "h1", "type": "req", "name": "hello", "ts": now_ms(),
                            "payload": {"role": "device", "device_id": self.device["device_id"],
                                        "secret": self.device["secret"],
                                        "agent": {"name": "sbp-panel", "host": socket.gethostname()},
                                        "caps": CAPS},
                        })
                        await self._push_index(force=True)
                        # 进 hub.clients：索引变了会顺着 sessions.index 推过来
                        self.hub.clients.add(self.sink)
                        backoff = 1.0
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            frame = json.loads(msg.data or "{}")
                            if frame.get("name") == "ping":
                                await self.send({"v": VERSION, "type": "evt", "name": "pong", "ts": now_ms()})
                                continue
                            if frame.get("type") == "req":
                                await self._on_req(frame)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    print(f"  [bridge] 断线：{exc}", file=sys.stderr, flush=True)
                finally:
                    self.hub.clients.discard(self.sink)
                    self.hub.detach_all(self.sink)
                    self.ws = None
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)

    def stop(self) -> None:
        self._stop = True


def start_bridge(hub, url: str, name: str = "") -> Optional[asyncio.Task]:
    """在面板进程里拉起 bridge 任务。url 为空就不启（普通面板用法不受影响）。"""
    url = (url or os.environ.get("SBP_BRIDGE") or "").strip()
    if not url:
        return None
    b = Bridge(url, hub, name=name)
    print(f"  [bridge] 上报到 {url}", file=sys.stderr, flush=True)
    return asyncio.create_task(b.run(), name="sbp-bridge")
