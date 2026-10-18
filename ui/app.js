/* 12306 车票监控 · 前端（Electron renderer） */
"use strict";

const bridge = window.api;
async function call(method, params) {
  try { return await bridge.call(method, params); }
  catch (e) {
    const msg = String(e && e.message || e);
    toast(msg, "err");
    pushLog("[RPC] " + method + " 失败：" + msg, "err");
    throw e;
  }
}

// ================= 全局状态 =================
const S = {
  page: "monitor",
  engineRunning: false,
  monitor: [], monitorBusy: false,
  grab: [], grabBusy: false,
  pax: [], paxStamp: null,
  presets: [], presetBusy: false,
  settings: null, session: null, account: null, settingsStamp: null,
  // 登录失效弹窗的两个防重标记：
  //   _expiredDialogOpen —— 同一时刻只允许一个弹窗（防叠加）
  //   _expiredDismissed  —— 用户点过「稍后」，本次失效期间不再打扰
  //                        （会话恢复后由 refreshSession 复位）
  _expiredDialogOpen: false,
  _expiredDismissed: false,
  history: [],
  queryHistory: [],
  searchQ: "", stationHits: [], searchDD: [], searchLoading: false, trainRes: null, pickFrom: null,
  mgr: false, mgrSel: [], // 任务列表管理模式
  appear: null,
  dd: null, // 右键菜单
};

// ================= 基础组件 =================
const $ = (sel, root) => (root || document).querySelector(sel);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(msg, kind) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (kind === "err" ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 2600);
}

function openModal(html) {
  const modal = $("#modal");
  modal.innerHTML = html;
  $("#overlay").classList.remove("hidden");
  const close = () => $("#overlay").classList.add("hidden");
  $("#overlay").onclick = (e) => { if (e.target.id === "overlay") close(); };
  return { modal, close };
}

function confirmBox(title, text, okLabel) {
  return new Promise((res) => {
    const { modal, close } = openModal(
      '<h3>' + esc(title) + '</h3><p class="muted" style="margin:8px 0 14px">' + esc(text) + '</p>' +
      '<div class="form-actions"><button class="btn btn-ghost" data-a="no">取消</button>' +
      '<button class="btn btn-call" data-a="yes">' + esc(okLabel || "确定") + '</button></div>');
    modal.querySelector('[data-a=no]').onclick = () => { close(); res(false); };
    modal.querySelector('[data-a=yes]').onclick = () => { close(); res(true); };
  });
}

// 右键菜单
function showDD(items, x, y) {
  const dd = $("#dd");
  // data-i 一律用数组下标：items 里的 i 只是调用方的编号，顺序调整后按下标取值才不会错绑
  dd.innerHTML = items.map((it, idx) =>
    '<div class="item' + (it.danger ? " danger" : "") + '" data-i="' + idx + '">' + esc(it.label) + "</div>").join("");
  dd.classList.remove("hidden");
  dd.style.left = Math.min(x, window.innerWidth - 190) + "px";
  dd.style.top = Math.min(y, window.innerHeight - items.length * 36 - 12) + "px";
  S.dd = items;
  const hide = () => { dd.classList.add("hidden"); window.removeEventListener("click", hide); };
  setTimeout(() => window.addEventListener("click", hide), 0);
  dd.onclick = (e) => {
    const it = S.dd[Number(e.target.dataset.i)];
    dd.classList.add("hidden");
    e.stopPropagation(); // 否则冒泡到 window 的 hide 会立刻关掉回调里打开的第二级菜单
    if (it && it.fn) it.fn();
  };
}

// 日志
function pushLog(line, tag) {
  const logs = $("#logs");
  const d = new Date();
  const ts = d.toTimeString().slice(0, 8);
  const tagMap = { ok: "OK", warn: "WARN", err: "ERR", info: "INFO" };
  const t = tagMap[tag] || "INFO";
  const div = document.createElement("div");
  div.className = "log-line t-" + (tag || "info");
  div.innerHTML = '<span class="ts">' + ts + '<span class="tag">' + t + '</span><span>' + esc(line) + '</span>';
  logs.appendChild(div);
  while (logs.children.length > 400) logs.firstChild.remove();
  logs.scrollTop = logs.scrollHeight;
}

// ================= 页签 =================
const PAGES = {
  monitor: { crumb: "监控任务", title: "监控任务" },
  grab: { crumb: "抢票任务", title: "抢票任务" },
  pax: { crumb: "乘车人", title: "乘车人" },
  history: { crumb: "历史记录", title: "历史" },
  settings: { crumb: "设置", title: "设置" },
};

function goto(page) {
  S.page = page;
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.page === page));
  const bs = $("#btnSettings");
  if (bs) bs.classList.toggle("active", page === "settings");
  $("#crumb").textContent = "/ " + PAGES[page].crumb;
  renderPage();
}

function renderPage() {
  const c = $("#content");
  c.innerHTML = "";
  const el = document.createElement("div");
  el.className = "page";
  c.appendChild(el);
  ({ monitor: renderMonitor, grab: renderGrab, pax: renderPax, history: renderHistory, settings: renderSettings })[S.page](el);
}

// 搜索过滤（列表项文本匹配）
function hitSearch(...texts) {
  const q = S.searchQ.trim().toLowerCase();
  if (!q) return true;
  return texts.join(" ").toLowerCase().includes(q);
}

// ================= 监控页 =================
// 右列「车次结果」卡：由顶栏搜索框驱动（两段式选站 → 自动查票 → 三态席别）
function resultCardInner() {
  const r = S.trainRes;
  const pf = S.pickFrom;
  const today = localDate();
  const fromV = r ? r.from.name : (pf ? pf.name : "");
  const toV = r ? r.to.name : "";
  const head = '<div class="sec-head"><h3>车次查询</h3>' +
    (r ? '<span class="more">' + esc(r.from.name) + " → " + esc(r.to.name) + " · " + r.trains.length + " 趟</span>" +
        '<button class="btn btn-mini btn-ghost" id="mQClear">✕ 清空</button>'
      : '<span class="more">' + (pf ? "已选「" + esc(pf.name) + "」，再选到达站" : "选站 → 查票") + "</span>") +
    "</div>";
  // 单行紧凑查询条：站A ⇄ 站B 日期 查票（贴卡片头部，结果区紧随其后）
  const bar = '<div class="trip-bar">' + stationInput("qf", fromV) +
    '<button class="btn btn-mini btn-ghost" id="mQSwap" title="交换出发 / 到达站">⇄</button>' +
    stationInput("qt", toV) +
    '<input type="date" id="mQDate" value="' + esc((r && r.date) || today) + '">' +
    '<button class="btn btn-mini btn-call" id="mQGo">🔍</button></div>';
  if (!r) {
    return head + bar +
      '<div class="empty-hint"><div class="big">🔎</div>' +
      '<p>' + (pf ? '已选出发站「<b>' + esc(pf.name) + '</b>」，填到达站后点「查票」' : "在上面填出发站与到达站（拼音 / 简拼 / 站名）") + "</p>" +
      '<p class="muted">也可用顶部搜索框逐段选站 · 官方免登录接口</p></div>';
  }
  return head + bar +
    '<div class="trains" id="mQResult">' + (r.trains.length ? trainRowsHtml(r.trains, {})
      : '<div class="muted" style="padding:8px 2px">' +
        (r.date === today ? "该日暂无余票车次（当天的车可能都已过点）—— 把日期改成明天再点「查票」" : "该日无车次，换个日期试试") +
        "</div>") + "</div>";
}

// 站点输入框 → {name,code}：优先取下拉已选项，否则现场搜索首个命中
async function resolveStation(root, id) {
  const box = root.querySelector('[data-st="' + id + '"]');
  const val = box && box.querySelector("input").value.trim();
  if (!val) return null;
  const picked = S["_st" + id];
  if (picked && picked.name === val) return picked;
  // T3：searchStation 挂了（后端崩溃/网络抖动）时给可操作的提示，而不是无声失败
  let r;
  try {
    r = await call("searchStation", { q: val.replace(/^[A-Za-z]/, (c) => c.toUpperCase()) });
  } catch (e) {
    toast("站名「" + val + "」搜索失败：后端无响应，请稍后重试", "err");
    return null;
  }
  const hits = (r && r.stations) || [];
  // 精确同名优先（"杭州"→杭州 HZH，而不是杭州东 HGH）；后端已把后缀容错做在评分里
  const hit = hits.find((s) => s.name === val) || hits[0];
  return hit || null;
}

function mountResultCard(root) {
  const r = S.trainRes;
  const pf = S.pickFrom;
  bindStation(root, "qf", r ? r.from.name : (pf ? pf.name : ""), r ? r.from.code : (pf ? pf.code : ""));
  bindStation(root, "qt", r ? r.to.name : "", r ? r.to.code : "");
  const clr = $("#mQClear", root);
  if (clr) clr.onclick = () => { S.trainRes = null; updateMonitorView(); };
  const swap = $("#mQSwap", root);
  if (swap) swap.onclick = () => {
    const ia = root.querySelector('[data-st="qf"] input');
    const ib = root.querySelector('[data-st="qt"] input');
    const tv = ia.value; ia.value = ib.value; ib.value = tv;
    const ta = S._stqf; S._stqf = S._stqt; S._stqt = ta;
    toast("已交换出发 / 到达");
  };
  const go = $("#mQGo", root);
  if (go) go.onclick = async () => {
    const date = $("#mQDate", root).value;
    const from = await resolveStation(root, "qf");
    if (!from) return toast("出发站没匹配上，换个写法试试", "err");
    const to = await resolveStation(root, "qt");
    if (!to) return toast("到达站没匹配上，换个写法试试", "err");
    if (from.code === to.code) return toast("出发站和到达站不能相同", "err");
    runSearch(from, to, "", date);
  };
  bindTrainButtons(root);
}

// 两段式选站 → 自动查票；日期默认今天，可在结果卡改后重查
async function runSearch(from, to, q, date) {
  if (!from || !to) {
    const r = await call("searchStation", { q: (q || "").trim().replace(/^[A-Za-z]/, (c) => c.toUpperCase()) });
    const hit = (r.stations || [])[0];
    if (hit) return queryTrainOrStation(hit);
    return runTaskSearch(q);
  }
  const d = date || localDate();
  const res = await call("queryTickets", { fromCode: from.code, toCode: to.code, date: d, purpose: "ADULT" });
  if (res && res.error) return toast(from.name + " → " + to.name + "：" + res.error, "err");
  S.trainRes = { from, to, date: d, trains: res.trains || [] };
  S.pickFrom = null;
  // 记录查询历史（去重、最多 10 条），失败不影响查票
  call("pushQueryHistory", { from: from.name, to: to.name, date: d }).then((r) => {
    if (r && r.query_history) S.queryHistory = r.query_history;
  }).catch(() => {});
  if (S.page !== "monitor") goto("monitor"); else updateMonitorView();
  toast(from.name + " → " + to.name + "：" + S.trainRes.trains.length + " 趟");
}

function queryTrainOrStation(station) {
  S.pickFrom = station;
  if (S.page !== "monitor") goto("monitor"); else updateMonitorView();
  toast("出发站：" + station.name + " —— 再搜到达站");
}

function runTaskSearch(q) {
  if (!q) return;
  const p = Object.keys(PAGES).find((k) => PAGES[k].crumb.includes(q) || PAGES[k].title.includes(q));
  if (p) return goto(p);
  S.searchQ = q;
  if (S.page !== "monitor") goto("monitor"); else updateMonitorView();
}

// ================= 外观设置 =================
const WALLS = { green: "#E8EBF2", blue: "#E7EEF9", sunset: "#F4ECE6" };
const APPEAR_KEY = "x12306.appear";
const APPEAR_DEF = {
  theme: "light", blur: 16, radius: 18, fs: 15,
  wall: "image", custom: "", accent: "#2E6BFF",
  glass: 55, density: "normal", motion: "on", logh: 8.6, font: "system",
};
const ACCENTS = ["#2E6BFF", "#0EA5A4", "#7C5CFF", "#E2544E", "#C9781E", "#0F766E", "#B91C1C"];
const FONTS = {
  system: '-apple-system, "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif',
  hei: '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif',
  song: '"SimSun", "Songti SC", "STSong", serif',
  kai: '"KaiTi", "STKaiti", "Kaiti SC", serif',
  mono: '"Cascadia Mono", Consolas, "Microsoft YaHei UI", monospace',
};

function hexToRgbStr(h) {
  const m = /^#?(..)(..)(..)$/.exec(h || "");
  if (!m) return null;
  return parseInt(m[1], 16) + "," + parseInt(m[2], 16) + "," + parseInt(m[3], 16);
}
function applyAppearance() {
  const a = S.appear = Object.assign({}, APPEAR_DEF, S.appear || {});
  const rt = document.documentElement;
  rt.dataset.theme = a.theme;
  rt.dataset.density = a.density;
  rt.dataset.motion = a.motion;
  rt.style.setProperty("--blur", a.blur + "px");
  rt.style.setProperty("--radius", a.radius + "px");
  rt.style.setProperty("--fs", a.fs + "px");
  rt.style.setProperty("--log-h", (a.logh || 8.6) + "rem");
  rt.style.setProperty("--font", FONTS[a.font] || FONTS.system);
  // 玻璃透明度：55 → 1.0 系数（白 55% 基准）
  const ga = Math.max(0.15, (Number(a.glass) || 55) / 55);
  rt.style.setProperty("--glass-a", (a.theme === "dark" ? 0.52 : 0.55) * ga);
  const rgb = hexToRgbStr(a.accent);
  if (rgb) {
    rt.style.setProperty("--accent", a.accent);
    rt.style.setProperty("--accent-grad", "linear-gradient(135deg," + a.accent + "cc," + a.accent + ")");
    rt.style.setProperty("--accent-soft", "rgb(" + rgb + ")");
  }
  document.body.dataset.wall = a.wall;
  if (a.wall === "custom" && a.custom) document.body.style.setProperty("--wall-img", "url('" + a.custom + "')");
  else if (a.wall === "flat-sunset") { /* noop */ }
  document.body.style.setProperty("--bg-flat", a.wall === "flat" ? "linear-gradient(140deg,#DCE4F5,#EEF1F8)"
    : a.wall === "flat-sunset" ? "linear-gradient(150deg,#F3E4D6,#EFE9F6)"
    : a.wall === "flat-green" ? "linear-gradient(150deg,#DDEBE2,#EAF3EC)" : "linear-gradient(140deg,#DCE4F5,#EEF1F8)");
  try { localStorage.setItem(APPEAR_KEY, JSON.stringify(a)); } catch (e) { /* 忽略 */ }
}
function loadAppearance() {
  try { S.appear = Object.assign({}, APPEAR_DEF, JSON.parse(localStorage.getItem(APPEAR_KEY) || "{}")); }
  catch (e) { S.appear = Object.assign({}, APPEAR_DEF); }
  if (S.appear.wall === "flat-sunset" || S.appear.wall === "flat-green") document.body.dataset.wall = "flat";
  if (S.appear.theme === "sys") {
    const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    S.appear.theme = dark ? "dark" : "light";
  }
  applyAppearance();
}

