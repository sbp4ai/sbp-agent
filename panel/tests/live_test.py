"""live.active_sid 的四级判定单测。

用法：python tests/live_test.py
不依赖真 claude：自己造一个 CLAUDE_HOME，再用一个「抱着文件不放」的假 agent
进程模拟 pane 里的 claude。
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="sbp-live-")
os.environ["CLAUDE_HOME"] = TMP
os.environ["PANEL_STATE"] = os.path.join(TMP, "state")

from tmux_panel import claude, live   # noqa: E402  必须在设完 env 之后

CWD = "/tmp/sbp-live-proj"
PJ = claude.project_dir(CWD)
os.makedirs(PJ, exist_ok=True)

NEW = "aaaaaaaa-1111-2222-3333-444444444444"
MID = "bbbbbbbb-1111-2222-3333-444444444444"
OLD = "cccccccc-1111-2222-3333-444444444444"
for sid, age in ((NEW, 5), (MID, 3600), (OLD, 7200)):
    p = PJ / f"{sid}.jsonl"
    p.write_text('{"type":"user","message":{"content":"hi"}}\n')
    os.utime(p, (time.time() - age, time.time() - age))

ITEMS = [claude.entry(s, str(PJ / f"{s}.jsonl"), CWD) for s in (NEW, MID, OLD)]
ITEMS.sort(key=lambda x: x["updated_at"], reverse=True)
assert [i["sid"][:8] for i in ITEMS] == ["aaaaaaaa", "bbbbbbbb", "cccccccc"]


def case(name, panes, agent_live, want_sid, want_lvl):
    sid, lvl = live.active_sid(panes, ITEMS, agent_live)
    ok = (sid == want_sid) and (lvl == want_lvl)
    print(("PASS " if ok else "FAIL ") + f"{name}: sid={sid[:8] or '-'} lvl={lvl} "
          f"(want {want_sid[:8] or '-'} / {want_lvl})")
    return ok


ok = []
# L0：pane 里只是个 shell，没有 agent —— 全灰
ok.append(case("no agent -> none", [{"pid": "1"}], False, "", "none"))
# L1：agent 在跑，最新文件刚被写过
ok.append(case("agent + fresh -> recent", [{"pid": "1"}], True, NEW, "recent"))
# L0.5：agent 在跑但文件冷了，仍然认最新的那个
ITEMS_OLD = [dict(i, updated_at=int((time.time() - 99999) * 1000)) for i in ITEMS]
sid, lvl = live.active_sid([{"pid": "1"}], ITEMS_OLD, True)
print(("PASS " if (sid == NEW and lvl == "guess") else "FAIL ") + f"agent + cold -> guess: {sid[:8]} {lvl}")
ok.append(sid == NEW and lvl == "guess")

# L2：真有一个进程抱着 OLD 那个文件（模拟 claude --resume 老会话）
holder = subprocess.Popen(
    [sys.executable, "-c",
     textwrap.dedent("""
        import sys, time
        f = open(sys.argv[1])
        time.sleep(30)
     """), str(PJ / f"{OLD}.jsonl")])
time.sleep(1.2)
try:
    ok.append(case("held old sid -> held", [{"pid": str(holder.pid)}], True, OLD, "held"))
finally:
    holder.kill()

# ---- dsh 这类 pty agent：只看「此刻还活着什么」 -------------------------
PANE_DSH = {"pid": "4242", "command": "bash", "start": "bash -c 'exec -a dsh /tmp/dsh'"}
_real_cmd_map = live._cmd_map


def fake_cmds(mapping):
    live._cmd_map = lambda pids: {p: mapping.get(p, "") for p in pids}


def case_agent(name, mapping, want):
    fake_cmds(mapping)
    got = live.running_agent(PANE_DSH)
    ok_res = got == want
    print(("PASS " if ok_res else "FAIL ") + f"{name}: agent={got or '-'} (want {want or '-'})")
    return ok_res


# 进程树里还有 dsh -> 激活（深色）
ok.append(case_agent("dsh alive", {4242: "dsh /bin/bash -c run"}, "dsh"))
# dsh 已退出，pane 只剩 shell -> 未激活（灰）
ok.append(case_agent("dsh exited", {4242: "/bin/bash"}, ""))
# ps 拿不到（沙箱）-> 退回启动痕迹，宁可信其有
ok.append(case_agent("ps unavailable -> fallback", {}, "dsh"))
live._cmd_map = _real_cmd_map

print()
print(f"{sum(1 for x in ok if x)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
