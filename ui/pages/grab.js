/* pages/grab.js —— 抢票任务页（卡片网格/任务表单/预设/查询历史）。 */
import { S, call, goto, renderPage, localDate, SEAT_CHOICES, registerPage } from "../store.js";
import { $, esc } from "../components/dom.js";
import { toast } from "../components/toast.js";
import { openModal, confirmBox } from "../components/modal.js";
import { showDD } from "../components/dd.js";
import { stationInput, bindStation, resolveStation } from "../components/station.js";
import { refreshPax } from "./pax.js";
import { runSearch } from "./monitor.js";


// ================= 抢票页 =================
export function renderGrab(root) {
  root.innerHTML =
    '<div class="sec-head"><h3>抢票任务</h3><span style="display:flex;gap:8px">' +
    '<button class="btn btn-mini btn-ghost" id="gPreset">预设 ▾</button>' +
    '<button class="btn btn-mini btn-ghost" id="gHist">🕘 查询历史</button>' +
    '<button class="btn btn-mini btn-call" id="gNew">＋ 新任务</button></span></div>' +
    '<div class="grid-3" id="gGrid"></div>';
  root.querySelector("#gNew").onclick = () => openGrabForm();
  root.querySelector("#gPreset").onclick = (e) => {
    const items = S.presets.map((p, i) => ({ i, label: "▸ " + p.name, fn: () => openGrabForm(null, p) }));
    items.push({ i: 98, label: "🗑 删除预设…", danger: true, fn: () => pickPresetToDelete(e) });
    items.push({ i: 99, label: "＋ 存当前为预设", fn: () => { toast("在任务表单里点「存为预设」"); } });
    showDD(items, e.clientX, e.clientY);
  };
  // B6 查询历史：点击回填到监控页并发起查票
  root.querySelector("#gHist").onclick = (e) => {
    const hist = S.queryHistory || [];
    if (!hist.length) return toast("暂无查询历史");
    showDD(hist.map((h, i) => ({
      i, label: h.from + " → " + h.to, fn: () => applyQueryHistory(h),
    })), e.clientX, e.clientY);
  };
  renderGrabGrid(root.querySelector("#gGrid"));
}

// 删除预设（B4）：先选预设再确认
export function pickPresetToDelete(e) {
  const ps = S.presets || [];
  if (!ps.length) return toast("暂无预设");
  showDD(ps.map((p, i) => ({
    i, label: "🗑 " + p.name, danger: true, fn: async () => {
      if (!await confirmBox("删除预设", "删除预设「" + p.name + "」？")) return;
      const r = await call("deletePreset", { name: p.name });
      if (r && r.error) return toast(r.error, "err");
      toast("预设已删除"); refreshPresets();
    },
  })), e.clientX, e.clientY);
}

// 查询历史回填：解析站名 → 监控页查票
export async function applyQueryHistory(h) {
  if (!h) return;
  goto("monitor");
  try {
    const r = await call("searchStation", { q: h.from });
    const fr = (r.stations || [])[0];
    if (!fr) return toast("站名无法解析：" + h.from, "err");
    const r2 = await call("searchStation", { q: h.to });
    const to = (r2.stations || [])[0];
    if (!to) return toast("站名无法解析：" + h.to, "err");
    runSearch({ name: fr.name, code: fr.code }, { name: to.name, code: to.code }, "", h.date);
  } catch (err) { /* call 已提示 */ }
}

// 抢票结果详情（A2 续）：后端 grabResult 返回内存中的完整 result 对象
export async function openGrabResult(t) {
  if (!t) return;
  const r = await call("grabResult", { id: t.id });
  const obj = r && r.result ? r.result : null;
  const body = obj
    ? '<pre class="mono" style="white-space:pre-wrap;max-height:50vh;overflow:auto;margin:0">' + esc(JSON.stringify(obj, null, 2)) + "</pre>"
    : '<div class="muted">详情已过期（后端重启或任务已清理）。<br>卡片上仍保留最后结果：' + esc(t.result_msg || "—") + "</div>";
  const { close } = openModal("<h3>抢票结果 · " + esc(t.name || t.id) + "</h3>" + body +
    '<div class="form-actions"><button class="btn btn-call" id="grOk">关闭</button></div>');
  document.querySelector("#grOk").onclick = close;
}