function statbarHtml() {
  const ts = S.monitor;
  const nRun = ts.filter((t) => ["monitoring", "retrying"].includes(t.status)).length;
  const nHit = ts.filter((t) => t.status === "success").length;
  const nPause = ts.filter((t) => t.status === "paused").length;
  const dirty = ts.some((t) => !t.from && !t.to);
  return '<div class="s"><b class="run">' + nRun + "</b><span>监控中</span></div><span class='sep'></span>" +
    '<div class="s"><b class="ok">' + nHit + "</b><span>已抢到</span></div><span class='sep'></span>" +
    '<div class="s"><b>' + nPause + "</b><span>已暂停</span></div><span class='sep'></span>" +
    '<div class="s"><b>' + ts.length + "</b><span>全部任务</span></div>" +
    (dirty ? '<span class="sep"></span><div class="s"><b class="warn">!</b><span>存在空任务</span></div>' : "") +
    (S.engineRunning ? '<span class="sep"></span><div class="s"><b class="run">●</b><span>引擎运行中</span></div>' : "") +
    '<div class="actions">' +
    '<button class="btn btn-pill btn-ghost" id="mPauseAll">⏸ 全部暂停</button>' +
    '<button class="btn btn-pill btn-ghost" id="mResumeAll">▶ 全部恢复</button>' +
    '<button class="btn btn-pill ' + (S.engineRunning ? "btn-ghost" : "btn-call") + '" id="mToggle">' + (S.engineRunning ? "■ 停止引擎" : "▶ 启动引擎") + "</button>" +
    '<button class="btn btn-pill btn-call" id="mNew">＋ 新建任务</button>' +
    "</div>";
}

// 监控页任务列表上方的搜索框（从顶栏搬来）：两段式选站查票 / 页签跳转 / 任务过滤
function searchBoxHtml() {
  return '<div class="search-wrap" id="searchWrap">' +
    '<span class="si">🔍</span>' +
    '<input id="globalSearch" autocomplete="off" spellcheck="false" placeholder="输入站名（长葛 / changge / zge）→ 自动查余票">' +
    '<span class="sk">↵</span>' +
    '<div class="search-dd hidden" id="searchDD"></div></div>';
}

function taskRowsHtml() {
  const rows = S.monitor.map((t) => taskRow(t, S.mgr)).join("");
  return rows || '<div class="muted" style="padding:22px;text-align:center">' +
    (S.monitor.length ? "无匹配任务" : "暂无任务，点「＋ 新建任务」") + "</div>";
}

function mgrBarInner() {
  return '<button class="btn btn-mini btn-ghost" id="mSelAll">全选</button>' +
    '<span class="muted" id="mSelN">已选 ' + S.mgrSel.length + " · 共 " + S.monitor.length + "</span>" +
    '<span class="btns"><button class="btn btn-mini btn-ghost" id="mDelDirty">删除空任务</button>' +
    '<button class="btn btn-mini btn-call" id="mDelSel"' + (S.mgrSel.length ? "" : " disabled") + ">删除所选</button></span>";
}

function renderMonitor(root) {
  root.innerHTML =
    '<div class="statbar" id="mStat">' + statbarHtml() + "</div>" +
    '<div class="grid">' +
    '<div class="col"><div class="card glass glass-surface grow">' +
    '<div class="sec-head"><h3>任务列表</h3><span class="mgr-link' + (S.mgr ? " on" : "") + '" id="mMgrBtn">' + (S.mgr ? "✓ 完成" : "管理") + "</span></div>" +
    searchBoxHtml() +
    '<div class="mgr-bar' + (S.mgr ? "" : " hidden") + '" id="mMgrBar">' + (S.mgr ? mgrBarInner() : "") + "</div>" +
    '<div class="tasks" id="mTasks">' + taskRowsHtml() + "</div>" +
    "</div></div>" +
    '<div class="col"><div class="card glass glass-surface grow" id="mResultCard">' + resultCardInner() + "</div></div>" +
    "</div>";
  bindStatbar();
  bindMgr();
  bindTaskRows(root);
  mountResultCard($("#mResultCard", root));
  bindSearch();
  invalidateMonitorView();
}

// 局部刷新：只重建统计条 / 管理条 / 任务列表，保留搜索框与右列查票结果
// 数据签名比对：内容没变就不动 DOM（保住滚动位置与 hover 状态）
let _monitorSig = "";
function updateMonitorView() {
  if (S.page !== "monitor") return;
  const sig = (S.mgr ? "1" : "0") + "|" + S.mgrSel.join(",") + "|" + S.engineRunning + "|" + S.searchQ + "|" +
    JSON.stringify(S.monitor) + "|" + JSON.stringify(S.trainRes) + "|" + JSON.stringify(S.pickFrom);
  if (sig === _monitorSig) return;
  _monitorSig = sig;
  const st = $("#mStat");
  if (st) { st.innerHTML = statbarHtml(); bindStatbar(); }
  const b = $("#mMgrBtn");
  if (b) { b.textContent = S.mgr ? "✓ 完成" : "管理"; b.classList.toggle("on", S.mgr); }
  const bar = $("#mMgrBar");
  if (bar) {
    bar.classList.toggle("hidden", !S.mgr);
    bar.innerHTML = S.mgr ? mgrBarInner() : "";
    bindMgr();
  }
  const tk = $("#mTasks");
  if (tk) { tk.innerHTML = taskRowsHtml(); bindTaskRows(tk.parentElement); }
  const rc = $("#mResultCard");
  if (rc) { rc.innerHTML = resultCardInner(); mountResultCard(rc); }
}
function invalidateMonitorView() { _monitorSig = ""; }

// 引擎启停：按后端真实返回值提示（stopEngine 返回 false = 仍在收尾）
async function toggleEngine() {
  const wanted = !S.engineRunning;
  const r = await call(wanted ? "startEngine" : "stopEngine");
  const ok = !!(r && r.ok);
  S.engineRunning = wanted && ok;
  if (wanted) toast(ok ? "引擎已启动" : "引擎启动失败", ok ? "" : "err");
  else toast(ok ? "引擎已停止" : "停止请求已发出，仍有任务在收尾", ok ? "" : "err");
  refreshMonitor();
}

function bindStatbar() {
  const nw = $("#mNew");
  if (nw) nw.onclick = () => openMonitorTaskForm();
  const pa = $("#mPauseAll");
  if (pa) pa.onclick = async () => {
    const r = await call("pauseAll");
    if (r && r.error) return toast(r.error, "err");
    toast("已全部暂停 " + ((r && r.count) || 0) + " 个任务");
    refreshMonitor();
  };
  const ra = $("#mResumeAll");
  if (ra) ra.onclick = async () => {
    const r = await call("resumeAll");
    if (r && r.error) return toast(r.error, "err");
    toast("已全部恢复 " + ((r && r.count) || 0) + " 个任务");
    refreshMonitor();
  };
  const tg = $("#mToggle");
  if (tg) tg.onclick = toggleEngine;
}

function bindMgr() {
  const b = $("#mMgrBtn");
  if (b) b.onclick = () => { S.mgr = !S.mgr; S.mgrSel = []; updateMonitorView(); };
  const sa = $("#mSelAll");
  if (sa) sa.onclick = () => {
    S.mgrSel = S.mgrSel.length === S.monitor.length ? [] : S.monitor.map((t) => t.name);
    updateMonitorView();
  };
  const dsel = $("#mDelSel");
  if (dsel) dsel.onclick = async () => {
    const names = S.mgrSel.slice();
    if (!names.length) return;
    if (!await confirmBox("删除任务", "确定删除所选 " + names.length + " 个任务？")) return;
    for (const n of names) await call("deleteTask", { name: n });
    S.mgrSel = []; toast("已删除 " + names.length + " 个任务");
    await refreshMonitor(); updateMonitorView();
  };
  const ddirty = $("#mDelDirty");
  if (ddirty) ddirty.onclick = async () => {
    const names = S.monitor.filter((t) => !t.from && !t.to).map((t) => t.name);
    if (!names.length) return toast("没有空任务");
    if (!await confirmBox("删除空任务", "将删除 " + names.length + " 个空任务（无区间配置）？")) return;
    for (const n of names) await call("deleteTask", { name: n });
    S.mgrSel = []; toast("已删除 " + names.length + " 个空任务");
    await refreshMonitor(); updateMonitorView();
  };
}

function toggleMgrSel(name) {
  const i = S.mgrSel.indexOf(name);
  if (i >= 0) S.mgrSel.splice(i, 1); else S.mgrSel.push(name);
  const el = document.querySelector('.task[data-name="' + CSS.escape(name) + '"]');
  if (el) { const c = el.querySelector("input"); if (c) c.checked = S.mgrSel.includes(name); }
  const n = $("#mSelN");
  if (n) n.textContent = "已选 " + S.mgrSel.length + " · 共 " + S.monitor.length;
  const ds = $("#mDelSel");
  if (ds) ds.disabled = !S.mgrSel.length;
}

function bindTaskRows(root) {
  root.querySelectorAll(".task").forEach((el) => {
    const name = el.dataset.name;
    const t = S.monitor.find((x) => x.name === name);
    el.oncontextmenu = (e) => {
      e.preventDefault();
      if (S.mgr || !t) return;
      const items = [];
      if (["monitoring", "retrying"].includes(t.status)) items.push({ i: 0, label: "⏸ 暂停", fn: () => setTask(name, "paused", "手动暂停") });
      else items.push({ i: 0, label: "▶ 恢复", fn: () => setTask(name, "monitoring", "手动恢复") });
      items.push({ i: 1, label: "✕ 取消", fn: () => setTask(name, "cancelled", "手动取消") });
      if (t.status === "paused") items.push({ i: 2, label: "✎ 编辑", fn: () => openMonitorTaskForm(t) });
      items.push({ i: 4, label: "♻ 重置防重", fn: async () => {
        if (!await confirmBox("重置防重", "清除「" + t.name + "」的本地下单防重记录并恢复监控？")) return;
        const r = await call("resetTask", { name });
        if (r && r.error) return toast(r.error, "err");
        toast("已重置防重，任务恢复监控"); refreshMonitor();
      } });
      items.push({ i: 3, label: "🗑 删除", danger: true, fn: async () => {
        // 带 uid：同名任务（编辑 bug 留下的重复）按名字删只会删到第一条
        if (await confirmBox("删除任务", "确定删除「" + t.name + "」？")) { await call("deleteTask", { uid: t.uid, name }); toast("已删除"); refreshMonitor(); }
      } });
      showDD(items, e.clientX, e.clientY);
    };
    el.ondblclick = () => { if (!S.mgr && t) openMonitorTaskForm(t); };
    el.onclick = (e) => {
      // T1：行尾暂停/恢复按钮（管理模式不渲染按钮，此分支只在非管理模式生效）
      if (!S.mgr) {
        const b = e.target.closest(".tk-pause");
        if (b) {
          e.stopPropagation();
          if (b.dataset.act === "pause") setTask(b.dataset.name, "paused", "手动暂停");
          else setTask(b.dataset.name, "monitoring", "手动恢复");
        }
        return;
      }
      if (e.target.tagName === "INPUT") e.preventDefault();
      toggleMgrSel(name);
    };
  });
}

async function setTask(name, status, message) {
  const r = await call("setTaskStatus", { name, status, message });
  if (r && r.error) return toast(r.error, "err");
  toast(status === "paused" ? "已暂停「" + name + "」" : "已恢复「" + name + "」");
  refreshMonitor();
}

function taskRow(t, mgr) {
  if (!hitSearch(t.name, t.from, t.to, t.trains.join(","), t.status_label)) return "";
  const st = t.status;
  const cls = st === "success" ? "hit" : st === "failed" ? "fail" : st === "cancelled" ? "canc" :
    ["monitoring", "retrying"].includes(st) ? "run" : "pause";
  const label = st === "success" ? "已抢到" : t.status_label;
  const route = (t.from && t.to) ? t.from + " → " + t.to : "空任务（无区间）";
  const sub = [t.dates && t.dates.length ? t.dates.join("/") : "", t.trains.join(" ") || "", t.seat_types.join(" ") || ""].filter(Boolean).join(" · ");
  // T1：行尾徽章（● 监控中 / ○ 未监控）+ 暂停/恢复按钮；管理模式不渲染，避免和勾选打架
  const mon = ["monitoring", "retrying"].includes(st);
  const monBadge = mgr ? "" :
    '<span class="mon-badge' + (mon ? " on" : "") + '">' + (mon ? "● 监控中" : "○ 未监控") + "</span>";
  // 「仅提醒」标：auto_order=false 的任务命中余票只记历史+发邮件，不下单
  // （老任务常存成 false，之前界面上完全看不出来 → 用户以为"有票没反应"）
  const ordBadge = (mgr || t.auto_order !== false) ? "" :
    '<span class="no-order" title="未开自动下单：命中余票只记录并邮件提醒，不会下单">仅提醒</span>';
  const pauseBtn = mgr ? "" :
    '<button class="btn btn-mini ' + (mon ? "btn-ghost" : "btn-call") + ' tk-pause" data-act="' + (mon ? "pause" : "resume") + '" data-name="' + esc(t.name) + '" title="' + (mon ? "暂停监控" : "恢复监控") + '">' + (mon ? "⏸ 暂停" : "▶ 恢复") + "</button>";
  return '<div class="task' + (mgr ? " mgr" : "") + (mgr && S.mgrSel.includes(t.name) ? " sel" : "") + '" data-name="' + esc(t.name) + '">' +
    (mgr ? '<input type="checkbox" class="tk-check" tabindex="-1"' + (S.mgrSel.includes(t.name) ? " checked" : "") + ">" : "") +
    '<span class="train-no">' + esc(t.trains[0] || "—") + "</span>" +
    '<div class="route"><div class="line"><b>' + esc(t.name) + '</b><span class="arrow">→</span><span>' + esc(route) + "</span></div>" +
    '<div class="sub">' + (esc(sub) || "（无配置）") + (t.message ? " · " + esc(t.message) : "") + "</div></div>" +
    ordBadge + monBadge + pauseBtn +
    '<span class="status ' + cls + '">' + esc(label) + "</span></div>";
}

