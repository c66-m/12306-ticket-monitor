/* 12306 抢票小窗（B7 第二步）—— 极简展示：账号 + 进行中任务 + 抢票日志。
   形态对齐 gui.py 的 MiniWindow：只留启停/置顶/回主窗，不放任何配置入口。
   关窗由主进程接管（会还原主窗）。 */
"use strict";

const bridge = window.api;
const $ = (sel) => document.querySelector(sel);

// ---- 与 gui.py MINI_LOG_TAGS 对齐：小窗只收抢票相关事件，不要 [运行]/[配置] 噪声 ----
const MINI_TAGS = ["[有票]", "[抢到]", "[错误]", "[冷却]", "[防重]", "[跳过]", "[会话]", "[状态]"];
const LOG_RE = /^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}),\d+ \[\w+\] (.*)$/;
const MAX_LINES = 400, TRIM_LINES = 60;
const ACTIVE = ["monitoring", "retrying"];

let engineRunning = false, busy = false, lastSig = null;

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------- 外观（继承主窗设置） ----------------
const APPEAR_DEF = { theme: "light", accent: "#2E6BFF", font: "system", glass: 55, fs: 14 };
const FONTS = {
  system: '-apple-system, "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif',
  hei: '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif',
  song: '"SimSun", "Songti SC", "STSong", serif',
  kai: '"KaiTi", "STKaiti", "Kaiti SC", serif',
  mono: '"Cascadia Mono", Consolas, "Microsoft YaHei UI", monospace',
};
function applyAppearance() {
  let a = Object.assign({}, APPEAR_DEF);
  try { a = Object.assign(a, JSON.parse(bridge.appearArgs || "{}")); } catch (e) { /* 用默认 */ }
  if (a.theme === "sys") a.theme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  const rt = document.documentElement;
  rt.dataset.theme = a.theme;
  rt.dataset.motion = a.motion || "on";
  rt.style.setProperty("--fs", ((Number(a.fs) || 15) - 1) + "px");  // 小窗整体小一号
  rt.style.setProperty("--font", FONTS[a.font] || FONTS.system);
  rt.style.setProperty("--blur", (Number(a.blur) || 16) + "px");
  if (a.accent) {
    rt.style.setProperty("--accent", a.accent);
    rt.style.setProperty("--accent-grad", "linear-gradient(135deg," + a.accent + "cc," + a.accent + ")");
  }
}

// ---------------- 日志 ----------------
function feedLog(line) {
  const raw = String(line || "");
  const m = LOG_RE.exec(raw);
  const ts = m ? m[1] : new Date().toTimeString().slice(0, 8);
  const msg = m ? m[2] : raw;
  if (!MINI_TAGS.some((t) => msg.indexOf(t) >= 0)) return;
  const cls = msg.indexOf("[抢到]") >= 0 ? "t-bought"
    : msg.indexOf("[有票]") >= 0 ? "t-hit"
    : (msg.indexOf("[错误]") >= 0) ? "t-err" : "";
  const box = $("#mLogs");
  const div = document.createElement("div");
  div.className = "mlog " + cls;
  div.innerHTML = '<span class="ts">' + esc(ts) + '</span><span class="tx">' + esc(msg) + "</span>";
  box.appendChild(div);
  while (box.children.length > MAX_LINES) {
    for (let i = 0; i < TRIM_LINES && box.firstChild; i++) box.firstChild.remove();
  }
  box.scrollTop = box.scrollHeight;
}

// ---------------- 任务 / 引擎状态 ----------------
function renderTasks(tasks) {
  const done = tasks.filter((t) => t.status === "success");
  const live = tasks.filter((t) => ACTIVE.indexOf(t.status) >= 0);
  const rows = done.concat(live);   // 抢到票是最终结果，置顶，不能从小窗里错过
  const sig = JSON.stringify(rows.map((t) => [t.name, t.status, t.message || ""]));
  if (sig === lastSig) return;
  lastSig = sig;
  const box = $("#mTasks");
  if (!rows.length) { box.innerHTML = '<div class="muted">暂无正在进行的任务</div>'; return; }
  box.innerHTML = rows.map((t) =>
    '<div class="mtask">' +
    '<span class="st st-' + esc(t.status) + '">' + esc(t.status_label || t.status) + "</span>" +
    '<span class="nm">' + esc(t.name || "") + "</span>" +
    '<span class="ms" title="' + esc(t.message || "") + '">' + esc(t.message || "") + "</span>" +
    "</div>").join("");
}

function setEngine(on, count) {
  engineRunning = !!on;
  const el = $("#mEngine");
  el.classList.toggle("on", engineRunning);
  $("#mEngineText").textContent = engineRunning ? ("监控中 · " + count + " 个任务") : "监控未启动";
  $("#mToggle").textContent = engineRunning ? "停止监控" : "启动监控";
  $("#mToggle").className = "btn btn-mini" + (engineRunning ? "" : " btn-call");
}

async function refresh() {
  if (busy) return;
  busy = true;
  try {
    const r = await bridge.call("listMonitorTasks");
    const tasks = (r && r.tasks) || [];
    setEngine(r && r.engineRunning, tasks.filter((t) => ACTIVE.indexOf(t.status) >= 0).length);
    renderTasks(tasks);
  } catch (e) {
    $("#mEngineText").textContent = "后端未就绪";
  } finally { busy = false; }
  try {
    const a = await bridge.call("getAccount");
    const chip = $("#mAcc");
    chip.textContent = a && a.logged_in ? ("已登录 " + (a.name || "")) : "未登录 · 点击回主窗";
    chip.className = "chip" + (a && a.logged_in ? " on" : "");
  } catch (e) { /* ignore */ }
}

// ---------------- 交互 ----------------
$("#mToggle").onclick = async () => {
  const wanted = !engineRunning;
  try {
    const r = await bridge.call(wanted ? "startEngine" : "stopEngine");
    const ok = !!(r && r.ok);
    if (!wanted && !ok) feedLog("[状态] 停止请求已发出，仍有任务在收尾");
    refresh();
  } catch (e) { feedLog("[错误] " + String(e && e.message || e)); }
};
$("#mEngine").onclick = () => bridge.miniWin("close");   // 引擎状态 → 回主窗
$("#mAcc").onclick = () => bridge.miniWin("close");
$("#mTop").onclick = () => bridge.miniWin("top");
$("#mMin").onclick = () => bridge.miniWin("min");
$("#mBack").onclick = () => bridge.miniWin("close");
$("#mClose").onclick = () => bridge.miniWin("close");

bridge.onMiniPin((on) => $("#mTop").classList.toggle("on", !!on));
bridge.onPush((msg) => {
  const m = msg && msg.method, p = (msg && msg.params) || {};
  if (m === "log") feedLog(p.line || "");
  else if (m === "backendDead") { $("#mEngineText").textContent = "后端已退出"; $("#mEngine").classList.remove("on"); }
  else if (m === "backendRestart") { $("#mEngineText").textContent = "后端重启中"; $("#mEngine").classList.remove("on"); }
  else if (m === "backendRestarted") { $("#mEngineText").textContent = "后端已拉起"; }
  else if (m === "ready") refresh();
  else if (m === "sessionExpired") feedLog("[提醒] 登录已失效，请回主窗重新登录");
});

applyAppearance();
refresh();
setInterval(refresh, 2000);