export function renderGrabGrid(el) {
  if (!S.grab.length) {
    el.innerHTML = '<div class="muted" style="grid-column:1/-1;padding:24px;text-align:center">暂无抢票任务，点「＋ 新任务」创建</div>';
    return;
  }
  el.innerHTML = S.grab.map((t) => {
    const running = t.running;
    const hasRes = t.result_msg !== undefined && t.result_msg !== null && t.result_msg !== "";
    const amb = t.result_reason === "ambiguous" || t.status === "ambiguous";
    const st = t.status === "ok" ? "succ" : running ? "run" : amb ? "warn" : "pause";
    const stLabel = t.status === "ok" ? "已抢到" : running ? "抢票中" : amb ? "待核对" : "待命";
    return '<div class="card glass glass-surface grab-card">' +
      '<div class="sec-head"><h3>' + esc(t.name || t.id) + '</h3><span class="status ' + st + '">' + stLabel + "</span></div>" +
      '<div class="g-count"' + (running ? ' style="color:var(--ok)"' : "") + ">" + esc(t.countdown || "—") + "</div>" +
      '<div class="muted" style="margin:4px 0">' + esc([t.from, t.to].filter(Boolean).join(" → ") || "未设区间") +
      (t.trains && t.trains.length ? " · " + esc(t.trains.join(",")) : "") + "</div>" +
      '<div class="muted">' + esc((t.seat_types || []).join("/") || "未选席别") + (t.armed ? " · 🔔已武装" : "") + (t.auto_fired ? " · ⚡已自动触发" : "") + "</div>" +
      (hasRes
        ? '<div class="g-result ' + (t.result_ok ? "ok" : amb ? "warn" : "err") + '">' + (t.result_ok ? "✔ " : amb ? "⚠ " : "✖ ") + esc(t.result_msg) +
          (t.result_at ? '<i class="muted">' + esc(t.result_at) + "</i>" : "") + "</div>"
        : "") +
      '<div class="form-actions" style="margin-top:10px">' +
      (running
        ? '<button class="btn btn-mini btn-ghost" data-g="stop">■ 停止</button>'
        : '<button class="btn btn-mini btn-call" data-g="start">▶ 开抢</button>') +
      '<button class="btn btn-mini btn-ghost" data-g="edit">✎ 编辑</button>' +
      (hasRes ? '<button class="btn btn-mini btn-ghost" data-g="detail">📄 结果详情</button>' : "") +
      "</div></div>";
  }).join("");

  el.querySelectorAll("[data-g]").forEach((b) => {
    b.onclick = async () => {
      const card = b.closest(".grab-card");
      const idx = Array.from(el.children).indexOf(card);
      const t = S.grab[idx]; if (!t) return;
      if (b.dataset.g === "start") { await call("startGrab", { id: t.id }); toast("已开抢"); }
      if (b.dataset.g === "stop") { await call("stopGrab", { id: t.id }); toast("已停止"); }
      if (b.dataset.g === "edit") openGrabForm(t);
      if (b.dataset.g === "detail") openGrabResult(t);
      if (b.dataset.g === "start" || b.dataset.g === "stop") setTimeout(refreshGrab, 400);
    };
  });
}