// 新建/编辑监控任务弹窗
async function openMonitorTaskForm(edit) {
  if (!S.pax.length) await refreshPax();
  const t = edit || {};
  const isEdit = !!edit;
  // 「自动下单」开关：编辑取任务原值，新建取设置里的任务默认值（与后端 _req_on 同口径）
  const autoOn = isEdit ? t.auto_order !== false
    : (((S.settings || {}).task_defaults || {}).auto_order) !== false;
  const { modal, close } = openModal(
    "<h3>" + (isEdit ? "编辑任务" : "新建监控任务") + "</h3>" +
    '<div class="mt-top"><div class="form-row grow"><label>出发站</label>' + stationInput("from", t.from) + "</div>" +
    '<div class="form-row grow"><label>到达站</label>' + stationInput("to", t.to) + "</div></div>" +
    '<div class="form-row"><label>车次（逗号分隔，可点查询选车）</label>' +
    '<div class="mt-qrow"><input id="fTrains" placeholder="K225, K1969（可留空 = 全部）" value="' + esc((t.trains || []).join(",")) + '">' +
    '<button class="btn btn-call mt-qbtn" id="fQuery" title="按出发/到达站与开始日期查询余票">🔍 查询</button></div>' +
    '<div class="mt-qresult hidden" id="fQResult"></div></div>' +
    '<div class="form-grid">' +
    '<div class="form-row"><label>开始日期</label><input type="date" id="fD1" value="' + esc(t.dates && t.dates[0] || localDate()) + '"></div>' +
    '<div class="form-row"><label>结束日期（跨度 ≤ 5 天）</label><input type="date" id="fD2" value="' + esc(t.dates && t.dates[t.dates.length - 1] || "") + '"></div>' +
    "</div>" +
    '<div class="form-row"><label>席别</label><div class="check-row" id="fSeats">' + SEAT_CHOICES.map((s) =>
      '<label class="check' + ((t.seat_types || []).includes(s) ? " on" : "") + '"><input type="checkbox" value="' + esc(s) + '"' + ((t.seat_types || []).includes(s) ? " checked" : "") + ">" + esc(s) + "</label>").join("") + "</div></div>" +
    '<div class="form-row"><label>首选席别（可选，如 硬座>硬卧）</label><input id="fPri" placeholder="不填 = 按勾选顺序" value="' + esc(t.seat_priority || "") + '"><div class="feedback" id="fPriFb"></div></div>' +
    '<div class="form-row"><label>乘车人</label><div class="check-row" id="fPax">' +
    S.pax.map((p) => '<label class="check' + ((t.passengers || []).includes(p.name) ? " on" : "") + '"><input type="checkbox" value="' + esc(p.name) + '"' + ((t.passengers || []).includes(p.name) ? " checked" : "") + ">" + esc(p.name) + "</label>").join("") +
    (S.pax.length ? "" : '<span class="muted">暂无乘车人</span>') + "</div></div>" +
    '<div class="form-grid">' +
    '<div class="form-row"><label>优先级 (1-10)</label><input type="number" id="fPrio" min="1" max="10" value="' + esc(t.priority || 5) + '"></div>' +
    '<div class="form-row"><label>票种</label><select id="fPurpose"><option value="ADULT"' + ((t.purpose || "ADULT") === "ADULT" ? " selected" : "") + '>成人票</option><option value="0X00"' + ((t.purpose || "") === "0X00" ? " selected" : "") + '>学生票</option></select></div>' +
    "</div>" +
    // 放进 .check-row：选中态由 :636 的通用绑定负责，无需额外事件代码
    '<div class="form-row"><label>自动下单</label><div class="check-row"><label class="check' + (autoOn ? " on" : "") + '" id="fAuto" title="关掉后命中余票只记录并发提醒邮件，不会真的下单"><input type="checkbox"' + (autoOn ? " checked" : "") + ">命中余票就直接下单</label></div></div>" +
    '<div class="form-actions"><button class="btn btn-ghost" id="fCancel">取消</button>' +
    '<button class="btn btn-call" id="fSave">' + (isEdit ? "保存" : "创建") + "</button></div>");

  modal.querySelector("#fCancel").onclick = close;
  bindStation(modal, "from", (t.from || ""), (t.fromCode || ""));
  bindStation(modal, "to", (t.to || ""), (t.toCode || ""));

  // 结束日期自动识别开始日期：改了开始日期，结束日期直接跟随为同一天。
  // 这样旧的区间（无论比新开始日期早还是晚）都会被自动清除，不会再落出
  // 倒置区间（起 > 止）——那是任务被判「监控日期已全部过期」的直接原因。
  // 需要区间就再手动改结束日期；min 同时挡住日期控件里选更早的日期。
  const d1El = modal.querySelector("#fD1");
  const d2El = modal.querySelector("#fD2");
  const syncEndDate = () => {
    const v = d1El.value;
    if (!v) return;
    d2El.min = v;
    d2El.value = v;      // 自动跟随 = 自动清除旧区间，回到单日
  };
  d1El.addEventListener("change", syncEndDate);
  if (d1El.value) d2El.min = d1El.value;

  // 车次查询：按出发/到达站 + 开始日期拉余票，点车次行即填入车次框
  modal.querySelector("#fQuery").onclick = async () => {
    const qRes = modal.querySelector("#fQResult");
    const from = await resolveStation(modal, "from"), to = await resolveStation(modal, "to");
    if (!from || !to) {
      toast("请先填有效的出发站和到达站再查询", "err");
      modal.querySelector('[data-st="' + (!from ? "from" : "to") + '"] input').focus();
      return;
    }
    const d1 = modal.querySelector("#fD1").value || localDate();
    qRes.classList.remove("hidden");
    qRes.innerHTML = '<div class="muted" style="padding:6px 2px">查询中…</div>';
    try {
      const r = await call("queryTickets", { fromCode: from.code, toCode: to.code, date: d1, purpose: "ADULT" });
      if (r && r.error) { qRes.innerHTML = '<div class="muted" style="padding:6px 2px">' + esc(r.error) + "</div>"; return; }
      const trains = r.trains || [];
      if (!trains.length) { qRes.innerHTML = '<div class="muted" style="padding:6px 2px">' + esc(d1) + " 无车次，换日期试试</div>"; return; }
      qRes.innerHTML = trains.map((tr) => {
        const seatChips = tr.seats.slice(0, 6).map((s) =>
          '<span class="seat ' + s.state + '">' + esc(s.name) + (s.count ? " " + esc(s.count) : "") + "</span>").join("");
        // 已选车次渲染黑色边框标记（.sel），与 fTrains 输入框保持一致
        const picked = (modal.querySelector("#fTrains").value || "").split(/[,，\s]+/).filter(Boolean)
          .map((x) => x.toUpperCase()).includes(tr.trainCode);
        return '<div class="train mt-train' + (picked ? " sel" : "") + '" data-code="' + esc(tr.trainCode) + '" title="点击选择/取消车次">' +
          '<div class="top"><span class="no">' + esc(tr.trainCode) + '</span><span class="dur">' + esc(tr.startTime) + " → " + esc(tr.arriveTime) + "</span></div>" +
          '<div class="seats">' + seatChips + "</div></div>";
      }).join("");
      qRes.querySelectorAll(".mt-train").forEach((el) => el.onclick = () => {
        const inp = modal.querySelector("#fTrains");
        const cur = inp.value.split(/[,，\s]+/).filter(Boolean);
        const i = cur.map((x) => x.toUpperCase()).indexOf(el.dataset.code);
        if (i < 0) { cur.push(el.dataset.code); el.classList.add("sel"); toast("已加入车次 " + el.dataset.code); }
        else { cur.splice(i, 1); el.classList.remove("sel"); toast("已移除车次 " + el.dataset.code); }
        inp.value = cur.join(", ");
        refreshPri();
      });
    } catch (e) { qRes.innerHTML = '<div class="muted" style="padding:6px 2px">查询失败：' + esc(e.message) + "</div>"; }
  };

  // 首选席别即时反馈
  const pri = modal.querySelector("#fPri");
  const priFb = modal.querySelector("#fPriFb");
  const refreshPri = async () => {
    const r = await call("seatPriorityFeedback", {
      text: pri.value, checked: checkedSeats(modal),
      trains: modal.querySelector("#fTrains").value.split(/[,，\s]+/).filter(Boolean),
    });
    priFb.textContent = r.text; priFb.className = "feedback" + (r.warn ? " warn" : "");
  };
  pri.addEventListener("input", refreshPri);
  modal.querySelector("#fTrains").addEventListener("input", refreshPri);
  refreshPri();
  modal.querySelectorAll("#fSeats input").forEach((x) => x.addEventListener("change", refreshPri));
  modal.querySelectorAll(".check-row label").forEach((l) => {
    l.addEventListener("change", () => l.classList.toggle("on", l.querySelector("input").checked));
  });

  modal.querySelector("#fSave").onclick = async () => {
    // 允许只打字不点下拉：resolveStation 会现场搜站兜底
    const from = await resolveStation(modal, "from"), to = await resolveStation(modal, "to");
    const d1 = modal.querySelector("#fD1").value, d2 = modal.querySelector("#fD2").value;
    if (!from || !to) { toast("请选择出发站和到达站", "err"); modal.querySelector('[data-st="from"] input').focus(); return; }
    if (!d1) { toast("请选择开始日期", "err"); modal.querySelector("#fD1").focus(); return; }
    // 结束早于开始：dateSpan 会返回空数组，落盘成「空日期任务」，引擎随即判
    // 「监控日期已全部过期」把它标成失败（2026-10-18 用户实测）。ISO 串可直接比。
    if (d2 && d2 < d1) { toast("结束日期不能早于开始日期", "err"); modal.querySelector("#fD2").focus(); return; }
    const dates = dateSpan(d1, d2 || d1);
    if (dates.length > 5) { toast("日期跨度不能超过 5 天", "err"); modal.querySelector("#fD2").focus(); return; }
    const seats = checkedSeats(modal);
    if (!seats.length) { toast("请勾选至少一个席别", "err"); modal.querySelector("#fSeats").scrollIntoView({ block: "center" }); return; }
    const paxNames = checkedPax(modal);
    if (!paxNames.length) { toast("请勾选至少一位乘车人", "err"); modal.querySelector("#fPax").scrollIntoView({ block: "center" }); return; }
    const p = {
      from: from.name, to: to.name, fromCode: from.code, toCode: to.code,
      // date_range 是 [起, 止] 区间契约（appcommon.parse_date_range / engine.expand_dates），
      // 传 "起~止" 字符串会被后端判为形状非法丢弃
      dates, date_range: d2 && d2 !== d1 ? [d1, d2] : [],
      trains: modal.querySelector("#fTrains").value.split(/[,，\s]+/).filter(Boolean).map((x) => x.toUpperCase()),
      seat_types: seats, seat_priority: pri.value.trim(),
      passengers: paxNames, purpose: modal.querySelector("#fPurpose").value,
      priority: modal.querySelector("#fPrio").value,
    };
    // 「自动下单」是本弹窗的显式开关，始终回传勾选值（后端 _req_on 显式传优先）。
    // 其余三个开关仍不出现：编辑时回传任务原值，新建时不传 → 后端取设置里的全局默认
    p.auto_order = !!modal.querySelector("#fAuto input").checked;
    if (isEdit) {
      p.stop_after_order = !!t.stop_after_order;
      p.notify = !!(t.notify || (t.notify_channels || []).length);
      p.start_now = false; // 编辑 ≠ 新建：保存后别顺手把引擎拉起来
    }
    try {
      // 无原地编辑接口：编辑 = 删旧建新。顺序必须是「先建后删」——反过来的话
      // createTask 一旦失败，旧任务已经被删掉，任务就永久消失了。
      const r = await call("createTask", p);
      if (r && r.error) { toast(r.error, "err"); return; }   // 日期为空等校验失败
      // 旧任务必须删：新任务名由「线路+车次+席别」生成，改日期后名字完全一样，
      // 原先靠 t.name !== r.name 判断 → 永远为假 → 旧任务不删，列表里出现两条
      // 同名任务（2026-10-18 用户反馈）。按 uid 删才不会误删新建的那条。
      if (isEdit && (t.uid || t.name)) {
        try { await call("deleteTask", { uid: t.uid || undefined, name: t.name }); }
        catch (e) { toast("旧任务删除失败，请在列表里手动删掉「" + t.name + "」", "err"); }
      }
      if (isEdit) {
        // 「仅提醒」改回「自动下单」：先清旧防重记录，否则命中余票仍会被挡住
        if (t.auto_order === false && p.auto_order) await call("resetTask", { name: r.name });
        // state.json 以任务名为键：同名重建会继承旧记录——旧任务若是 failed /
        // 「日期已全部过期」，新任务一出生就是 failed，点恢复也跑不起来。
        // 删旧任务会清掉该名字的状态，这里按编辑前的运行态显式回设一次。
        const wasActive = ["monitoring", "retrying"].includes(t.status);
        await call("setTaskStatus", {
          name: r.name, status: wasActive ? "monitoring" : "paused",
          message: wasActive ? "编辑任务，恢复监控" : "编辑任务",
        });
      }
      close(); toast(isEdit ? "已保存" : "任务已创建");
      refreshMonitor();
    } catch (e) { /* call 已 toast */ }
  };
}

