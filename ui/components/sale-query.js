/* components/sale-query.js —— 抢票页「查询开售日期」弹窗。
   数据来自后端 querySaleDates（12306 官方起售时间 + 本地 14 天预售期）。
   只负责展示与提醒按钮，计算全部在 sale_calendar.py。 */
import { call } from "../store.js";
import { esc } from "./dom.js";
import { toast } from "./toast.js";
import { openModal } from "./modal.js";
import { stationInput, bindStation, resolveStation } from "./station.js";


function countdownText(minutes) {
  const m = Math.max(0, Number(minutes) || 0);
  if (m < 60) return m + " 分钟";
  const d = Math.floor(m / 1440);
  const h = Math.floor((m % 1440) / 60);
  const mm = m % 60;
  const parts = [];
  if (d) parts.push(d + " 天");
  if (h) parts.push(h + " 小时");
  if (!d && mm) parts.push(mm + " 分");
  return parts.join(" ") || "不足 1 分钟";
}


function storedMap(data) {
  const map = {};
  (data.storedTasks || []).forEach((t) => { map[t.id] = t; });
  return map;
}


function resultHtml(data) {
  const stored = storedMap(data);
  const rows = data.rows || [];
  const head = '<div class="sale-route"><b>' + esc(data.from.name) + " → " + esc(data.to.name) +
    '</b><span class="muted">出发站起售时间 ' + esc(data.from.saleTime) +
    " · 预售期 " + esc(data.presaleDays) + " 天</span></div>" +
    '<div class="muted sale-hint">点「储存任务」保存日期；新建抢票任务时可点一条直接回填出发站、到达站、日期和开抢时间。</div>';
  if (!rows.length) {
    return head + '<div class="muted sale-empty">未来 ' + esc(data.days) + " 天内没有尚未开售的日期。</div>";
  }
  const table = '<div class="sale-table-wrap"><table class="sale-table"><thead><tr>' +
    "<th>乘车日期</th><th>开售日期</th><th>开售时间</th><th>距开售</th><th>储存任务</th>" +
    "</tr></thead><tbody>" +
    rows.map((r, i) => {
      const item = r.storedTaskId ? stored[r.storedTaskId] : null;
      let action;
      if (item) {
        action = '<span class="sale-pill ok">已储存</span>' +
          '<button class="btn btn-mini btn-ghost" data-sale-del-store="' + esc(item.id) + '">取消</button>';
      } else {
        action = '<button class="btn btn-mini btn-ghost" data-sale-store="' + i + '">储存任务</button>';
      }
      return "<tr><td><b>" + esc(r.travelDate) + '</b> <span class="muted">' + esc(r.weekday) +
        "</span></td><td>" + esc(r.saleDate) + "</td><td>" + esc(r.saleTime) +
        "</td><td>" + esc(countdownText(r.minutesUntilSale)) + "</td><td>" + action + "</td></tr>";
    }).join("") + "</tbody></table></div>";

  const storedRows = (data.storedTasks || []).map((t) =>
    '<div class="sale-reminder-row"><span>' + esc(t.fromName) + " → " + esc(t.toName) +
    " · " + esc(t.travelDate) + " 乘车（" + esc(t.saleAt) + " 开售）</span>" +
    '<span class="sale-pill warn">待用</span>' +
    '<button class="btn btn-mini btn-ghost" data-sale-del-store="' + esc(t.id) + '">取消</button></div>').join("");
  const storedBox = '<div class="sale-reminders"><h4>已储存任务</h4>' +
    (storedRows || '<div class="muted">还没有储存任务。</div>') + "</div>";
  return head + table + storedBox;
}


export function openSaleQuery() {
  const { modal, close: closeRaw } = openModal(
    "<h3>查询开售日期</h3>" +
    '<div class="sale-toolbar">' +
    '<div class="form-row grow"><label>出发站</label>' + stationInput("sFrom", "") + "</div>" +
    '<div class="form-row grow"><label>到达站</label>' + stationInput("sTo", "") + "</div>" +
    '<div class="form-row"><label>查询范围</label><select id="sDays">' +
    '<option value="30">未来 30 天</option><option value="60" selected>未来 60 天</option>' +
    '<option value="90">未来 90 天</option></select></div>' +
    '<button class="btn btn-call" id="sQuery">查询</button></div>' +
    '<div class="muted sale-hint">按出发站官方起售时间与 14 天预售期计算，只显示尚未开售的日期。</div>' +
    '<div id="saleResult"><div class="muted sale-empty">选择出发站和到达站后点「查询」。</div></div>' +
    '<div class="form-actions"><button class="btn btn-ghost" id="saleClose">关闭</button></div>');
  modal.classList.add("sale-wide");
  const close = () => { modal.classList.remove("sale-wide"); closeRaw(); };
  bindStation(modal, "sFrom", "", "");
  bindStation(modal, "sTo", "", "");
  modal.querySelector("#saleClose").onclick = close;

  const box = modal.querySelector("#saleResult");
  let data = null;

  const wire = () => {
    if (!data) return;
    box.querySelectorAll("[data-sale-store]").forEach((btn) => {
      btn.onclick = async () => {
        const row = data.rows[Number(btn.dataset.saleStore)];
        if (!row) return;
        btn.disabled = true;
        try {
          const r = await call("saveStoredTask", {
            fromCode: data.from.code, toCode: data.to.code, travelDate: row.travelDate,
          });
          if (r && r.error) { toast(r.error, "err"); btn.disabled = false; return; }
          toast(r && r.duplicate ? "该日期已储存" : "已储存任务");
          await runQuery();
        } catch (e) { btn.disabled = false; }
      };
    });
    box.querySelectorAll("[data-sale-del-store]").forEach((btn) => {
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          const r = await call("deleteStoredTask", { id: btn.dataset.saleDelStore });
          if (r && r.error) { toast(r.error, "err"); btn.disabled = false; return; }
          toast("储存任务已取消");
          await runQuery();
        } catch (e) { btn.disabled = false; }
      };
    });
  };

  const runQuery = async () => {
    const from = await resolveStation(modal, "sFrom");
    const to = await resolveStation(modal, "sTo");
    if (!from || !to) return toast("请先选择有效的出发站和到达站", "err");
    box.innerHTML = '<div class="muted sale-empty">查询中…</div>';
    try {
      const r = await call("querySaleDates", {
        fromCode: from.code, toCode: to.code,
        days: Number(modal.querySelector("#sDays").value) || 60,
      });
      if (r && r.error) { box.innerHTML = '<div class="sale-warn">' + esc(r.error) + "</div>"; return; }
      data = r;
      box.innerHTML = resultHtml(data);
      wire();
    } catch (e) {
      box.innerHTML = '<div class="sale-warn">查询失败：' + esc(e.message || e) + "</div>";
    }
  };

  modal.querySelector("#sQuery").onclick = runQuery;
}
