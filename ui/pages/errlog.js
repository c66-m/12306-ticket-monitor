/* pages/errlog.js —— 问题日志页（落盘台账 data/problem_log.jsonl 的前端）。 */
import { S, call, renderPage, registerPage } from "../store.js";
import { $, esc } from "../components/dom.js";
import { toast } from "../components/toast.js";
import { confirmBox } from "../components/modal.js";


// ================= 问题日志页 =================
// 数据源：后端 data/problem_log.jsonl（logutil.ProblemLogHandler 写入的
// WARNING/ERROR 台账），不是底部「全局日志」那份内存缓冲——那份重启即丢。
export const PROBLEM_LEVELS = {
  ERROR: ["错误", "fail"],
  WARNING: ["警告", "pause"],
  INFO: ["信息", "run"],
};

export function problemRowHtml(r, i) {
  const lv = PROBLEM_LEVELS[r.level] || [String(r.level || "—"), "canc"];
  const msg = String(r.message || "").trim();
  const first = msg.split("\n")[0];
  const short = first.length > 120 ? first.slice(0, 120) + "…" : first;
  return '<div class="s pl-item" data-i="' + i + '" title="点击展开堆栈 / 详情">' +
    '<span class="k"><b>' + esc(short) + "</b>" +
    (r.source ? ' <span class="muted" style="font-size:11px;opacity:.72">' + esc(r.source) + "</span>" : "") +
    "</span>" +
    '<span class="v">' + esc(r.ts || "") + "</span>" +
    '<span class="chip status ' + lv[1] + '">' + esc(lv[0]) + "</span></div>" +
    '<div class="problem-trace hidden" data-t="' + i + '">' + esc(r.trace || msg) + "</div>";
}

export function renderProblemLog(root) {
  const all = S.problems || [];
  const f = S.problemFilter || "all";
  const rows = all.filter((r) => f === "all" || r.level === f);
  const count = (lv) => all.filter((r) => r.level === lv).length;
  const chip = (k, label) => {
    const n = k === "all" ? all.length : count(k);
    return '<button class="btn btn-mini ' + (f === k ? "btn-call" : "btn-ghost") +
      '" data-fl="' + k + '">' + esc(label) + " " + n + "</button>";
  };
  root.innerHTML =
    '<div class="card glass glass-surface" style="flex:1"><div class="sec-head"><h3>问题日志</h3>' +
    '<span style="display:flex;gap:8px;align-items:center">' +
    chip("all", "全部") + chip("ERROR", "错误") + chip("WARNING", "警告") +
    '<button class="btn btn-mini btn-ghost" id="plRefresh">刷新</button>' +
    '<button class="btn btn-mini btn-ghost" id="plClear">清空</button></span></div>' +
    '<div class="set" style="overflow-y:auto">' +
    (rows.length ? rows.map(problemRowHtml).join("")
      : '<div class="empty-hint"><div class="big">🧾</div><p>暂无问题记录</p>' +
        '<p class="muted">引擎告警与接口异常会自动记到这里，重启不丢</p></div>') +
    "</div></div>";

  root.querySelectorAll("[data-fl]").forEach((b) => {
    b.onclick = () => { S.problemFilter = b.dataset.fl; renderProblemLog(root); };
  });
  root.querySelectorAll(".pl-item").forEach((el) => {
    el.onclick = () => {
      const t = root.querySelector('[data-t="' + el.dataset.i + '"]');
      if (t) t.classList.toggle("hidden");
    };
  });
  const rb = root.querySelector("#plRefresh");
  if (rb) rb.onclick = async () => {
    rb.disabled = true;
    try { await refreshProblemLogs(); } finally { rb.disabled = false; }
  };
  const cb = root.querySelector("#plClear");
  if (cb) cb.onclick = async () => {
    if (!await confirmBox("清空问题日志", "确定清空全部问题记录？此操作不可恢复。")) return;
    const r = await call("clearProblemLogs", { confirm: true });
    if (r && r.ok === false) return toast("清空失败", "err");
    await refreshProblemLogs();
    toast("已清空问题日志");
  };
}

export async function refreshProblemLogs() {
  try {
    const r = await call("listProblemLogs", { limit: 200 });
    S.problems = r.records || [];
    const b = $("#badge-errlog");
    if (b) b.textContent = S.problems.filter((x) => x.level === "ERROR").length;
    if (S.page === "errlog") renderPage();
  } catch (e) { /* ignore */ }
}

registerPage("errlog", renderProblemLog, refreshProblemLogs);
