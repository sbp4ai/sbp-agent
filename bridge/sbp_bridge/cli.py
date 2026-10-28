"""sbp-bridge CLI:  sbp-bridge up --server http://127.0.0.1:8787"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .client import Bridge, default_adapter


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("sbp-bridge", description="SBP device-side connector")
    sub = ap.add_subparsers(dest="cmd_name")

    up = sub.add_parser("up", help="启动连接器")
    up.add_argument("--server", default=os.environ.get("SBP_SERVER", "http://127.0.0.1:8787"))
    up.add_argument("--agent", default=None, help="要接管的命令，如 'claude' / 'bash'")
    up.add_argument("--adapter", default=None, help="强制指定 adapter，如 claude-code@1")
    up.add_argument("--cwd", default=os.getcwd())
    up.add_argument("--title", default=None)
    up.add_argument("--rows", type=int, default=40)
    up.add_argument("--cols", type=int, default=100)
    up.add_argument("--auto-bind", action="store_true", help="本地自动完成绑定并打印网页 URL（仅调试）")
    up.add_argument("--sid", default=None)

    args = ap.parse_args(argv)
    if args.cmd_name != "up":
        ap.print_help()
        return 2

    cmd = args.agent.split() if args.agent else [os.environ.get("SHELL", "/bin/zsh"), "-i"]
    adapter = default_adapter(cmd, args.adapter)
    title = args.title or f"{Path(args.cwd).name} · {adapter.agent}"

    base = args.server.rstrip("/")
    ws = base.replace("https://", "wss://").replace("http://", "ws://") + "/sbp"

    print(f"  adapter : {adapter.name}  caps={len(adapter.caps)}")
    print(f"  server  : {ws}")

    bridge = Bridge(http_base=base, ws_url=ws, cmd=cmd, adapter=adapter,
                    cwd=args.cwd, rows=args.rows, cols=args.cols, title=title,
                    auto_bind=args.auto_bind, sid=args.sid)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\n  bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
