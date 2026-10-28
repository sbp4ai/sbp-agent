#!/usr/bin/env python3
"""传输层资源开销对比：WebSocket 推送 / HTTP 轮询 / HTTP 轮询+ETag。

问题：定时 HTTP 懒加载是不是比 WebSocket 更省服务器资源？
答法：不看感觉，看数。主指标是服务端的「子进程累计 CPU」和「fork 次数」——
     一次 tmux capture-pane 要 fork 好几个进程，这是本服务最贵的原子操作。

跑法：
    python tests/transport_bench.py            # 默认 20s/档
    python tests/transport_bench.py --dur 30 --clients 1,3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websockets

PY = sys.executable
ROOT = Path(__file__).resolve().parents[1]
PORT = 20018
BASE = f"http://127.0.0.1:{PORT}"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(path: str, timeout: float = 10.0) -> dict:
    with OP.open(BASE + path, timeout=timeout) as r:
        return json.load(r)


def http_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with OP.open(req, timeout=10) as r:
        return json.load(r)


def wait_up(timeout: float = 30.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if http("/api/health", 2).get("ok"):
                return
        except Exception:
            time.sleep(0.3)
    raise SystemExit("server did not come up")


# ------------------------------------------------------------------ 各种客户端
async def ws_clients(sid: str, n: int, dur: float) -> dict:
    """N 个 WebSocket 客户端 attach 后被动收推送。"""
    bytes_rx = [0] * n

    async def one(i: int):
        async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws", max_size=None) as ws:
            await ws.send(json.dumps({"v": "sbp/0.1", "id": "h", "type": "req",
                                      "name": "hello", "payload": {}}))
            await ws.send(json.dumps({"v": "sbp/0.1", "id": "a", "type": "req",
                                      "name": "session.attach", "sid": sid, "payload": {}}))
            end = time.time() + dur
            while time.time() < end:
                try:
                    m = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                bytes_rx[i] += len(m)

    await asyncio.gather(*(one(i) for i in range(n)))
    return {"bytes": sum(bytes_rx)}


async def poll_clients(sid: str, n: int, dur: float, interval: float, etag: bool) -> dict:
    """N 个 HTTP 客户端按 interval 轮询。etag=True 时带 If-None-Match。"""
    bytes_rx = [0] * n
    per_client: list[dict] = [{} for _ in range(n)]   # 每个客户端各自的 ETag，别串味

    async def one(i: int):
        etags = per_client[i]
        end = time.time() + dur
        while time.time() < end:
            for path, key in (("/api/state?sid=%s&interval=%.1f" % (sid, interval), "s"),
                              ("/api/index", "i")):
                try:
                    req = urllib.request.Request(BASE + path.replace(" ", "%20"))
                    if etag and key in etags:
                        req.add_header("If-None-Match", etags[key])
                    with OP.open(req, timeout=5) as r:
                        body = r.read()
                        bytes_rx[i] += len(body)
                        tag = r.headers.get("ETag")
                        if tag:
                            etags[key] = tag
                except urllib.error.HTTPError as e:
                    if e.code == 304:
                        bytes_rx[i] += 60          # 304 只有响应头，没有 body
                    elif e.code != 404:
                        raise
            await asyncio.sleep(interval)

    await asyncio.gather(*(one(i) for i in range(n)))
    return {"bytes": sum(bytes_rx)}


# ------------------------------------------------------------------ 主流程
def run_mode(name: str, fn, dur: float) -> dict:
    http_post("/api/stats/reset", {})
    s0 = http("/api/stats")
    t0 = time.time()
    rx = asyncio.run(fn())
    wall = time.time() - t0
    s1 = http("/api/stats")
    per_min = 60.0 / max(wall, 0.001)
    return {
        "mode": name,
        "wall": round(wall, 1),
        "cpu_kids_per_min": round((s1["cpu_kids"] - s0["cpu_kids"]) * per_min, 3),
        "cpu_self_per_min": round((s1["cpu_self"] - s0["cpu_self"]) * per_min, 3),
        "tmux_runs_per_min": round((s1["tmux_runs"] - s0["tmux_runs"]) * per_min, 1),
        "captures_per_min": round((s1["captures"] - s0["captures"]) * per_min, 1),
        "client_bytes_per_min": round(rx["bytes"] * per_min),
        "ticks_per_min": round((s1["ticks"] - s0["ticks"]) * per_min, 1),
        "period": s1["period"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dur", type=float, default=20.0)
    ap.add_argument("--clients", default="1,3")
    ap.add_argument("--interval", default="5", help="轮询间隔，逗号分隔多档，如 5,1")
    ap.add_argument("--sid", default="")
    args = ap.parse_args()

    srv = subprocess.Popen([PY, "-m", "tmux_panel", "--host", "127.0.0.1",
                            "--port", str(PORT), "--no-auth"],
                           cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_up()
        sid = args.sid
        if not sid:
            idx = http("/api/index?refresh=1")
            for g in idx["groups"]:
                for it in g["items"]:
                    if it.get("kind") == "term" or g["name"].startswith("bench"):
                        sid = it["sid"]
                        break
                if sid:
                    break
            if not sid and idx["groups"]:
                sid = idx["groups"][0]["items"][0]["sid"]
        print(f"target sid = {sid}\n")

        rows = []
        for n in [int(x) for x in args.clients.split(",")]:
            print(f"--- {n} 个客户端，持续 {args.dur:.0f}s")
            rows.append(run_mode(f"WS 推送 ×{n}",
                                 lambda n=n: ws_clients(sid, n, args.dur), args.dur))
            for iv in [float(x) for x in args.interval.split(",")]:
                rows.append(run_mode(f"HTTP 轮询 {iv:g}s ×{n}",
                                     lambda n=n, iv=iv: poll_clients(sid, n, args.dur, iv, False),
                                     args.dur))
                rows.append(run_mode(f"HTTP 轮询+ETag {iv:g}s ×{n}",
                                     lambda n=n, iv=iv: poll_clients(sid, n, args.dur, iv, True),
                                     args.dur))
            for r in rows[-(1 + 2 * len(args.interval.split(","))):]:
                print("   %-28s 子进程CPU %.3f s/min | fork %6.1f/min (抓屏 %5.1f) | "
                      "客户端收 %8d B/min | tick %.1f/min 周期%ss"
                      % (r["mode"], r["cpu_kids_per_min"], r["tmux_runs_per_min"],
                         r["captures_per_min"], r["client_bytes_per_min"],
                         r["ticks_per_min"], r["period"]))
            print()

        out = ROOT / "docs" / "transport_bench.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
        print("->", out)
        return 0
    finally:
        srv.send_signal(signal.SIGTERM)
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()


if __name__ == "__main__":
    sys.exit(main())
