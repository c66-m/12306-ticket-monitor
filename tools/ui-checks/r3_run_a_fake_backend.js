// Run A：脚本化 ipcMain 假后端 + 生产 preload + 真实 index.html/mini.html，
// 断言 R3 前端簇第 3/4/5/6/7 条的渲染层行为。
const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");

const REPO = "/home/hatch/workspace/12306-worktrees/refactor-p4b";
let failPax = true;      // 初始即失败：S.pax 保持空，表单预拉取必走失败路径
let failSearch = false;
let tasks = [
  { name: "待核对任务", from: "北京南", to: "上海虹桥", trains: ["G1"], seat_types: ["二等座"], dates: ["2026-10-24"], status: "ambiguous", status_label: "待核对", message: "", uid: "u1" },
  { name: "监控中任务", from: "北京", to: "天津", trains: ["C1"], seat_types: ["一等座"], dates: ["2026-10-24"], status: "monitoring", status_label: "监控中", message: "", uid: "u2" },
  { name: "删不掉任务", from: "郑州", to: "西安", trains: ["G2"], seat_types: ["二等座"], dates: ["2026-10-24"], status: "paused", status_label: "已暂停", message: "", uid: "u3" },
  { name: "空任务甲", from: "", to: "", trains: [], seat_types: [], dates: [], status: "paused", status_label: "已暂停", message: "", uid: "u4" },
  { name: "空任务乙", from: "", to: "", trains: [], seat_types: [], dates: [], status: "paused", status_label: "已暂停", message: "", uid: "u5" },
];

ipcMain.handle("rpc", async (_e, method, params) => {
  switch (method) {
    case "tasks.list": return { tasks, engineRunning: false };
    case "passengers.list":
      if (failPax) throw new Error("后端抖动中");
      return { passengers: [{ name: "张三", id_no: "110***********1234", mobile: "138****0000", id_type_code: "1", is_default: true, is_adult: true }], stamp: "st1" };
    case "stations.search":
      if (failSearch) throw new Error("搜索服务不可用");
      return { stations: [{ name: "北京南", code: "VNP", kind: "" }] };
    case "tasks.delete": {
      const n = params && params.name;
      if (n === "删不掉任务" || n === "空任务乙") return { error: "后端拒绝删除：" + n };
      tasks = tasks.filter((t) => t.name !== n);
      return { ok: true };
    }
    case "history.list": return { records: [] };
    case "problems.list": return { logs: [] };
    case "presets.list": return { presets: [] };
    default: return {};
  }
});
ipcMain.handle("win-top", () => false);
ipcMain.handle("mini-open", () => true);
ipcMain.handle("mini-close", () => true);
ipcMain.on("win", () => {});
ipcMain.on("mini-win", () => {});

