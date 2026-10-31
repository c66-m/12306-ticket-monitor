/* pages/history.js —— 购票历史页与订单核对。 */
import { S, call, renderPage, hitSearch, registerPage } from "../store.js";
import { $, esc } from "../components/dom.js";
import { toast } from "../components/toast.js";


// ================= 历史页 =================
export function renderHistory(root) {
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
        (r.message ? '<div class="muted" style="margin:-4px 0 6px 2px;font-size:11px">' + esc(r.message) + "</div>" : "") +
        (r.screenshot ? '<div style="margin:-2px 0 8px 2px"><span class="chip" data-shot="' + esc(r.screenshot) + '" style="cursor:pointer">🖼 截图</span></div>' : "");
    }).join("") || '<div class="muted" style="padding:20px;text-align:center">暂无记录</div>') + "</div></div>";

  // 本地截图路径 → file:// URL：按路径段逐段编码（整串 encodeURI 会
  // 漏掉 # ? %，含这些字符的路径打不开）；盘符冒号与 UNC 服务器名
  // 不编码（2026-10-25 审）。
  function shotUrl(p) {
    const s = String(p).replace(/\\/g, "/");
    if (s.startsWith("//")) {
      return "file://" + s.slice(2).split("/").map(encodeURIComponent).join("/");
    }
    const parts = s.replace(/^\/+/, "").split("/");
    const head = parts.length && /^[A-Za-z]:$/.test(parts[0]) ? parts.shift() : null;
    return "file:///" + (head ? head + "/" : "") + parts.map(encodeURIComponent).join("/");
  }

  // 截图留证：点「🖼 截图」懒加载 <img>（file:// 直读，无需新增 RPC）
  root.querySelectorAll("[data-shot]").forEach((chip) => {
    chip.onclick = () => {
      let img = chip.parentElement.querySelector("img");
      if (!img) {
        img = document.createElement("img");
        img.src = shotUrl(chip.dataset.shot);
        img.style.cssText = "display:block;max-width:100%;margin:6px 0 2px;border-radius:8px";
        // 30 天清理后旧记录的图已删：旧口径只剩破图无说明，换成
        // muted 文案（2026-10-25 审）。
        img.onerror = () => {
          const note = document.createElement("span");
          note.className = "muted";
          note.textContent = "截图已过期清理";
          img.replaceWith(note);
        };
        chip.parentElement.appendChild(img);
      }
      img.hidden = !img.hidden;
    };
  });

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

export async function refreshHistory(opts) {
  try { const r = await call("listHistory", { limit: 200 }, opts); S.history = r.records || []; if (S.page === "history") renderPage(); }
  catch (e) { if (!opts || !opts.quiet) toast("历史刷新失败", "err"); }
}
// failed = 本轮未成功、下轮继续重试，不是「下单失败」（真失败时 12306 往往
  // 已经生成了未支付订单，拒绝重复下单的原文是「包含未付款订单」）。
  // 与 appcommon.HISTORY_RESULT_LABEL 保持一致。
  export const HISTORY_LABELS = {
    success: "下单成功", dup: "防重跳过", notified: "已通知",
    hit_no_order: "有余票未下单", ambiguous: "结果待核对",
    browser_dead: "浏览器中断", seat_unavailable: "席别不可售",
    failed: "本轮未成功", error: "异常",
    confirmed_unpaid: "已对账·有未付单", confirmed_paid: "已对账·已支付",
  };

registerPage("history", renderHistory, () => refreshHistory({ quiet: true }));
