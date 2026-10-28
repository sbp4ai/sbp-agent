/* tmux-claude-panel 前端：无构建、单文件。
 * 协议见 ../../../spec/SBP-0.1.md（req/res/evt 信封，懒加载 session.history）。
 */
(function () {
  "use strict";

  var V = "sbp/0.1";

  var S = {
    ws: null,
    retry: 0,
    closed: false,
    seq: 0,
    pending: {},
    groups: [],
    sidToTmux: {},
    cur: null,          // {sid, tmux, editable, lastSeq, total, nextBefore, hasMore, loading}
    seen: {},           // seq 去重
    screen: [],
    collapsed: {},
    authOn: false,
    prompt: null        // 当前 open 的 PromptSpec（SBP-0.2）
  };

  var $ = function (sel) { return document.querySelector(sel); };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function toast(msg) {
    var t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(t._h);
    t._h = setTimeout(function () { t.classList.remove("show"); }, 1600);
  }

  function ago(ms) {
    var d = (Date.now() - (ms || 0)) / 1000;
    if (d < 60) return "刚刚";
    if (d < 3600) return Math.floor(d / 60) + "m";
    if (d < 86400) return Math.floor(d / 3600) + "h";
    return Math.floor(d / 86400) + "d";
  }

  function hhmm(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d)) return "";
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    var now = new Date();
    var same = d.toDateString() === now.toDateString();
    return same ? p(d.getHours()) + ":" + p(d.getMinutes())
                : (d.getMonth() + 1) + "/" + d.getDate() + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function collapseState() {
    try { return JSON.parse(localStorage.getItem("panel.collapsed") || "{}"); } catch (e) { return {}; }
  }
  function saveCollapse() {
    try { localStorage.setItem("panel.collapsed", JSON.stringify(S.collapsed)); } catch (e) {}
  }

  // ------------------------------------------------------------ auth
  function boot() {
    S.collapsed = collapseState();
    fetch("/api/me", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (me) {
        S.authOn = !!me.auth;
        if (me.ok) start();
        else showLogin();
      })
      .catch(function () { showLogin(); });
  }

  function showLogin() {
    $("#login").hidden = false;
    $("#app").hidden = true;
    $("#loginForm").onsubmit = function (e) {
      e.preventDefault();
      var body = { account: $("#acc").value.trim(), password: $("#pwd").value };
      fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          if (res.ok && res.j.ok) {
            $("#login").hidden = true;
            start();
          } else {
            var el = $("#loginErr");
            el.textContent = (res.j && (res.j.error || res.j.detail)) || "登录失败";
            el.style.display = "block";
          }
        });
    };
  }

  // ------------------------------------------------------------ transport
  // 两种传输，同一套 SBP 帧语义：
  //   rest —— 定时 HTTP 懒加载，GET 带 ETag（默认，省服务器资源、无长连接）
  //   ws   —— 长连接，服务端主动推（用 ?transport=ws 才开，延迟最低）
  // 默认走 rest；?transport=ws 显式开长连接，连不上时自动降级回 rest。
  var TP = { mode: "rest", interval: 5000, etags: {}, timer: null };

  function initTransport() {
    var q = new URLSearchParams(location.search);
    var m = (q.get("transport") || "").toLowerCase();
    TP.mode = (m === "ws") ? "ws" : "rest";
    TP.interval = Math.max(500, Math.min(120000,
      parseInt(q.get("interval") || "5000", 10) || 5000));
  }

  function getJSON(url, key) {
    var h = {};
    if (TP.etags[key]) h["If-None-Match"] = TP.etags[key];
    return fetch(url, { headers: h }).then(function (r) {
      var tag = r.headers.get("ETag");
      if (tag) TP.etags[key] = tag;
      if (r.status === 304) return null;        // 内容没变，省掉整份 body
      if (!r.ok) throw { msg: "HTTP " + r.status };
      return r.json();
    });
  }

  /** 把 SBP 帧装进一次 HTTP 请求：res 取回，evt 顺手喂给 onFrame。 */
  function rpc(frames) {
    var id = frames[0].id;
    return fetch("/api/rpc", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ frames: frames })
    }).then(function (r) { return r.json(); }).then(function (j) {
      var out = null, err = null;
      (j.frames || []).forEach(function (f) {
        if (f.type === "res" && f.ref === id) {
          if (f.ok) out = f.payload || {}; else err = f.err || { msg: "请求失败" };
        } else {
          onFrame(f);
        }
      });
      if (err) throw err;
      return out || {};
    });
  }

  function reqRest(name, payload, sid) {
    payload = payload || {};
    if (name === "sessions.index") {
      // 不传 refresh：索引由服务端 tick 统一重建，这里只读缓存 + ETag 比对
      return getJSON("/api/index", "idx");
    }
    if (name === "pane.snapshot") {
      return getJSON("/api/state?sid=" + encodeURIComponent(sid) +
        "&interval=" + (TP.interval / 1000), "st:" + sid);
    }
    if (name === "session.history") {
      return getJSON("/api/history?sid=" + encodeURIComponent(sid) +
        "&before=" + encodeURIComponent(payload.before || "") +
        "&limit=" + (payload.limit || 20), "hi:" + sid + ":" + (payload.before || ""));
    }
    return rpc([{ v: V, id: "c" + (++S.seq), type: "req", name: name, sid: sid, payload: payload }]);
  }

  function startPolling() {
    if (TP.mode !== "rest" || TP.timer) return;
    var tick = function () {
      if (document.hidden) return;              // 页面不可见就别轮询了
      loadIndex();
      if (S.cur) refreshScreen();
    };
    TP.timer = setInterval(tick, TP.interval);
  }

  function transportChip() {
    var el = $("#transport");
    if (!el) return;
    el.textContent = TP.mode === "rest" ? ("REST " + (TP.interval / 1000) + "s") : "WS";
    el.className = "chip " + (TP.mode === "rest" ? "warn" : "tmux");
    el.title = TP.mode === "rest"
      ? "定时 HTTP 懒加载，GET 带 ETag（内容没变回 304）。用 ?transport=ws 切回长连接"
      : "WebSocket 长连接，服务端变化即推。用 ?transport=rest&interval=5000 切换成轮询";
  }

  // ------------------------------------------------------------ ws
  function fallbackToRest(why) {
    if (TP.mode === "rest") return;
    TP.mode = "rest";
    TP.etags = {};
    console.warn("[sbp] WebSocket 不可用（" + why + "），降级为 HTTP 轮询 " +
                 (TP.interval / 1000) + "s");
    transportChip();
    loadIndex();
    if (S.cur) refreshScreen();
    startPolling();
  }

  function connect() {
    if (TP.mode === "rest") {                   // 明确指定轮询，不建长连接
      transportChip();
      loadIndex();
      startPolling();
      return;
    }
    var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
    var q = new URLSearchParams(location.search).get("token");
    if (q) url += "?token=" + encodeURIComponent(q);
    var ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      fallbackToRest("构造失败");
      return;
    }
    S.ws = ws;

    ws.onopen = function () {
      S.retry = 0;
      req("hello", {}).then(function () {
        loadIndex();
        // 断线重连后恢复订阅与屏幕
        if (S.cur) { attach(S.cur.sid); refreshScreen(); }
      }).catch(function () { loadIndex(); });
    };
    ws.onmessage = function (ev) {
      var f;
      try { f = JSON.parse(ev.data); } catch (e) { return; }
      onFrame(f);
    };
    ws.onerror = function () {
      // 代理/网关拦 WebSocket 时最常见的症状：连不上且不给 close
      if (S.retry >= 2) fallbackToRest("连接失败");
    };
    ws.onclose = function () {
      if (S.closed) return;
      S.retry++;
      if (S.retry >= 3) { fallbackToRest("反复断线"); return; }
      setTimeout(connect, Math.min(15000, 600 * Math.pow(2, S.retry)));
    };
  }

  function req(name, payload, sid) {
    if (TP.mode === "rest") return reqRest(name, payload, sid);
    var id = "c" + (++S.seq);
    var frame = { v: V, id: id, type: "req", name: name, payload: payload || {} };
    if (sid) frame.sid = sid;
    return new Promise(function (resolve, reject) {
      if (!S.ws || S.ws.readyState !== 1) { reject(new Error("offline")); return; }
      S.pending[id] = { resolve: resolve, reject: reject };
      S.ws.send(JSON.stringify(frame));
      setTimeout(function () {
        if (S.pending[id]) { delete S.pending[id]; reject(new Error("timeout:" + name)); }
      }, 12000);
    });
  }

  function onFrame(f) {
    if (f.type === "res") {
      var p = S.pending[f.ref];
      if (p) {
        delete S.pending[f.ref];
        f.ok ? p.resolve(f.payload || {}) : p.reject(f.err || { msg: "请求失败" });
      }
      return;
    }
    if (f.type !== "evt") return;

    if (f.name === "sessions.index") {
      applyIndex(f.payload || {});
    } else if (f.name === "session.message") {
      if (S.cur && f.sid === S.cur.sid) appendMsg(f.payload.message, true);
    } else if (f.name === "output.delta") {
      if (S.cur && f.sid === S.cur.sid) renderScreen(f.payload.lines || []);
    } else if (f.name === "session.state") {
      if (S.cur && f.sid === S.cur.sid) setScreenState(f.payload.status);
    } else if (f.name === "session.prompt") {
      if (S.cur && f.sid === S.cur.sid) renderPrompt(f.payload || {});
    }
  }

  // ------------------------------------------------------------ sidebar
  function loadIndex() {
    req("sessions.index", { refresh: true }).then(function (pl) {
      if (pl) applyIndex(pl);                   // null = 304，索引没变
    }).catch(function () {
      $("#tree").innerHTML = '<div class="sb-empty">会话列表加载失败<br>检查 tmux 是否在跑</div>';
    });
  }

  function applyIndex(pl) {
    // 正在改注释时别重画，否则输入框会被冲掉；存着，等编辑结束再补
    if (document.querySelector(".it-input")) {
      S.pendingIndex = pl;
      return;
    }
    S.groups = pl.groups || [];
    S.sidToTmux = pl.sid_to_tmux || {};
    renderTree();
  }

  function flushPendingIndex() {
    if (!S.pendingIndex) return;
    var pl = S.pendingIndex;
    S.pendingIndex = null;
    applyIndex(pl);
  }

  function renderTree() {
    var total = 0;
    S.groups.forEach(function (g) { total += g.count; });
    $("#stat").textContent = total + " 会话";

    if (!S.groups.length) {
      $("#tree").innerHTML =
        '<div class="sb-empty">没有发现 tmux 会话<br><br>' +
        '先开一个：<br><code>tmux new -s work</code><br>再在里面跑 <code>claude</code></div>';
      return;
    }

    var html = S.groups.map(function (g, gi) {
      var key = g.kind + ":" + g.name;
      var col = S.collapsed[key] ? " collapsed" : "";
      var items = g.items.map(function (it) { return itemHTML(it, g); }).join("");
      var liveDot = g.live ? '<span class="dot live"></span>' : '<span class="dot"></span>';
      return '<div class="group' + col + '" data-gkey="' + esc(key) + '">' +
        '<div class="group-head" data-toggle="' + esc(key) + '">' +
          '<span class="caret">▾</span>' + liveDot +
          '<span class="group-name">' + esc(g.name) + '</span>' +
          '<span class="group-num">' + g.count + '</span>' +
          '<span class="group-path" title="' + esc(g.cwd) + '">' + esc(shortPath(g.cwd)) + '</span>' +
        '</div>' +
        '<div class="group-items">' + (items || '<div class="sb-empty" style="padding:12px">此目录下没有 claude 会话</div>') + '</div>' +
      '</div>';
    }).join("");

    $("#tree").innerHTML = html;

    Array.prototype.forEach.call(document.querySelectorAll("[data-toggle]"), function (el) {
      el.onclick = function () {
        var k = el.getAttribute("data-toggle");
        S.collapsed[k] = !S.collapsed[k];
        saveCollapse();
        el.parentNode.classList.toggle("collapsed", !!S.collapsed[k]);
      };
    });

    Array.prototype.forEach.call(document.querySelectorAll(".item"), function (el) {
      el.onclick = function (e) {
        if (el.querySelector("input")) return;
        if (e.target.classList.contains("it-edit")) return;
        openSession(el.getAttribute("data-sid"));
      };
    });

    Array.prototype.forEach.call(document.querySelectorAll(".it-edit"), function (el) {
      el.onclick = function (e) {
        e.stopPropagation();
        editSubtitle(el.closest(".item"));
      };
    });
  }

  function shortPath(p) {
    if (!p) return "";
    var home = p.indexOf("/Users/") === 0 ? p.replace(/^\/Users\/[^/]+/, "~") : p;
    var parts = home.split("/");
    return parts.length > 3 ? "…/" + parts.slice(-2).join("/") : home;
  }

  function itemHTML(it, g) {
    // active = 这个 tmux 会话里当前正被 agent 用着的会话：深黑 + 绿点；其余全灰
    var cur = S.cur && S.cur.sid === it.sid ? " active" : "";
    var on = it.active ? " live" : "";
    var idle = it.active ? "" : " idle";
    var sub = it.subtitle || "";
    var cls = "it-sub" + (it.custom ? " custom" : "");
    var tip = it.active ? "正在 " + (g.name || "tmux") + " 里运行" : (it.kind === "term" ? "未激活 · agent 已退出" : "未激活 · 历史会话");
    return '<div class="item' + cur + idle + '" data-sid="' + esc(it.sid) +
      '" data-kind="' + esc(it.kind || "claude") + '" title="' + esc(tip) + '">' +
      '<div class="it-main">' +
        '<div class="it-title"><span class="dot' + on + '"></span>' + esc(it.title) + '</div>' +
        '<div class="' + cls + '" title="' + esc(sub) + '">' + esc(sub || "（无标题）") + '</div>' +
        '<div class="it-meta"><span>' + (it.count == null ? "—" : it.count + " 条") + '</span>' +
          '<span>' + ago(it.updated_at) + '</span>' +
          (it.branch ? '<span>' + esc(it.branch) + '</span>' : "") +
        '</div>' +
      '</div>' +
      '<button class="it-edit" title="编辑注释">✎</button>' +
    '</div>';
  }

  function editSubtitle(itemEl) {
    var sid = itemEl.getAttribute("data-sid");
    var subEl = itemEl.querySelector(".it-sub");
    if (!subEl || itemEl.querySelector("input")) return;
    var old = subEl.textContent === "（无标题）" ? "" : subEl.textContent;
    var input = document.createElement("input");
    input.className = "it-input";
    input.value = old;
    input.placeholder = "自定义注释，回车保存";
    subEl.replaceWith(input);
    input.focus();
    input.select();

    var done = function (save) {
      var val = input.value.trim();
      input.replaceWith(subEl);
      flushPendingIndex();
      if (!save) return;
      req("session.subtitle", { sid: sid, subtitle: val }, sid).then(function () {
        toast(val ? "注释已保存" : "已清除注释");
      }).catch(function (e) { toast((e && e.msg) || "保存失败"); });
    };
    input.onkeydown = function (e) {
      if (e.key === "Enter") { e.preventDefault(); done(true); }
      else if (e.key === "Escape") { e.preventDefault(); done(false); }
    };
    input.onblur = function () { done(true); };
  }

  // ------------------------------------------------------------ session
  function openSession(sid) {
    if (S.cur && S.cur.sid === sid) return;
    S.cur = { sid: sid, tmux: "", editable: false, lastSeq: 0, total: 0,
              nextBefore: null, hasMore: false, loading: false };
    S.seen = {};
    S.screen = [];
    S._screenOpened = false;
    clearPrompt();

    $("#empty").hidden = true;
    $("#stream").hidden = false;
    $("#composer").hidden = false;
    $("#screenWrap").hidden = false;
    $("#snapshot").hidden = false;
    $("#msgs").innerHTML = '<div class="more">加载中…</div>';
    $("#screen").textContent = "";
    $("#curSid").textContent = sid.slice(0, 8);
    $("#curSid").title = sid;
    $("#curChips").innerHTML = "";
    location.hash = "s=" + sid;
    closeDrawer();
    renderTree();

    loadLatest();
  }

  function loadLatest() {
    var sid = S.cur.sid;
    // dsh / codex 这类没有 jsonl 的会话：没有历史可翻，直接进实时屏幕
    if (sid.slice(0, 5) === "term:") {
      clearPrompt();
      $("#msgs").innerHTML = '<div class="more">纯终端会话 · 没有历史文件，下方是实时屏幕</div>';
      attach(sid);
      updateComposerHint();
      return;
    }
    req("session.history", { sid: sid, limit: 20 }, sid).then(function (pl) {
      if (!S.cur || S.cur.sid !== sid) return;
      $("#msgs").innerHTML = "";
      S.seen = {};
      (pl.items || []).forEach(function (m) { appendMsg(m, false); });
      S.cur.total = pl.total || 0;
      S.cur.hasMore = !!pl.has_more;
      S.cur.nextBefore = pl.next_before;
      renderMoreHint();
      scrollBottom();
      attach(sid);
      updateComposerHint();
    }).catch(function (e) {
      $("#msgs").innerHTML = '<div class="more">历史加载失败：' + esc((e && e.msg) || "未知错误") + "</div>";
    });
  }

  function attach(sid) {
    req("session.attach", { sid: sid }, sid).then(function (pl) {
      if (!S.cur || S.cur.sid !== sid) return;
      S.cur.tmux = pl.tmux || "";
      S.cur.editable = !!pl.editable;
      S.cur.total = Math.max(S.cur.total, pl.last_seq || 0);
      renderChips();
      updateComposerHint();
      refreshScreen();
      renderPrompt(pl.prompt);      // SBP-0.2：进会话就拿到卡片，不等下一轮 tick
    }).catch(function () {});
  }

  function renderChips() {
    var html = "";
    if (S.cur && S.cur.tmux) {
      html += '<span class="chip tmux">tmux: ' + esc(S.cur.tmux) + "</span>";
    } else {
      html += '<span class="chip warn">不在 tmux，只读</span>';
    }
    if (S.cur && S.cur.total) html += '<span class="chip">' + S.cur.total + " 行</span>";
    $("#curChips").innerHTML = html;
  }

  function updateComposerHint() {
    var input = $("#input");
    if (!S.cur) return;
    if (!S.cur.tmux) {
      input.disabled = true;
      input.placeholder = "该会话不在 tmux 会话里，无法输入";
      $("#send").disabled = true;
      $("#keys").innerHTML = "";
      $("#hint").textContent = "只读模式：历史可翻页；要输入请在 tmux 里跑 claude";
      return;
    }
    input.disabled = false;
    input.placeholder = "输入指令，Enter 发送，Shift+Enter 换行";
    $("#send").disabled = !input.value.trim();
    $("#hint").textContent = "发送到 tmux · " + S.cur.tmux + "（等价于在该窗口敲键盘）";
    renderKeys();
  }

  var KEYS = [
    { k: "ENTER", label: "⏎" }, { k: "ESC", label: "Esc" }, { k: "TAB", label: "Tab" },
    { k: "UP", label: "↑" }, { k: "DOWN", label: "↓" }, { k: "BS", label: "⌫" },
    { k: "1", label: "1" }, { k: "2", label: "2" }, { k: "3", label: "3" },
    { k: "y", label: "y" }, { k: "n", label: "n" },
    { k: "C-C", label: "Ctrl-C", danger: true }
  ];

  function renderKeys() {
    $("#keys").innerHTML = KEYS.map(function (x) {
      return '<button class="key' + (x.danger ? " danger" : "") + '" data-key="' + x.k + '">' + x.label + "</button>";
    }).join("");
    Array.prototype.forEach.call($("#keys").children, function (b) {
      b.onclick = function () { sendKey(b.getAttribute("data-key")); };
    });
  }

  // ------------------------------------------------------------ messages
  function appendMsg(m, live) {
    if (!m || !S.cur) return;
    if (S.seen[m.seq]) return;
    S.seen[m.seq] = 1;
    if (m.seq > S.cur.lastSeq) S.cur.lastSeq = m.seq;

    var box = $("#msgs");
    var hint = box.querySelector(".more");
    var wrap = document.createElement("div");
    wrap.className = "msg " + (m.role === "user" ? "user" : "agent");
    wrap.setAttribute("data-seq", m.seq);

    var meta = hhmm(m.ts) + (m.model ? " · " + m.model : "");
    var clipped = (m.text || "").length > 900 ? " clipped" : "";
    wrap.innerHTML =
      '<div class="msg-meta">' + esc(meta) + "</div>" +
      '<div class="bubble' + (m.kind === "tool" ? " tool" : "") + clipped + '">' + esc(m.text) + "</div>";

    if (clipped) {
      var btn = document.createElement("div");
      btn.className = "expand";
      btn.textContent = "展开";
      btn.onclick = function () {
        var b = wrap.querySelector(".bubble");
        b.classList.remove("clipped");
        btn.remove();
      };
      wrap.appendChild(btn);
    }

    var nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
    box.appendChild(wrap);
    if (live && nearBottom) scrollBottom();
  }

  function renderMoreHint() {
    var box = $("#msgs");
    var old = box.querySelector(".more");
    if (old) old.remove();
    if (!S.cur || !S.cur.hasMore) return;
    var d = document.createElement("div");
    d.className = "more";
    d.textContent = "↑ 上滑加载更早";
    d.onclick = loadOlder;
    box.insertBefore(d, box.firstChild);
    // IntersectionObserver 比 scroll 事件可靠：滚到顶就续页
    if (window.IntersectionObserver) {
      if (S._io) S._io.disconnect();
      S._io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (en.isIntersecting) loadOlder();
        });
      }, { root: $("#stream"), threshold: 0.05 });
      S._io.observe(d);
    }
  }

  function loadOlder() {
    if (!S.cur || !S.cur.hasMore || S.cur.loading) return;
    S.cur.loading = true;
    var sid = S.cur.sid;
    var before = S.cur.nextBefore;
    var box = $("#msgs");
    var scroller = $("#stream");           // 真正滚动的元素是 .stream
    var first = box.querySelector(".more");
    if (first) first.textContent = "加载中…";
    var h0 = scroller.scrollHeight;

    req("session.history", { sid: sid, before: before, limit: 20 }, sid).then(function (pl) {
      if (!S.cur || S.cur.sid !== sid) return;
      var frag = document.createDocumentFragment();
      (pl.items || []).forEach(function (m) {
        if (S.seen[m.seq]) return;
        S.seen[m.seq] = 1;
        var wrap = document.createElement("div");
        wrap.className = "msg " + (m.role === "user" ? "user" : "agent");
        wrap.setAttribute("data-seq", m.seq);
        wrap.innerHTML =
          '<div class="msg-meta">' + esc(hhmm(m.ts) + (m.model ? " · " + m.model : "")) + "</div>" +
          '<div class="bubble' + (m.kind === "tool" ? " tool" : "") + '">' + esc(m.text) + "</div>";
        frag.appendChild(wrap);
      });
      var ref = box.querySelector(".more");
      box.insertBefore(frag, ref ? ref.nextSibling : box.firstChild);
      S.cur.hasMore = !!pl.has_more;
      S.cur.nextBefore = pl.next_before;
      scroller.scrollTop = scroller.scrollHeight - h0;   // 保持视口停在原内容上
      renderMoreHint();
      S.cur.loading = false;
    }).catch(function () {
      S.cur.loading = false;
      var h = box.querySelector(".more");
      if (h) h.textContent = "加载失败，点击重试";
    });
  }

  function scrollBottom() {
    var box = $("#stream");
    box.scrollTop = box.scrollHeight;
  }

  // ------------------------------------------------------------ screen
  // ------------------------------------------- SBP-0.2 交互提示（选项卡片）
  function clearPrompt() {
    var box = $("#prompts");
    box.hidden = true;
    box.innerHTML = "";
    S.prompt = null;
  }

  function renderPrompt(pl) {
    if (!pl || !pl.pid) return;
    if (pl.state !== "open") {                 // answered / gone：只收自己的卡片
      if (S.prompt && S.prompt.pid === pl.pid) clearPrompt();
      return;
    }
    var box = $("#prompts");
    var risk = pl.risk || "safe";
    var h = '<div class="prompt risk-' + risk + '">';
    h += '<div class="p-head">' +
         '<span class="p-title">' + esc(pl.title || "请选择") + "</span>" +
         '<span class="p-tag">' + (risk === "danger" ? "危险操作" : risk === "warn" ? "需要授权" : "") + "</span>" +
         '<button class="p-x" data-act="dismiss" title="忽略（不作答）">✕</button>' +
         "</div>";
    if (pl.detail) h += '<div class="p-detail">' + esc(pl.detail) + "</div>";
    h += '<div class="p-opts">';
    (pl.options || []).forEach(function (o) {
      h += '<button class="p-opt' + (o.risk === "danger" ? " danger" : "") +
           (o.default ? " cur" : "") + '" data-act="choice" data-i="' + o.index +
           '" data-free="' + (o.free_text ? "1" : "0") + '">' +
           '<span class="p-idx">' + o.index + "</span>" +
           '<span class="p-lab">' + esc(o.label) + "</span>" +
           (o.desc ? '<span class="p-desc">' + esc(o.desc) + "</span>" : "") +
           "</button>";
    });
    h += "</div></div>";
    box.innerHTML = h;
    box.hidden = false;
    S.prompt = pl;
  }

  function answer(payload) {
    if (!S.cur) return;
    req("session.answer", payload, S.cur.sid).then(function () {
      clearPrompt();
    }).catch(function (err) {
      // 过期卡片必须收掉，否则留一个按下去没反应的按钮
      if (err && err.code === "E_PROMPT_STALE") { clearPrompt(); toast("该选项已过期"); }
      else toast((err && err.msg) || "作答失败");
    });
  }

  function onPromptClick(e) {
    var b = e.target.closest("[data-act]");
    if (!b || !S.prompt) return;
    var pid = S.prompt.pid;
    if (b.getAttribute("data-act") === "dismiss") { answer({ pid: pid, dismiss: true }); return; }
    var i = parseInt(b.getAttribute("data-i"), 10);
    if (b.getAttribute("data-free") === "1") {
      var t = window.prompt("选了这项之后，agent 会等一段说明：", "");
      if (t === null) return;
      answer({ pid: pid, choice: i, text: t });
    } else {
      answer({ pid: pid, choice: i });
    }
  }

  function renderScreen(lines) {
    S.screen = lines;
    var pre = $("#screen");
    var stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
    pre.textContent = lines.join("\n");
    if (stick) pre.scrollTop = pre.scrollHeight;
    if (lines.length && !S._screenOpened) {
      S._screenOpened = true;
      $("#screenWrap").classList.remove("closed");
    }
  }

  function setScreenState(status) {
    var el = $("#screenState");
    var map = { running: ["运行中", "run"], idle: ["空闲", ""], waiting_input: ["等待确认", "warn"] };
    var v = map[status] || ["—", ""];
    el.textContent = v[0];
    el.className = "chip " + v[1];
  }

  function refreshScreen() {
    if (!S.cur || !S.cur.tmux) return;
    req("pane.snapshot", { sid: S.cur.sid, lines: 120 }, S.cur.sid)
      .then(function (pl) {
        if (!pl) return;                        // 304：屏幕没变，什么都不用画
        if (pl.lines) renderScreen(pl.lines);
        if (pl.status) setScreenState(pl.status);
        renderPrompt(pl.prompt);
      })
      .catch(function () {});
  }

  // ------------------------------------------------------------ input
  function sendText() {
    if (!S.cur || !S.cur.tmux) return;
    var input = $("#input");
    var text = input.value;
    if (!text.trim()) return;
    input.value = "";
    autosize(input);
    $("#send").disabled = true;
    req("session.input", { sid: S.cur.sid, kind: "text", text: text, submit: true }, S.cur.sid)
      .then(function () { setTimeout(refreshScreen, 350); })
      .catch(function (e) { toast((e && e.msg) || "发送失败"); });
  }

  function sendKey(k) {
    if (!S.cur || !S.cur.tmux) return;
    req("session.input", { sid: S.cur.sid, kind: "key", key: k }, S.cur.sid)
      .then(function () { setTimeout(refreshScreen, 300); })
      .catch(function (e) { toast((e && e.msg) || "按键失败"); });
  }

  function autosize(t) {
    t.style.height = "auto";
    t.style.height = Math.min(120, t.scrollHeight) + "px";
  }

  // ------------------------------------------------------------ drawer
  function openDrawer() {
    $("#sidebar").classList.add("open");
    $("#scrim").classList.add("show");
  }
  function closeDrawer() {
    $("#sidebar").classList.remove("open");
    $("#scrim").classList.remove("show");
  }

  // ------------------------------------------------------------ boot
  function start() {
    $("#login").hidden = true;
    $("#app").hidden = false;
    initTransport();
    transportChip();
    connect();

    $("#refresh").onclick = function () { loadIndex(); toast("已刷新"); };
    $("#menu").onclick = function () {
      $("#sidebar").classList.contains("open") ? closeDrawer() : openDrawer();
    };
    $("#scrim").onclick = closeDrawer;
    $("#snapshot").onclick = refreshScreen;

    $("#screenHead").onclick = function () {
      $("#screenWrap").classList.toggle("closed");
    };

    $("#prompts").addEventListener("click", onPromptClick);
    // 焦点不在输入框时，直接按数字键作答
    document.addEventListener("keydown", function (e) {
      if (!S.prompt || e.metaKey || e.ctrlKey || e.altKey) return;
      var t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
      if (e.key < "1" || e.key > "9") return;
      var hit = (S.prompt.options || []).filter(function (o) { return String(o.index) === e.key; })[0];
      if (hit) { answer({ pid: S.prompt.pid, choice: hit.index }); e.preventDefault(); }
    });

    $("#curSid").onclick = function () {
      if (!S.cur) return;
      navigator.clipboard && navigator.clipboard.writeText(S.cur.sid);
      toast("已复制 session id");
    };

    var input = $("#input");
    input.addEventListener("input", function () {
      autosize(input);
      $("#send").disabled = !input.value.trim() || !S.cur || !S.cur.tmux;
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        sendText();
      }
    });
    $("#send").onclick = sendText;

    $("#stream").addEventListener("scroll", function () {
      if ($("#stream").scrollTop < 48 && S.cur && S.cur.hasMore) loadOlder();
    });

    document.addEventListener("keydown", function (e) {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      if (e.key === "r") loadIndex();
    });

    var hash = new URLSearchParams(location.hash.slice(1));
    if (hash.get("s")) {
      // 刷新后恢复上次会话：等索引回来再打开，避免 chip 缺失
      setTimeout(function () { openSession(hash.get("s")); }, 260);
    }
    window.addEventListener("hashchange", function () {
      var s = new URLSearchParams(location.hash.slice(1)).get("s");
      if (s && (!S.cur || S.cur.sid !== s)) openSession(s);
    });
  }

  boot();
})();
