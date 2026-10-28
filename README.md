# sbp-agent · 本机守护（装在跑 tmux + claude 的机器上）

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

- 协议规范见 [sbp-spec](https://github.com/slamKunTech/sbp-spec)
- 公网中继见 [sbp-server](https://github.com/slamKunTech/sbp-server)
- 手机 / IM 客户端见 [sbp-client](https://github.com/slamKunTech/sbp-client)
