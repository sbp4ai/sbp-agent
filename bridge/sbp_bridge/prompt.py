"""屏读 → PromptSpec：SBP-0.2 §5 的参考实现。

与 panel/tmux_panel/prompt.py 同源，两个包各自独立分发，改一处请同步另一处。
"""
from __future__ import annotations

import hashlib
import re
import time

# ---------------------------------------------------------------- 清洗
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]|\r")
_EMPTY = re.compile(r"^[\s│─╭╮╰╯|]*$")

# ---------------------------------------------------------------- 识别
# 编号选项：` 1. Yes` / `❯ 1. Yes` / ` 1) 继续`
_ITEM = re.compile(r"^\s*[❯>]*\s*(\d{1})[.)、]\s+(.+?)\s*$")
# 光标行：Claude Code 用 ❯ 标记当前高亮项
_CURSOR = "❯"
# 是/否
_YN = re.compile(r"\[(y/n)\]|\((y/n)\)|\[(Y/N)\]", re.I)
# 提示句
_ASK = re.compile(r"(Do you want to (proceed|allow|continue)|Allow this|Confirm|Proceed\?|"
                  r"Are you sure|Continue\?)", re.I)
# 这些词出现在选项里，说明选完还要等一段自由文本
_FREE_TEXT = re.compile(r"tell Claude what to do|type a (message|reason)|specify|other|"
                        r"其他|自定义|enter .*(message|reason)", re.I)
# 危险动作
_DANGER = re.compile(r"rm\s+-rf|DROP\s+TABLE|--force|-f\s+origin|deploy\s+--prod|sudo\s|"
                     r"chmod\s+777|DELETE\s+FROM|truncate\s|git\s+reset\s+--hard", re.I)

TAIL = 22           # 只看屏幕尾部这么多行
RAW_MAX = 800