// 监控任务编辑：api 无独立 save 方法——编辑=删除旧的+createTask（名字可同名覆盖？append_monitor_task 按 name 追加）
// 简化：编辑仅对 paused 任务，先 deleteTask 再 createTask（name 会变，属可接受差异）

// 本地时区 YYYY-MM-DD（toISOString 是 UTC，东八区 0-8 点会取到前一天）
function localDate(d) {
  const x = d || new Date();
  const p = (n) => String(n).padStart(2, "0");
  return x.getFullYear() + "-" + p(x.getMonth() + 1) + "-" + p(x.getDate());
}

function dateSpan(d1, d2) {
  const out = []; const a = new Date(d1), b = new Date(d2);
  if (a > b) return out;
  for (let d = new Date(a); d <= b; d.setDate(d.getDate() + 1))
    out.push(localDate(d));
  return out;
}
function checkedSeats(modal) { return Array.from(modal.querySelectorAll("#fSeats input:checked")).map((x) => x.value); }
function checkedPax(modal) { return Array.from(modal.querySelectorAll("#fPax input:checked")).map((x) => x.value); }

// 车站联想输入框
function stationInput(id, val) {
  return '<div class="station-box" data-st="' + id + '">' +
    '<input autocomplete="off" placeholder="输入拼音 / 简拼 / 站名" value="' + esc(val || "") + '">' +
    '<div class="station-dd hidden"></div></div>';
}
function bindStation(root, id, name, code) {
  const box = root.querySelector('[data-st="' + id + '"]');
  const input = box.querySelector("input");
  const ddEl = box.querySelector(".station-dd");
  S["_st" + id] = name ? { name, code } : null;
  let timer = null;
  const load = async () => {
    const q = input.value.trim();
    if (!q) { ddEl.classList.add("hidden"); return; }
    const r = await call("searchStation", { q });
    // 不过滤与输入同名的项：同名精确命中排第一，用户要能看到并直接选它
    const hits = r.stations || [];
    ddEl.innerHTML = hits.map((s) => '<div class="item" data-n="' + esc(s.name) + '" data-c="' + esc(s.code) + '"><b>' + esc(s.name) + "</b>" +
      (s.kind ? '<span class="kind">' + esc(s.kind) + "</span>" : "") +
      "<i>" + esc(s.code) + "</i></div>").join("");
    ddEl.classList.toggle("hidden", !hits.length);
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    S["_st" + id] = null;              // 手动改字即作废上次下拉选中，避免陈旧站码
    timer = setTimeout(load, 220);
  });
  // 聚焦时若框里已有字，直接给候选：填错了（"杭州市"/"郑州东站"）也能立刻看到正确站
  input.addEventListener("focus", () => { clearTimeout(timer); if (!S["_st" + id]) load(); });
  input.addEventListener("blur", () => setTimeout(() => ddEl.classList.add("hidden"), 180));
  ddEl.addEventListener("mousedown", (e) => {
    const el = e.target.closest(".item"); if (!el) return;
    input.value = el.dataset.n;
    S["_st" + id] = { name: el.dataset.n, code: el.dataset.c };
    ddEl.classList.add("hidden");
  });
}

function trainRowsHtml(trains, opts) {
  opts = opts || {};
  return trains.map((tr) => {
    const seatChips = tr.seats.slice(0, 6).map((s) =>
      '<span class="seat ' + s.state + '">' + esc(s.name) + (s.count ? " " + esc(s.count) : "") + "</span>").join("");
    const more = tr.seats.length > 6 ? '<span class="muted">+' + (tr.seats.length - 6) + "</span>" : "";
    return '<div class="train"><div class="top"><span class="no">' + esc(tr.trainCode) + '</span><span class="dur">' + esc(tr.duration || "") + "</span>" +
      (opts && !opts.compact ? '<button class="btn btn-mini btn-ghost" data-mon="' + esc(tr.trainCode) + '" style="margin-left:8px">＋监控</button>' : "") + "</div>" +
      '<div class="times">' + esc(tr.startTime) + " → " + esc(tr.arriveTime) + ' <span class="arr">' + esc(tr.toName) + "</span></div>" +
      '<div class="seats">' + seatChips + more + "</div></div>";
  }).join("");
}

function bindTrainButtons(root) {
  root.querySelectorAll("[data-mon]").forEach((b) => b.onclick = () => {
    const r = S.trainRes || {};
    const f = r.from || { name: "", code: "" }, t = r.to || { name: "", code: "" };
    openMonitorTaskForm({ trains: [b.dataset.mon], from: f.name, to: t.name, fromCode: f.code, toCode: t.code, dates: r.date ? [r.date] : [] });
  });
}

function renderTrains(el, trains, opts, ctx) {
  if (!trains.length) { el.innerHTML = '<div class="muted" style="padding:8px 2px">无车次</div>'; return; }
  el.innerHTML = trainRowsHtml(trains, opts);
  if (opts && !opts.compact) {
    el.querySelectorAll("[data-mon]").forEach((b) => b.onclick = () => {
      openMonitorTaskForm({ trains: [b.dataset.mon], from: ctx.from.name, to: ctx.to.name, fromCode: ctx.from.code, toCode: ctx.to.code, dates: [ctx.date] });
    });
  }
}

// ================= 抢票页 =================
function renderGrab(root) {
  root.innerHTML =
    '<div class="sec-head"><h3>抢票任务</h3><span style="display:flex;gap:8px">' +
    '<button class="btn btn-mini btn-ghost" id="gPreset">预设 ▾</button>' +
    '<button class="btn btn-mini btn-ghost" id="gHist">🕘 查询历史</button>' +
    '<button class="btn btn-mini btn-ghost" id="gLoc">📍 定位</button>' +
    '<button class="btn btn-mini btn-call" id="gNew">＋ 新任务</button></span></div>' +
    '<div class="grid-3" id="gGrid"></div>';
  root.querySelector("#gNew").onclick = () => openGrabForm();
  root.querySelector("#gPreset").onclick = (e) => {
    const items = S.presets.map((p, i) => ({ i, label: "▸ " + p.name, fn: () => openGrabForm(null, p) }));
    items.push({ i: 98, label: "🗑 删除预设…", danger: true, fn: () => pickPresetToDelete(e) });
    items.push({ i: 99, label: "＋ 存当前为预设", fn: () => { toast("在任务表单里点「存为预设」"); } });
    showDD(items, e.clientX, e.clientY);
  };
  // B6 查询历史：点击回填到监控页并发起查票
  root.querySelector("#gHist").onclick = (e) => {
    const hist = S.queryHistory || [];
    if (!hist.length) return toast("暂无查询历史");
    showDD(hist.map((h, i) => ({
      i, label: h.from + " → " + h.to, fn: () => applyQueryHistory(h),
    })), e.clientX, e.clientY);
  };
  // B1 本机 IP 归属地
  root.querySelector("#gLoc").onclick = async () => {
    const r = await call("ipLocate").catch(() => null);
    const t = (r && r.text) || "";
    const bad = !t || t.indexOf("定位失败") === 0;
    toast(bad ? (t || "定位失败（网络不可用）") : "本机 IP 归属地：" + t, bad ? "err" : "");
    pushLog("[定位] " + (t || "获取失败"), bad ? "warn" : "info");
  };
  renderGrabGrid(root.querySelector("#gGrid"));
}

// 删除预设（B4）：先选预设再确认
function pickPresetToDelete(e) {
  const ps = S.presets || [];
  if (!ps.length) return toast("暂无预设");
  showDD(ps.map((p, i) => ({
    i, label: "🗑 " + p.name, danger: true, fn: async () => {
      if (!await confirmBox("删除预设", "删除预设「" + p.name + "」？")) return;
      const r = await call("deletePreset", { name: p.name });
      if (r && r.error) return toast(r.error, "err");
      toast("预设已删除"); refreshPresets();
    },
  })), e.clientX, e.clientY);
}

// 查询历史回填：解析站名 → 监控页查票
async function applyQueryHistory(h) {
  if (!h) return;
  goto("monitor");
  try {
    const r = await call("searchStation", { q: h.from });
    const fr = (r.stations || [])[0];
    if (!fr) return toast("站名无法解析：" + h.from, "err");
    const r2 = await call("searchStation", { q: h.to });
    const to = (r2.stations || [])[0];
    if (!to) return toast("站名无法解析：" + h.to, "err");
    runSearch({ name: fr.name, code: fr.code }, { name: to.name, code: to.code }, "", h.date);
  } catch (err) { /* call 已提示 */ }
}

// 抢票结果详情（A2 续）：后端 grabResult 返回内存中的完整 result 对象
async function openGrabResult(t) {
  if (!t) return;
  const r = await call("grabResult", { id: t.id });
  const obj = r && r.result ? r.result : null;
  const body = obj
    ? '<pre class="mono" style="white-space:pre-wrap;max-height:50vh;overflow:auto;margin:0">' + esc(JSON.stringify(obj, null, 2)) + "</pre>"
    : '<div class="muted">详情已过期（后端重启或任务已清理）。<br>卡片上仍保留最后结果：' + esc(t.result_msg || "—") + "</div>";
  const { close } = openModal("<h3>抢票结果 · " + esc(t.name || t.id) + "</h3>" + body +
    '<div class="form-actions"><button class="btn btn-call" id="grOk">关闭</button></div>');
  document.querySelector("#grOk").onclick = close;
}

function renderGrabGrid(el) {
  if (!S.grab.length) {
    el.innerHTML = '<div class="muted" style="grid-column:1/-1;padding:24px;text-align:center">暂无抢票任务，点「＋ 新任务」创建</div>';
    return;
  }
  el.innerHTML = S.grab.map((t) => {
    const running = t.running;
    const hasRes = t.result_msg !== undefined && t.result_msg !== null && t.result_msg !== "";
    const st = t.status === "ok" ? "succ" : running ? "run" : "pause";
    const stLabel = t.status === "ok" ? "已抢到" : running ? "抢票中" : "待命";
    return '<div class="card glass glass-surface grab-card">' +
      '<div class="sec-head"><h3>' + esc(t.name || t.id) + '</h3><span class="status ' + st + '">' + stLabel + "</span></div>" +
      '<div class="g-count"' + (running ? ' style="color:var(--ok)"' : "") + ">" + esc(t.countdown || "—") + "</div>" +
      '<div class="muted" style="margin:4px 0">' + esc([t.from, t.to].filter(Boolean).join(" → ") || "未设区间") +
      (t.trains && t.trains.length ? " · " + esc(t.trains.join(",")) : "") + "</div>" +
      '<div class="muted">' + esc((t.seat_types || []).join("/") || "未选席别") + (t.armed ? " · 🔔已武装" : "") + (t.auto_fired ? " · ⚡已自动触发" : "") + "</div>" +
      (hasRes
        ? '<div class="g-result ' + (t.result_ok ? "ok" : "err") + '">' + (t.result_ok ? "✔ " : "✖ ") + esc(t.result_msg) +
          (t.result_at ? '<i class="muted">' + esc(t.result_at) + "</i>" : "") + "</div>"
        : "") +
      '<div class="form-actions" style="margin-top:10px">' +
      (running
        ? '<button class="btn btn-mini btn-ghost" data-g="stop">■ 停止</button>'
        : '<button class="btn btn-mini btn-call" data-g="start">▶ 开抢</button>') +
      '<button class="btn btn-mini btn-ghost" data-g="edit">✎ 编辑</button>' +
      (hasRes ? '<button class="btn btn-mini btn-ghost" data-g="detail">📄 结果详情</button>' : "") +
      '<button class="btn btn-mini btn-ghost" data-g="rename">✎ 改名</button>' +
      '<button class="btn btn-mini btn-ghost" data-g="del" style="color:var(--err)">🗑</button>' +
      "</div></div>";
  }).join("");

  el.querySelectorAll("[data-g]").forEach((b) => {
    b.onclick = async () => {
      const card = b.closest(".grab-card");
      const idx = Array.from(el.children).indexOf(card);
      const t = S.grab[idx]; if (!t) return;
      if (b.dataset.g === "start") { await call("startGrab", { id: t.id }); toast("已开抢"); }
      if (b.dataset.g === "stop") { await call("stopGrab", { id: t.id }); toast("已停止"); }
      if (b.dataset.g === "edit") openGrabForm(t);
      if (b.dataset.g === "rename") openRenameForm(t);
      if (b.dataset.g === "detail") openGrabResult(t);
      if (b.dataset.g === "del" && await confirmBox("删除", "删除抢票任务「" + (t.name || t.id) + "」？")) {
        await call("deleteGrabTask", { id: t.id }); toast("已删除"); refreshGrab();
      }
      if (b.dataset.g === "start" || b.dataset.g === "stop") setTimeout(refreshGrab, 400);
    };
  });
}

// 抢票任务改名（B5）
function openRenameForm(t) {
  if (!t) return;
  const { modal, close } = openModal(
    "<h3>重命名抢票任务</h3>" +
    '<div class="form-row"><label>任务名</label><input id="rName" value="' + esc(t.name || "") + '"></div>' +
    '<div class="form-actions"><button class="btn btn-ghost" id="rCancel">取消</button>' +
    '<button class="btn btn-call" id="rSave">保存</button></div>');
  modal.querySelector("#rCancel").onclick = close;
  const inp = modal.querySelector("#rName");
  inp.focus(); inp.select();
  modal.querySelector("#rSave").onclick = async () => {
    const name = inp.value.trim();
    if (!name) return toast("请填写任务名", "err");
    const r = await call("renameGrabTask", { id: t.id, name });
    if (r && r.error) return toast(r.error, "err");
    close(); toast("已改名"); refreshGrab();
  };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") modal.querySelector("#rSave").click(); });
}

