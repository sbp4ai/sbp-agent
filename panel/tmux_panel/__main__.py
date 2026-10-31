"""CLI：端口占用自动 +1、首次启动生成 .env、打印访问地址。

用法：
    python -m tmux_panel                 # 127.0.0.1:20016 起
    python -m tmux_panel --host 0.0.0.0  # 局域网访问
    PORT=9999 python -m tmux_panel
"""
from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
from pathlib import Path

DEFAULT_PORT = 20016
DEFAULT_HOST = "127.0.0.1"


def find_free_port(start: int, tries: int = 50) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"端口 {start}~{start + tries - 1} 全被占用")


def ensure_env() -> tuple[str, str, bool]:
    """cwd 无 .env 时生成随机账号密码。返回 (account, password, created)。"""
    p = Path.cwd() / ".env"
    if p.exists():
        from dotenv import load_dotenv
        load_dotenv(p)
        return os.getenv("PANEL_ACCOUNT", ""), os.getenv("PANEL_SECERT", ""), False
    acc = secrets.token_hex(4)
    pwd = secrets.token_urlsafe(12)
    try:
        p.write_text(f"# 首次启动自动生成，可编辑后重启生效\nPANEL_ACCOUNT={acc}\nPANEL_SECERT={pwd}\n")
        os.chmod(p, 0o600)
    except OSError:
        return "", "", False
    return acc, pwd, True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("tmux-panel", description="tmux + Claude Code 本地面板")
    ap.add_argument("--port", "-p", type=int, default=int(os.getenv("PORT", DEFAULT_PORT)))
    ap.add_argument("--host", "-H", default=os.getenv("HOST", DEFAULT_HOST))
    ap.add_argument("--no-auth", action="store_true", help="关闭登录（仅本机调试）")
    ap.add_argument("--version", action="version", version="tmux-ai-coder-panel 1.0.4")
    args = ap.parse_args(argv)

    acc, pwd, created = ensure_env()
    if args.no_auth:
        os.environ["PANEL_ACCOUNT"] = ""
        os.environ["PANEL_SECERT"] = ""
        acc = ""

    port = find_free_port(args.port)
    import uvicorn
    from .server import app

    print("=" * 62)
    print("  tmux-claude-panel 已启动")
    print(f"  本机访问:  http://127.0.0.1:{port}")
    if args.host == "0.0.0.0":
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            ip = "127.0.0.1"
        print(f"  局域网:    http://{ip}:{port}")
    if acc:
        if created:
            print(f"  账号:      {acc}")
            print(f"  密码:      {pwd}   （已写入 .env，可修改）")
        else:
            print(f"  账号:      {acc}   （见 .env）")
    else:
        print("  登录:      已关闭（--no-auth）")
    print("  Ctrl+C 停止")
    print("=" * 62)

    try:
        uvicorn.run(app, host=args.host, port=port, log_level="warning")
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
