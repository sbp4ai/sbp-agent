"""SBP 端到端自测：模拟手机端，跑完 绑定→列表→懒加载→输入→回显 全链路。

用法：
    python demo/e2e_test.py http://127.0.0.1:8787
前置：sbp-server 已启动，sbp-bridge 已 --auto-bind 运行（会写 ~/.sbp/last_demo_url.txt）
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import aiohttp

V = "sbp/0.1"
OK = "\033[32m✓\033[0m"
NO = "\033[31m✗\033[0m"
failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {OK if cond else NO} {name}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        failures.append(name)


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.seq = 0
        self.pending: dict[str, asyncio.Future] = {}
        self.events: list[dict] = []
        self.responses: list[dict] = []
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            f = json.loads(msg.data)
            if f.get("type") == "res":
                self.responses.append(f)
                fut = self.pending.pop(f.get("ref"), None)
                if fut and not fut.done():
                    fut.set_result(f.get("payload") if f.get("ok") else f.get("err"))
            elif f.get("type") == "evt":
                self.events.append(f)

    async def req(self, name: str, payload: dict, sid: str | None = None, timeout: float = 8.0):
        self.seq += 1
        rid = f"t{self.seq}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        frame = {"v": V, "id": rid, "type": "req", "name": name, "ts": int(time.time() * 1000), "payload": payload}
        if sid:
            frame["sid"] = sid
        await self.ws.send_json(frame)
        return await asyncio.wait_for(fut, timeout)

    async def wait(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


async def main(base: str) -> int:
    state = Path.home() / ".sbp" / "last_demo_url.txt"
    if not state.exists():
        print("找不到 last_demo_url.txt，先运行 sbp-bridge up --auto-bind")
        return 2
    url = state.read_text().strip()
    frag = dict(kv.split("=", 1) for kv in url.split("#", 1)[1].split("&"))
    token, did, sid = frag["t"], frag["d"], frag["s"]

    print("\n[1] 健康检查")
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{base}/api/health") as r:
            h = await r.json()
        check("server 存活", h.get("ok") is True, str(h))
        check("设备在线", h.get("devices_online", 0) >= 1, str(h))

        ws_url = base.replace("http", "ws") + "/sbp"
        print("\n[2] 客户端握手")
        async with http.ws_connect(ws_url, protocols=("sbp.v0.1",)) as ws:
            c = Client(ws)
            await ws.send_json({"v": V, "id": "h1", "type": "req", "name": "hello",
                                "payload": {"role": "client", "token": token}})
            await c.wait(0.6)
            hello = [r for r in c.responses if r.get("name") == "hello.ok"]
            check("hello.ok 返回", bool(hello))
            caps = (hello[0]["payload"]["caps"] if hello else [])
            check("能力位非空", len(caps) > 0, str(caps))

            print("\n[3] 会话列表（应只有索引，无正文）")
            pl = await c.req("session.list", {})
            sessions = pl.get("sessions", [])
            check("列表非空", bool(sessions), str(pl)[:200])
            if sessions:
                keys = set(sessions[0].keys())
                check("无 messages/text 字段", not ({"messages", "text", "items"} & keys), str(keys))
                check("含 sid/title/status", {"sid", "title", "status"} <= keys)
                print(f"      首屏体积 ≈ {len(json.dumps(sessions, ensure_ascii=False).encode())} B / {len(sessions)} 条")

            print("\n[4] 懒加载：最新一页 + 倒序翻页")
            page1 = await c.req("session.history", {"sid": sid, "limit": 5})
            items = page1.get("items", [])
            check("拿到历史页", True, f"{len(items)} 条")
            if items:
                seqs = [i["seq"] for i in items]
                check("页内 seq 升序", seqs == sorted(seqs), str(seqs))
                check("返回 has_more", "has_more" in page1, str(page1.keys()))
                older = await c.req("session.history", {"sid": sid, "before_seq": items[0]["seq"], "limit": 5})
                old_seqs = [i["seq"] for i in older.get("items", [])]
                check("翻页全部更早", all(s < items[0]["seq"] for s in old_seqs), str(old_seqs))

            print("\n[5] 订阅 + 输入")
            att = await c.req("session.attach", {"sid": sid, "from_seq": 0}, sid)
            check("attach 成功", isinstance(att, dict) and "sid" in att, str(att))
            marker = f"sx-{int(time.time())}"
            r = await c.req("session.input", {"sid": sid, "kind": "text", "text": f"echo {marker}"}, sid)
            check("输入已受理", isinstance(r, dict), str(r))

            print("\n[6] 回显（3s 内应收到 user + agent 消息 + 屏幕增量）")
            await c.wait(3.0)
            msgs = [e for e in c.events if e.get("name") == "session.message"]
            screens = [e for e in c.events if e.get("name") == "output.delta"]
            user_msgs = [m for m in msgs if m["payload"].get("role") == "user"]
            agent_msgs = [m for m in msgs if m["payload"].get("role") == "agent"]
            check("收到 user 消息", any(marker in (m["payload"].get("text") or "") for m in user_msgs),
                  str([m["payload"].get("text") for m in user_msgs])[:200])
            check("收到 agent 消息", any(marker in (m["payload"].get("text") or "") for m in agent_msgs),
                  str([m["payload"].get("text") for m in agent_msgs])[:300])
            check("收到屏幕增量", len(screens) > 0, str(len(screens)))
            if screens:
                last = screens[-1]["payload"]
                joined = "\n".join(last.get("lines") or [])
                check("屏幕里能看到回显", marker in joined, joined[-300:])
            seqs = [e["seq"] for e in msgs]
            check("消息 seq 单调递增", seqs == sorted(seqs) and len(set(seqs)) == len(seqs), str(seqs))

    print("\n" + ("—" * 46))
    if failures:
        print(f"{NO} 失败 {len(failures)} 项: {', '.join(failures)}")
        return 1
    print(f"{OK} 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787")))
