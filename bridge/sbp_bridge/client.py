"""sbp-bridge: 本地 agent 会话 -> SBP 反向长连接。"""
from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path

import aiohttp

from .adapters import Adapter, load_dir, pick
from .screen import Screen
from .source import PtySource, TmuxSource
from . import prompt as promptmod

VERSION = "sbp/0.1"
SUB_PROTO = "sbp.v0.1"
STATE_DIR = Path(os.environ.get("SBP_HOME") or (Path.home() / ".sbp"))
REPO = Path(__file__).resolve().parents[2]
ADAPTER_DIR = REPO / "spec" / "adapters"


def now_ms() -> int:
    return int(time.time() * 1000)


def load_device() -> dict | None:
    p = STATE_DIR / "device.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def save_device(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / "device.json"
    p.write_text(json.dumps(data, indent=2))
    os.chmod(p, 0o600)


class Bridge:
    def __init__(self, *, http_base: str, ws_url: str, cmd: list[str], adapter: Adapter,
                 cwd: str, rows: int, cols: int, title: str, auto_bind: bool = False,
                 sid: str | None = None, session_limit: int = 1):
        self.http_base = http_base.rstrip("/")
        self.ws_url = ws_url
        self.cmd = cmd
        self.adapter = adapter
        self.cwd = cwd
        self.rows, self.cols = rows, cols
        self.title = title
        self.auto_bind = auto_bind
        self.sid = sid or self._derive_sid()
        self.screen = Screen(rows=rows, cols=cols)
        self.source: PtySource | TmuxSource | None = None
        self.ws: aiohttp.ClientWSSession | None = None
        self.prev_lines: list[str] | None = None
        self.last_data = 0.0
        self.pending_turn: dict | None = None
        self.last_state = "idle"
        self.last_pid = ""          # SBP-0.2：上一屏的提示指纹
        self.prompt: dict | None = None
        self.device: dict = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ setup
    def _derive_sid(self) -> str:
        h = hashlib.sha1(f"{Path(self.cwd).resolve()}|{self.adapter.agent}".encode()).hexdigest()[:8]
        return f"s_{h}"

    async def ensure_device(self, http: aiohttp.ClientSession) -> None:
        # 服务端按 fingerprint 幂等：同一台机器重连只会轮换 secret，不会新建设备
        fp = hashlib.sha1(f"{socket.gethostname()}|{getpass.getuser()}".encode()).hexdigest()[:16]
        async with http.post(f"{self.http_base}/api/device/register",
                             json={"name": socket.gethostname(), "agent": self.adapter.agent,
                                   "fingerprint": fp}) as r:
            data = await r.json()
        self.device = {
            "device_id": data["device_id"],
            "secret": data["device_secret"],
            "server": self.http_base,
            "bind_code": data["bind_code"],
            "bind_url": data["bind_url"],
        }
        save_device(self.device)
        print(f"\n  设备已注册: {data['device_id']}")
        print(f"  绑定码    : {data['bind_code']}   (10 分钟内有效)")
        print(f"  绑定链接  : {data['bind_url']}\n")
        if self.auto_bind:
            r2 = await http.post(f"{self.http_base}/api/bind",
                                 json={"code": data["bind_code"], "im_user": "local-demo", "channel": "cli"})
            tok = await r2.json()
            if tok.get("ok"):
                url = f"{self.http_base}/#t={tok['client_token']}&d={data['device_id']}&s={self.sid}"
                print(f"  [demo] 网页入口: {url}\n")
                (STATE_DIR / "last_demo_url.txt").write_text(url)

    # ------------------------------------------------------------ source
    def start_source(self, loop: asyncio.AbstractEventLoop) -> None:
        t = self.adapter.transport
        ctx = {"sid": self.sid, "rows": self.rows, "cols": self.cols, "cwd": self.cwd,
               "shell": os.environ.get("SHELL", "/bin/zsh"), "title": self.title}
        if t.get("type") == "tmux":
            self.source = TmuxSource(target=self.sid, on_data=self._on_data,
                                     rows=self.rows,
                                     interval_ms=int(self.adapter.capture().get("interval_ms", 500)))
        else:
            argv = self.cmd or self.adapter.spawn_argv(ctx)
            self.source = PtySource(argv, self._on_data, cwd=self.cwd,
                                    env=t.get("env") or {}, cols=self.cols, rows=self.rows)
        self.source.start(loop)

    def _on_data(self, data: bytes) -> None:
        self.screen.feed(data)
        self.last_data = time.time()

    # ------------------------------------------------------------ ws
    async def connect(self, session: aiohttp.ClientSession) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                ws = await session.ws_connect(self.ws_url, protocols=(SUB_PROTO,), heartbeat=25)
                self.ws = ws
                await self.send({
                    "v": VERSION, "id": "h1", "type": "req", "name": "hello",
                    "ts": now_ms(),
                    "payload": {
                        "role": "device",
                        "device_id": self.device["device_id"],
                        "secret": self.device["secret"],
                        "agent": {"name": self.adapter.agent, "adapter": self.adapter.name},
                        "caps": self.adapter.caps,
                    },
                })
                await self.emit_index()
                await self._pump(ws)
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001
                print(f"  [ws] 连接断开: {exc}", file=sys.stderr)
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def send(self, obj: dict) -> None:
        if self.ws and not self.ws.closed:
            try:
                await self.ws.send_json(obj)
            except Exception:
                pass

    async def _pump(self, ws) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data or "{}")
            if frame.get("type") == "req":
                await self._on_req(frame)

    # ------------------------------------------------------------ inbound
    async def _on_req(self, frame: dict) -> None:
        name = frame.get("name")
        payload = frame.get("payload") or {}
        sid = frame.get("sid") or payload.get("sid") or self.sid
        ok, err = True, None
        if os.environ.get("SBP_DEBUG"):
            print(f"  [dbg] req {name} sid={sid} self={self.sid}", flush=True)

        if name == "session.input":
            if sid != self.sid:
                ok, err = False, {"code": "E_NO_SESSION", "msg": "会话不存在"}
            else:
                await self._apply_input(payload)
        elif name == "session.resize":
            if self.source:
                self.source.resize(int(payload.get("cols", self.cols)), int(payload.get("rows", self.rows)))
                self.screen.rows = int(payload.get("rows", self.rows))
                self.prev_lines = None
        elif name == "session.snapshot":
            self.prev_lines = None
            await self.emit_screen(force=True)
        elif name == "session.answer":
            await self._apply_answer(frame)
            return                      # _apply_answer 自己回包
        elif name == "session.spawn":
            ok, err = False, {"code": "E_NOT_ALLOWED", "msg": "该 adapter 不支持新建会话"}
        else:
            ok, err = False, {"code": "E_UNKNOWN_NAME", "msg": f"未知请求 {name}"}

        res = {"v": VERSION, "type": "res", "ref": frame.get("id"), "name": name, "ok": ok, "ts": now_ms()}
        if err:
            res["err"] = err
        await self.send(res)

    async def _apply_answer(self, frame: dict) -> None:
        """SBP-0.2：把一次选项点击翻译成终端写入。"""
        payload = frame.get("payload") or {}

        async def reply(ok: bool, data: dict | None = None, err: dict | None = None) -> None:
            r = {"v": VERSION, "type": "res", "ref": frame.get("id"), "name": "session.answer",
                 "ok": ok, "ts": now_ms()}
            if data:
                r["payload"] = data
            if err:
                r["err"] = err
            await self.send(r)

        spec = self.prompt
        pid = payload.get("pid") or ""
        if not spec or (pid and spec["pid"] != pid):
            await reply(False, None, {"code": "E_PROMPT_STALE", "msg": "该选项已过期"})
            return

        writes = promptmod.answer_writes(spec, payload)
        if not writes:
            # dismiss / 空作答：只收卡片，不碰终端
            await reply(True, {"pid": spec["pid"], "state": "answered", "noop": True})
            return

        for i, (text, submit) in enumerate(writes):
            if i:
                await asyncio.sleep(0.3)      # 等 agent 把输入框画出来
            if self.source:
                self.source.write(text.encode())
                if submit:
                    self.source.write(b"\r")

        # 卡片收起，但记住 pid：下一屏若是同一提示（作答回显），别再弹回来
        self.prompt = None
        self.last_pid = spec["pid"]
        await reply(True, {"pid": spec["pid"], "state": "answered"})
        await self.send({"v": VERSION, "type": "evt", "name": "session.prompt", "sid": self.sid,
                         "ts": now_ms(), "payload": {"pid": spec["pid"], "state": "answered"}})
        if payload.get("choice") is not None:
            hit = [o for o in spec["options"] if o["index"] == int(payload["choice"])]
            await self.emit_message("user", "choice",
                                    f"{payload['choice']}. {hit[0]['label'] if hit else ''}")
        elif payload.get("text"):
            await self.emit_message("user", "text", str(payload["text"]))

    async def _apply_input(self, payload: dict) -> None:
        kind = payload.get("kind", "text")
        text = str(payload.get("text", ""))
        ctx = {"text": text, "index": payload.get("index", 1), "label": payload.get("label", ""),
               "key": payload.get("key", ""), "value": bool(payload.get("value")), "sid": self.sid}
        writes = self.adapter.action(f"input.{kind}", ctx)
        if os.environ.get("SBP_DEBUG"):
            print(f"  [dbg] input kind={kind} writes={writes!r}", flush=True)
        for w in writes:
            if self.source:
                self.source.write(w)

        if kind == "interrupt":
            await self.emit_message("user", "text", "[中断]")
        elif kind == "choice":
            await self.emit_message("user", "choice", f"{payload.get('index')}. {payload.get('label', '')}")
        elif kind == "approve":
            await self.emit_message("user", "text", "允许" if payload.get("value") else "拒绝")
        elif kind == "key":
            await self.emit_message("user", "text", f"[{payload.get('key')}]")
        elif text:
            await self.emit_message("user", "text", text)
        self.pending_turn = {"mark": self.screen.mark, "echo": text, "ts": time.time()}

    # ------------------------------------------------------------ outbound
    async def emit_index(self) -> None:
        entry = {
            "sid": self.sid, "title": self.title, "agent": self.adapter.agent,
            "adapter": self.adapter.name, "status": self.last_state,
            "updated_at": now_ms(),
            "caps": self.adapter.caps,
            "preview": (self.screen.snapshot() or [""])[-1][:120],
        }
        await self.send({"v": VERSION, "type": "evt", "name": "session.index", "ts": now_ms(),
                         "payload": {"sessions": [entry]}})

    _last_seq = 0
    _msg_count = 0

    async def emit_message(self, role: str, kind: str, text: str) -> None:
        await self.send({"v": VERSION, "type": "evt", "name": "session.message", "sid": self.sid,
                         "ts": now_ms(),
                         "payload": {"role": role, "kind": kind, "text": text, "ts": now_ms()}})

    async def emit_screen(self, force: bool = False) -> None:
        d = self.screen.diff(None if force else self.prev_lines)
        if not d.get("changed"):
            return
        self.prev_lines = self.screen.snapshot()
        payload = {"mode": "screen", "rows": self.rows, "cols": self.cols,
                   "lines": d["lines"], "full": d.get("full", False),
                   "cursor": {"row": self.rows - 1, "col": self.screen.col},
                   "ts": now_ms()}
        if not payload["full"]:
            payload["start"] = d.get("start", 0)
        await self.send({"v": VERSION, "type": "evt", "name": "output.delta", "sid": self.sid,
                         "payload": payload})
        await self.emit_prompt()

    # ------------------------------------------------------------ SBP-0.2
    async def emit_prompt(self) -> None:
        """把终端上的选项变成结构化卡片；解得不出来就什么都不发。"""
        text = "\n".join(self.screen.snapshot())
        spec = promptmod.extract(text, self.adapter.agent)
        pid = spec["pid"] if spec else ""
        if pid == self.last_pid:
            return
        if self.last_pid and self.prompt:
            await self.send({"v": VERSION, "type": "evt", "name": "session.prompt", "sid": self.sid,
                             "ts": now_ms(), "payload": {"pid": self.last_pid, "state": "gone"}})
        if spec:
            await self.send({"v": VERSION, "type": "evt", "name": "session.prompt", "sid": self.sid,
                             "ts": now_ms(), "payload": spec})
        self.last_pid = pid
        self.prompt = spec

    # ------------------------------------------------------------ loops
    async def tick(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(0.25)
            try:
                await self.emit_screen()
                text = "\n".join(self.screen.snapshot())
                state = self.adapter.state_of(text)
                if state != self.last_state:
                    self.last_state = state
                    await self.send({"v": VERSION, "type": "evt", "name": "session.state",
                                     "sid": self.sid, "ts": now_ms(),
                                     "payload": {"status": state,
                                                 "choices": self.adapter.parse_choices(text)}})
                # 一轮输出静默 -> 落一条 agent 消息（demo 用；真 adapter 由工具事件驱动）
                if self.pending_turn and time.time() - self.last_data > 0.9:
                    mark = self.pending_turn["mark"]
                    body = self.screen.tail_text(mark)
                    echo = self.pending_turn.get("echo")
                    lines = body.splitlines()
                    if lines and echo and lines[0].strip() == echo.strip():
                        lines = lines[1:]
                    body = "\n".join(lines).strip()
                    self.pending_turn = None
                    if body:
                        await self.emit_message("agent", "text", body)
            except Exception:
                continue

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        async with aiohttp.ClientSession() as http:
            await self.ensure_device(http)
            self.start_source(loop)
            await asyncio.sleep(0.4)
            self.prev_lines = None
            tick_task = loop.create_task(self.tick())
            try:
                await self.connect(http)
            finally:
                tick_task.cancel()
                if self.source:
                    self.source.close()


def default_adapter(cmd: list[str], explicit: str | None) -> Adapter:
    adapters = load_dir(ADAPTER_DIR)
    return pick(adapters, cmd[0] if cmd else "sh", explicit)
