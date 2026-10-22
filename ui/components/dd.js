/* components/dd.js —— 右键/下拉菜单（#dd）；菜单项暂存于 S.dd。 */
import { S } from "../store.js";
import { $, esc } from "./dom.js";


// 右键菜单
export function showDD(items, x, y) {
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
