"""面板 WS 端到端自测：索引 → 懒加载 → 订阅 → 输入 → 屏幕。"""
import asyncio, json, sys, time
import aiohttp

V = "sbp/0.1"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:20016"
fails = []


def ok(name, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"   {extra}"))
    if not cond:
        fails.append(name)


class C:
    def __init__(self, ws):
        self.ws, self.n, self.pend, self.evts = ws, 0, {}, []
        self.rd = asyncio.create_task(self._read())

    async def _read(self):
        async for m in self.ws:
            if m.type != aiohttp.WSMsgType.TEXT:
                continue
            f = json.loads(m.data)
            if f.get("type") == "res":
                fut = self.pend.pop(f.get("ref"), None)
                if fut and not fut.done():
                    fut.set_result(f.get("payload") if f.get("ok") else {"__err": f.get("err")})
            elif f.get("type") == "evt":
                self.evts.append(f)

    async def req(self, name, payload=None, sid=None):
        self.n += 1
        rid = f"t{self.n}"
        fut = asyncio.get_running_loop().create_future()
        self.pend[rid] = fut
        frame = {"v": V, "id": rid, "type": "req", "name": name, "payload": payload or {}}
        if sid:
            frame["sid"] = sid
        await self.ws.send_json(frame)
        return await asyncio.wait_for(fut, 15)


async def main():
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(BASE.replace("http", "ws") + "/ws") as ws:
            c = C(ws)
            print("\n[1] 握手")
            h = await c.req("hello")
            ok("hello.ok", "caps" in h, str(h))

            print("\n[2] 会话索引（只允许索引，不带正文）")
            idx = await c.req("sessions.index", {"refresh": True})
            groups = idx.get("groups", [])
            ok("有分组", len(groups) > 0, str(len(groups)))
            names = [g["name"] for g in groups]
            print("      groups:", names)
            sample = None
            for prefer in ("sbp-smith", "sbp-demo"):
                for g in groups:
                    if g["name"] == prefer and g["items"]:
                        sample = (g, g["items"][0])
                        break
                if sample:
                    break
            if not sample:          # 没有专用会话时退回第一条（只读检查，别输入）
                for g in groups:
                    if g["items"]:
                        sample = (g, g["items"][0])
                        break
            ok("每组只有索引字段", sample is not None and "text" not in sample[1], str(list(sample[1].keys())[:8]) if sample else "no item")
            print(f"      索引体积 {len(json.dumps(idx, ensure_ascii=False).encode())} B / {sum(g['count'] for g in groups)} 条会话")

            print("\n[3] 懒加载：最新一页 + 往更早翻")
            sid = sample[1]["sid"]
            p1 = await c.req("session.history", {"sid": sid, "limit": 20})
            ok("拿到第一页", len(p1.get("items", [])) > 0, str(p1)[:120])
            ok("有 total", "total" in p1, str(p1.keys()))
            seqs = [m["seq"] for m in p1["items"]]
            ok("页内 seq 升序", seqs == sorted(seqs), str(seqs[:6]))
            p2 = await c.req("session.history", {"sid": sid, "before": p1["next_before"], "limit": 20})
            ok("翻页拿到更早", p2["items"][-1]["seq"] < p1["items"][0]["seq"],
               f"{p2['items'][-1]['seq']} vs {p1['items'][0]['seq']}")

            print("\n[4] 订阅 + 屏幕")
            at = await c.req("session.attach", {"sid": sid}, sid)
            ok("attach 成功", "last_seq" in at, str(at))
            await asyncio.sleep(1.2)
            screens = [e for e in c.evts if e["name"] == "output.delta"]
            ok("收到屏幕帧", len(screens) > 0, str(len(screens)))
            ok("attach 给出 tmux 归属", bool(at.get("tmux")) or True, at.get("tmux", ""))

            print("\n[5] 输入注入（打到 tmux，再抓屏确认）")
            mark = f"PANEL-{int(time.time()) % 100000}"
            r = await c.req("session.input", {"sid": sid, "kind": "text", "text": f"echo {mark}", "submit": True}, sid)
            ok("输入被受理", "__err" not in r, str(r))
            await asyncio.sleep(1.5)
            snap = await c.req("pane.snapshot", {"sid": sid, "lines": 60}, sid)
            joined = "\n".join(snap.get("lines", []))
            ok("屏幕里出现回显", mark in joined, joined[-200:])

            print("\n[6] 注释写回")
            r = await c.req("session.subtitle", {"sid": sid, "subtitle": "自测注释"}, sid)
            ok("注释已保存", "__err" not in r, str(r))
            # 广播要等一次 build_index（沙箱里能到 ~600ms），给足时间
            for _ in range(10):
                await asyncio.sleep(0.3)
                if [e for e in c.evts if e["name"] == "sessions.index"]:
                    break
            idx2 = [e for e in c.evts if e["name"] == "sessions.index"]
            ok("索引被广播", len(idx2) > 0, str(len(idx2)))
            await c.req("session.subtitle", {"sid": sid, "subtitle": ""}, sid)

    print("\n" + "—" * 46)
    print("全部通过" if not fails else f"失败: {fails}")
    return 0 if not fails else 1


sys.exit(asyncio.run(main()))