// 抢票任务表单
async function openGrabForm(edit, preset) {
  if (!S.pax.length) await refreshPax();
  const t = Object.assign({}, _DEFAULT_LC, edit || {}, preset ? {
    from: preset.from, to: preset.to, trains: preset.trains || [], seat_types: preset.seat_types || [],
    seat_priority: preset.seat_priority || "",
  } : {});
  // 逐车席别（车次 → Set<席别名>）：删除整排席别勾选后，唯一的席别入口
  const perTrain = {};
  const prioRaw = Array.isArray(t.seat_priority) ? t.seat_priority.join(",") : (t.seat_priority || "");
  prioRaw.split(/[,，、;；]+/).forEach((e) => {
    const i = e.indexOf("=");
    if (i > 0) {
      const code = e.slice(0, i).trim().toUpperCase();
      const set = perTrain[code] || (perTrain[code] = new Set());
      e.slice(i + 1).split(/[/\s]+/).filter(Boolean).forEach((s) => set.add(s));
    }
  });
  // 老任务兼容：无逐车规则但存了全局席别 → 每车按全局席别标记
  if (!Object.keys(perTrain).length && (t.seat_types || []).length && (t.trains || []).length)
    (t.trains || []).forEach((code) => { perTrain[String(code).toUpperCase()] = new Set(t.seat_types); });
  const { modal, close } = openModal(
    "<h3>" + (edit ? "编辑抢票任务" : "新建抢票任务") + "</h3>" +
    '<div class="form-row"><label>任务名</label><input id="gName" value="' + esc(t.name || "") + '"></div>' +
    '<div class="mt-top"><div class="form-row grow"><label>出发站</label>' + stationInput("gfrom", t.from) + "</div>" +
    '<div class="form-row grow"><label>到达站</label>' + stationInput("gto", t.to) + "</div></div>" +
    '<div class="form-row"><label>车次与席别（点查询后逐车选择席别）</label>' +
    '<div class="mt-qrow"><input id="gTrains" placeholder="点击下方车次加入，点击席别标签为该车选席别" value="' + esc((t.trains || []).join(",")) + '">' +
    '<button class="btn btn-call mt-qbtn" id="gQuery" title="按出发/到达站与日期查询余票，识别各车次席别">🔍 查询</button></div>' +
    '<div class="mt-qresult hidden" id="gQResult"></div></div>' +
    '<div class="form-grid">' +
    '<div class="form-row"><label>日期</label><input type="date" id="gDate" value="' + esc(t.date || "") + '"></div>' +
    '<div class="form-row"><label>截止日期（可选）</label><input type="date" id="gDateTo" value="' + esc(t.date_to || "") + '"></div>' +
    "</div>" +
    '<div class="form-grid">' +
    '<div class="form-row"><label>开抢时间（可选，留空 = 手动）</label><input type="datetime-local" id="gStart" value="' + esc((t.start_time || "").replace(" ", "T")) + '"></div>' +
    '<div class="form-row"><label>提前提醒（分钟）</label><input type="number" id="gRemind" value="' + esc(t.remind_minutes ?? 10) + '"></div>' +
    "</div>" +
    '<div class="form-grid">' +
    '<div class="form-row"><label>预热窗口（分钟）</label><input type="number" id="gWarm" value="' + esc(t.warm_minutes ?? 10) + '"></div>' +
    '<div class="form-row"><label>轮询间隔（秒）</label><input type="number" id="gPoll" value="' + esc(t.poll_seconds ?? 3) + '"></div>' +
    "</div>" +
    '<div class="form-row"><label>乘车人</label><div id="gPax">' +
    S.pax.map((p) =>
      '<div class="form-row" style="margin-bottom:4px"><div class="check-row"><label class="check' + ((t.passenger_names || []).includes(p.name) ? " on" : "") + '"><input type="checkbox" value="' + esc(p.name) + '"' + ((t.passenger_names || []).includes(p.name) ? " checked" : "") + ">" + esc(p.name) + '</label>' +
      '<select class="gpax-purpose" style="margin-left:8px"><option value="ADULT"' + (((t.pax_purpose || {})[p.name] || "ADULT") === "ADULT" ? " selected" : "") + '>成人票</option><option value="0X00"' + (((t.pax_purpose || {})[p.name] || "") === "0X00" ? " selected" : "") + '>学生票</option></select></div></div>').join("") +
    (S.pax.length ? "" : '<span class="muted">暂无乘车人</span>') + "</div></div>" +
    '<div class="form-actions"><button class="btn btn-ghost" id="gCancel">取消</button>' +
    (edit ? "" : '<button class="btn btn-ghost" id="gPresetSave">存为预设</button>') +
    '<button class="btn btn-call" id="gSave">' + (edit ? "保存" : "创建") + "</button></div>");

  modal.querySelector("#gCancel").onclick = close;
  bindStation(modal, "gfrom", t.from || "", t.fromCode || "");
  bindStation(modal, "gto", t.to || "", t.toCode || "");

  // 车次查询：识别每趟车的席别（哪些有硬/软卧、一/二等座、硬座/无座），逐车点选席别
  modal.querySelector("#gQuery").onclick = async () => {
    const qRes = modal.querySelector("#gQResult");
    const from = await resolveStation(modal, "gfrom"), to = await resolveStation(modal, "gto");
    if (!from || !to) {
      toast("请先填有效的出发站和到达站再查询", "err");
      modal.querySelector('[data-st="' + (!from ? "gfrom" : "gto") + '"] input').focus();
      return;
    }
    const d1 = modal.querySelector("#gDate").value || localDate();
    qRes.classList.remove("hidden");
    qRes.innerHTML = '<div class="muted" style="padding:6px 2px">查询中…</div>';
    try {
      const r = await call("queryTickets", { fromCode: from.code, toCode: to.code, date: d1, purpose: "ADULT" });
      if (r && r.error) { qRes.innerHTML = '<div class="muted" style="padding:6px 2px">' + esc(r.error) + "</div>"; return; }
      const trains = r.trains || [];
      if (!trains.length) { qRes.innerHTML = '<div class="muted" style="padding:6px 2px">' + esc(d1) + " 无车次，换日期试试</div>"; return; }
      const pickedCodes = (modal.querySelector("#gTrains").value || "").split(/[,，\s]+/).filter(Boolean).map((x) => x.toUpperCase());
      qRes.innerHTML = trains.map((tr) => {
        const sel = perTrain[tr.trainCode] || new Set();
        // 全部席别都渲染（不截断）：识别功能要看全；sold 灰显不可选
        const seatChips = tr.seats.map((s) =>
          '<span class="seat ' + s.state + (s.state !== "sold" ? " pick" : "") + (sel.has(s.name) ? " on" : "") + '" data-seat="' + esc(s.name) + '">' + esc(s.name) + (s.count ? " " + esc(s.count) : "") + "</span>").join("");
        return '<div class="train mt-train' + (pickedCodes.includes(tr.trainCode) ? " sel" : "") + '" data-code="' + esc(tr.trainCode) + '" title="点击行选择/取消车次；点击席别标签为该车单独选席别">' +
          '<div class="top"><span class="no">' + esc(tr.trainCode) + '</span><span class="dur">' + esc(tr.startTime) + " → " + esc(tr.arriveTime) + "</span></div>" +
          '<div class="seats">' + seatChips + "</div></div>";
      }).join("");
      qRes.querySelectorAll(".mt-train").forEach((el) => {
        el.onclick = () => {
          const inp = modal.querySelector("#gTrains");
          const cur = inp.value.split(/[,，\s]+/).filter(Boolean);
          const i = cur.map((x) => x.toUpperCase()).indexOf(el.dataset.code);
          if (i < 0) { cur.push(el.dataset.code); el.classList.add("sel"); toast("已加入车次 " + el.dataset.code + "，请点击席别标签"); }
          else { cur.splice(i, 1); el.classList.remove("sel"); delete perTrain[el.dataset.code]; toast("已移除车次 " + el.dataset.code); }
          inp.value = cur.join(", ");
        };
        el.querySelectorAll(".seat.pick").forEach((ch) => ch.onclick = (e) => {
          e.stopPropagation();
          const set = perTrain[el.dataset.code] || (perTrain[el.dataset.code] = new Set());
          const name = ch.dataset.seat;
          if (set.has(name)) set.delete(name); else set.add(name);
          ch.classList.toggle("on", set.has(name));
          if (!set.size) delete perTrain[el.dataset.code];
        });
      });
    } catch (e) { qRes.innerHTML = '<div class="muted" style="padding:6px 2px">查询失败：' + esc(e.message) + "</div>"; }
  };

  modal.querySelectorAll("#gPax label.check").forEach((l) => {
    l.addEventListener("change", () => l.classList.toggle("on", l.querySelector("input").checked));
  });

  modal.querySelector("#gSave").onclick = async () => {
    const from = await resolveStation(modal, "gfrom"), to = await resolveStation(modal, "gto");
    if (!from || !to) return toast("请选择出发站和到达站", "err");
    const date = modal.querySelector("#gDate").value;
    if (!date) return toast("请选择日期", "err");
    const trains = modal.querySelector("#gTrains").value.split(/[,，\s]+/).filter(Boolean).map((x) => x.toUpperCase());
    if (!trains.length) { toast("请点「查询」并选择要抢的车次", "err"); modal.querySelector("#gQuery").scrollIntoView({ block: "center" }); return; }
    for (const code of trains) {
      if (!(perTrain[code] && perTrain[code].size)) {
        toast("请为车次 " + code + " 选择席别（点击查询结果中该车的席别标签）", "err");
        modal.querySelector("#gQResult").scrollIntoView({ block: "center" });
        return;
      }
    }
    // seat_types=并集；各车席别集合相同时只存并集，否则生成「车次=席别」专属规则
    const unionSet = new Set(); trains.forEach((code) => perTrain[code].forEach((s) => unionSet.add(s)));
    const seats = SEAT_CHOICES.filter((s) => unionSet.has(s));
    const perSets = trains.map((code) => [...perTrain[code]].join("/"));
    const seat_priority = perSets.every((x) => x === perSets[0]) ? "" : trains.map((code, i) => code + "=" + perSets[i]).join(",");
    const pax = Array.from(modal.querySelectorAll("#gPax input:checked")).map((x) => x.value);
    if (!pax.length) return toast("请勾选乘车人", "err");
    const paxPurpose = {};
    modal.querySelectorAll("#gPax .form-row").forEach((row) => {
      const cb = row.querySelector("input:checked");
      if (cb) paxPurpose[cb.value] = row.querySelector("select").value;
    });
    const st = modal.querySelector("#gStart").value;
    const task = Object.assign({}, _DEFAULT_LC, {
      id: edit ? edit.id : (crypto && crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "").slice(0, 8) : Date.now().toString(16)),
      name: modal.querySelector("#gName").value.trim() || ("任务 " + (S.grab.length + 1)),
      from: from.name, to: to.name, fromCode: from.code, toCode: to.code,
      date, date_to: modal.querySelector("#gDateTo").value,
      trains,
      seat_types: seats, seat_priority,
      start_time: st ? st.replace("T", " ") + ":00" : "",
      remind_minutes: Number(modal.querySelector("#gRemind").value) || 10,
      warm_minutes: Number(modal.querySelector("#gWarm").value) || 10,
      poll_seconds: Number(modal.querySelector("#gPoll").value) || 3,
      passenger_names: pax, pax_purpose: paxPurpose,
      status: edit ? edit.status : "idle",
    });
    const saved = await call("saveGrabTask", task);
    if (saved && saved.error) return toast("保存失败：" + saved.error, "err");
    close(); toast("已保存"); refreshGrab();
  };
  const savePresetBtn = modal.querySelector("#gPresetSave");
  if (savePresetBtn) savePresetBtn.onclick = async () => {
    const from = await resolveStation(modal, "gfrom"), to = await resolveStation(modal, "gto");
    if (!from || !to) return toast("先选出发/到达站", "err");
    const name = modal.querySelector("#gName").value.trim() || ("预设 " + (S.presets.length + 1));
    const trains = modal.querySelector("#gTrains").value.split(/[,，\s]+/).filter(Boolean).map((x) => x.toUpperCase());
    const pUnion = new Set(); trains.forEach((code) => (perTrain[code] || []).forEach((s) => pUnion.add(s)));
    const saved = await call("savePreset", {
      name, from: from.name, to: to.name,
      trains,
      seat_types: SEAT_CHOICES.filter((s) => pUnion.has(s)),
      seat_priority: trains.map((code) => code + "=" + [...(perTrain[code] || [])].join("/")).join(","),
      date: modal.querySelector("#gDate").value,
    });
    if (saved && saved.error) return toast("保存失败：" + saved.error, "err");
    toast("预设已保存"); refreshPresets();
  };
}

// ================= 乘车人页 =================
function renderPax(root) {
  root.innerHTML =
    '<div class="sec-head"><h3>乘车人</h3><span style="display:flex;gap:8px">' +
    '<button class="btn btn-mini btn-ghost" id="pSync">从 12306 同步</button>' +
    '<button class="btn btn-mini btn-call" id="pNew">＋ 新增</button></span></div>' +
    '<div class="card glass glass-surface" style="flex:1"><div class="pax" id="pList">' + S.pax.map(paxRow).join("") + "</div></div>";

  root.querySelector("#pNew").onclick = () => openPaxForm();
  root.querySelector("#pSync").onclick = async () => {
    const r = await call("sync12306Passengers");
    if (r.error) return toast(r.error, "err");
    toast("已同步 " + (r.passengers || []).length + " 位");
    await call("savePassengers", { passengers: r.passengers, expect_stamp: S.paxStamp });
    refreshPax();
  };
  root.querySelectorAll("#pList .p").forEach((el) => {
    el.ondblclick = () => { const p = S.pax.find((x) => x.name === el.dataset.name); if (p) openPaxForm(p); };
    el.oncontextmenu = (e) => {
      e.preventDefault();
      const p = S.pax.find((x) => x.name === el.dataset.name);
      if (!p) return;
      showDD([
        { i: 0, label: "✎ 编辑", fn: () => { openPaxForm(p); } },
        { i: 1, label: p.is_default ? "已是默认" : "☆ 设为默认", fn: async () => {
          if (p.is_default) return;
          await call("savePassengers", { passengers: S.pax.map((x) => Object.assign({}, x, { is_default: x.name === p.name })), expect_stamp: S.paxStamp });
          refreshPax();
        } },
        { i: 2, label: "🗑 删除", danger: true, fn: async () => {
          if (await confirmBox("删除", "删除乘车人「" + p.name + "」？")) {
            await call("savePassengers", { passengers: S.pax.filter((x) => x.name !== p.name), expect_stamp: S.paxStamp });
            refreshPax();
          }
        } },
      ], e.clientX, e.clientY);
    };
  });
}