const results = [];
function rec(name, ok, detail) {
  results.push({ name, ok: !!ok });
  console.log((ok ? "PASS" : "FAIL") + " | " + name + " | " + (detail || ""));
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 1440, height: 900, show: false,
    webPreferences: { preload: path.join(REPO, "electron", "preload.js"), contextIsolation: true, nodeIntegration: false },
  });
  const js = async (e) => {
    try { return await win.webContents.executeJavaScript(e); }
    catch (err) { console.log("JSFAIL | " + String(e).slice(0, 110) + " | " + err.message); throw err; }
  };
  await win.loadFile(path.join(REPO, "ui", "index.html"));
  await wait(2500);

  // ---- 第 6 条：preload 暴露收敛 + 退订（主窗） ----
  const a6 = await js(`({ ns: window.api._NS_TO_OLD, ons: window.api._OLD_TO_NS, callT: typeof window.api.call, unsubT: typeof window.api.onPush(function(){}), pinT: typeof window.api.onMiniPin(function(){}) })`);
  rec("A6 映射表不再暴露", a6.ns === undefined && a6.ons === undefined, JSON.stringify(a6));
  rec("A6 onPush/onMiniPin 返回退订函数", a6.unsubT === "function" && a6.pinT === "function" && a6.callT === "function", JSON.stringify(a6));
  await js(`window.__pc = 0; window.__unsub = window.api.onPush(function(m){ if (m && m.method === "__probe") window.__pc++; }); true`);
  win.webContents.send("py-push", { method: "__probe", params: {} });
  await wait(250);
  const pc1 = await js(`window.__pc`);
  await js(`window.__unsub();`);
  win.webContents.send("py-push", { method: "__probe", params: {} });
  await wait(250);
  const pc2 = await js(`window.__pc`);
  rec("A6 退订后不再投递", pc1 === 1 && pc2 === 1, pc1 + "->" + pc2);

  // ---- 第 6 条：mini.html 同一 preload ----
  const mini = new BrowserWindow({
    width: 460, height: 420, show: false,
    webPreferences: { preload: path.join(REPO, "electron", "preload.js"), contextIsolation: true, nodeIntegration: false },
  });
  await mini.loadFile(path.join(REPO, "ui", "mini.html"));
  await wait(1200);
  const m6 = await mini.webContents.executeJavaScript(`({ ns: window.api._NS_TO_OLD, pinT: typeof window.api.onMiniPin(function(){}), pushT: typeof window.api.onPush(function(){}) })`);
  rec("A6 mini 窗 preload 同口径", m6.ns === undefined && m6.pinT === "function" && m6.pushT === "function", JSON.stringify(m6));
  mini.close();

  // ---- 第 5 条：统计条待核对 + 点击过滤 ----
  await js(`document.querySelector('.nav-item[data-page="monitor"]').click()`);
  await wait(1000);
  const stTxt = await js(`(document.querySelector('#mStat') || {}).textContent || ""`);
  rec("A5 统计条单列待核对", stTxt.includes("待核对"), stTxt.slice(0, 90));
  const amb = await js(`(function(){ var b = document.querySelector('#mAmb b'); return b ? { n: b.textContent, cls: b.className } : null; })()`);
  rec("A5 待核对计数=1 且 warn 色", !!amb && amb.n === "1" && amb.cls.includes("warn"), JSON.stringify(amb));
  await js(`document.querySelector('#mAmb').click()`);
  await wait(400);
  const rows1 = await js(`Array.from(document.querySelectorAll('#mTasks .task')).map(function(e){ return e.dataset.name; })`);
  rec("A5 点击过滤只剩待核对", rows1.length === 1 && rows1[0] === "待核对任务", JSON.stringify(rows1));
  await js(`document.querySelector('#mAmb').click()`);
  await wait(400);
  const rows2 = await js(`document.querySelectorAll('#mTasks .task').length`);
  rec("A5 再点取消过滤", rows2 === 5, "rows=" + rows2);

  // ---- 第 4 条：搜旧名「监控任务」命中直接抢票页 ----
  await js(`(function(){ var i = document.querySelector('#globalSearch'); i.focus(); i.value = "监控任务"; i.dispatchEvent(new Event("input", { bubbles: true })); })()`);
  await wait(700);
  const ddNames = await js(`Array.from(document.querySelectorAll('#searchDD .it b')).map(function(e){ return e.textContent; })`);
  rec("A4 旧名搜出页面建议", ddNames.includes("直接抢票"), JSON.stringify(ddNames));
  await js(`(function(){ var i = document.querySelector('#globalSearch'); i.value = ""; i.dispatchEvent(new Event("input", { bubbles: true })); })()`);

  // ---- 第 3+4 条：listPassengers 失败时表单照常打开 + 标题派生 ----
  await js(`document.querySelector('#mNew').click()`);
  await wait(900);
  const h3 = await js(`(document.querySelector('#modal h3') || {}).textContent || ""`);
  rec("A4 新建弹窗标题=新建直接抢票任务", h3 === "新建直接抢票任务", h3);
  rec("A3 乘车人拉取失败表单仍打开", h3.length > 0, h3);

  // ---- 第 3 条：station 联想失败清空旧候选 ----
  await js(`(function(){ var inp = document.querySelector('#modal [data-st="from"] input'); inp.focus(); inp.value = "北京"; inp.dispatchEvent(new Event("input", { bubbles: true })); })()`);
  await wait(700);
  const ddq = `(function(){ var dd = document.querySelector('#modal [data-st="from"] .station-dd'); return { hidden: dd.classList.contains("hidden"), html: dd.innerHTML }; })()`;
  const dd1 = await js(ddq);
  rec("A3 联想候选正常显示", !dd1.hidden && dd1.html.includes("北京南"), JSON.stringify(dd1).slice(0, 120));
  failSearch = true;
  await js(`(function(){ var inp = document.querySelector('#modal [data-st="from"] input'); inp.value = "上海"; inp.dispatchEvent(new Event("input", { bubbles: true })); })()`);
  await wait(700);
  const dd2 = await js(ddq);
  rec("A3 联想失败清空不残留", dd2.hidden && dd2.html === "", JSON.stringify(dd2).slice(0, 120));
  failSearch = false;
  await js(`document.querySelector('#modal #fCancel').click()`);
  await wait(300);

  // ---- 第 3 条：refreshPax 失败保留旧缓存 ----
  failPax = false;
  await js(`document.querySelector('.nav-item[data-page="pax"]').click()`);
  await wait(500);
  await js(`window.refreshAll()`);
  await wait(800);
  const pax1 = await js(`(document.querySelector('#pList') || {}).textContent || ""`);
  rec("A3 乘车人加载成功", pax1.includes("张三"), pax1.slice(0, 60));
  failPax = true;
  await js(`window.refreshAll()`);
  await wait(800);
  const pax2 = await js(`(document.querySelector('#pList') || {}).textContent || ""`);
  rec("A3 刷新失败保留旧缓存", pax2.includes("张三"), pax2.slice(0, 60));

  // ---- 第 7 条：批量删除逐项核对 ----
  await js(`document.querySelector('.nav-item[data-page="monitor"]').click()`);
  await wait(900);
  await js(`document.querySelector('#mMgrBtn').click()`);
  await wait(400);
  await js(`document.querySelector('.task[data-name="删不掉任务"]').click()`);
  await js(`document.querySelector('.task[data-name="监控中任务"]').click()`);
  await wait(300);
  const selN = await js(`(document.querySelector('#mSelN') || {}).textContent || ""`);
  rec("A7 已选 2 个", selN.includes("已选 2"), selN);
  await js(`document.querySelector('#mDelSel').click()`);
  await wait(400);
  await js(`document.querySelector('#modal [data-a="yes"]').click()`);
  let toastTxt = "";
  for (let i = 0; i < 30; i++) { await wait(120); toastTxt = await js(`(document.querySelector('#toast') || {}).textContent || ""`); if (toastTxt.includes("删除失败")) break; }
  rec("A7 删除所选部分失败汇总", toastTxt.includes("已删除 1 个任务，1 个删除失败") && toastTxt.includes("删不掉任务"), toastTxt);
  await wait(600);
  await js(`document.querySelector('#mDelDirty').click()`);
  await wait(400);
  await js(`document.querySelector('#modal [data-a="yes"]').click()`);
  toastTxt = "";
  for (let i = 0; i < 30; i++) { await wait(120); toastTxt = await js(`(document.querySelector('#toast') || {}).textContent || ""`); if (toastTxt.includes("删除失败")) break; }
  rec("A7 删除空任务部分失败汇总", toastTxt.includes("已删除 1 个空任务，1 个删除失败") && toastTxt.includes("空任务乙"), toastTxt);

  const fails = results.filter((r) => !r.ok).length;
  console.log("SUMMARY pass=" + (results.length - fails) + "/" + results.length);
  app.exit(fails ? 1 : 0);
}).catch((e) => { console.error("HARNESS ERROR", e); app.exit(2); });