def _clean(screen: str) -> list[str]:
    txt = _ANSI.sub("", screen or "")
    lines = [ln.rstrip() for ln in txt.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _pid(title: str, labels: list[str]) -> str:
    raw = "|".join([_norm(title)] + [_norm(x) for x in labels])
    return "p_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def extract(screen: str, agent: str = "claude-code") -> dict | None:
    """从屏幕文本里抽一份 PromptSpec；抽不到返回 None。"""
    lines = _clean(screen)
    if not lines:
        return None
    lines = lines[-TAIL:]

    n = len(lines)

    # 1) 从**底部**往上扫出一个连续的编号块。
    #    只看尾部是必须的：屏幕上方滚动历史里也有编号列表，那不是提示。
    hits: list[tuple[int, int, str, bool]] = []   # (行号, 序号, 标签, 光标命中)
    i = n - 1
    skipped = 0
    while i >= 0 and len(hits) < 9:
        m = _ITEM.match(lines[i])
        if m:
            idx = int(m.group(1))
            label = _norm(m.group(2))
            if not label:
                break
            # 序号必须自下而上递减（…3,2,1），否则是正文里的编号，不是选项块
            if hits and idx != hits[-1][1] - 1:
                break
            hits.append((i, idx, label, lines[i].lstrip().startswith(_CURSOR)))
        elif skipped < 1 and len(_norm(lines[i])) <= 4:
            # 刚敲进去的字符回显会插进选项块中间，跨过去一行
            skipped += 1
        else:
            break
        i -= 1
    hits.reverse()

    # 2) 最后几行里的 [y/N] / (yes/no)
    yn = None
    for j in range(n - 1, max(-1, n - 7), -1):
        if _YN.search(lines[j]):
            yn = j
            break

    # 屏幕底部是 [y/N] 时，它比更早的编号块更新，以它为准
    use_yn = yn is not None and (not hits or yn > hits[-1][0])
    if not hits and not use_yn:
        return None

    # 2) 标题 = 选项块上方最近的「问句」；命令/上下文放进 detail
    title = ""
    detail = ""
    if use_yn:
        top = yn
        # [y/N] 那一问本身就是标题
        rawline = _norm(_YN.sub("", lines[yn]).strip(" ?：:"))
        if rawline:
            title = rawline + "?"
    else:
        top = hits[0][0]
    for j in range(top - 1, max(-1, top - 7), -1):
        cand = _norm(lines[j]).strip("│| ").strip()
        if not cand or _EMPTY.match(lines[j]) or _ITEM.match(lines[j]):
            continue
        if _ASK.search(cand) and not title:
            title = cand
        elif title and not detail:
            detail = cand
        elif title and detail:
            break
    if not title:
        # 没有标准问句时，拿选项块上方第一句当标题
        for j in range(top - 1, max(-1, top - 5), -1):
            cand = _norm(lines[j]).strip("│| ").strip()
            if cand and not _EMPTY.match(lines[j]) and not _ITEM.match(lines[j]):
                title = cand.rstrip("：:")
                break
    if not title:
        title = "请选择" if hits else "需要确认"

    # 3) 选项
    options: list[dict] = []
    if use_yn:
        # 屏幕只说 [y/N]，协议上仍然是两个 Option，UI 才有一致的渲染路径
        options = [
            {"index": 1, "key": "y", "label": "Yes", "desc": "", "action": "input.choice",
             "payload": "y", "risk": "safe", "default": False,
             "free_text": False, "terminal": False},
            {"index": 2, "key": "n", "label": "No", "desc": "", "action": "input.choice",
             "payload": "n", "risk": "danger", "default": False,
             "free_text": False, "terminal": True},
        ]
    else:
        for _i, idx, label, cursor in hits:
            short, desc = _split(label)
            options.append({
                "index": idx,
                "key": None,
                "label": short,
                "desc": desc if desc != short else "",
                "action": "input.choice",
                "payload": str(idx),
                "risk": "danger" if _yn_label_is_no(label) else "safe",
                "default": cursor,
                "free_text": bool(_FREE_TEXT.search(label)),
                "terminal": bool(_FREE_TEXT.search(label)) or _yn_label_is_no(label),
            })

    # 4) 风险：命令危险 → danger；权限类 → warn
    ctx = " ".join([title, detail] + [o["label"] for o in options])
    risk = "danger" if _DANGER.search(ctx) else ("warn" if _ASK.search(title) else "safe")

    kind = "confirm" if use_yn else "choice"

    raw = "\n".join(lines[-14:])[-RAW_MAX:]

    return {
        "pid": _pid(title, [o["label"] for o in options]),
        "kind": kind,
        "state": "open",
        "title": title,
        "detail": detail,
        "risk": risk,
        "options": options,
        "fields": [],
        "allow_free_text": any(o["free_text"] for o in options),
        "ts": int(time.time() * 1000),
        "raw": raw,
        "source": "screen",
        "agent": agent,
    }


def _yn_label_is_no(label: str) -> bool:
    return bool(re.match(r"^(no|n\b|deny|reject|cancel|放弃|拒绝)", _norm(label), re.I))


def _split(label: str) -> tuple[str, str]:
    """长选项拆出「短标签 + 说明」，长路径/反引号别塞进按钮主文案。"""
    if len(label) <= 30:
        return label, ""
    # "Yes, and don't ask again for ..." → 在 ", and " 处断
    m = re.match(r"^(.{2,26}?),\s+(and|but|then)\s+(.+)$", label, re.I)
    if m:
        return m.group(1).rstrip(" ,，"), m.group(2) + " " + m.group(3)
    head = label[:30]
    cut = max(head.rfind(", "), head.rfind(" "))
    if cut > 10:
        head = label[:cut]
    return head.rstrip(" ,，").rstrip(), label


def fingerprint(spec: dict | None) -> str:
    return spec["pid"] if spec else ""


# ---------------------------------------------------------------- 作答
def answer_writes(spec: dict, payload: dict) -> list[tuple[str, bool]]:
    """把一次 session.answer 翻译成一串终端写入。

    返回 [(文本, 是否回车), ...]。由调用方负责延时。
    """
    out: list[tuple[str, bool]] = []
    if not spec:
        return out

    if payload.get("dismiss"):
        return out

    choice = payload.get("choice")
    if choice is not None:
        for o in spec["options"]:
            if o["index"] == int(choice):
                out.append((o["payload"], True))
                if o.get("free_text") and payload.get("text"):
                    out.append((str(payload["text"]), True))
                return out
        return out

    key = payload.get("option")
    if key:
        for o in spec["options"]:
            if (o.get("key") or "").lower() == str(key).lower():
                out.append((o["payload"], True))
                return out
        out.append((str(key), True))
        return out

    fields = payload.get("fields")
    if fields:
        for f in spec.get("fields") or []:
            v = fields.get(f["name"])
            if v:
                out.append((str(v), True))
        return out

    text = payload.get("text")
    if text:
        out.append((str(text), True))
    return out