function paxRow(p) {
  if (!hitSearch(p.name, p.id_no || "", p.mobile || "")) return "";
  const idt = ID_TYPE_NAMES[p.id_type_code] || p.id_type_code || "";
  return '<div class="p" data-name="' + esc(p.name) + '"><span>👤 <b>' + esc(p.name) + "</b>" +
    (p.is_adult ? "" : ' <span class="muted">(未成年)</span>') + "</span>" +
    (p.is_default ? '<span class="def">默认</span>' : "") +
    '<span class="idno">' + esc(idt + " " + (p.id_no || "")) + "</span></div>";
}

async function openPaxForm(edit) {
  let t = edit ? Object.assign({}, edit) : {};
  // 列表接口返回的是掩码证件号/手机号，编辑时必须取回明文（否则保存会把掩码写进磁盘）
  let detailFailed = false;
  if (edit && edit.name) {
    try {
      const d = await call("passengerDetail", { name: edit.name });
      if (d && d.ok && d.passenger) t = Object.assign({}, edit, d.passenger);
      else detailFailed = true;
    } catch (e) { detailFailed = true; /* call 已提示 */ }
  }
  // 取不回明文时绝不能沿用列表里的掩码：那张掩码被原样保存一次，真证件号就永久没了。
  // 清空敏感字段逼用户重填，比静默写坏安全。
  if (detailFailed) {
    t = Object.assign({}, t, { id_no: "", mobile: "" });
    toast("未能读取原证件号，请重新填写", "err");
  }
  const { modal, close } = openModal(
    "<h3>" + (edit ? "编辑乘车人" : "新增乘车人") + "</h3>" +
    '<div class="form-row"><label>姓名</label><input id="pName" value="' + esc(t.name || "") + '"></div>' +
    '<div class="form-grid">' +
    '<div class="form-row"><label>证件类型</label><select id="pIdType">' +
    Object.entries(ID_TYPE_NAMES).map(([k, v]) => '<option value="' + k + '"' + ((t.id_type_code || "1") === k ? " selected" : "") + ">" + esc(v) + "</option>").join("") +
    "</select></div>" +
    '<div class="form-row"><label>证件号</label><div class="eye-wrap">' +
    '<input id="pIdNo" type="password" value="' + esc(t.id_no || "") + '">' +
    '<button class="eye" type="button" id="pIdEye" title="显示 / 隐藏证件号">👁</button></div></div>' +
    "</div>" +
    '<div class="form-row"><label>手机号</label><input id="pMobile" value="' + esc(t.mobile || "") + '"></div>' +
    '<div class="form-actions"><button class="btn btn-ghost" id="pCancel">取消</button>' +
    '<button class="btn btn-call" id="pSave">保存</button></div>');

  modal.querySelector("#pCancel").onclick = close;
  const eye = modal.querySelector("#pIdEye");
  if (eye) eye.onclick = () => {
    const i = modal.querySelector("#pIdNo");
    i.type = i.type === "password" ? "text" : "password";
    eye.classList.toggle("on", i.type === "text");
  };
  modal.querySelector("#pSave").onclick = async () => {
    const name = modal.querySelector("#pName").value.trim();
    if (!name) return toast("请填写姓名", "err");
    const id_no = modal.querySelector("#pIdNo").value.trim();
    if (!id_no) return toast("请填写证件号", "err");
    const rec = {
      name, id_type_code: modal.querySelector("#pIdType").value,
      id_no, mobile: modal.querySelector("#pMobile").value.trim(),
      is_default: edit ? edit.is_default : S.pax.length === 0,
      is_adult: edit ? edit.is_adult : true,
    };
    const list = edit ? S.pax.map((x) => (x.name === edit.name ? rec : x)) : S.pax.concat([rec]);
    await call("savePassengers", { passengers: list, expect_stamp: S.paxStamp });
    close(); toast("已保存"); refreshPax();
  };
}

// ================= 历史页 =================
function renderHistory(root) {
  const recs = S.history.filter((r) => hitSearch(r.task || "", r.train || "", r.date || "", r.from || "", r.to || "", r.result || "", r.message || ""));
  root.innerHTML =
    '<div class="card glass glass-surface" style="flex:1"><div class="sec-head"><h3>购票历史</h3><span style="display:flex;gap:8px;align-items:center">' +
    '<span class="more">最近 ' + S.history.length + ' 条</span>' +
    '<button class="btn btn-mini btn-ghost" id="hReconcile" title="拿当前登录会话查 12306 官方订单（只读：不下单、不支付、不取消）">核对订单</button></span></div>' +
    '<div class="set" style="overflow-y:auto">' + (recs.map((r) => {
      const cls = r.result === "success" ? "succ"
        : r.result === "confirmed_unpaid" || r.result === "confirmed_paid" ? "succ"
        : r.result === "dup" ? "pause"
        : r.result === "notified" || r.result === "hit_no_order" ? "run"
        : r.result === "ambiguous" || r.result === "seat_unavailable" ? "pause"
        : r.result === "browser_dead" || r.result === "error" ? "fail"
        : "canc";
      return '<div class="s"><span class="k"><b>' + esc(r.train || "—") + "</b> " + esc([r.from, r.to].filter(Boolean).join(" → ") || "") +
        (r.date ? ' <span class="muted" style="font-size:11px;opacity:.72">' + esc(r.date) + "</span>" : "") + "</span>" +
        '<span class="v">' + esc(r.time || "") + "</span>" +
        '<span class="chip status ' + cls + '">' + esc(HISTORY_LABELS[r.result] || r.result) + "</span></div>" +
        (r.message ? '<div class="muted" style="margin:-4px 0 6px 2px;font-size:11px">' + esc(r.message) + "</div>" : "");
    }).join("") || '<div class="muted" style="padding:20px;text-align:center">暂无记录</div>') + "</div></div>";

  const hb = root.querySelector("#hReconcile");
  if (hb) hb.onclick = async () => {
    hb.disabled = true;
    try {
      const r = await call("reconcileHistory", { limit: 50 });
      toast(r.message || "已核对", r.ok ? undefined : "err");
      if (r.changed) await refreshHistory();
    } catch (e) { /* call() 已 toast 并记日志 */ }
    finally { hb.disabled = false; }
  };
}

