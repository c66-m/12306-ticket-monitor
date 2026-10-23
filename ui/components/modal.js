/* components/modal.js —— 模态框（#overlay/#modal）与确认框。
   #modal 是全应用唯一的弹窗槽：openModal 无条件替换内容。弹窗分两类：
   business（任务/乘车人表单、确认框等业务弹窗，默认）与 expired（登录失效
   弹窗）。openModal 记录当前类别，close 时通知 onModalClosed 订阅者——
   session.js 据此实现「业务弹窗开着时失效提示先降级、弹窗关后再补弹」，
   避免失效弹窗直接冲掉用户未保存的表单输入。 */
import { $, esc } from "./dom.js";


let _kind = null;   // 当前弹窗类别："business" | "expired" | null（无弹窗）
let _seq = 0;       // 每次 openModal 递增；close 只清自己那一层的类别标记
const _closedHooks = [];

export function modalKind() { return _kind; }
export function onModalClosed(fn) { _closedHooks.push(fn); }

export function openModal(html, kind) {
  const modal = $("#modal");
  modal.innerHTML = html;
  $("#overlay").classList.remove("hidden");
  _kind = kind || "business";
  const mySeq = ++_seq;
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    $("#overlay").classList.add("hidden");
    if (_seq === mySeq) _kind = null;
    _closedHooks.forEach((fn) => { try { fn(); } catch (e) { /* 订阅者异常不影响关窗 */ } });
  };
  $("#overlay").onclick = (e) => { if (e.target.id === "overlay") close(); };
  return { modal, close };
}

export function confirmBox(title, text, okLabel) {
  return new Promise((res) => {
    const { modal, close } = openModal(
      '<h3>' + esc(title) + '</h3><p class="muted" style="margin:8px 0 14px">' + esc(text) + '</p>' +
      '<div class="form-actions"><button class="btn btn-ghost" data-a="no">取消</button>' +
      '<button class="btn btn-call" data-a="yes">' + esc(okLabel || "确定") + '</button></div>');
    modal.querySelector('[data-a=no]').onclick = () => { close(); res(false); };
    modal.querySelector('[data-a=yes]').onclick = () => { close(); res(true); };
  });
}