// 抢票任务表单
export async function openGrabForm(edit, preset) {
  if (!S.pax.length) await refreshPax();
  const t = Object.assign({}, _DEFAULT_LC, edit || {}, preset ? {
    from: preset.from, to: preset.to, trains: preset.trains || [], seat_types: preset.seat_types || [],
    seat_priority: preset.seat_priority || "",
  } : {});
  // 逐车席别（车次 → Set<席别名>）：删除整排席别勾选后，唯一的席别入口
  const perTrain = {};
  const prioRaw = Array.isArray(t.seat_priority) ? t.seat_priority.join(",") : (t.seat_priority || "");
  prioRaw.split(/[,，、;；]+/).forEach((e) => {
    const i = e.indexOf("=");
    if (i > 0) {
      const code = e.slice(0, i).trim().toUpperCase();
      const set = perTrain[code] || (perTrain[code] = new Set());
      e.slice(i + 1).split(/[/\s]+/).filter(Boolean).forEach((s) => set.add(s));
    }
  });
  // 老任务兼容：无逐车规则但存了全局席别 → 每车按全局席别标记
  if (!Object.keys(perTrain).length && (t.seat_types || []).length && (t.trains || []).length)
    (t.trains || []).forEach((code) => { perTrain[String(code).toUpperCase()] = new Set(t.seat_types); });
  const { modal, close } = openModal(
    "<h3>" + (edit ? "编辑抢票任务" : "新建抢票任务") + "</h3>" +
    '<div class="form-row"><label>任务名</label><span style="display:flex;gap:8px;flex:1">' +
    '<input id="gName" value="' + esc(t.name || "") + '" style="flex:1">' +
    (edit ? '<button class="btn btn-mini btn-ghost" id="gRename">改名</button>' : "") +
    "</span></div>" +
    '<div class="mt-top"><div class="form-row grow"><label>出发站</label>' + stationInput("gfrom", t.from) + "</div>" +
    '<div class="form-row grow"><label>到达站</label>' + stationInput("gto", t.to) + "</div></div>" +
    '<div class="form-row"><label>车次与席别（点查询后逐车选择席别）</label>' +
    '<div class="mt-qrow"><input id="gTrains" placeholder="点击下方车次加入，点击席别标签为该车选席别" value="' + esc((t.trains || []).join(",")) + '">' +
    '<button class="btn btn-call mt-qbtn" id="gQuery" title="按出发/到达站与日期查询余票，识别各车次席别">🔍 查询</button></div>' +
    '<div class="mt-qresult hidden" id="gQResult"></div></div>' +
    '<div class="form-grid">' +
    '<div class="form-row"><label>日期</label><input type="date" id="gDate" value="' + esc(t.date || "") + '"></div>' +
    '<div class="form-row"><label>截止日期（可选）</label><input type="date" id="gDateTo" value="' + esc(t.date_to || "") + '"></div>' +
    "</div>" +
    '<div class="form-grid">' +
    '<div class="form-row"><label>开抢时间（可选，留空 = 手动）</label><input type="datetime-local" id="gStart" value="' + esc((t.start_time || "").replace(" ", "T")) + '"></div>' +
    '<div class="form-row"><label>提前提醒（分钟）</label><input type="number" id="gRemind" value="' + esc(t.remind_minutes ?? 10) + '"></div>' +
    "</div>" +
    '<div class="form-grid">' +
    '<div class="form-row"><label>预热窗口（分钟）</label><input type="number" id="gWarm" value="' + esc(t.warm_minutes ?? 10) + '"></div>' +
    '<div class="form-row"><label>轮询间隔（秒）</label><input type="number" id="gPoll" value="' + esc(t.poll_seconds ?? 3) + '"></div>' +
    "</div>" +
    '<div class="form-row"><label>乘车人</label><div id="gPax">' +
    S.pax.map((p) =>
      '<div class="form-row" style="margin-bottom:4px"><div class="check-row"><label class="check' + ((t.passenger_names || []).includes(p.name) ? " on" : "") + '"><input type="checkbox" value="' + esc(p.name) + '"' + ((t.passenger_names || []).includes(p.name) ? " checked" : "") + ">" + esc(p.name) + '</label>' +
      '<select class="gpax-purpose" style="margin-left:8px"><option value="ADULT"' + (((t.pax_purpose || {})[p.name] || "ADULT") === "ADULT" ? " selected" : "") + '>成人票</option><option value="0X00"' + (((t.pax_purpose || {})[p.name] || "") === "0X00" ? " selected" : "") + '>学生票</option></select></div></div>').join("") +
    (S.pax.length ? "" : '<span class="muted">暂无乘车人</span>') + "</div></div>" +
    '<div class="form-actions"><button class="btn btn-ghost" id="gCancel">取消</button>' +
    (edit ? "" : '<button class="btn btn-ghost" id="gPresetSave">存为预设</button>') +
    (edit ? '<button class="btn btn-ghost" id="gDelete">删除</button>' : "") +
    '<button class="btn btn-call" id="gSave">' + (edit ? "保存" : "创建") + "</button></div>");

  modal.querySelector("#gCancel").onclick = close;
  bindStation(modal, "gfrom", t.from || "", t.fromCode || "");
  bindStation(modal, "gto", t.to || "", t.toCode || "");

  // 改名：编辑弹窗内独立按钮（复用 openRenameForm 逻辑）
  const renameBtn = modal.querySelector("#gRename");
  if (renameBtn && edit) {
    renameBtn.onclick = async () => {
      const name = modal.querySelector("#gName").value.trim();
      if (!name) return toast("请填写任务名", "err");
      const r = await call("renameGrabTask", { id: edit.id, name });
      if (r && r.error) return toast(r.error, "err");
      toast("已改名");
      close();
      refreshGrab();
    };
  }

  // 删除：编辑弹窗底部普通样式按钮（不套红色）
  const delBtn = modal.querySelector("#gDelete");
  if (delBtn && edit) {
    delBtn.onclick = async () => {
      if (!await confirmBox("删除", "删除抢票任务「" + (edit.name || edit.id) + "」？")) return;
      await call("deleteGrabTask", { id: edit.id });
      toast("已删除");
      close();
      refreshGrab();
    };
  }

  // 车次查询：识别每趟车的席别（哪些有硬/软卧、一/二等座、硬座/无座），逐车点选席别
  modal.querySelector("#gQuery").onclick = async () => {
    const qRes = modal.querySelector("#gQResult");
    const from = await resolveStation(modal, "gfrom"), to = await resolveStation(modal, "gto");
    if (!from || !to) {
      toast("请先填有效的出发站和到达站再查询", "err");
      modal.querySelector('[data-st="' + (!from ? "gfrom" : "gto") + '"] input').focus();
      return;
    }
    const d1 = modal.querySelector("#gDate").value || localDate();
    qRes.classList.remove("hidden");
    qRes.innerHTML = '<div class="muted" style="padding:6px 2px">查询中…</div>';
    try {
      const r = await call("queryTickets", { fromCode: from.code, toCode: to.code, date: d1, purpose: "ADULT" });
      if (r && r.error) { qRes.innerHTML = '<div class="muted" style="padding:6px 2px">' + esc(r.error) + "</div>"; return; }
      const trains = r.trains || [];
      if (!trains.length) { qRes.innerHTML = '<div class="muted" style="padding:6px 2px">' + esc(d1) + " 无车次，换日期试试</div>"; return; }
      const pickedCodes = (modal.querySelector("#gTrains").value || "").split(/[,，\s]+/).filter(Boolean).map((x) => x.toUpperCase());
      qRes.innerHTML = trains.map((tr) => {
        const sel = perTrain[tr.trainCode] || new Set();
        // 全部席别都渲染（不截断）：识别功能要看全；sold 灰显不可选
        const seatChips = tr.seats.map((s) =>
          '<span class="seat ' + s.state + (s.state !== "sold" ? " pick" : "") + (sel.has(s.name) ? " on" : "") + '" data-seat="' + esc(s.name) + '">' + esc(s.name) + (s.count ? " " + esc(s.count) : "") + "</span>").join("");
        return '<div class="train mt-train' + (pickedCodes.includes(tr.trainCode) ? " sel" : "") + '" data-code="' + esc(tr.trainCode) + '" title="点击行选择/取消车次；点击席别标签为该车单独选席别">' +
          '<div class="top"><span class="no">' + esc(tr.trainCode) + '</span><span class="dur">' + esc(tr.startTime) + " → " + esc(tr.arriveTime) + "</span></div>" +
          '<div class="seats">' + seatChips + "</div></div>";
      }).join("");
      qRes.querySelectorAll(".mt-train").forEach((el) => {
        el.onclick = () => {
          const inp = modal.querySelector("#gTrains");
          const cur = inp.value.split(/[,，\s]+/).filter(Boolean);
          const i = cur.map((x) => x.toUpperCase()).indexOf(el.dataset.code);
          if (i < 0) { cur.push(el.dataset.code); el.classList.add("sel"); toast("已加入车次 " + el.dataset.code + "，请点击席别标签"); }
          else { cur.splice(i, 1); el.classList.remove("sel"); delete perTrain[el.dataset.code]; toast("已移除车次 " + el.dataset.code); }
          inp.value = cur.join(", ");
        };
        el.querySelectorAll(".seat.pick").forEach((ch) => ch.onclick = (e) => {
          e.stopPropagation();
          const set = perTrain[el.dataset.code] || (perTrain[el.dataset.code] = new Set());
          const name = ch.dataset.seat;
          if (set.has(name)) set.delete(name); else set.add(name);
          ch.classList.toggle("on", set.has(name));
          if (!set.size) delete perTrain[el.dataset.code];
        });
      });
    } catch (e) { qRes.innerHTML = '<div class="muted" style="padding:6px 2px">查询失败：' + esc(e.message) + "</div>"; }
  };

  modal.querySelectorAll("#gPax label.check").forEach((l) => {
    l.addEventListener("change", () => l.classList.toggle("on", l.querySelector("input").checked));
  });

  modal.querySelector("#gSave").onclick = async () => {
    const from = await resolveStation(modal, "gfrom"), to = await resolveStation(modal, "gto");
    if (!from || !to) return toast("请选择出发站和到达站", "err");
    const date = modal.querySelector("#gDate").value;
    if (!date) return toast("请选择日期", "err");
    const trains = modal.querySelector("#gTrains").value.split(/[,，\s]+/).filter(Boolean).map((x) => x.toUpperCase());
    if (!trains.length) { toast("请点「查询」并选择要抢的车次", "err"); modal.querySelector("#gQuery").scrollIntoView({ block: "center" }); return; }
    for (const code of trains) {
      if (!(perTrain[code] && perTrain[code].size)) {
        toast("请为车次 " + code + " 选择席别（点击查询结果中该车的席别标签）", "err");
        modal.querySelector("#gQResult").scrollIntoView({ block: "center" });
        return;
      }
    }
    // seat_types=并集；各车席别集合相同时只存并集，否则生成「车次=席别」专属规则
    const unionSet = new Set(); trains.forEach((code) => perTrain[code].forEach((s) => unionSet.add(s)));
    const seats = SEAT_CHOICES.filter((s) => unionSet.has(s));
    const perSets = trains.map((code) => [...perTrain[code]].join("/"));
    const seat_priority = perSets.every((x) => x === perSets[0]) ? "" : trains.map((code, i) => code + "=" + perSets[i]).join(",");
    const pax = Array.from(modal.querySelectorAll("#gPax input:checked")).map((x) => x.value);
    if (!pax.length) return toast("请勾选乘车人", "err");
    const paxPurpose = {};
    modal.querySelectorAll("#gPax .form-row").forEach((row) => {
      const cb = row.querySelector("input:checked");
      if (cb) paxPurpose[cb.value] = row.querySelector("select").value;
    });
    const st = modal.querySelector("#gStart").value;
    const task = Object.assign({}, _DEFAULT_LC, {
      id: edit ? edit.id : (crypto && crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "").slice(0, 8) : Date.now().toString(16)),
      name: modal.querySelector("#gName").value.trim() || ("任务 " + (S.grab.length + 1)),
      from: from.name, to: to.name, fromCode: from.code, toCode: to.code,
      date, date_to: modal.querySelector("#gDateTo").value,
      trains,
      seat_types: seats, seat_priority,
      start_time: st ? st.replace("T", " ") + ":00" : "",
      remind_minutes: Number(modal.querySelector("#gRemind").value) || 10,
      warm_minutes: Number(modal.querySelector("#gWarm").value) || 10,
      poll_seconds: Number(modal.querySelector("#gPoll").value) || 3,
      passenger_names: pax, pax_purpose: paxPurpose,
      status: edit ? edit.status : "idle",
    });
    const saved = await call("saveGrabTask", task);
    if (saved && saved.error) return toast("保存失败：" + saved.error, "err");
    close(); toast("已保存"); refreshGrab();
  };
  const savePresetBtn = modal.querySelector("#gPresetSave");
  if (savePresetBtn) savePresetBtn.onclick = async () => {
    const from = await resolveStation(modal, "gfrom"), to = await resolveStation(modal, "gto");
    if (!from || !to) return toast("先选出发/到达站", "err");
    const name = modal.querySelector("#gName").value.trim() || ("预设 " + (S.presets.length + 1));
    const trains = modal.querySelector("#gTrains").value.split(/[,，\s]+/).filter(Boolean).map((x) => x.toUpperCase());
    const pUnion = new Set(); trains.forEach((code) => (perTrain[code] || []).forEach((s) => pUnion.add(s)));
    const saved = await call("savePreset", {
      name, from: from.name, to: to.name,
      trains,
      seat_types: SEAT_CHOICES.filter((s) => pUnion.has(s)),
      seat_priority: trains.map((code) => code + "=" + [...(perTrain[code] || [])].join("/")).join(","),
      date: modal.querySelector("#gDate").value,
    });
    if (saved && saved.error) return toast("保存失败：" + saved.error, "err");
    toast("预设已保存"); refreshPresets();
  };
}