// ================= 设置页 =================
function renderSettings(root) {
  const s = S.settings || {};
  const email = s.email || {};
  const td = s.task_defaults || { auto_order: true, stop_after_order: true, notify: true, start_now: true };
  const ses = S.session;
  const a = S.appear || {};
  const wallOpts = [["image", "壁纸"], ["flat", "蓝白"], ["flat-sunset", "晚霞"], ["flat-green", "松绿"], ["custom", "自定义"]];
  root.innerHTML =
    '<div class="set-wrap">' +
    '<div class="card glass glass-surface"><div class="sec-head"><h3>外观</h3><span class="more">即时生效 · 本地保存</span></div>' +
    '<div class="appear-row"><label>主题</label><div class="seg" id="aTheme">' +
    ["light|浅色", "dark|深色", "sys|跟随系统"].map((x) => { const p = x.split("|"); return '<button data-v="' + p[0] + '"' + (a.theme === p[0] ? ' class="on"' : "") + ">" + p[1] + "</button>"; }).join("") +
    "</div></div>" +
    '<div class="appear-row"><label>模糊度</label><input type="range" id="aBlur" min="0" max="40" step="1" value="' + (a.blur ?? 16) + '"><span class="val" id="aBlurV">' + (a.blur ?? 16) + "px</span></div>" +
    '<div class="appear-row"><label>圆角</label><input type="range" id="aRadius" min="6" max="28" step="1" value="' + (a.radius ?? 18) + '"><span class="val" id="aRadiusV">' + (a.radius ?? 18) + "px</span></div>" +
    '<div class="appear-row"><label>玻璃透明度</label><input type="range" id="aGlass" min="15" max="100" step="1" value="' + (a.glass ?? 55) + '"><span class="val" id="aGlassV">' + (a.glass ?? 55) + "%</span></div>" +
    '<div class="appear-row"><label>字号</label><input type="range" id="aFs" min="13" max="18" step="1" value="' + (a.fs ?? 15) + '"><span class="val" id="aFsV">' + (a.fs ?? 15) + "px</span></div>" +
    '<div class="appear-row"><label>字体</label><select id="aFont" class="grow">' +
    [["system", "系统默认"], ["hei", "微软雅黑"], ["song", "宋体"], ["kai", "楷体"], ["mono", "等宽"]].map(([v, t]) => '<option value="' + v + '"' + ((a.font || "system") === v ? " selected" : "") + ">" + t + "</option>").join("") +
    "</select></div>" +
    '<div class="appear-row"><label>界面密度</label><div class="seg" id="aDensity">' +
    ["compact|紧凑", "normal|标准", "roomy|宽松"].map((x) => { const p = x.split("|"); return '<button data-v="' + p[0] + '"' + ((a.density || "normal") === p[0] ? ' class="on"' : "") + ">" + p[1] + "</button>"; }).join("") +
    "</div></div>" +
    '<div class="appear-row"><label>动效</label><div class="seg" id="aMotion">' +
    ["on|开启", "off|关闭"].map((x) => { const p = x.split("|"); return '<button data-v="' + p[0] + '"' + ((a.motion || "on") === p[0] ? ' class="on"' : "") + ">" + p[1] + "</button>"; }).join("") +
    "</div></div>" +
    '<div class="appear-row"><label>日志高度</label><input type="range" id="aLogh" min="5" max="22" step="0.2" value="' + (a.logh ?? 8.6) + '"><span class="val" id="aLoghV">' + (a.logh ?? 8.6) + "rem</span></div>" +
    '<div class="appear-row"><label>主色</label><div class="swatches" id="aAccent">' +
    ACCENTS.map((c) => '<span class="swatch' + (a.accent === c ? " on" : "") + '" data-v="' + c + '" style="background:' + c + '"></span>').join("") +
    "</div></div>" +
    '<div class="appear-row"><label>背景</label><div class="seg" id="aWall">' +
    wallOpts.map(([v, t]) => '<button data-v="' + v + '"' + (a.wall === v ? ' class="on"' : "") + ">" + t + "</button>").join("") +
    "</div></div>" +
    '<div class="appear-row" id="aWRow"' + (a.wall === "custom" ? "" : ' style="display:none"') + '><label>图片</label><input class="grow" id="aWallUrl" placeholder="https://... 或本地路径" value="' + esc(a.custom || "") + '"><button class="btn btn-mini btn-ghost" id="aWallPick">选文件</button><input type="file" id="aWallFile" accept="image/*" style="display:none"></div>' +
    '<div class="form-actions"><button class="btn btn-ghost" id="aReset">恢复默认外观</button><button class="btn btn-ghost" id="aWipe">清空本地数据</button></div></div>' +
    '<div class="card glass glass-surface"><div class="sec-head"><h3>邮件通知（SMTP）</h3><span class="status ' + (s.notify_enabled ? "succ" : "canc") + '">' + (s.notify_enabled ? "已启用" : "未启用") + "</span></div>" +
    '<div class="form-row"><label>启用</label><label class="check"><input type="checkbox" id="sEnabled"' + (email.enabled ? " checked" : "") + ">发送订单通知邮件</label></div>" +
    '<div class="form-row"><label>SMTP 服务器</label><input id="sHost" value="' + esc(email.smtp_host || "") + '" placeholder="smtp.qq.com"></div>' +
    '<div class="form-row"><label>端口</label><input type="number" id="sPort" value="' + esc(email.smtp_port || 465) + '"></div>' +
    '<div class="form-row"><label>发件邮箱</label><input id="sUser" value="' + esc(email.username || "") + '"></div>' +
    '<div class="form-row"><label>授权码</label><input type="password" id="sPwd" value="' + esc(email.password || "") + '" placeholder="******"></div>' +
    '<div class="form-row"><label>收件人（逗号分隔）</label><input id="sTo" value="' + esc((email.to || []).join(",")) + '"></div>' +
    '<div class="form-actions"><button class="btn btn-ghost" id="sTest">发送测试邮件</button>' +
    '<button class="btn btn-call" id="sSave">保存设置</button></div></div>' +
    '<div class="card glass glass-surface"><div class="sec-head"><h3>下单与登录</h3></div>' +
    '<div class="form-row"><label>下单方式</label><select id="sMode"><option value="browser"' + (s.order_mode === "browser" ? " selected" : "") + '>浏览器模式（真实 Edge 滑块）</option><option value="http"' + (s.order_mode === "http" ? " selected" : "") + ">HTTP 模式</option></select></div>" +
    '<div class="form-row"><label>监控轮询间隔（秒）</label><input type="number" id="sPoll" min="7" value="' + esc(s.poll_interval_seconds ?? 7) + '"></div>' +
    '<div class="sec-head" style="margin-top:10px"><h3>登录会话</h3></div>' +
    '<div class="s"><span class="k">当前状态</span><span class="status ' + (ses && ses.ok ? "succ" : "canc") + '">' + esc(ses ? (ses.ok ? "已登录" : "未登录") : "检测中…") + "</span></div>" +
    (ses && !ses.ok ? '<div class="muted">' + esc(ses.message || "") + "</div>" : "") +
    '<div class="form-actions"><button class="btn btn-ghost" id="sCheck">检查会话</button>' +
    '<button class="btn btn-call" id="sRelogin">重新登录</button></div></div>' +
    // ---- 任务默认值（原先在新建任务弹窗里的四个开关）----
    '<div class="card glass glass-surface"><div class="sec-head"><h3>任务默认值</h3><span class="more">新建监控任务时自动套用</span></div>' +
    [["auto_order", "自动下单", "有票就直接下单"], ["stop_after_order", "抢到后停止", "成功后不再继续抢"],
     ["notify", "邮件通知", "抢到后发邮件（需先在下面配好 SMTP）"], ["start_now", "立即启动", "创建后马上开始监控"]]
      .map(([k, t, d]) => '<div class="form-row"><label title="' + esc(d) + '">' + t + '</label>' +
        '<label class="check' + (td[k] ? " on" : "") + '" data-td="' + k + '"><input type="checkbox"' + (td[k] ? " checked" : "") + ">" + esc(d) + "</label></div>").join("") +
    "</div>" +
    // ---- 关于（B2 检查更新 + B1 归属地）----
    '<div class="card glass glass-surface"><div class="sec-head"><h3>关于</h3><span class="more" id="abCur">—</span></div>' +
    '<div class="s"><span class="k">本机 IP 归属地</span><span class="v" id="abIp">未获取</span></div>' +
    '<div class="muted" id="abUpd"></div>' +
    '<div class="form-actions"><button class="btn btn-ghost" id="abLoc">获取归属地</button>' +
    '<button class="btn btn-call" id="abUpdBtn">检查更新</button></div></div></div>';

  root.querySelector("#sSave").onclick = async () => {
    const emailNew = {
      enabled: root.querySelector("#sEnabled").checked,
      smtp_host: root.querySelector("#sHost").value.trim(),
      smtp_port: Number(root.querySelector("#sPort").value) || 465,
      username: root.querySelector("#sUser").value.trim(),
      password: root.querySelector("#sPwd").value,
      from: root.querySelector("#sUser").value.trim(),
      to: root.querySelector("#sTo").value.split(/[,，\s]+/).filter(Boolean),
    };
    if (!await saveSettings({
      email: emailNew,
      order_mode: root.querySelector("#sMode").value,
      poll_interval_seconds: Number(root.querySelector("#sPoll").value),
    })) return;
    toast("设置已保存"); refreshSettings();
  };
  root.querySelector("#sTest").onclick = async () => {
    toast("正在发送测试邮件…");
    const r = await call("testEmail");
    toast((r.message || "") + (r.ok ? "" : "（失败）"), r.ok ? "" : "err");
  };
  root.querySelector("#sCheck").onclick = async () => {
    toast("正在校验会话…");
    // force：走真实浏览器复核，绕过 20 秒缓存（用户点这一下就是要确切结论）
    S.session = await call("checkSession", { force: true }).catch(() => null);
    if (!S.session) { toast("校验失败", "err"); return; }
    if (S.session.unknown) { toast("这一轮没验成：" + (S.session.message || "稍后自动重试"), "err"); renderSessionCard(); return; }
    S.account = await call("getAccount").catch(() => null);
    renderSessionCard();
    toast(S.session.ok ? "会话有效" : "未登录：" + (S.session.message || ""), S.session.ok ? "" : "err");
  };
  root.querySelector("#sRelogin").onclick = async () => {
    // 先让后端预检：会话有效时不弹窗（弹了是「窗口一闪」），确认后才强制重登
    let r = await call("relogin").catch(() => null);
    if (!r) return;
    if (r.running) { toast(r.message || "登录窗口已经打开了"); return; }
    if (r.already) {
      const yes = await confirmBox("重新登录", "当前已登录（" + (r.name || "已登录") +
        "），仍要重新拉起登录窗口吗？", "重新登录");
      if (!yes) return;
      r = await call("relogin", { force: true }).catch(() => null);
      if (!r) return;
    }
    toast(r.message || "已拉起浏览器");
    setTimeout(refreshSession, 5000);
  };

  // ---- 外观 ----
  // ---- 关于：版本 / 归属地 / 检查更新（B2）----
  const abCur = root.querySelector("#abCur"), abIp = root.querySelector("#abIp"), abUpd = root.querySelector("#abUpd");
  const doCheck = async (notify) => {
    const r = await call("checkUpdate").catch(() => null);
    if (!r) return;
    abCur.textContent = "v" + (r.current || "?");
    if (r.has_new) abUpd.textContent = "发现新版本 " + r.version + (r.notes ? "：" + r.notes : "");
    else abUpd.textContent = r.configured ? "已是最新版本" : "未配置更新地址（launcher_config.json → update_url）";
    if (notify) toast(r.has_new ? "发现新版本 " + r.version : (r.configured ? "已是最新版本" : "未配置更新地址"));
  };
  root.querySelector("#abUpdBtn").onclick = () => doCheck(true);
  root.querySelector("#abLoc").onclick = async () => {
    abIp.textContent = "获取中…";
    const r = await call("ipLocate").catch(() => null);
    abIp.textContent = (r && r.text) || "获取失败";
    abIp.title = abIp.textContent;
  };
  doCheck(false);

  // ---- 任务默认值：勾一下立刻落盘（不整页重渲染，免得丢焦点）----
  root.querySelectorAll("[data-td]").forEach((el) => {
    el.querySelector("input").addEventListener("change", async (e) => {
      el.classList.toggle("on", e.target.checked);
      const next = {};
      root.querySelectorAll("[data-td]").forEach((x) => { next[x.dataset.td] = x.querySelector("input").checked; });
      S.settings = Object.assign({}, S.settings, { task_defaults: next });
      if (!await saveSettings({ task_defaults: next })) return;
      toast("任务默认值已保存");
    });
  });

  const save = () => { applyAppearance(); renderSettings(root); };
  root.querySelectorAll("#aTheme button").forEach((b) => b.onclick = () => { S.appear.theme = b.dataset.v; save(); });
  const bindRange = (id, key) => {
    const el = root.querySelector("#" + id), lab = root.querySelector("#" + id + "V");
    el.oninput = () => {
      S.appear[key] = Number(el.value); lab.textContent = el.value + "px";
      applyAppearance();
    };
    el.onchange = () => save();
  };
  bindRange("aBlur", "blur"); bindRange("aRadius", "radius"); bindRange("aFs", "fs");
  // 玻璃透明度 / 日志高度：单位与数值不同，单独绑
  const bindRange2 = (id, key, unit) => {
    const el = root.querySelector("#" + id), lab = root.querySelector("#" + id + "V");
    if (!el) return;
    el.oninput = () => { S.appear[key] = Number(el.value); lab.textContent = el.value + unit; applyAppearance(); };
    el.onchange = () => save();
  };
  bindRange2("aGlass", "glass", "%"); bindRange2("aLogh", "logh", "rem");
  const aFont = root.querySelector("#aFont");
  if (aFont) aFont.onchange = () => { S.appear.font = aFont.value; save(); };
  [["aDensity", "density"], ["aMotion", "motion"]].forEach(([bid, key]) => {
    root.querySelectorAll("#" + bid + " button").forEach((b) => b.onclick = () => { S.appear[key] = b.dataset.v; save(); });
  });
  root.querySelectorAll("#aAccent .swatch").forEach((b) => b.onclick = () => { S.appear.accent = b.dataset.v; save(); });
  root.querySelectorAll("#aWall button").forEach((b) => b.onclick = () => {
    S.appear.wall = b.dataset.v; applyAppearance();
    root.querySelector("#aWRow").style.display = b.dataset.v === "custom" ? "" : "none";
    root.querySelectorAll("#aWall button").forEach((x) => x.classList.toggle("on", x === b));
  });
  const wurl = root.querySelector("#aWallUrl");
  if (wurl) wurl.onchange = () => { S.appear.custom = wurl.value.trim(); save(); };
  const wfile = root.querySelector("#aWallFile");
  root.querySelector("#aWallPick").onclick = () => wfile.click();
  wfile.onchange = () => {
    const f = wfile.files && wfile.files[0];
    if (!f) return;
    const fr = new FileReader();
    fr.onload = () => { S.appear.custom = String(fr.result); save(); toast("背景已更新"); };
    fr.readAsDataURL(f);
  };
  root.querySelector("#aReset").onclick = () => { S.appear = Object.assign({}, APPEAR_DEF); save(); toast("外观已恢复默认"); };
  root.querySelector("#aWipe").onclick = async () => {
    if (!await confirmBox("清空本地数据？", "仅清除本机保存的外观偏好与缓存，不影响服务器上的任务与配置。")) return;
    try { localStorage.clear(); } catch (e) { /* 忽略 */ }
    toast("已清空本地数据"); setTimeout(() => location.reload(), 600);
  };
}

// ================= 数据刷新 =================
async function refreshMonitor() {
  if (S.monitorBusy) return;
  S.monitorBusy = true;
  try {
    const r = await call("listMonitorTasks");
    S.monitor = r.tasks || [];
    S.engineRunning = !!r.engineRunning;
    $("#badge-monitor").textContent = S.monitor.filter((t) => ["monitoring", "retrying"].includes(t.status)).length;
    $("#monitorToggle").classList.toggle("off", !S.engineRunning);
    $("#mtText").textContent = S.engineRunning ? "监控运行中" : "监控未启动";
    // 正在输入（含站名联想）时不整页重建，否则会清空表单
    const ae = document.activeElement;
    const typing = ae && ae.tagName === "INPUT" && !/^(min|max|close|button|submit)$/i.test(ae.type || "");
    if (S.page === "monitor" && !typing) updateMonitorView(); // 局部刷新，保留搜索框与查票结果
  } catch (e) { /* call 已提示 */ }
  S.monitorBusy = false;
}

async function refreshGrab() {
  if (S.grabBusy) return;
  S.grabBusy = true;
  try {
    const r = await call("grabTicker");
    S.grab = r.tasks || [];
    $("#badge-grab").textContent = S.grab.filter((t) => t.running).length;
    if (S.page === "grab") renderPage();
  } catch (e) { /* ignore */ }
  S.grabBusy = false;
}

async function refreshPax() {
  const r = await call("listPassengers");
  S.pax = r.passengers || [];
  S.paxStamp = r.stamp;
  if (S.page === "pax") renderPage();
}

async function refreshPresets() {
  if (S.presetBusy) return;
  S.presetBusy = true;
  try {
    const r = await call("listPresets");
    S.presets = r.presets || [];
    S.queryHistory = r.query_history || [];
  } catch (e) { /* ignore */ }
  S.presetBusy = false;
}

async function refreshSettings() {
  try {
    S.settings = await call("getSettings");
    S.settingsStamp = (S.settings && S.settings.stamp) || null;
    if (S.page === "settings") renderPage();
  } catch (e) { /* ignore */ }
}

// 设置页保存的统一收口：带上乐观并发基线，冲突（抢票进程/另一窗口已改）
// 时后端返回 {ok:false} 且不落盘 —— 提示并使本地状态回落到磁盘真实值。
async function saveSettings(params) {
  const r = await call("saveSettings", Object.assign({ expect_stamp: S.settingsStamp }, params));
  if (r && r.ok === false) { toast(r.error || "保存失败", "err"); refreshSettings(); return false; }
  if (r && r.stamp) S.settingsStamp = r.stamp;
  return true;
}

async function refreshSession() {
  try {
    S.session = await call("checkSession");
    S.account = await call("getAccount");
    // unknown：这一轮没验成（浏览器正忙 / 风控 / 网络），不是掉线的证据 ——
    // 保留上一次结论，免得登录成功后界面又显示「未登录」
    if (S.session.unknown) { renderSessionCard(); return; }
    const ok = S.session.ok;
    // 会话恢复：复位「已忽略」与冷却窗口，下次真失效时重新提示
    if (ok) { S._expiredDismissed = false; S._expiredAt = 0; writeExpiredDismissed(false); }
    const name = ok ? ((S.account && S.account.name) || "已登录") : "未登录";
    const sub = ok ? "会话有效 · 点击管理" : "点击此处登录";
    renderSessionCard();
  } catch (e) { /* ignore */ }
}

function renderSessionCard() {
  const ses = S.session || {}, acc = S.account || {}, ok = !!ses.ok;
  const name = ok ? (acc.name || "已登录") : "未登录";
  const sub = ok ? "会话有效 · 点击管理" : "点击此处登录";
  const sc = $("#sessionCard");
  if (sc) {
    sc.querySelector("b").textContent = name;
    const sp = sc.querySelector("span");
    sp.textContent = sub;
    sp.className = ok ? "on" : "off";
  }
  const ch = $("#sessionChip"); // 顶栏头像：与会话卡同款状态
  if (ch) {
    ch.querySelector("b").textContent = name;
    ch.querySelector("i").textContent = sub;
    ch.classList.toggle("on", ok);
  }
}

async function refreshHistory() {
  try { const r = await call("listHistory", { limit: 200 }); S.history = r.records || []; if (S.page === "history") renderPage(); } catch (e) { /* ignore */ }
}

// ================= 常量（与 Python 侧对齐） =================
const SEAT_CHOICES = ["商务座", "特等座", "优选一等座", "一等座", "二等座", "高级软卧", "动卧", "软卧", "硬卧", "软座", "硬座", "无座"];
const ID_TYPE_NAMES = { "1": "二代身份证", "2": "一代身份证", "C": "港澳通行证", "G": "台湾通行证", "B": "护照", "H": "永久居留证", "L": "其他" };
// failed = 本轮未成功、下轮继续重试，不是「下单失败」（真失败时 12306 往往
  // 已经生成了未支付订单，拒绝重复下单的原文是「包含未付款订单」）。
  // 与 appcommon.HISTORY_RESULT_LABEL 保持一致。
  const HISTORY_LABELS = {
    success: "下单成功", dup: "防重跳过", notified: "已通知",
    hit_no_order: "有余票未下单", ambiguous: "结果待核对",
    browser_dead: "浏览器中断", seat_unavailable: "席别不可售",
    failed: "本轮未成功", error: "异常",
    confirmed_unpaid: "已对账·有未付单", confirmed_paid: "已对账·已支付",
  };
