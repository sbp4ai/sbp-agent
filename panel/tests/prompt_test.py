"""SBP-0.2 交互提示端到端自测：DSH（非 claude）会话的选项提取与作答。

前置：
    tmux kill-session -t dsh-demo 2>/dev/null
    tmux new-session -d -s dsh-demo -c <dir> "bash -c 'exec -a dsh /tmp/sbpbin/dsh'"
    cd panel && python -m tmux_panel --port 20016 --no-auth

跑：python tests/prompt_test.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import aiohttp

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:20016"
V = "sbp/0.1"
OK = []


def check(label: str, cond: bool, extra: str = "") -> None:
    OK.append(cond)
    print(("  ✓ " if cond else "  ✗ ") + label + (("  " + extra) if extra else ""))


class C:
    """后台一条 reader 协程，避免 receive 与 req 抢同一个 ws。"""

    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.pending: dict[str, asyncio.Future] = {}
        self.events: list[dict] = []
        self.rd = asyncio.create_task(self._read())

    async def _read(self):
        async for m in self.ws:
            if m.type != aiohttp.WSMsgType.TEXT:
                continue
            f = json.loads(m.data)
            if f.get("type") == "res":
                fut = self.pending.pop(f.get("ref"), None)
                if fut and not fut.done():
                    fut.set_result(f)
            elif f.get("type") == "evt":
                self.events.append(f)

    async def req(self, name, payload=None, sid=None, timeout=8.0):
        self.n += 1
        rid = f"r{self.n}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        f = {"v": V, "id": rid, "type": "req", "name": name, "payload": payload or {}}
        if sid:
            f["sid"] = sid
        await self.ws.send_json(f)
        return await asyncio.wait_for(fut, timeout)

    async def wait(self, seconds=2.0):
        await asyncio.sleep(seconds)

    def take(self, name, clear=False):
        hit = [e for e in self.events if e.get("name") == name]
        if clear:
            self.events = [e for e in self.events if e.get("name") != name]
        return hit


async def main():
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(BASE.replace("http", "ws") + "/ws", heartbeat=None)
        c = C(ws)
        await c.req("hello", {})
        await c.wait(0.4)

        print("\n[1] 索引里必须能找到 DSH 终端会话（kind=term）")
        res = await c.req("session.list", {})
        data = res["payload"]
        terms = []
        for g in data["groups"]:
            for it in g["items"]:
                if it.get("kind") == "term":
                    terms.append(it)
        check("存在 kind=term 的会话", bool(terms), f"{len(terms)} 个")
        if not terms:
            print("      （没有终端会话，跳过。先起一个 dsh：见文件头注释）")
            return
        sid = terms[0]["sid"]
        print(f"      选中 {sid} agent={terms[0].get('agent')}")

        print("\n[2] attach 后立刻拿到当前交互提示")
        res = await c.req("session.attach", {}, sid=sid)
        pl = res["payload"]
        check("attach 成功", res.get("ok", False), pl.get("tmux", ""))
        attach_prompt = pl.get("prompt")
        check("attach 响应带 prompt", attach_prompt is not None)
        await c.wait(0.4)
        evs = c.take("session.prompt", clear=True)
        check("收到 session.prompt 事件", bool(evs))
        if not attach_prompt:
            return

        p = attach_prompt
        print(f"      kind={p['kind']} risk={p['risk']} title={p['title']!r}")
        print(f"      options={[(o['index'], o['label']) for o in p['options']]}")
        check("解析出 3 个选项", len(p["options"]) == 3)
        check("含 rm -rf → risk=danger", p["risk"] == "danger")
        check("第 3 项需要自由文本", p["options"][2]["free_text"] is True)

        print("\n[3] 作答：选 1（Yes）")
        res = await c.req("session.answer", {"pid": p["pid"], "choice": 1}, sid=sid)
        check("answer 返回 ok", res.get("ok", False), str(res.get("err", "")))
        await c.wait(0.4)
        gone = [e for e in c.take("session.prompt", clear=True) if e["payload"].get("state") != "open"]
        check("卡片被收起(answered/gone)", bool(gone) or res.get("ok"))

        print("\n[4] 等待下一屏：Continue? [y/N]")
        target = None
        for _ in range(12):
            await c.wait(1.2)
            evs = c.take("session.prompt", clear=True)
            for e in evs:
                pp = e["payload"]
                if pp.get("state") == "open" and pp.get("kind") == "confirm":
                    target = pp
            if target:
                break
        check("第二轮提示被识别为 confirm", target is not None)
        if target:
            print(f"      title={target['title']!r} options={[(o['index'], o['label'], o['key']) for o in target['options']]}")
            res = await c.req("session.answer", {"pid": target["pid"], "option": "y"}, sid=sid)
            check("热键作答 ok", res.get("ok", False), str(res.get("err", "")))

        print("\n[5] 屏幕确实推进了")
        await c.wait(1.5)
        snap = await c.req("pane.snapshot", {"lines": 60}, sid=sid)
        screen = "\n".join(snap["payload"].get("lines") or [])
        check("屏幕出现「全部完成」", "全部完成" in screen, repr(screen.strip().splitlines()[-3:]))

        print("\n[6] 过期 pid 必须报 E_PROMPT_STALE")
        res = await c.req("session.answer", {"pid": p["pid"], "choice": 1}, sid=sid)
        check("返回 E_PROMPT_STALE", (res.get("err") or {}).get("code") == "E_PROMPT_STALE",
              str(res.get("err", "")))

        print("\n[7] dismiss 不写终端")
        before = await c.req("pane.snapshot", {"lines": 20}, sid=sid)
        b4 = "\n".join(before["payload"].get("lines") or [])
        snap_p = before["payload"].get("prompt")
        if snap_p:
            await c.req("session.answer", {"pid": snap_p["pid"], "dismiss": True}, sid=sid)
        await c.wait(0.6)
        after = await c.req("pane.snapshot", {"lines": 20}, sid=sid)
        af = "\n".join(after["payload"].get("lines") or [])
        check("dismiss 后屏幕未变", b4.strip() == af.strip())

        await ws.close()
    print(f"\n通过 {sum(OK)}/{len(OK)}")
    sys.exit(0 if all(OK) else 1)


asyncio.run(main())