// 秒级刷新只在「除倒计时以外的内容」变化时重建整页；只变倒计时就原地改文本
export let _grabSig = "";

export function grabSig(tasks) {
  return JSON.stringify(tasks.map((t) => [t.id, t.name, t.from, t.to,
    t.trains, t.seat_types, t.running, t.status, t.result_msg, t.result_ok,
    t.result_at, t.armed, t.auto_fired]));
}

export function updateGrabCountdowns() {
  const grid = $("#gGrid");
  if (!grid) return;
  for (let i = 0; i < grid.children.length && i < S.grab.length; i++) {
    const el = grid.children[i].querySelector(".g-count");
    const txt = S.grab[i].countdown || "—";
    if (el && el.textContent !== txt) el.textContent = txt;
  }
}

export async function refreshGrab() {
  if (S.grabBusy) return;
  S.grabBusy = true;
  try {
    const r = await call("grabTicker");
    S.grab = r.tasks || [];
    $("#badge-grab").textContent = S.grab.filter((t) => t.running).length;
    if (S.page === "grab") {
      const sig = grabSig(S.grab);
      if (sig !== _grabSig) { _grabSig = sig; renderPage(); }
      else updateGrabCountdowns();
    }
  } catch (e) { /* ignore */ }
  S.grabBusy = false;
}

export async function refreshPresets() {
  if (S.presetBusy) return;
  S.presetBusy = true;
  try {
    const r = await call("listPresets");
    S.presets = r.presets || [];
    S.queryHistory = r.query_history || [];
  } catch (e) { /* ignore */ }
  S.presetBusy = false;
}
export const _DEFAULT_LC = {
  version: 1, passenger_names: [], from: "", to: "", fromCode: "", toCode: "",
  trains: [], seat_types: [], seat_priority: "", date: "", date_to: "",
  start_time: "", remind_minutes: 10, warm_minutes: 10, poll_seconds: 3,
  purpose_code: "ADULT", pax_purpose: {}, id: "", name: "", status: "idle",
};

registerPage("grab", renderGrab);
