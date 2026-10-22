/* components/modal.js —— 模态框（#overlay/#modal）与确认框。 */
import { $, esc } from "./dom.js";


export function openModal(html) {
  const modal = $("#modal");
  modal.innerHTML = html;
  $("#overlay").classList.remove("hidden");
  const close = () => $("#overlay").classList.add("hidden");
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
