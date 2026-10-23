/* 12306 车票监控 · 前端（Electron renderer） */
/* 入口（ES Module）：导入即完成各页注册；本文件只做组装——
   推送订阅、全局交互、refreshAll 聚合与启动引导。实现已拆至
   store.js / components/*.js / pages/*.js（A-1 拆分，零行为变更）。 */
import { bridge, S, call, goto, renderPage } from "./store.js";
import { $ } from "./components/dom.js";
import { toast } from "./components/toast.js";
import { pushLog, pushLogBatch } from "./components/log.js";
import { loadAppearance } from "./components/appearance.js";
import { _SESSION_DEAD_RE, showSessionExpiredDialog, refreshSession } from "./components/session.js";
import { bindSearch, toggleEngine, refreshMonitor } from "./pages/monitor.js";
import { refreshGrab, refreshPresets } from "./pages/grab.js";
import { refreshPax } from "./pages/pax.js";
import { refreshSettings } from "./pages/settings.js";
import { refreshHistory } from "./pages/history.js";
import { refreshProblemLogs } from "./pages/errlog.js";


// ================= 推送订阅 =================
bridge.onPush((msg) => {
  const m = msg && msg.method, p = (msg && msg.params) || {};
  if (m === "log") {
    const items = Array.isArray(p.lines) ? p.lines : [{ line: p.line || "", tag: p.tag }];
    pushLogBatch(items);
    const line = items.map((x) => x.line || "").join("\n");
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

// Ctrl/⌘+F 聚焦搜索框：全局只挂一次（搜索框会被监控页重渲染替换）
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
    const cur = $("#globalSearch");
    if (!cur) return;
    e.preventDefault(); cur.focus(); cur.select();
  }
});

// 头像 = 登录入口（与会话卡同款）：已登录 → 乘车人，未登录 → 拉起登录
export async function doLogin() {
  toast("正在检查登录状态…");
  // relogin 未决期间抑制失效弹窗自动弹出：用户刚点了登录，看门狗推送 /
  // 引擎日志若抢先弹「登录已失效」，两个动作只会互相打架。RPC 一返回就
  // 清除——窗口开起来之后由后端 _LOGIN_GUARD 期间的推送抑制接棒。
  S._reloginPending = true;
  let r;
  try {
    r = await call("relogin").catch(() => null);
  } finally {
    S._reloginPending = false;
  }
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

export function bindGlobal() {
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

export async function refreshAll() {
  await Promise.allSettled([refreshMonitor(), refreshGrab(), refreshPax(), refreshPresets(), refreshSettings(), refreshSession(), refreshHistory(), refreshProblemLogs()]);
}
// 主进程关掉小窗后会调它对齐数据（主窗隐藏期间轮询被降频）
window.refreshAll = refreshAll;

// ================= 启动 =================
loadAppearance();
bindGlobal();
bindSearch();
renderPage();
refreshAll();
// 窗口隐藏（打开迷你小窗/最小化）时停掉后台轮询，回到前台立刻对齐一次：
// 隐藏期间主窗的定时器仍会跑，白白和后端争 RPC 队列（2026-10-19 审查）。
setInterval(() => { if (!document.hidden && S.page === "monitor") refreshMonitor(); }, 2000);
setInterval(() => { if (!document.hidden) refreshGrab(); }, 1000);
setInterval(() => { if (!document.hidden && S.page === "history") refreshHistory(); }, 30000);
setInterval(() => { if (!document.hidden) refreshSession(); }, 60000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshAll();
});
// B2：启动静默检查更新（未配置 update_url 时后端直接返回，无网络开销）
call("checkUpdate").then((r) => {
  if (r && r.has_new) { toast("发现新版本 " + r.version); pushLog("[更新] 发现新版本 " + r.version, "info"); }
}).catch(() => {});
