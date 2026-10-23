// Run B2：生产 main.js + PYTHON 指向不存在的可执行文件。
// 在第一次失败广播到达渲染层的那一刻断言：启动失败只广播一次、没有附带
// backendDead（若 'exit' 在本平台也触发且无守卫，这里会出现第二条处理痕迹）。
const { app, BrowserWindow } = require("electron");
require("/home/hatch/workspace/12306-worktrees/refactor-p4b/electron/main.js");

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
function rec(name, ok, detail) {
  results.push({ name, ok: !!ok });
  console.log((ok ? "PASS" : "FAIL") + " | " + name + " | " + (detail || ""));
}

app.whenReady().then(async () => {
  let win = null;
  for (let i = 0; i < 40 && !win; i++) { await wait(100); win = BrowserWindow.getAllWindows()[0] || null; }
  if (!win) { console.error("no window"); app.exit(2); return; }
  const js = (e) => win.webContents.executeJavaScript(e);
  const count = (txt) => js(`Array.from(document.querySelectorAll('#logs .log-line')).filter(function(e){ return e.textContent.includes("${txt}"); }).length`);
  // 等第一条「启动失败」出现（spawn error 很快），出现后再多等 250ms 看有无第二路处理痕迹
  let seen = 0;
  for (let i = 0; i < 50 && !seen; i++) { await wait(80); seen = await count("Python 后端启动失败").catch(() => 0); }
  await wait(250);
  const failLines = await count("Python 后端启动失败");
  const deadLines = await count("后端已退出");
  rec("B2 spawn 失败只广播一次", seen >= 1 && failLines === 1, "failLines=" + failLines);
  rec("B2 无附带 backendDead", deadLines === 0, "deadLines=" + deadLines);
  const fails = results.filter((r) => !r.ok).length;
  console.log("SUMMARY pass=" + (results.length - fails) + "/" + results.length);
  app.exit(fails ? 1 : 0);
}).catch((e) => { console.error("HARNESS ERROR", e); app.exit(2); });
