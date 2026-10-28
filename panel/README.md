# tmux-claude-panel

基于 `example/tmux_ai_coder_panel_src` 改造的本地 Web 面板：
**左边是 tmux 会话里跑着的 Claude Code，右边是那段会话的 jsonl 历史 + 实时屏幕 + 键盘输入。**

一个进程、一个端口、零前端构建。

```
┌────────────────┬──────────────────────────────────────────┐
│ tmux · claude  │  e389d12e  [tmux: main] [837 行]     ⟳   │
│ 6 会话         │  ────────────────────────────────────────│
│ ▾ main ●       │   14:22  · claude-sonnet                │
│   ● e389d12e   │   ⚙ Bash · npm test                     │
│     修复播放     │   测试全过，271 个用例                    │
│   ○ bc0dca7a   │                                          │
│ ▸ tmp          │   ↑ 上滑加载更早                          │
│ ▾ 其他会话     │  ── 实时屏幕 ────────────────────────────  │
│   ○ 9c81df58   │  $ claude ...                            │
│                │  [⏎ Esc ↑ ↓ 1 2 y n Ctrl-C]              │
│                │  [输入指令…………………]  [发送]             │
└────────────────┴──────────────────────────────────────────┘
```

## 快速开始

```bash
cd panel
pip install -r requirements.txt
python -m tmux_panel                 # 127.0.0.1:20016，被占用自动 +1
# python -m tmux_panel --host 0.0.0.0   # 手机/局域网访问
```

### 两种传输，同一套帧语义

| 模式 | 打开方式 | 适合 |
| --- | --- | --- |
| REST 轮询（默认） | `http://host:20016/?transport=rest&interval=5000` | 无长连接、最省服务端；代理/网关/小程序都友好 |
| WebSocket 推送（可选） | `http://host:20016/?transport=ws` | 局域网内最跟手，变化即推；连不上自动降级回轮询 |

右上角小徽章显示当前是 `REST 5s` 还是 `WS`。**默认就是 REST 轮询**（无长连接，最省
服务端），`?transport=ws` 才显式开长连接；**WS 连不上时前端自动降级为轮询**，不需要手动切。
轮询走 `GET /api/index` + `GET /api/state`，都带 `ETag`：
内容没变回 304 不带 body，实测 1s × 3 客户端从 2.29 MB/min 降到 193 KB/min。

服务端省资源的机制（与协议无关，详见 `../spec/SBP-transport.md`）：
共享抓屏缓存、索引只在 tick 里重建一次、tick 周期自适应（5s 轮询就按 2.5s 抓屏）、
扇出只序列化一次。诊断数据看 `GET /api/stats`。

首次启动会在当前目录生成 `.env`（随机账号密码并打印），登录后即可用。
`--no-auth` 可关闭登录，只在纯本机调试时用。

左侧出现会话的前提：
1. 有 tmux 会话：`tmux new -s work -c ~/codes/foo`
2. 该会话的工作目录里跑过 `claude`（`~/.claude/projects/<slug>/<uuid>.jsonl` 存在）

### 侧栏的深色 / 灰色（活跃判定）

一个 tmux 组里只有「当前正被 agent 用着」的那个会话是深黑 + 绿点，其余一律灰掉并挂
`历史` 角标；这个 tmux 里没跑 agent 时整组全灰。判定在 `tmux_panel/live.py`，三级证据：

| 级别 | 证据 | 含义 |
| --- | --- | --- |
| `held` | pane 进程树里有进程打开着 `<sid>.jsonl`（`lsof` / `/proc`） | 最硬：claude 正挂着这个会话 |
| `recent` | pane 里跑着 agent，且最新 jsonl 在 180s 内被写过 | agent 在跑、刚有动静 |
| `guess` | agent 在跑但文件已冷 | 仍认最新的那个（claude 默认续最近会话） |
| `none` | 没有 agent | 全灰 |

dsh / codex 这类没有会话文件的 pty agent 同一套规则，只是证据换成进程：
按 **pane** 维护一条 `term:<tmux>`（多 agent pane 时第二个起是 `term:<tmux>#<pane_id>`），
进程树里此刻还有 agent 进程 → 深黑 + 绿点，副标题取自屏幕上的 `task:` 行；
agent 已退出（pane 只剩 shell）→ 灰色 + `未激活` 角标，但仍可点开看屏幕。
纯 shell 的 pane 不进列表。判定都在 `tmux_panel/live.py` 的 `running_agent()`。

索引每 6s 重算一次，指纹（活跃 sid / 数量 / 标题）变了才向前端广播；
前端正在编辑注释时暂存推送，编辑结束再补渲染。见 `panel/docs/ui_active_grey.png`。

## 它到底做了什么

