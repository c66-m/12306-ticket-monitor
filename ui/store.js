/* store.js —— 全局状态 S、RPC 封装 call()、页签注册表与导航（goto/renderPage）。
   页面模块经 registerPage 注册渲染器，导航只查表不反向 import 页面，无循环依赖。 */
import { $ } from "./components/dom.js";
import { toast } from "./components/toast.js";
import { pushLog } from "./components/log.js";


export const bridge = window.api;
// opts.quiet：后台轮询专用——失败只 throw，不 toast、不写日志区。后端重启
// 退避（最长 30s）期间 1s/2s 的轮询若每次失败都提示，会反复刷屏并把日志
// 400 行缓冲冲掉真实记录。用户主动操作触发的调用不传 quiet，保持原样。
export async function call(method, params, opts) {
  try { return await bridge.call(method, params); }
  catch (e) {
    if (!opts || !opts.quiet) {
      const msg = String(e && e.message || e);
      toast(msg, "err");
      pushLog("[RPC] " + method + " 失败：" + msg, "err");
    }
    throw e;
  }
}

// ================= 全局状态 =================
export const S = {
  page: "grab",
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
  // _expiredPending：失效事件到达时正有业务弹窗（表单/确认框）开着，弹窗先
  // 降级提示并把消息存这里；业务弹窗关闭后由 session.js 补弹。null = 无待弹。
  _expiredPending: null,
  history: [],
  problems: [], problemFilter: "all",
  queryHistory: [],
  searchQ: "", stationHits: [], searchDD: [], searchLoading: false, trainRes: null, pickFrom: null,
  mgr: false, mgrSel: [], // 任务列表管理模式
  ambOnly: false, // 监控页统计条「待核对」点击过滤：只看 ambiguous 任务
  appear: null,
  dd: null, // 右键菜单
};
// 页签注册表：各 pages/*.js 在模块求值时经 registerPage 注册渲染器，
// goto/renderPage 只查注册表 —— 避免 store 反向 import 页面模块形成循环依赖。
// _enterHooks：进页时的一次性动作（原 goto 里写死的 errlog 拉取）；
// _closePanelHook：设置页注册的关面板动作（原 goto 里写死的 closeSettingPanel）。
const _renderers = {};
const _enterHooks = {};
let _closePanelHook = null;
export function registerPage(name, renderer, onEnter) {
  _renderers[name] = renderer;
  if (onEnter) _enterHooks[name] = onEnter;
}
export function registerClosePanelHook(fn) { _closePanelHook = fn; }

// ================= 页签 =================
export const PAGES = {
  // aliases：改名前的旧称。用户按旧习惯搜「监控任务」时仍应命中本页
  // （顶栏搜索的页面建议与 runTaskSearch 都经 pageNameHit 匹配）。
  monitor: { crumb: "直接抢票", title: "直接抢票", aliases: ["监控任务"] },
  grab: { crumb: "抢票任务", title: "抢票任务" },
  pax: { crumb: "乘车人", title: "乘车人" },
  history: { crumb: "历史记录", title: "历史" },
  errlog: { crumb: "问题日志", title: "问题日志" },
  settings: { crumb: "设置", title: "设置" },
};

// 页签名命中：crumb/title 子串，或别名（旧称）子串
export function pageNameHit(k, q) {
  const p = PAGES[k];
  return p.crumb.includes(q) || p.title.includes(q) || (p.aliases || []).some((a) => a.includes(q));
}

export function goto(page) {
  S.page = page;
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.page === page));
  const bs = $("#btnSettings");
  if (bs) bs.classList.toggle("active", page === "settings");
  $("#crumb").textContent = "/ " + PAGES[page].crumb;
  // 切页即收起设置放大面板（面板只在设置页语境使用，避免跨页残留）
  if (_closePanelHook) _closePanelHook();
  renderPage(true); // 导航走抽屉动画
  // 问题日志是落盘数据，进页时拉一次最新（不在后台定时刷，避免展开的堆栈被重置）
  const _enterHook = _enterHooks[page]; if (_enterHook) _enterHook();
}

// animationend 兜底：关动效（html[data-motion="off"]）时 CSS 把 animation
// 全部禁用，animationend 永不触发，依赖它的清理会永远悬空（切页时旧页堆积、
// 重复 id）。统一走这里：动画正常结束就按动画走；450ms 没等到则由定时器
// 执行同一清理。清理幂等，只跑一次，先到者取消后到者。
function afterAnim(el, name, fn) {
  let done = false;
  const timer = setTimeout(run, 450);
  function run() {
    if (done) return;
    done = true;
    clearTimeout(timer);
    el.removeEventListener("animationend", onEnd);
    fn();
  }
  function onEnd(e) { if (e.animationName === name) run(); }
  el.addEventListener("animationend", onEnd);
}

export function renderPage(animate) {
  const c = $("#content");
  const hasOld = c.firstElementChild !== null;
  // 动画切换：不清空，旧页执行退回动画后移除，新页从抽屉拉出
  if (animate && hasOld) {
    const old = c.firstElementChild;
    old.classList.add("page-out");
    afterAnim(old, "page-draw-out", () => old.remove());
  } else if (!animate) {
    c.innerHTML = ""; // 刷新重建路径：无动画直接清空
  }
  const el = document.createElement("div");
  el.className = "page" + (animate ? " page-in" : "");
  c.appendChild(el);
  if (animate) {
    // 进入动画结束移除 page-in：恢复毛玻璃（动画期间禁用 backdrop-filter 防模糊）
    afterAnim(el, "page-draw-in", () => el.classList.remove("page-in"));
  }
  _renderers[S.page](el);
}

// 搜索过滤（列表项文本匹配）
export function hitSearch(...texts) {
  const q = S.searchQ.trim().toLowerCase();
  if (!q) return true;
  return texts.join(" ").toLowerCase().includes(q);
}


// 本地时区 YYYY-MM-DD（toISOString 是 UTC，东八区 0-8 点会取到前一天）
export function localDate(d) {
  const x = d || new Date();
  const p = (n) => String(n).padStart(2, "0");
  return x.getFullYear() + "-" + p(x.getMonth() + 1) + "-" + p(x.getDate());
}

export function dateSpan(d1, d2) {
  const out = []; const a = new Date(d1), b = new Date(d2);
  if (a > b) return out;
  for (let d = new Date(a); d <= b; d.setDate(d.getDate() + 1))
    out.push(localDate(d));
  return out;
}

// ================= 常量（与 Python 侧对齐） =================
export const SEAT_CHOICES = ["商务座", "特等座", "优选一等座", "一等座", "二等座", "高级软卧", "动卧", "软卧", "硬卧", "软座", "硬座", "无座"];
