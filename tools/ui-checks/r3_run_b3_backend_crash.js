// Run B3：生产 main.js + 崩溃版假后端。断言同一次进程死亡只产生一次
// backendDead 广播（#logs 里只出现一行「后端已退出（code 3）」），且在途
// RPC 被 reject 一次、文案带 code 3。
const { app, BrowserWindow } = require("electron");
require("/home/hatch/workspace/12306-worktrees/refactor-p4b/electron/main.js");

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
function rec(name, ok, detail) {
  results.push({ name, ok: !!ok });
  console.log((ok ? "PASS" : "FAIL") + " | " + name + " | " + (detail || ""));
}

app.whenReady().then(async () => {
  await wait(1600);
  const win = BrowserWindow.getAllWindows()[0];
  if (!win) { console.error("no window"); app.exit(2); return; }
  const js = (e) => win.webContents.executeJavaScript(e);
  // 在后端崩溃（启动后 2.5s）前发起一个永不回包的调用
  await js(`window.__probe = null;
    window.api.call("getSettings", {}).then(function(){ window.__probe = { s: "resolved" }; }, function(e){ window.__probe = { s: "rejected", msg: String(e && e.message || e) }; });
    true`);
  await wait(2900); // 崩溃发生在约 2.5s，backendDead 广播随后到；首轮重启的新实例 5s 才再崩，断言窗口内只有一次
  // 只数 backendDead 广播专属行（「，自动重启中」后缀）：在途 RPC 被 reject 后
  // store.call 记的失败日志也含「后端已退出（code 3）」字样，不能混计。
  const deadLines = await js(`Array.from(document.querySelectorAll('#logs .log-line')).filter(function(e){ return e.textContent.includes("后端已退出（code 3），自动重启中"); }).length`);
  rec("B3 backendDead 只广播一次", deadLines === 1, "lines=" + deadLines);
  const probe = await js(`window.__probe`);
  rec("B3 在途 RPC 被 reject 且文案带 code 3", !!probe && probe.s === "rejected" && probe.msg.includes("code 3"), JSON.stringify(probe));
  const fails = results.filter((r) => !r.ok).length;
  console.log("SUMMARY pass=" + (results.length - fails) + "/" + results.length);
  app.exit(fails ? 1 : 0);
}).catch((e) => { console.error("HARNESS ERROR", e); app.exit(2); });
