/* pages/monitor.js —— 监控任务页（统计条/任务列表/右列车次结果卡/搜索框）。 */
import { S, call, goto, hitSearch, localDate, dateSpan, SEAT_CHOICES, PAGES, registerPage } from "../store.js";
import { $, esc } from "../components/dom.js";
import { toast } from "../components/toast.js";
import { openModal, confirmBox } from "../components/modal.js";
import { showDD } from "../components/dd.js";
import { stationInput, bindStation, resolveStation } from "../components/station.js";
import { refreshPax } from "./pax.js";


// ================= 监控页 =================
// 右列「车次结果」卡：由顶栏搜索框驱动（两段式选站 → 自动查票 → 三态席别）
export function resultCardInner() {
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

export function mountResultCard(root) {
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
export async function runSearch(from, to, q, date) {
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

export function queryTrainOrStation(station) {
  S.pickFrom = station;
  if (S.page !== "monitor") goto("monitor"); else updateMonitorView();
  toast("出发站：" + station.name + " —— 再搜到达站");
}

export function runTaskSearch(q) {
  if (!q) return;
  const p = Object.keys(PAGES).find((k) => PAGES[k].crumb.includes(q) || PAGES[k].title.includes(q));
  if (p) return goto(p);
  S.searchQ = q;
  if (S.page !== "monitor") goto("monitor"); else updateMonitorView();
}

export function statbarHtml() {
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
export function searchBoxHtml() {
  return '<div class="search-wrap" id="searchWrap">' +
    '<span class="si">🔍</span>' +
    '<input id="globalSearch" autocomplete="off" spellcheck="false" placeholder="输入站名（长葛 / changge / zge）→ 自动查余票">' +
    '<span class="sk">↵</span>' +
    '<div class="search-dd hidden" id="searchDD"></div></div>';
}

export function taskRowsHtml() {
  const rows = S.monitor.map((t) => taskRow(t, S.mgr)).join("");
  return rows || '<div class="muted" style="padding:22px;text-align:center">' +
    (S.monitor.length ? "无匹配任务" : "暂无任务，点「＋ 新建任务」") + "</div>";
}

export function mgrBarInner() {
  return '<button class="btn btn-mini btn-ghost" id="mSelAll">全选</button>' +
    '<span class="muted" id="mSelN">已选 ' + S.mgrSel.length + " · 共 " + S.monitor.length + "</span>" +
    '<span class="btns"><button class="btn btn-mini btn-ghost" id="mDelDirty">删除空任务</button>' +
    '<button class="btn btn-mini btn-call" id="mDelSel"' + (S.mgrSel.length ? "" : " disabled") + ">删除所选</button></span>";
}

export function renderMonitor(root) {
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
export let _monitorSig = "";
export function updateMonitorView() {
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
export function invalidateMonitorView() { _monitorSig = ""; }

// 引擎启停：按后端真实返回值提示（stopEngine 返回 false = 仍在收尾）
export async function toggleEngine() {
  const wanted = !S.engineRunning;
  const r = await call(wanted ? "startEngine" : "stopEngine");
  const ok = !!(r && r.ok);
  S.engineRunning = wanted && ok;
  if (wanted) toast(ok ? "引擎已启动" : "引擎启动失败", ok ? "" : "err");
  else toast(ok ? "引擎已停止" : "停止请求已发出，仍有任务在收尾", ok ? "" : "err");
  refreshMonitor();
}

export function bindStatbar() {
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

export function bindMgr() {
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

export function toggleMgrSel(name) {
  const i = S.mgrSel.indexOf(name);
  if (i >= 0) S.mgrSel.splice(i, 1); else S.mgrSel.push(name);
  const el = document.querySelector('.task[data-name="' + CSS.escape(name) + '"]');
  if (el) { const c = el.querySelector("input"); if (c) c.checked = S.mgrSel.includes(name); }
  const n = $("#mSelN");
  if (n) n.textContent = "已选 " + S.mgrSel.length + " · 共 " + S.monitor.length;
  const ds = $("#mDelSel");
  if (ds) ds.disabled = !S.mgrSel.length;
}

export function bindTaskRows(root) {
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

export async function setTask(name, status, message) {
  const r = await call("setTaskStatus", { name, status, message });
  if (r && r.error) return toast(r.error, "err");
  toast(status === "paused" ? "已暂停「" + name + "」" : "已恢复「" + name + "」");
  refreshMonitor();
}

export function taskRow(t, mgr) {
  if (!hitSearch(t.name, t.from, t.to, t.trains.join(","), t.status_label)) return "";
  const st = t.status;
  const cls = st === "success" ? "hit" : st === "failed" ? "fail" : st === "cancelled" ? "canc" :
    st === "ambiguous" ? "warn" :
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
export async function openMonitorTaskForm(edit) {
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
        // 防重已下线：不再有"旧防重记录挡住余票"的问题，无需额外清理。
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
export function checkedSeats(modal) { return Array.from(modal.querySelectorAll("#fSeats input:checked")).map((x) => x.value); }
export function checkedPax(modal) { return Array.from(modal.querySelectorAll("#fPax input:checked")).map((x) => x.value); }

export function trainRowsHtml(trains, opts) {
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

export function bindTrainButtons(root) {
  root.querySelectorAll("[data-mon]").forEach((b) => b.onclick = () => {
    const r = S.trainRes || {};
    const f = r.from || { name: "", code: "" }, t = r.to || { name: "", code: "" };
    openMonitorTaskForm({ trains: [b.dataset.mon], from: f.name, to: t.name, fromCode: f.code, toCode: t.code, dates: r.date ? [r.date] : [] });
  });
}

export function renderTrains(el, trains, opts, ctx) {
  if (!trains.length) { el.innerHTML = '<div class="muted" style="padding:8px 2px">无车次</div>'; return; }
  el.innerHTML = trainRowsHtml(trains, opts);
  if (opts && !opts.compact) {
    el.querySelectorAll("[data-mon]").forEach((b) => b.onclick = () => {
      openMonitorTaskForm({ trains: [b.dataset.mon], from: ctx.from.name, to: ctx.to.name, fromCode: ctx.from.code, toCode: ctx.to.code, dates: [ctx.date] });
    });
  }
}

// ================= 数据刷新 =================
export async function refreshMonitor(opts) {
  if (S.monitorBusy) return;
  S.monitorBusy = true;
  try {
    const r = await call("listMonitorTasks", undefined, opts);
    S.monitor = r.tasks || [];
    S.engineRunning = !!r.engineRunning;
    $("#badge-monitor").textContent = S.monitor.filter((t) => ["monitoring", "retrying"].includes(t.status)).length;
    $("#monitorToggle").classList.toggle("off", !S.engineRunning);
    $("#mtText").textContent = S.engineRunning ? "监控运行中" : "监控未启动";
    // 正在输入（含站名联想）时不整页重建，否则会清空表单
    const ae = document.activeElement;
    const typing = ae && ae.tagName === "INPUT" && !/^(min|max|close|button|submit)$/i.test(ae.type || "");
    if (S.page === "monitor" && !typing) updateMonitorView(); // 局部刷新，保留搜索框与查票结果
  } catch (e) { /* call 已提示；定时轮询传入 quiet 时不提示、静默等下一轮 */ }
  S.monitorBusy = false;
}

// ================= 全局交互 =================
// 顶栏搜索框：站名两段式（先出发后到达）/ 页签名跳转 / 其它当任务过滤
export function bindSearch() {
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

registerPage("monitor", renderMonitor);