| 能力 | 实现 |
|---|---|
| 列出 tmux 会话 | 扫 `~/.tmux/tmux-<uid>/` 与 `/tmp/tmux-<uid>/` 下**所有** socket，逐个 `tmux -L <name> list-sessions`（systemd 不 source shell rc 也能找到） |
| 会话 ↔ claude 映射 | tmux pane 的 `pane_current_path` → `~/.claude/projects/<slug>/`；slug = 路径非字母数字全替换成 `-` |
| 侧栏标题 | claude session id（前 8 位），副标题默认取 jsonl 里的 `aiTitle` / `lastPrompt` |
| 自定义注释 | 点条目上的 `✎` 改副标题，存在 `~/.tmux-claude-panel/annotations.json` |
| 懒加载 | 打开会话才请求 `session.history{limit:20}`；上滑按 `before=<行号>` 继续翻 |
| 倒序分页 | 首次访问给 jsonl 建**字节偏移索引**（837 行 / 2ms，单页 0.3ms），之后按行号 O(1) 定位，不整文件重读 |
| 实时增量 | 1.2s 轮询被 attach 的 jsonl，`size/mtime` 变化就追加推送 `session.message` |
| 实时屏幕 | `tmux capture-pane -p -S -40`，变化才推 `output.delta` |
| 输入 | `tmux send-keys -l -- "<text>"` + `Enter`，特殊键走键名映射（Esc/↑/Ctrl-C…） |

## 协议（SBP v0.1 子集）

信封与 `spec/SBP-0.1.md` 一致，`WS /ws`：

```jsonc
→ {"v":"sbp/0.1","id":"c1","type":"req","name":"sessions.index","payload":{"refresh":true}}
← {"v":"sbp/0.1","type":"res","ref":"c1","name":"sessions.index","ok":true,
   "payload":{"groups":[{"name":"main","cwd":"/Users/mac","live":true,"items":[...]}]}}

→ {"name":"session.history","sid":"e389…","payload":{"before":818,"limit":20}}
→ {"name":"session.attach","sid":"e389…"}          // 订阅增量 + 屏幕
→ {"name":"session.input","sid":"e389…","payload":{"kind":"text","text":"继续","submit":true}}
← evt session.message / output.delta / session.state
```

| req | 说明 |
|---|---|
| `sessions.index` | 只要索引：sid / 标题 / 副标题 / 条数 / 时间 / 是否 live |
| `session.history` | `before` 为行号（不含），返回 `items / has_more / next_before / total` |
| `session.attach` | 返回 `last_seq` 与 `tmux` 归属，决定前端能否输入 |
| `session.input` | `kind=text` 或 `kind=key` |
| `session.subtitle` | 写注释后广播新索引 |
| `pane.snapshot` | 主动抓屏 |

## 从原项目改了什么

保留：
- `TmuxManager` 的多 socket 解析、`path_to_session_name`、鉴权（HMAC cookie + `?token=` 兜底）、
  `find_free_port` / `.env` 自动生成 / 启动横幅 —— 这些是踩过坑的地方，直接沿用。

替换：
- **Vue3 打包产物**（`frontend_dist`，改不动）→ 换成 `web/` 下无构建的三件套（html/css/js）。
- **WebSocket 语义**：原来是「键盘 LED 面板」事件（`keypress`/`set_mode`），
  现在是「会话流」协议（索引/分页/订阅/输入），并按 SBP 的信封规范命名。
- **新增数据层**：`claude.py` 负责 jsonl 发现、索引、分页、增量；`hub.py` 负责分组与推送。

没有改 `example/` 下的任何文件，两边可以并存对比。

## 已知边界

- 只做了 Claude Code 的 jsonl（`~/.claude/projects`）。换别的 agent 只需换 `claude.py` 的解析层。
- 子代理消息（`isSidechain:true`）默认不展示，避免刷屏；需要时改 `normalize()`。
- 一个 tmux 会话里如果切过多个 claude 会话，侧栏会把该目录下的会话全列出来（按 mtime 倒序），
  真正"当前在跑"的那个排最前并带绿点。
- jsonl 超过 8MB 时侧栏不显示条数（避免每次列表都全量扫行）；打开后仍可正常翻页。

## 目录

```
panel/
├── tmux_panel/
│   ├── tmux.py      # tmux 访问（多 socket + send-keys/capture-pane）
│   ├── claude.py    # jsonl 发现/索引/倒序分页/增量/注释
│   ├── hub.py       # 分组索引 + 1.2s 推送循环
│   ├── server.py    # FastAPI + WS 协议
│   └── __main__.py  # CLI：端口探测 / .env / 横幅
└── web/
    ├── index.html
    ├── style.css
    └── app.js
```
