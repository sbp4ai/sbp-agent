"""按「微信小程序会发的那串帧」跑一遍，验证 panel 对 minis/ 的兼容性。

minis/utils/sbp.js 发出的每一个帧，这里都发一次，字段都按小程序的写法。

前置：
    cd panel && python -m tmux_panel --port 20016 --no-auth
    tmux new-session -d -s dsh-demo -c <dir> "bash -c 'exec -a dsh /tmp/sbpbin/dsh'"

跑：python tests/minis_sim.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import aiohttp

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:20016"
V = "sbp/0.1"
RESULTS = []


def ck(label: str, cond: bool, extra: str = "") -> bool:
    RESULTS.append(cond)
    print(("  ✓ " if cond else "  ✗ ") + label + (("  " + extra) if extra else ""))
    return cond


class Mini:
    """小程序侧 sbp.js 的等价实现（只保留会用到的部分）。"""

    def __init__(self, ws):
        self.ws, self.n, self.pend, self.evts = ws, 0, {}, []

    async def _read(self):
        async for m in self.ws:
            if m.type != aiohttp.WSMsgType.TEXT:
                continue
            f = json.loads(m.data)
            if f.get("type") == "res":
                fut = self.pend.pop(f.get("ref"), None)
                if fut and not fut.done():
                    fut.set_result(f)
            elif f.get("type") == "evt":
                self.evts.append(f)

    async def req(self, name, payload=None, sid=None):
        self.n += 1
        rid = "m" + str(self.n)
        fut = asyncio.get_running_loop().create_future()
        self.pend[rid] = fut
        f = {"v": V, "id": rid, "type": "req", "name": name, "payload": payload or {}}
        if sid:
            f["sid"] = sid
        await self.ws.send_json(f)
        return await asyncio.wait_for(fut, 8)

    def take(self, name):
        hit = [e["payload"] for e in self.evts if e.get("name") == name]
        self.evts = [e for e in self.evts if e.get("name") != name]
        return hit


async def main():
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(BASE.replace("http", "ws") + "/ws", heartbeat=None)
        c = Mini(ws)
        rd = asyncio.create_task(c._read())

        print("\n[1] hello（小程序握手）")
        r = await c.req("hello", {})
        caps = (r.get("payload") or {}).get("caps") or []
        ck("hello 有响应", r.get("ok", False))
        ck("caps 含 session.prompt（0.2）", "session.prompt" in caps, str(caps[:4]))

        print("\n[2] session.list（列表页只拉索引）")
        r = await c.req("session.list")
        idx = r["payload"]
        groups = idx.get("groups") or []
        total = sum(len(g.get("items") or []) for g in groups)
        ck("返回 groups 结构", bool(groups), f"{len(groups)} 组 / {total} 个会话")
        # 懒加载的硬指标：索引里不能出现正文
        blob = json.dumps(idx, ensure_ascii=False)
        sample = None
        term_sid = None
        for g in groups:
            for it in g["items"]:
                if it.get("kind") == "term" and not term_sid:
                    term_sid = it["sid"]
                if it.get("kind") == "claude" and not sample:
                    sample = it
        ck("索引里有正文文本键", "text" not in blob, f"{len(blob)//1024}KB / {total} 条")

        # ---- claude 会话：读路径
        if sample:
            print(f"\n[3] claude 会话 {sample['short']} 的懒加载")
            sid = sample["sid"]
            r = await c.req("session.attach", {"sid": sid}, sid=sid)
            pl = r["payload"]
            ck("attach ok", r.get("ok", False))
            ck("attach 带 editable 字段", "editable" in pl, str(pl.get("editable")))
            r = await c.req("session.history", {"sid": sid, "before": None, "limit": 20}, sid=sid)
            h1 = r["payload"]
            items1 = h1.get("items") or []
            ck("首屏 20 条以内", 0 < len(items1) <= 20, f"{len(items1)} 条 / 共 {h1.get('total')}")
            ck("has_more 有值", "has_more" in h1)
            if h1.get("has_more"):
                nb = h1.get("next_before")
                r = await c.req("session.history", {"sid": sid, "before": nb, "limit": 20}, sid=sid)
                h2 = r["payload"]
                items2 = h2.get("items") or []
                ck("翻页拿到更早内容", len(items2) > 0, f"{len(items2)} 条")
                if items1 and items2:
                    ck("页内 seq 升序", items1[0]["seq"] < items1[-1]["seq"])
                    ck("翻页确实更早", items2[-1]["seq"] < items1[0]["seq"],
                       f"{items2[-1]['seq']} < {items1[0]['seq']}")
            await c.req("session.detach", {"sid": sid}, sid=sid)
            print("      （claude 会话只做读取，避免污染真实 tmux）")

        # ---- 终端会话：交互路径
        if not term_sid:
            print("\n[4] 跳过终端会话测试：没有跑着 agent 的 tmux 会话")
        else:
            print(f"\n[4] 终端会话 {term_sid}：屏幕 + 选项卡片")
            r = await c.req("session.attach", {"sid": term_sid}, sid=term_sid)
            pl = r["payload"]
            ck("kind=term", pl.get("kind") == "term", str(pl.get("kind")))
            ck("attach 就带 prompt", bool(pl.get("prompt")))
            p = pl.get("prompt")
            if p:
                print(f"      {p['kind']} / {p['risk']} / {p['title']}")
                print(f"      {[(o['index'], o['label']) for o in p['options']]}")
                r = await c.req("session.answer", {"pid": p["pid"], "choice": 1}, sid=term_sid)
                ck("点第 1 项后作答成功", r.get("ok", False), str(r.get("err", "")))
                # 等下一屏的 [y/N]
                target = None
                for _ in range(10):
                    await asyncio.sleep(1.2)
                    for e in c.take("session.prompt"):
                        if e.get("state") == "open" and e.get("kind") == "confirm":
                            target = e
                    if target:
                        break
                ck("第二轮 confirm 提示", target is not None)
                if target:
                    r = await c.req("session.answer", {"pid": target["pid"], "option": "y"}, sid=term_sid)
                    ck("热键 y 作答成功", r.get("ok", False))
                await asyncio.sleep(1.5)
                r = await c.req("pane.snapshot", {"sid": term_sid, "lines": 40}, sid=term_sid)
                screen = "\n".join(r["payload"].get("lines") or [])
                ck("屏幕推进到完成", "全部完成" in screen, repr(screen.strip().splitlines()[-2:]))
                # 过期保护
                r = await c.req("session.answer", {"pid": p["pid"], "choice": 1}, sid=term_sid)
                ck("过期 pid → E_PROMPT_STALE", (r.get("err") or {}).get("code") == "E_PROMPT_STALE")
                # 输入
                r = await c.req("session.input", {"kind": "text", "text": "echo 小程序", "submit": True},
                                sid=term_sid)
                ck("文本输入成功", r.get("ok", False))
                r = await c.req("session.input", {"kind": "key", "key": "ENTER"}, sid=term_sid)
                ck("按键输入成功", r.get("ok", False))
            await c.req("session.detach", {"sid": term_sid}, sid=term_sid)

        rd.cancel()
        await ws.close()

    print(f"\n通过 {sum(RESULTS)}/{len(RESULTS)}")
    sys.exit(0 if all(RESULTS) else 1)


asyncio.run(main())
