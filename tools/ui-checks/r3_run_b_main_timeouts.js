// Run B：生产 electron/main.js（require 进本进程）+ 假 python（PYTHON 环境变量），
// 断言 R3 第 1 条（pyCall 超时语义/放宽/迟到丢弃）与第 2 条 stderr 行缓冲部分。
const { app, BrowserWindow } = require("electron");
require("/home/hatch/workspace/12306-worktrees/refactor-p4b/electron/main.js");

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
function rec(name, ok, detail) {
  results.push({ name, ok: !!ok });
  console.log((ok ? "PASS" : "FAIL") + " | " + name + " | " + (detail || ""));
}

app.whenReady().then(async () => {
  await wait(1800);
  const win = BrowserWindow.getAllWindows()[0];
  if (!win) { console.error("no window"); app.exit(2); return; }
  const js = (e) => win.webContents.executeJavaScript(e);
  await wait(2200); // stderr 脚本在 0.8/1.5/2.2s 写完（相对 python 启动）

  // ---- 第 2 条：stderr 按行广播 ----
  const lines = await js(`Array.from(document.querySelectorAll('#logs .log-line')).map(function(e){ return { t: e.textContent, cls: e.className }; })`);
  const jia = lines.find((l) => l.t.includes("中文日志行甲"));
  rec("B2 跨 chunk 汉字行完整无乱码", !!jia && !jia.t.includes("�"), jia ? jia.t.slice(0, 60) : "未找到该行");
  const yi = lines.find((l) => l.t.includes("警告行乙"));
  const bing = lines.find((l) => l.t.includes("错误行丙"));
  rec("B2 同 chunk 两行被拆开且级别各自判定", !!yi && !!bing && yi.cls.includes("t-warn") && bing.cls.includes("t-err"),
    JSON.stringify({ yi: yi && yi.cls, bing: bing && bing.cls }));
  rec("B2 无合并行", !lines.some((l) => l.t.includes("警告行乙") && l.t.includes("错误行丙")), "");

  // ---- 第 1 条：超时语义 ----
  await js(`window.__probe = { a: null, b: null }; window.__t0 = Date.now();
    window.api.call("getSettings", {}).then(function(){ window.__probe.a = { s: "resolved", at: Date.now() }; }, function(e){ window.__probe.a = { s: "rejected", at: Date.now(), msg: String(e && e.message || e) }; });
    window.api.call("queryTickets", { fromCode: "VNP", toCode: "SHH", date: "2026-10-24", purpose: "ADULT" }).then(function(r){ window.__probe.b = { s: "resolved", at: Date.now(), r: JSON.stringify(r) }; }, function(e){ window.__probe.b = { s: "rejected", at: Date.now(), msg: String(e && e.message || e) }; });
    true`);
  let probe = null;
  for (let i = 0; i < 95; i++) {
    await wait(1000);
    probe = await js(`window.__probe`);
    if (probe && probe.b) break;
  }
  const a = probe && probe.a, b = probe && probe.b;
  rec("B1 默认方法 60s 超时且文案注明后端仍在执行", !!a && a.s === "rejected" && a.msg.includes("后端仍在执行") && a.msg.includes("将被丢弃"),
    a ? a.s + " @" + Math.round((a.at - 0) / 1000) + " msg=" + a.msg : "a 未 settle");
  const t0 = await js(`window.__t0`);
  rec("B1 a 在约 60s 被 reject", !!a && Math.abs(a.at - t0 - 60000) < 6000, a ? "dt=" + (a.at - t0) : "");
  rec("B1 queryTickets 70s 回包成功 resolve（120s 放宽生效）", !!b && b.s === "resolved" && b.r.includes("trains"),
    b ? b.s + " dt=" + (b.at - t0) : "b 未 settle");
  // settings.get 的迟到响应（约在 probe 后 62s 到达）已被丢弃，主进程仍可正常服务
  const alive = await js(`window.api.call("getAccount", {}).then(function(){ return "ok"; }, function(e){ return "err:" + (e && e.message); })`);
  rec("B1 迟到响应丢弃后主进程仍可用", alive === "ok", alive);

  const fails = results.filter((r) => !r.ok).length;
  console.log("SUMMARY pass=" + (results.length - fails) + "/" + results.length);
  app.exit(fails ? 1 : 0);
}).catch((e) => { console.error("HARNESS ERROR", e); app.exit(2); });