const _DEFAULT_LC = {
  version: 1, passenger_names: [], from: "", to: "", fromCode: "", toCode: "",
  trains: [], seat_types: [], seat_priority: "", date: "", date_to: "",
  start_time: "", remind_minutes: 10, warm_minutes: 10, poll_seconds: 3,
  purpose_code: "ADULT", pax_purpose: {}, id: "", name: "", status: "idle",
};

// 会话失效文案：engine「登录会话失效」/orderflow「浏览器会话不可用」/browser_order「已跳转登录门户」
// 「登录会话已?失效」两种写法都要（engine.py:1506「已失效」/ :1222「失效」），
// gui.py:4036 的 tkinter 版用的是同一判据。
const _SESSION_DEAD_RE = /登录会话已?失效|浏览器会话不可用|已跳转登录门户/;

// ================= 推送订阅 =================
bridge.onPush((msg) => {
  const m = msg && msg.method, p = (msg && msg.params) || {};
  if (m === "log") {
    const line = p.line || "";
    pushLog(line, p.tag);
    // 引擎/下单自己发现会话失效（LOG.error 经 stderr→py-push 过来），比 60 秒一轮的
    // 会话看门狗更早。就地弹窗，否则只剩日志区一行小字，用户当成「点了没反应」。
    // 5 分钟冷却：看门狗和引擎会先后报同一件事，不能连着弹两次。
    if (_SESSION_DEAD_RE.test(line) && Date.now() - (S._expiredAt || 0) > 300000) {
      S._expiredAt = Date.now();
      showSessionExpiredDialog(line);
    }
  } else if (m === "backendDead") {
    // 主进程会自动拉起新后端（backendRestart 推送随后到）；这里只提示崩溃
    toast("后端已退出（code " + p.code + "），正在自动重启…", "err");
    pushLog("[系统] 后端已退出（code " + p.code + "），自动重启中", "err");
  } else if (m === "backendRestart") {
    const sc = $("#sessionChip");
    if (sc) { sc.querySelector("b").textContent = "后端重启中"; sc.querySelector("i").textContent = "请稍候"; sc.classList.remove("on"); }
  } else if (m === "backendRestarted") {
    const sc = $("#sessionChip");
    if (sc) { sc.querySelector("b").textContent = "后端已拉起"; sc.querySelector("i").textContent = "等待就绪…"; }
  } else if (m === "ready") {
    pushLog("[系统] 后端就绪（pid " + p.pid + "）", "info");
    refreshAll();
  } else if (m === "session") {
    // 后端在登录流程结束时推的「已登录/未完成」：立刻刷新账号显示，
    // 不用等 60 秒轮的 session 轮询（对话框关掉后界面还是「未登录」就出在这）
    refreshSession();
  } else if (m === "sessionExpired") {
    showSessionExpiredDialog(p.message || "");
  }
});

// 「稍后」记忆的存储层：sessionStorage 可能不可用（隐私模式 / 受限沙箱），
// 失败时静默退化为纯内存标记，不影响主流程。
const EXPIRED_DISMISSED_KEY = "sessionExpiredDismissed";

function readExpiredDismissed() {
  try { return sessionStorage.getItem(EXPIRED_DISMISSED_KEY) === "1"; }
  catch (e) { return false; }
}

function writeExpiredDismissed(on) {
  try {
    if (on) sessionStorage.setItem(EXPIRED_DISMISSED_KEY, "1");
    else sessionStorage.removeItem(EXPIRED_DISMISSED_KEY);
  } catch (e) { /* 存储不可用：内存标记已生效 */ }
}

// 登录失效弹窗（api 会话看门狗推 sessionExpired）
// 两层防重：_expiredDialogOpen 防同时叠加；_expiredDismissed 记住「用户已点稍后」，
// 本次失效期间不再打扰（会话恢复后由 refreshSession 复位，下次真失效仍会提示）。
function showSessionExpiredDialog(message) {
  if (S._expiredDialogOpen || S._expiredDismissed) return;
  // 刷新 / 重载界面后仍记得「点过稍后」：sessionStorage 是窗口级存储，
  // 关掉软件即失效，所以下次启动仍会正常提示一次。
  if (readExpiredDismissed()) { S._expiredDismissed = true; return; }
  S._expiredDialogOpen = true;
  // 统一冷却时间戳：日志（引擎自报）与看门狗（sessionExpired 推送）两个来源共用
  // 同一个 5 分钟窗口。此前只有日志分支设 _expiredAt，sessionExpired 分支不设，
  // 于是两条链路各自判断、先后各弹一次。
  S._expiredAt = Date.now();
  const { modal, close } = openModal(
    "<h3>登录已失效</h3>" +
    '<p class="muted" style="margin:8px 0 14px">' + esc(message || "12306 登录状态已失效") + "</p>" +
    '<p class="muted">不重新登录的话，自动抢票无法继续。</p>' +
    '<div class="form-actions"><button class="btn btn-ghost" data-a="later">稍后</button>' +
    '<button class="btn btn-call" data-a="relogin">立即登录</button></div>');
  // remember=true：用户明确表示「知道了」（点稍后 / 点遮罩关闭）→ 本次失效不再弹。
  const dismiss = (remember) => {
    close();
    S._expiredDialogOpen = false;
    if (remember) { S._expiredDismissed = true; writeExpiredDismissed(true); }
  };
  modal.querySelector('[data-a=later]').onclick = () => dismiss(true);
  // 点遮罩也算「稍后」：openModal 默认的遮罩关闭不会复位标记，这里统一接管，
  // 否则会留下「关掉后标记仍为 true → 以后永远不弹」的隐性 bug。
  $("#overlay").onclick = (e) => { if (e.target.id === "overlay") dismiss(true); };
  modal.querySelector('[data-a=relogin]').onclick = async () => {
    dismiss(false);   // 用户想解决；登录失败后应允许再次提示
    // force：弹窗只在看门狗已确认会话失效时出现，预检注定失败却要先打一次
    // HTTP + 开一次真浏览器复核，白白多拖几十秒
    const r = await call("relogin", { force: true }).catch(() => null);
    if (r && r.running) toast(r.message || "登录窗口已经打开了");
    setTimeout(refreshSession, 3000);
  };
}

// ================= 全局交互 =================
// 顶栏搜索框：站名两段式（先出发后到达）/ 页签名跳转 / 其它当任务过滤
function bindSearch() {
  const inp = $("#globalSearch");
  const dd = $("#searchDD");
  if (!inp || !dd) return;
  if (inp.dataset.bound === "1") return;   // 已绑定的 input 不重复挂监听
  inp.dataset.bound = "1";
  let timer = null, hits = [], sel = -1;

  // .hidden 是 display:none !important，必须走 class 切换
  const close = () => { dd.classList.add("hidden"); dd.innerHTML = ""; hits = []; sel = -1; };
  const open = (items) => {
    hits = items; sel = -1;
    dd.innerHTML = items.map((it, i) =>
      '<div class="it" data-i="' + i + '"><b>' + esc(it.name || it.title) + "</b><i>" + esc(it.hint || "") + "</i></div>").join("");
    dd.classList.toggle("hidden", !items.length);
    dd.querySelectorAll(".it").forEach((el) => el.onmousedown = (e) => { e.preventDefault(); pick(Number(el.dataset.i)); });
  };
  const pick = (i) => {
    const it = hits[i];
    close();
    if (!it) return;
    // 纯导航类动作走整页渲染：blur 避免焦点还在已移除的输入框上
    if (it.kind === "page" || it.kind === "task") inp.blur();
    if (it.kind === "station") return pickStation(it);
    if (it.kind === "cancelfrom") { S.pickFrom = null; updateMonitorView(); return toast("已取消出发站"); }
    if (it.kind === "page") return goto(it.key);
    if (it.kind === "task") { S.searchQ = it.q; if (S.page !== "monitor") goto("monitor"); else updateMonitorView(); toast("按任务过滤：" + it.q); }
  };
  const pickStation = (st) => {
    if (S.pickFrom && S.pickFrom.code !== st.code) {
      const from = S.pickFrom;
      S.pickFrom = null;
      inp.value = "";
      inp.placeholder = "输入站名（长葛 / changge / zge）→ 自动查余票";
      runSearch(from, st, "");
    } else {
      S.pickFrom = st;
      S._stqf = st;                       // 同步到结果卡的出发站输入框
      inp.value = "";
      inp.placeholder = "再搜到达站…（如 确山 / queshan）";
      if (S.page !== "monitor") goto("monitor"); else updateMonitorView();
      inp.focus();
      toast("出发站：" + st.name + " → 再输到达站");
    }
  };

  inp.addEventListener("input", () => {
    const q = inp.value.trim();
    clearTimeout(timer);
    if (!q) { S.searchQ = ""; close(); if (S.page === "monitor") updateMonitorView(); return; }
    timer = setTimeout(async () => {
      const r = await call("searchStation", { q }).catch(() => ({ stations: [] }));
      const items = (r.stations || []).slice(0, 6).map((s) => ({ kind: "station", name: s.name, code: s.code, hint: s.code }));
      Object.keys(PAGES).forEach((k) => {
        if (PAGES[k].crumb.includes(q) || PAGES[k].title.includes(q)) items.push({ kind: "page", title: PAGES[k].crumb, hint: "页面", key: k, name: PAGES[k].crumb });
      });
      const hits2 = S.monitor.filter((t) => hitSearch(q, t.name, t.from, t.to, t.trains.join(","))).slice(0, 4);
      if (!items.length && hits2.length) items.push({ kind: "task", name: q, hint: hits2.length + " 个任务", q });
      if (S.pickFrom && items.length && items[0].kind === "station") items[0].hint = "到达站 · " + items[0].hint;
      else if (S.pickFrom) items.unshift({ kind: "cancelfrom", name: "取消已选出发站", hint: S.pickFrom.name });
      open(items);
    }, 180);
  });

  inp.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { inp.blur(); close(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!hits.length) return;
      e.preventDefault();
      sel = (sel + (e.key === "ArrowDown" ? 1 : -1) + hits.length) % hits.length;
      dd.querySelectorAll(".it").forEach((el, i) => el.classList.toggle("on", i === sel));
      return;
    }
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (sel >= 0) return pick(sel);
    const q = inp.value.trim();
    if (!q) return;
    if (hits.length && hits[0].kind === "station") return pick(0);
    if (S.pickFrom) {
      call("searchStation", { q }).then((r) => {
        const st = (r.stations || [])[0];
        if (st) { const f = S.pickFrom; S.pickFrom = null; runSearch(f, st, ""); }
        else runTaskSearch(q);
      });
      return;
    }
    if (PAGES[q] || Object.keys(PAGES).some((k) => PAGES[k].crumb === q)) return goto(Object.keys(PAGES).find((k) => PAGES[k].crumb === q));
    close();
    runTaskSearch(q);
  });
  inp.addEventListener("blur", () => setTimeout(close, 150));
}

// Ctrl/⌘+F 聚焦搜索框：全局只挂一次（搜索框会被监控页重渲染替换）
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
    const cur = $("#globalSearch");
    if (!cur) return;
    e.preventDefault(); cur.focus(); cur.select();
  }
});

// 头像 = 登录入口（与会话卡同款）：已登录 → 乘车人，未登录 → 拉起登录
async function doLogin() {
  toast("正在检查登录状态…");
  const r = await call("relogin").catch(() => null);
  if (!r) return;
  if (r.running) { toast(r.message || "登录窗口已经打开了"); return; }
  if (r.already) {          // 会话其实还活着：说清楚，别拉窗口让用户看它一闪而过
    toast(r.message || "当前已登录");
    refreshSession();
    return;
  }
  toast(r.message || "已拉起浏览器");
  setTimeout(refreshSession, 5000);
}

function bindGlobal() {
  // 窗口控制
  document.querySelectorAll("[data-win]").forEach((el) => el.onclick = () => bridge.win(el.dataset.win));
  // 窗口置顶（B7）：状态以主进程为准
  const pin = $("#btnPin");
  if (pin) pin.onclick = async () => {
    const on = await bridge.top();
    pin.classList.toggle("on", !!on);
    toast(on ? "窗口已置顶" : "已取消置顶");
  };
  // 小窗模式（B7 第二步）：主窗隐藏，小窗只放抢票结果与日志
  const mb = $("#btnMini");
  if (mb) mb.onclick = () => bridge.mini();
  // 页签
  document.querySelectorAll(".nav-item").forEach((n) => n.onclick = () => goto(n.dataset.page));
  const bs = $("#btnSettings");
  if (bs) bs.onclick = () => goto("settings");
  const chip = $("#sessionChip");
  // 以界面实际显示的状态为准：探测没验成（unknown）时显示仍是上一次结论，
  // 点进乘车人才不会「已登录却拉起登录框」
  if (chip) chip.onclick = () => { if (chip.classList.contains("on")) goto("pax"); else doLogin(); };
  const cl = $("#btnClearLog");
  if (cl) cl.onclick = () => { $("#logs").innerHTML = ""; };
  const mt = $("#monitorToggle");
  if (mt) mt.onclick = toggleEngine;
}

async function refreshAll() {
  await Promise.allSettled([refreshMonitor(), refreshGrab(), refreshPax(), refreshPresets(), refreshSettings(), refreshSession(), refreshHistory()]);
}
// 主进程关掉小窗后会调它对齐数据（主窗隐藏期间轮询被降频）
window.refreshAll = refreshAll;

// ================= 启动 =================
loadAppearance();
bindGlobal();
bindSearch();
renderPage();
refreshAll();
setInterval(() => { if (S.page === "monitor") refreshMonitor(); }, 2000);
setInterval(refreshGrab, 1000);
setInterval(() => { if (S.page === "history") refreshHistory(); }, 30000);
setInterval(refreshSession, 60000);
// B2：启动静默检查更新（未配置 update_url 时后端直接返回，无网络开销）
call("checkUpdate").then((r) => {
  if (r && r.has_new) { toast("发现新版本 " + r.version); pushLog("[更新] 发现新版本 " + r.version, "info"); }
}).catch(() => {});
