"""FastAPI 应用：本地面板 + SBP 风格 WebSocket。

消息信封与 spec/SBP-0.1.md 保持一致：
  {"v":"sbp/0.1","id":..,"type":"req|res|evt","name":..,"sid":..,"seq":..,"payload":{}}
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time

try:
    import resource
except ImportError:      # pragma: no cover - Windows
    resource = None
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import claude
from . import prompt as promptmod
from .hub import Hub
from .tmux import Tmux

VERSION = "sbp/0.1"
PROMPT_VER = "sbp/0.2"

# web/ 在开发时位于 tmux_panel/web（与 server.py 同包），打包安装后需用 importlib.resources
# 取出。两者都试，保证开发态与 wheel 安装态都能找到静态目录。
def _resolve_web_dir() -> Path:
    here = Path(__file__).resolve().parent
    local = here / "web"
    if local.is_dir():
        return local
    try:
        from importlib.resources import files as _files
        cand = _files("tmux_panel") / "web"
        if cand.is_dir():
            return Path(str(cand))
    except Exception:
        pass
    return local

WEB_DIR = _resolve_web_dir()

load_dotenv(os.path.join(os.getcwd(), ".env"))
ACCOUNT = os.getenv("PANEL_ACCOUNT", "")
SECRET_TEXT = os.getenv("PANEL_SECERT", "")
AUTH_ON = bool(ACCOUNT and SECRET_TEXT)
SECRET = (SECRET_TEXT or "local-panel").encode()
COOKIE = "panel_session"
MAX_AGE = 7 * 24 * 3600


def _load_hmac_keys() -> dict[str, str]:
    """.env 里 SBP_HMAC_KEYS=keyId1:secret1,keyId2:secret2。

    不配就不要求签名（本地 --no-auth 场景照旧）；配了，客户端必须在 hello 里带 HMAC 签名。
    """
    out: dict[str, str] = {}
    for part in os.getenv("SBP_HMAC_KEYS", "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


HMAC_KEYS = _load_hmac_keys()
SBP_AUTH_ON = bool(HMAC_KEYS) or AUTH_ON


def _check_hmac(payload: dict) -> bool:
    """校验小程序/外部客户端的握手签名。

    签名内容 = "sbp1\\n{keyId}\\n{ts}\\n{nonce}"，与 app/minis/utils/hmac.js 一致。
    没配密钥时放行；配了就必须签，并且 ts 要在 5 分钟内（防重放）。
    """
    if not HMAC_KEYS:
        return True
    a = payload.get("auth") or {}
    if a.get("scheme") != "hmac-sha256":
        return False
    kid = str(a.get("keyId") or "")
    secret = HMAC_KEYS.get(kid)
    if not secret:
        return False
    msg = "sbp1\n%s\n%s\n%s" % (kid, a.get("ts"), a.get("nonce"))
    want = hmac.new(secret.encode(), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, str(a.get("sig") or "")):
        return False
    try:
        if abs(int(time.time() * 1000) - int(a.get("ts"))) > 5 * 60 * 1000:
            return False
    except (TypeError, ValueError):
        return False
    return True

hub = Hub()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(hub.tick_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="tmux-claude-panel", version="0.1.0", lifespan=lifespan)


# ------------------------------------------------------------------ CORS
# 让千问办公（QwenWork）侧边栏里的 qwapp、以及任何跨源前端都能调本地 panel API。
# 本地单机的 localhost 服务，* 足够；若要收紧可改成具体的源列表。
@app.middleware("http")
async def _cors(request: Request, call_next):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        })
    resp = await call_next(request)
    resp.headers.setdefault("Access-Control-Allow-Origin", "*")
    resp.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    resp.headers.setdefault("Access-Control-Allow-Headers", "*")
    return resp


# ---------------------------------------------------------------- auth
def _sign(s: str) -> str:
    return hmac.new(SECRET, s.encode(), hashlib.sha256).hexdigest()


def make_token(account: str) -> str:
    return f"{account}.{_sign(account)}"


def verify(token: Optional[str]) -> Optional[str]:
    if not AUTH_ON:
        return "local"
    if not token or "." not in token:
        return None
    acc, sig = token.rsplit(".", 1)
    if acc != ACCOUNT:
        return None
    return acc if hmac.compare_digest(sig, _sign(acc)) else None


def _token_from_ws(ws: WebSocket) -> Optional[str]:
    raw = ws.headers.get("cookie", "")
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k == COOKIE:
                return v
    return ws.query_params.get("token")


@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    acc = (body.get("account") or "").strip()
    pwd = body.get("password") or ""
    if not AUTH_ON:
        return {"ok": True, "account": "local", "auth": False}
    if acc == ACCOUNT and pwd == SECRET_TEXT:
        tok = make_token(acc)
        response.set_cookie(COOKIE, tok, httponly=True, samesite="lax", path="/", max_age=MAX_AGE)
        return {"ok": True, "account": acc, "token": tok, "auth": True}
    return JSONResponse({"ok": False, "error": "账号或密码错误"}, status_code=401)


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request):
    acc = verify(request.cookies.get(COOKIE))
    return {"ok": bool(acc), "account": acc, "auth": AUTH_ON}


def _http_auth(request: Request) -> bool:
    """GET/POST 类 HTTP 接口的鉴权：cookie（浏览器）或 HMAC 查询串（小程序/curl）。

    HMAC 参数与 WS 的 hello 帧一致，只是放到 query 里：
      ?keyId=..&ts=..&nonce=..&sig=..
    """
    if not SBP_AUTH_ON:
        return True
    if verify(request.cookies.get(COOKIE)) or verify(request.query_params.get("token")):
        return True
    if not HMAC_KEYS:
        return False
    q = request.query_params
    a = {"scheme": "hmac-sha256", "keyId": q.get("keyId"), "ts": q.get("ts"),
         "nonce": q.get("nonce"), "sig": q.get("sig")}
    h = request.headers.get("authorization", "")
    if h.startswith("SBP-HMAC "):
        for part in h[9:].split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip().lower()
                if k in ("keyid", "ts", "nonce", "sig"):
                    a["keyId" if k == "keyid" else k] = v.strip()
    return _check_hmac({"auth": a})


def _deny() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "未授权：缺 cookie 或 HMAC 签名"}, status_code=401)


def _not_modified(request: Request, etag: str) -> Response | None:
    """客户端带上次的 ETag，内容没变就回 304 —— 不带 body，省掉整份序列化+传输。"""
    inm = request.headers.get("if-none-match", "")
    if inm and inm.strip('"') == etag.strip('"'):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-store"})
    return None


@app.get("/api/index")
async def http_index(request: Request, refresh: int = 0):
    """侧栏索引（懒加载：只要 sid + 标题 + 状态）。带 ETag，没变回 304。"""
    if not _http_auth(request):
        return _deny()
    data = await hub.index(force=bool(refresh))
    tag = hub.index_etag(data)
    if not refresh:
        nm = _not_modified(request, tag)
        if nm:
            return nm
    return JSONResponse({"ok": True, **data}, headers={"ETag": tag, "Cache-Control": "no-store"})


@app.get("/api/state")
async def http_state(request: Request, sid: str = "", interval: float = 5.0):
    """REST 轮询主接口：一次拿「屏幕 + 选项卡 + 状态」。

    interval 告诉服务端你的轮询节奏，服务端据此把抓屏周期压到 interval/2，
    并且多个轮询者共享同一次抓屏 —— 人数不放大成本。
    """
    if not _http_auth(request):
        return _deny()
    if not sid:
        return JSONResponse({"ok": False, "error": "缺 sid"}, status_code=400)
    await hub.index()                       # 先建索引，才能知道 sid 属于哪个 tmux
    w = hub.touch(sid, interval)
    if not w:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    if not w.last_screen and w.tmux_name:
        # 首屏别等下一轮 tick，先抓一次
        await hub.screen(w.tmux_name, 120, ttl=0)
        w.last_screen = hub._snap.get(w.tmux_name, (0, ""))[1]
        if w.last_screen:
            w.prompt = promptmod.extract(w.last_screen, w.agent)
    st = hub.poll_state(sid)
    body = json.dumps(st, ensure_ascii=False, sort_keys=True)
    tag = hub.etag(body)
    nm = _not_modified(request, tag)
    if nm:
        return nm
    return Response(body, media_type="application/json",
                    headers={"ETag": tag, "Cache-Control": "no-store"})


@app.get("/api/history")
async def http_history(request: Request, sid: str = "", before: str = "", limit: int = 20):
    """倒序分页拉历史（懒加载：点开才读）。"""
    if not _http_auth(request):
        return _deny()
    path = claude.find_by_sid(sid)
    if not path:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, lambda: claude.page(path, before or None, int(limit or 20)))
    return {"ok": True, "sid": sid, **data}


@app.post("/api/rpc")
async def http_rpc(request: Request):
    """把 SBP 帧装进一次 HTTP 请求：req 进、res + evt 出。

    写操作（input / answer / subtitle / attach）走这里，和 WS 共用 _dispatch，
    所以新增一个 RPC 只需要在一处实现。小程序、curl、IM 卡片都能用。
    鉴权与 GET 一致：cookie 或 HMAC 查询串/Authorization 头；通过后帧里的
    hello 不必再带签名。
    """
    if not _http_auth(request):
        return _deny()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body 必须是 JSON"}, status_code=400)
    frames = body.get("frames") if isinstance(body, dict) else body
    if isinstance(frames, dict):
        frames = [frames]
    if not isinstance(frames, list):
        return JSONResponse({"ok": False, "error": "需要帧数组"}, status_code=400)

    c = RestClient()
    c.preauth = SBP_AUTH_ON
    for f in frames:
        if not isinstance(f, dict) or f.get("type") != "req":
            continue
        await _dispatch(c, f)
    # 请求结束就摘掉订阅：RestClient 没有长连接，留在 hub.watches 里
    # 会让 tick 一直为这个已经消失的客户端抓屏（泄漏）
    for sid in list(c.subs):
        hub.detach(c, sid)
    return {"ok": True, "frames": c.out}


@app.get("/api/stats")
async def stats():
    """传输相关的诊断计数，用来对比不同协议的服务端开销。

    cpu_kids 是所有被 wait 掉的子进程（tmux/ps/lsof）累计 CPU —— 抓屏的
    真实代价就落在这一项上，所以它是「哪种传输更省服务端」的主指标。
    """
    try:
        me = resource.getrusage(resource.RUSAGE_SELF)
        kids = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = {"cpu_self": round(me.ru_utime + me.ru_stime, 4),
               "cpu_kids": round(kids.ru_utime + kids.ru_stime, 4)}
    except Exception:
        cpu = {}
    return {"ok": True, "clients": len(hub.clients), "ws_subs": len(hub.watches),
            "pollers": len(hub._pollers), "watches": len(hub._watch_state),
            "ticks": hub._ticks, "period": round(hub._period(), 2),
            "tmux_runs": Tmux.COUNTS["run"], "captures": Tmux.COUNTS["capture"], **cpu}


@app.post("/api/stats/reset")
async def stats_reset():
    """benchmark 用：把 fork 计数归零、清掉上一档留下的轮询者，方便取差值。

    不清的话上一档 1s 轮询的人还挂在那儿，tick 周期会一直被拖在 0.6s，
    把下一档的测量结果污染掉。
    """
    Tmux.COUNTS["run"] = 0
    Tmux.COUNTS["capture"] = 0
    hub._pollers.clear()
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"ok": True, "v": VERSION, "auth": SBP_AUTH_ON,
            "sessions": len(Tmux.list_sessions())}


# ---------------------------------------------------------------- ws
class Client:
    """WebSocket 客户端。"""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.subs: set[str] = set()

    async def send(self, obj: dict) -> None:
        await self.send_raw(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    async def send_raw(self, blob: str) -> None:
        """hub 扇出时已经序列化好了，这里只管写，别再 dumps 一遍。"""
        try:
            await self.ws.send_text(blob)
        except Exception:
            pass

    async def send_evt(self, name: str, payload: dict, sid: str | None = None) -> None:
        frame = {"v": VERSION, "type": "evt", "name": name, "payload": payload}
        if sid:
            frame["sid"] = sid
        await self.send(frame)

    async def close(self, code: int = 1000) -> None:
        try:
            await self.ws.close(code=code)
        except Exception:
            pass


class RestClient:
    """把 SBP 帧塞进一次 HTTP 请求里跑完：req 进、res/evt 出。

    和 WS 共用 _dispatch，所以「加一个 RPC 只用写一处」。
    REST 轮询这条路上没有长连接，也就没有扇出——只服务这一次请求。
    """

    def __init__(self) -> None:
        self.out: list[dict] = []
        self.subs: set[str] = set()

    async def send(self, obj: dict) -> None:
        self.out.append(obj)

    async def send_raw(self, blob: str) -> None:
        try:
            self.out.append(json.loads(blob))
        except Exception:
            pass

    async def send_evt(self, name: str, payload: dict, sid: str | None = None) -> None:
        frame = {"v": VERSION, "type": "evt", "name": name, "payload": payload}
        if sid:
            frame["sid"] = sid
        self.out.append(frame)

    async def close(self, code: int = 1000) -> None:
        pass


def _res(ref: str, name: str, ok: bool = True, payload: dict | None = None, err: dict | None = None) -> dict:
    out = {"v": VERSION, "type": "res", "ref": ref, "name": name, "ok": ok}
    if payload is not None:
        out["payload"] = payload
    if err:
        out["err"] = err
    return out


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    # 配了 HMAC 密钥时，放行到 hello 帧再验签（外部客户端没有浏览器 cookie）
    if not HMAC_KEYS and not verify(_token_from_ws(ws)):
        await ws.send_json(_res("h0", "hello", False, err={"code": "E_AUTH", "msg": "未登录"}))
        await ws.close(code=4401)
        return

    client = Client(ws)
    hub.clients.add(client)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if frame.get("type") != "req":
                continue
            await _dispatch(client, frame)
    except Exception:
        pass
    finally:
        hub.clients.discard(client)
        hub.detach_all(client)


async def _tmux_of(sid: str) -> str:
    """sid -> 所在 tmux 会话名；不在 tmux 里则为空串。"""
    w = hub._watch_state.get(sid)
    if w and w.tmux_name:
        return w.tmux_name
    data = await hub.index()
    return (data.get("sid_to_tmux") or {}).get(sid, "")


async def _dispatch(c: Client, f: dict) -> None:
    rid = f.get("id") or "?"
    name = f.get("name") or ""
    p = f.get("payload") or {}
    sid = f.get("sid") or p.get("sid") or ""

    if name == "ping":
        # 应用层心跳：小程序/IM 卡片靠它判断线路还活着
        await c.send(_res(rid, "ping", True, {"t": p.get("t"), "ts": int(time.time() * 1000)}))
        return

    if name == "hello":
        # REST 通道已经验过 HTTP 层签名（query/Authorization）就不再要求帧内签名
        if not (getattr(c, "preauth", False) or _check_hmac(p)):
            await c.send(_res(rid, "hello", False,
                              err={"code": "E_AUTH", "msg": "签名校验失败，检查 KeyId 与 Secret"}))
            await c.close(4408)
            return
        await c.send(_res(rid, "hello", True, {
            "server": {"v": VERSION, "prompt": PROMPT_VER, "ts": int(time.time() * 1000),
                       "sbp_auth": SBP_AUTH_ON},
            "caps": ["sessions.index", "session.list", "session.history", "session.attach",
                     "session.input", "session.subtitle", "pane.snapshot",
                     "session.prompt", "session.answer", "ping"],
            "auth": SBP_AUTH_ON,
        }))
        return

    # SBP-0.2 §8 帧名别名：两种 server 实现共用一套 client
    if name in ("sessions.index", "sessions.list", "session.list"):
        data = await hub.index(force=bool(p.get("refresh")))
        await c.send(_res(rid, name, True, data))
        return

    if name == "session.history":
        path = claude.find_by_sid(sid)
        if not path:
            await c.send(_res(rid, name, False, err={"code": "E_NO_SESSION", "msg": "会话不存在"}))
            return
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, lambda: claude.page(path, p.get("before"), int(p.get("limit") or 20)))
        await c.send(_res(rid, name, True, {"sid": sid, **data}))
        return

    if name == "session.attach":
        await hub.index()                      # 先建索引，attach 才能拿到 tmux 归属
        w = hub.attach(c, sid)
        if not w:
            await c.send(_res(rid, name, False, err={"code": "E_NO_SESSION", "msg": "会话不存在"}))
            return
        c.subs.add(sid)
        screen = Tmux.capture(w.tmux_name, lines=40) if w.tmux_name else ""
        # 打开会话时立刻解析一次，不等下一轮 tick
        spec = promptmod.extract(screen, w.agent) if screen else None
        w.last_screen = screen
        w.last_pid = spec["pid"] if spec else ""
        w.prompt = spec
        await c.send(_res(rid, name, True, {
            "sid": sid, "last_seq": w.last_seq, "tmux": w.tmux_name,
            "editable": bool(w.tmux_name), "kind": "term" if not w.path else "claude",
            "prompt": spec,
        }))
        if screen:
            await c.send_evt("output.delta", {"sid": sid, "mode": "screen",
                                              "lines": screen.splitlines()[-40:]}, sid=sid)
        if spec:
            await c.send_evt("session.prompt", {"sid": sid, **spec}, sid=sid)
        return

    if name == "session.detach":
        hub.detach(c, sid)
        c.subs.discard(sid)
        await c.send(_res(rid, name, True, {"sid": sid}))
        return

    if name == "pane.snapshot":
        tmux_name = await _tmux_of(sid)
        if not tmux_name:
            await c.send(_res(rid, name, False, err={"code": "E_NO_TMUX", "msg": "该会话不在 tmux 中"}))
            return
        # 走共享缓存：并发/高频轮询共用一次 capture-pane（省 4 次 fork/次）
        screen = await hub.screen(tmux_name, int(p.get("lines") or 120))
        w = hub._watch_state.get(sid)
        spec = (w.prompt if w and w.last_screen == screen
                else promptmod.extract(screen, w.agent if w else "claude-code"))
        await c.send(_res(rid, name, True, {"sid": sid, "tmux": tmux_name,
                                            "lines": screen.splitlines(), "prompt": spec}))
        return

    if name == "session.input":
        tmux_name = await _tmux_of(sid)
        if not tmux_name:
            await c.send(_res(rid, name, False, err={"code": "E_NO_TMUX", "msg": "该会话不在 tmux 中，无法输入"}))
            return
        kind = p.get("kind") or "text"
        loop = asyncio.get_running_loop()
        if kind == "key":
            ok = await loop.run_in_executor(None, lambda: Tmux.send_key(tmux_name, str(p.get("key") or "ENTER")))
        else:
            ok = await loop.run_in_executor(
                None, lambda: Tmux.send_text(tmux_name, str(p.get("text") or ""), bool(p.get("submit", True))))
        await c.send(_res(rid, name, ok, {"sid": sid, "tmux": tmux_name},
                          None if ok else {"code": "E_INTERNAL", "msg": "tmux 写入失败"}))
        return

    if name == "session.answer":
        w = hub._watch_state.get(sid)
        spec = w.prompt if w else None
        pid = p.get("pid") or ""
        if not spec or (pid and spec["pid"] != pid):
            await c.send(_res(rid, name, False,
                              err={"code": "E_PROMPT_STALE", "msg": "该选项已过期，请按屏幕最新内容操作"}))
            return
        tmux_name = await _tmux_of(sid)
        if not tmux_name:
            await c.send(_res(rid, name, False, err={"code": "E_NO_TMUX", "msg": "会话不可写"}))
            return
        writes = promptmod.answer_writes(spec, p)
        if not writes:
            # dismiss / 空作答：只收卡片，不写终端
            await c.send(_res(rid, name, True, {"sid": sid, "pid": spec["pid"],
                                                "state": "answered", "noop": True}))
            return
        loop = asyncio.get_running_loop()
        for i, (text, submit) in enumerate(writes):
            if i:
                await asyncio.sleep(0.3)      # 让 agent 先把输入框画出来
            ok = await loop.run_in_executor(
                None, lambda t=text, s=submit: Tmux.send_text(tmux_name, t, s))
            if not ok:
                await c.send(_res(rid, name, False, err={"code": "E_INTERNAL", "msg": "tmux 写入失败"}))
                return
        if w:
            # 卡片收起，但记住这个 pid：否则下一轮 tick 抓到「作答回显」那一屏时
            # 解析出同一个 pid，会误判成新提示又把卡片弹回来
            w.prompt = None
            w.last_pid = spec["pid"]
        await c.send(_res(rid, name, True, {"sid": sid, "pid": spec["pid"], "state": "answered"}))
        await hub._fanout(list(hub.watches.get(sid) or ()),
                          {"v": VERSION, "type": "evt", "name": "session.prompt", "sid": sid,
                           "payload": {"sid": sid, "pid": spec["pid"], "state": "answered"}})
        return

    if name == "session.subtitle":
        claude.anno.set(sid, title=p.get("title"), subtitle=p.get("subtitle"))
        hub.invalidate()
        await c.send(_res(rid, name, True, {"sid": sid, "anno": claude.anno.get(sid)}))
        await hub.broadcast_index()
        return

    await c.send(_res(rid, name, False, err={"code": "E_UNKNOWN_NAME", "msg": f"未知请求 {name}"}))


# ---------------------------------------------------------------- static
if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index_page():
    f = WEB_DIR / "index.html"
    if f.exists():
        return FileResponse(f)
    return HTMLResponse("<h1>web/ 缺失</h1>", status_code=500)


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)
