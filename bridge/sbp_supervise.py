"""sbp-supervise —— 为每个 tmux 会话维护一个 sbp-bridge 上报进程。

设计约束（来自 SBP 本身，改之前先看 spec）：

1. 一个 sbp-bridge 进程只能接管一个会话（Bridge.session_limit == 1），
   所以「监控全部 tmux 会话」= 会话数 × 端点数 个 bridge 子进程。
2. transport.type == tmux 的 adapter 把 sid 直接当 tmux target 用
   （TmuxSource(target=self.sid)），因此子进程的 --sid 必须等于真实会话名。
3. bridge 用 ~/.sbp/device.json 里的 device_id/secret 鉴权，缺失时会自动向
   /api/device/register 注册。多个 server（本地 + 远端）不能共用一个身份，
   所以这里给每个端点发一个独立的 SBP_HOME。

用法：
    python sbp_supervise.py                # 前台跑（launchd 用）
    python sbp_supervise.py --once         # 只打一圈：会话/端点/将拉起的命令
    python sbp_supervise.py --config X     # 指定配置
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 pyyaml: pip install pyyaml\n")
    raise

HERE = Path(__file__).resolve().parent          # .../sbp-agent/bridge
REPO = HERE.parent                              # .../sbp-agent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

SBP_HOME = Path(os.environ.get("SBP_HOME") or (Path.home() / ".sbp"))
CONFIG_PATH = Path(os.environ.get("SBP_SUPERVISOR_CONFIG") or (SBP_HOME / "bridge-supervisor.yaml"))
LOG_DIR = SBP_HOME / "logs"

DEFAULT_CONFIG: dict[str, Any] = {
    # 跑 bridge 用的解释器；留空表示用当前解释器
    "python": None,
    "bridge_root": str(HERE),
    "poll_seconds": 5,
    "rows": 44,
    "cols": 120,
    # 兜底 adapter；自动识别失败时用它
    "default_adapter": "tmux@1",
    # 按 pane 里跑的命令名换成专属 adapter（仅当该 adapter 是 tmux 传输）
    "auto_detect_adapter": True,
    "agent_adapters": {
        "claude": "claude-code@1",
        "qwen": "qwen-code@1",
        "qwen-code": "qwen-code@1",
    },
    "exclude": [],   # 会话名正则，命中即跳过
    "include": [],   # 非空则只监控命中的会话名
    "endpoints": [
        {"name": "local", "url": "http://127.0.0.1:8787", "enabled": True},
        # 远端公网 server：填好 url 后把 enabled 改成 true
        {"name": "remote", "url": "https://your-sbp-server.example:8787", "enabled": False},
    ],
}

TMUX_NAME_FMT = "#{session_name}\t#{session_path}\t#{pane_start_command}"


# ------------------------------------------------------------------ config
def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path) -> dict:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# sbp-supervise 配置。改完重启生效："
            "launchctl kickstart -k gui/$UID/com.sbp4ai.bridge\n"
            "# endpoints 里每一项都会独立上报；enabled: true 才会真的拉起进程。\n"
        )
        path.write_text(header + yaml.safe_dump(DEFAULT_CONFIG, allow_unicode=True, sort_keys=False))
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        sys.stderr.write(f"配置解析失败，回落到默认值: {e}\n")
        data = {}
    if not isinstance(data, dict):
        data = {}
    return deep_merge(DEFAULT_CONFIG, data)


def load_env_file(p: Path) -> dict[str, str]:
    """读 ~/.sbp/env.sh：KEY=value 形式，忽略 # 注释、支持 export 前缀。

    存在的理由：launchd 不继承你的 shell 环境，而 qwen / node / API key
    都在里面。把需要的变量写进这个文件即可。
    """
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if v and v[0] in "\"'" and v[-1] == v[0] and len(v) > 1:
            v = v[1:-1]
        elif v.endswith(" #"):
            v = v[:-2].rstrip()
        v = os.path.expanduser(v)
        if k:
            out[k] = v
    return out


# ------------------------------------------------------------------ tmux
def tmux_sessions() -> list[dict[str, str]]:
    """列出全部会话。tmux 不在 PATH 或没有服务时返回空列表（不算错误）。"""
    try:
        p = subprocess.run(["tmux", "list-sessions", "-F", TMUX_NAME_FMT],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    out = []
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].strip():
            continue
        name, path = parts[0].strip(), parts[1].strip()
        cmd = parts[2].strip() if len(parts) > 2 else ""
        out.append({"name": name, "path": path or str(Path.home()), "cmd": cmd})
    return out


# ------------------------------------------------------------------ adapters
def tmux_type_adapters() -> dict[str, bool]:
    """返回 {adapter_name: 是否是 tmux 传输}。

    只把会话升级到 transport.type == tmux 的 adapter —— 换成一个 pty adapter
    会让 bridge 去 spawn 新进程，而不是接管你想看的那一个会话。
    """
    try:
        from sbp_bridge.adapters import Adapter, load_dirs
        from sbp_bridge.client import ADAPTER_DIRS
        return {a.name: (a.transport.get("type") == "tmux") for a in load_dirs(ADAPTER_DIRS)}
    except Exception:
        try:
            from sbp_bridge.adapters import load_dirs
            from sbp_bridge.client import ADAPTER_DIRS
            return {a.name: (a.transport.get("type") == "tmux") for a in load_dirs(ADAPTER_DIRS)}
        except Exception:
            return {}


def pick_adapter(sess: dict[str, str], cfg: dict) -> str:
    default = cfg.get("default_adapter") or "tmux@1"
    if not cfg.get("auto_detect_adapter"):
        return default
    base = os.path.basename((sess.get("cmd") or "").split()[0]) if sess.get("cmd") else ""
    if not base:
        return default
    want = (cfg.get("agent_adapters") or {}).get(base)
    if not want:
        return default
    kinds = tmux_type_adapters()
    # 只有确认是 tmux 传输才升级，否则宁可用通用兜底
    return want if kinds.get(want, False) else default


# ------------------------------------------------------------------ env / plan
def child_env(cfg: dict, endpoint: dict) -> dict[str, str]:
    """给 bridge 子进程组装环境：PATH 保证能找到 tmux/qwen/node，每个端点独立身份。"""
    env = os.environ.copy()
    env.update(load_env_file(SBP_HOME / "env.sh"))

    bins = ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    # nvm 装的 node 不在登录 PATH 里，qwen 却在那儿
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        bins = [str(p / "bin") for p in sorted(nvm_root.iterdir(), reverse=True) if (p / "bin").is_dir()] + bins
    bins.append(env.get("PATH", ""))
    seen, parts = set(), []
    for b in bins:
        for seg in b.split(":"):
            seg = os.path.expanduser(seg)
            if seg and seg not in seen and Path(seg).is_dir():
                seen.add(seg)
                parts.append(seg)
    env["PATH"] = ":".join(parts)

    from sbp_bridge.client import ADAPTER_DIRS
    env["SBP_ADAPTER_DIR"] = os.pathsep.join(str(d) for d in ADAPTER_DIRS)
    env["SBP_HOME"] = str(SBP_HOME / "endpoints" / endpoint["name"])
    Path(env["SBP_HOME"]).mkdir(parents=True, exist_ok=True)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def build_cmd(sess: dict[str, str], endpoint: dict, cfg: dict) -> list[str]:
    py = cfg.get("python") or sys.executable
    return [
        py, "-m", "sbp_bridge.cli", "up",
        "--sid", sess["name"],
        "--adapter", pick_adapter(sess, cfg),
        "--server", str(endpoint["url"]).rstrip("/"),
        "--cwd", sess["path"],
        "--title", f"{sess['name']} · {os.path.basename(sess['path'])}",
        "--rows", str(cfg.get("rows", 44)),
        "--cols", str(cfg.get("cols", 120)),
    ]


def keep(sess_name: str, cfg: dict) -> bool:
    inc, exc = cfg.get("include") or [], cfg.get("exclude") or []
    if inc and not any(re.search(p, sess_name) for p in inc):
        return False
    if any(re.search(p, sess_name) for p in exc):
        return False
    return True


# ------------------------------------------------------------------ supervisor
class Supervisor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.procs: dict[tuple[str, str], subprocess.Popen] = {}
        self.fails: dict[tuple[str, str], float] = {}
        self.attempts: dict[tuple[str, str], int] = {}
        self.spawned_at: dict[tuple[str, str], float] = {}
        self.logs: dict[tuple[str, str], Any] = {}
        self._stop = False
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ helpers
    def _open_log(self, key: tuple[str, str]):
        if key in self.logs:
            return self.logs[key]
        f = open(LOG_DIR / f"bridge-{key[1]}-{key[0]}.log", "a", buffering=1)
        self.logs[key] = f
        return f

    def _backoff(self, key: tuple[str, str]) -> float:
        """指数退避：5s → 10s → 20s … 上限 60s。成功存活会重置计数。"""
        n = self.attempts.get(key, 0)
        return min(60.0, 5.0 * (2 ** max(0, n - 1)))

    def _spawn(self, sess: dict, endpoint: dict):
        key = (sess["name"], endpoint["name"])
        env = child_env(self.cfg, endpoint)
        cmd = build_cmd(sess, endpoint, self.cfg)
        log = self._open_log(key)
        log.write(f"\n===== {time.strftime('%F %T')} spawn: {shlex.join(cmd)}\n")
        try:
            p = subprocess.Popen(cmd, cwd=self.cfg["bridge_root"], env=env,
                                 stdout=log, stderr=subprocess.STDOUT, text=True)
        except Exception as e:
            log.write(f"!! spawn failed: {e}\n")
            self.fails[key] = time.time()
            return
        self.procs[key] = p
        self.spawned_at[key] = time.time()
        print(f"  [bridge] UP   {key[1]:<7s} {key[0]:<16s} pid={p.pid}", flush=True)

    def _reap(self, key: tuple[str, str]):
        p = self.procs.pop(key, None)
        if not p:
            return
        try:
            p.terminate()
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
        except Exception:
            pass
        print(f"  [bridge] DOWN {key[1]:<7s} {key[0]}")

    def _health(self, key: tuple[str, str]) -> bool:
        p = self.procs.get(key)
        if p is None:
            return False
        if p.poll() is None:
            return True
        self.procs.pop(key, None)
        return False

    # ------------------------------------------------------------ loop
    def tick(self) -> None:
        sessions = [s for s in tmux_sessions() if keep(s["name"], self.cfg)]
        eps = [e for e in (self.cfg.get("endpoints") or []) if e.get("enabled") and e.get("url")]
        wanted = {(s["name"], e["name"]) for s in sessions for e in eps}
        by_name = {s["name"]: s for s in sessions}
        ep_by = {e["name"]: e for e in eps}

        # 会话消失，或者端点被禁用 —— 回收对应的子进程
        for key in list(self.procs.keys()):
            if key not in wanted and self._health(key):
                self._reap(key)

        for key in sorted(wanted):
            p = self.procs.get(key)
            if p is not None:
                if p.poll() is None:
                    # 稳定存活超过 60s 就当作成功，把退避计数清零
                    if time.time() - self.spawned_at.get(key, 0.0) > 60:
                        self.attempts[key] = 0
                    continue                       # 活得好好的
                # 自己退出了：记一次失败，退避后才会重启（避免崩溃↔重启打满 CPU）
                self.procs.pop(key, None)
                self.attempts[key] = self.attempts.get(key, 0) + 1
                self.fails[key] = time.time()
                self._open_log(key).write(f"!! exit rc={p.returncode} attempts={self.attempts[key]}\n")
                print(f"  [bridge] EXIT {key[1]:<7s} {key[0]:<16s} rc={p.returncode} "
                      f"attempt={self.attempts[key]}", flush=True)
                continue

            if self.fails.get(key) and time.time() - self.fails[key] < self._backoff(key):
                continue
            self._spawn(by_name[key[0]], ep_by[key[1]])

    def run(self) -> int:
        print(f"  sbp-supervise 已启动 | config={CONFIG_PATH} | poll={self.cfg['poll_seconds']}s")
        print(f"  bridge_root={self.cfg['bridge_root']} | logs={LOG_DIR}")
        while not self._stop:
            try:
                self.tick()
            except Exception as e:                      # 主循环不能死
                print(f"  [supervise] tick error: {e}", flush=True)
            time.sleep(float(self.cfg.get("poll_seconds") or 5))
        for key in list(self.procs.keys()):
            if self._health(key):
                self._reap(key)
        return 0


def plan(cfg: dict) -> int:
    """--once：只算一遍，打印将要拉起的命令，不真的执行。"""
    sessions = [s for s in tmux_sessions() if keep(s["name"], cfg)]
    eps = [e for e in (cfg.get("endpoints") or []) if e.get("enabled") and e.get("url")]
    print(f"config : {CONFIG_PATH}")
    print(f"tmux   : {len(sessions)} session(s) -> {[s['name'] for s in sessions]}")
    print(f"endpoint: {[(e['name'], e['url']) for e in eps] or '(none enabled)'}")
    print()
    for s in sessions:
        for e in eps:
            cmd = build_cmd(s, e, cfg)
            print(f"{e['name']:<7s} {s['name']:<16s} adapter={pick_adapter(s, cfg):<14s}")
            print(f"        {shlex.join(cmd)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("sbp-supervise", description="为全部 tmux 会话维护 sbp-bridge 上报进程")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--once", action="store_true", help="只做一次规划并打印，不拉起进程")
    args = ap.parse_args(argv)

    cfg_path = Path(os.path.expanduser(args.config))
    cfg = load_config(cfg_path)
    if args.once:
        return plan(cfg)

    sup = Supervisor(cfg)

    def _sig(signum, _frame):
        print(f"\n  [supervise] 收到 signal {signum}，正在回收子进程…")
        sup._stop = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGHUP, _sig)
    try:
        return sup.run()
    finally:
        for f in sup.logs.values():
            try:
                f.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
