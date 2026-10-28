# sbp-agent · Local Daemon (installed on the machine running tmux + claude)

The "device side" of SBP: deployed on your dev machine / server where a tmux session and claude-code
(or codex / dsh / hermes / openclaw / workbuddy / qwen-office) are running. It dials out to sbp-server
in reverse and exposes the local session.

> This is exactly "the server installed onto the machine actually running the tmux session and claude code cli".

## Contains

| Dir | Role |
|---|---|
| `panel/` | **tmux-claude-panel**: FastAPI + no-build web UI, reads `~/.claude/projects/*/*.jsonl` and tmux panes directly. REST polling primary, WS optional. |
| `bridge/` | **sbp-bridge**: pty/tmux dual source + screen-line model + adapter execution (PC-side bridge closer to sbp-spec's original 3-module design). |
| `demo/` | `e2e_test.py`: end-to-end self-test script. |

## Quick Start (panel, most common)

```bash
cd panel
pip install -r requirements.txt
cp panel/.env.example panel/.env   # optional: configure account / HMAC
PYTHONPATH=panel python -m tmux_panel --port 20016 --no-auth
# open http://127.0.0.1:20016/  (default REST 5s polling)
# for lowest latency: http://127.0.0.1:20016/?transport=ws
```

HMAC device auth (optional, disabled if unset):

```bash
SBP_HMAC_KEYS=mac-mini-01:your-secret PYTHONPATH=panel python -m tmux_panel --port 20017
```

## Relation to the protocol

- Protocol spec: [sbp-spec](https://github.com/sbp4ai/sbp-spec)
- Public relay: [sbp-server](https://github.com/sbp4ai/sbp-server)
- Phone / IM clients: [sbp-minis](https://github.com/sbp4ai/sbp-minis) (WeChat mini-program)
  + [sbp-im-embed](https://github.com/sbp4ai/sbp-im-embed) (IM-embed skill)

---

# sbp-agent · 本机守护（装在跑 tmux + claude 的机器上）（中文）

SBP 的「设备端」：部署在你开发机 / 服务器上，那里正跑着 tmux 会话和 claude-code
（或 codex / dsh / hermes / openclaw / workbuddy / qwen-office）。它反向出站连 sbp-server，
把本地会话暴露出去。

> 这正是「安装到实际运行 tmux 会话和 claude code cli 的 server」。

## 包含

| 目录 | 角色 |
|---|---|
| `panel/` | **tmux-claude-panel**：FastAPI + 无构建网页 UI，直接读 `~/.claude/projects/*/*.jsonl` 与 tmux pane。REST 轮询为主、WS 可选。 |
| `bridge/` | **sbp-bridge**：pty/tmux 双源 + 屏幕行模型 + adapter 执行（更贴近 sbp-spec 原始三模块设计的 PC 侧桥接）。 |
| `demo/` | `e2e_test.py`：端到端自测脚本。 |

## 快速开始（panel，最常用）

```bash
cd panel
pip install -r requirements.txt
cp panel/.env.example panel/.env   # 可选：配账号 / HMAC
PYTHONPATH=panel python -m tmux_panel --port 20016 --no-auth
# 浏览器打开 http://127.0.0.1:20016/  （默认 REST 5s 轮询）
# 想最低延迟：http://127.0.0.1:20016/?transport=ws
```

HMAC 设备鉴权（可选，不设则关闭）：

```bash
SBP_HMAC_KEYS=mac-mini-01:your-secret PYTHONPATH=panel python -m tmux_panel --port 20017
```

## 与协议的关系

- 协议规范见 [sbp-spec](https://github.com/sbp4ai/sbp-spec)
- 公网中继见 [sbp-server](https://github.com/sbp4ai/sbp-server)
- 手机 / IM 客户端见 [sbp-minis](https://github.com/sbp4ai/sbp-minis)（微信小程序）
  + [sbp-im-embed](https://github.com/sbp4ai/sbp-im-embed)（IM 内嵌技能）
