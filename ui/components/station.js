/* components/station.js —— 车站联想输入框与站名解析（监控/抢票表单共用）。 */
import { S, call } from "../store.js";
import { esc } from "./dom.js";
import { toast } from "./toast.js";


// 站点输入框 → {name,code}：优先取下拉已选项，否则现场搜索首个命中
export async function resolveStation(root, id) {
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

// 车站联想输入框
export function stationInput(id, val) {
  return '<div class="station-box" data-st="' + id + '">' +
    '<input autocomplete="off" placeholder="输入拼音 / 简拼 / 站名" value="' + esc(val || "") + '">' +
    '<div class="station-dd hidden"></div></div>';
}
export function bindStation(root, id, name, code) {
  const box = root.querySelector('[data-st="' + id + '"]');
  const input = box.querySelector("input");
  const ddEl = box.querySelector(".station-dd");
  S["_st" + id] = name ? { name, code } : null;
  let timer = null;
  const load = async () => {
    const q = input.value.trim();
    if (!q) { ddEl.classList.add("hidden"); return; }
    let r;
    try {
      r = await call("searchStation", { q });
    } catch (e) {
      // 搜索失败（后端抖动）：清空并收起下拉。旧候选对应的是上一个查询串，
      // 留着会让用户误选与当前输入无关的站；失败原因 call() 已 toast。
      ddEl.innerHTML = "";
      ddEl.classList.add("hidden");
      return;
    }
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
