/* components/session.js —— 会话状态卡、登录失效弹窗与「稍后」记忆。 */
import { S, call } from "../store.js";
import { $, esc } from "./dom.js";
import { openModal, modalKind, onModalClosed } from "./modal.js";
import { toast } from "./toast.js";
import { autoSyncPax } from "../pages/pax.js";


export async function refreshSession(opts) {
  try {
    const sess = await call("checkSession", undefined, opts);
    const acc = await call("getAccount", undefined, opts);
    // unknown：这一轮没验成（浏览器正忙 / 风控 / 网络），不是掉线的证据 ——
    // 保留上一次结论，免得登录成功后界面又显示「未登录」（S.session 不动）
    if (sess.unknown) { renderSessionCard(); return; }
    const prevOk = !!(S.session && S.session.ok);
    S.session = sess;
    S.account = acc;
    const ok = S.session.ok;
    // 会话恢复：复位「已忽略」与冷却窗口，下次真失效时重新提示；
    // 同时作废待弹的失效提示——会话已恢复，业务弹窗关掉后不该再补弹。
    if (ok) { S._expiredDismissed = false; S._expiredAt = 0; S._expiredPending = null; writeExpiredDismissed(false); }
    // 每次登录（未登录→已登录，含启动时已是登录态）自动扫描一次乘车人
    if (ok && !prevOk && !S._paxAutoDone) {
      S._paxAutoDone = true;
      autoSyncPax();
    } else if (!ok) {
      S._paxAutoDone = false;
    }
    renderSessionCard();
  } catch (e) { /* ignore */ }
}

export function renderSessionCard() {
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

// 会话失效文案：engine「登录会话失效」/orderflow「浏览器会话不可用」/browser_order「已跳转登录门户」
// 「登录会话已?失效」两种写法都要（engine.py:1506「已失效」/ :1222「失效」），
// gui.py:4036 的 tkinter 版用的是同一判据。
export const _SESSION_DEAD_RE = /登录会话已?失效|浏览器会话不可用|已跳转登录门户/;

// 「稍后」记忆的存储层：sessionStorage 可能不可用（隐私模式 / 受限沙箱），
// 失败时静默退化为纯内存标记，不影响主流程。
export const EXPIRED_DISMISSED_KEY = "sessionExpiredDismissed";

export function readExpiredDismissed() {
  try { return sessionStorage.getItem(EXPIRED_DISMISSED_KEY) === "1"; }
  catch (e) { return false; }
}

export function writeExpiredDismissed(on) {
  try {
    if (on) sessionStorage.setItem(EXPIRED_DISMISSED_KEY, "1");
    else sessionStorage.removeItem(EXPIRED_DISMISSED_KEY);
  } catch (e) { /* 存储不可用：内存标记已生效 */ }
}

// 失效降级提示：复用会话卡 / 顶栏头像的元素把状态改成失效红字（off 类），
// 下一次 refreshSession 渲染会按后端真实状态覆盖回来。
function markSessionExpired() {
  const sc = $("#sessionCard");
  if (sc) {
    const sp = sc.querySelector("span");
    if (sp) { sp.textContent = "登录已失效 · 待重新登录"; sp.className = "off"; }
  }
  const ch = $("#sessionChip");
  if (ch) {
    const it = ch.querySelector("i");
    if (it) it.textContent = "登录已失效 · 待重新登录";
    ch.classList.remove("on");
  }
}

// 登录失效弹窗（api 会话看门狗推 sessionExpired）
// 两层防重：_expiredDialogOpen 防同时叠加；_expiredDismissed 记住「用户已点稍后」，
// 本次失效期间不再打扰（会话恢复后由 refreshSession 复位，下次真失效仍会提示）。
export function showSessionExpiredDialog(message) {
  // 头像 doLogin 发起的 relogin 还没返回：用户正在登录，别抢着弹失效弹窗
  if (S._reloginPending) return;
  if (S._expiredDialogOpen || S._expiredDismissed) return;
  // 刷新 / 重载界面后仍记得「点过稍后」：sessionStorage 是窗口级存储，
  // 关掉软件即失效，所以下次启动仍会正常提示一次。
  if (readExpiredDismissed()) { S._expiredDismissed = true; return; }
  // 业务弹窗（任务/乘车人表单、确认框）正开着：openModal 会无条件替换
  // #modal 内容，此刻弹失效窗会把未保存的输入无声冲掉。改为降级提示
  // （toast + 会话卡红字）并登记待弹，业务弹窗关掉后补弹（见文件末注册）。
  if (modalKind() === "business") {
    const first = S._expiredPending == null;
    S._expiredPending = message || "";
    S._expiredAt = Date.now();   // 与日志链路共用同一冷却戳，防两路重复降级刷屏
    if (first) {
      toast("登录已失效：请先处理完当前窗口，关闭后将提示重新登录", "err");
      markSessionExpired();
    }
    return;
  }
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
    '<button class="btn btn-call" data-a="relogin">立即登录</button></div>', "expired");
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

// 业务弹窗关闭后补弹被推迟的失效提示：任何弹窗关闭都会走这里，先取走
// 待弹消息再判防重标记——已在弹/已忽略/正在重登时直接丢弃，不留残项。
onModalClosed(() => {
  if (S._expiredPending == null) return;
  const msg = S._expiredPending;
  S._expiredPending = null;
  if (S._expiredDialogOpen || S._expiredDismissed || S._reloginPending) return;
  showSessionExpiredDialog(msg);
});
