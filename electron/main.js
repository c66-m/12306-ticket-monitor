// Electron 主进程：无边框窗口 + Python 子进程（JSON-RPC over stdin/stdout）
const { app, BrowserWindow, ipcMain } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const readline = require("readline");

const PY_DIR = path.join(__dirname, "..");          // C:\develep\产物\12306
let quitting = false;                                 // 用户退出：不再自动重启 Python
const UI_DIR = path.join(PY_DIR, "ui");
let win = null;
let py = null;

// 广播给所有窗口：小窗打开时主窗是隐藏的，日志/状态两个窗口都要收到
function broadcast(channel, payload) {
  for (const w of BrowserWindow.getAllWindows()) {
    if (!w.isDestroyed()) w.webContents.send(channel, payload);
  }
}

// ---------- JSON-RPC 桥 ----------
const pending = new Map();   // id -> {resolve, reject}
let rpcId = 0;

function pyCall(method, params) {
  return new Promise((resolve, reject) => {
    if (!py || !py.stdin.writable) return reject(new Error("Python 后端正在重启，请稍后重试"));
    if (process.env.DSH_DEBUG) console.log("[rpc]", method, "->", JSON.stringify(params || {}).slice(0, 120));
    const id = ++rpcId;
    pending.set(id, { resolve, reject });
    try { py.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n"); }
    catch (e) { pending.delete(id); return reject(new Error("Python 后端已退出（" + (e && e.message) + "）")); }
    // 60s 超时兜底（查票/会话检查可能慢）
    setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        reject(new Error(method + " 超时"));
      }
    }, 60000);
  });
}

function startPython() {
  const exe = process.env.PYTHON || "python";
  // -X utf8：让 Python 进程从启动起就用 UTF-8（中文 Windows 默认 GBK，会把中文参数变乱码）
  py = spawn(exe, ["-X", "utf8", "api.py"], {
    cwd: PY_DIR,
    windowsHide: true,
    stdio: ["pipe", "pipe", "pipe"],
  });
  // EPIPE 兜底：py 已退出瞬间 stdin 写失败会触发 'error'，吞掉由 pyCall 的 try/catch 走 reject
  py.stdin.on("error", () => {});

  const rl = readline.createInterface({ input: py.stdout, crlfDelay: Infinity });
  rl.on("line", (line) => {
    if (!line.trim()) return;
    let msg;
    try { msg = JSON.parse(line); } catch { return; }
    if (msg.id !== undefined && (msg.result !== undefined || msg.error !== undefined)) {
      // RPC 响应
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        if (msg.error) p.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
        else p.resolve(msg.result);
      }
    } else if (msg.method) {
      // 主动推送（日志 / 状态）
      broadcast("py-push", { method: msg.method, params: msg.params || {} });
    }
  });

  py.stderr.on("data", (d) => {
    // Python stderr 只记不崩（traceback 在这里）；控制台启动时落 _elog.txt/_eerr.txt，
    // 日志页同时收一份（api.py 的 _warn 走这条路）。
    console.error("[py]", d.toString());
    broadcast("py-push", { method: "log", params: { line: d.toString(), tag: "err" } });
  });

  py.on("exit", (code) => {
    // 正常退出（code 0/null）不刷屏；崩溃时留痕，配合上面的 stderr 转发定位
    if (code) console.error("[py] exited", code);
    py = null;
    // 崩溃前未回包的在途 RPC 全部立刻失败，前端不会干等 60 秒超时
    for (const [, p] of pending) p.reject(new Error("Python 后端已退出（code " + code + "）"));
    pending.clear();
    broadcast("py-push", { method: "backendDead", params: { code } });
    // 自动重启：应用退出（window-all-closed 收尾）时不再拉起
    if (!quitting) {
      broadcast("py-push", { method: "backendRestart", params: {} });
      setTimeout(() => { if (!quitting) { startPython(); broadcast("py-push", { method: "backendRestarted", params: {} }); } }, 500);
    }
  });
}

// ---------- IPC ----------
ipcMain.handle("rpc", (_e, method, params) => pyCall(method, params));
// 置顶开关：返回切换后的状态（前端据此同步图标）
ipcMain.handle("win-top", () => {
  if (!win) return false;
  win.setAlwaysOnTop(!win.isAlwaysOnTop());
  return win.isAlwaysOnTop();
});
ipcMain.on("win", (_e, action) => {
  if (!win) return;
  if (action === "min") win.minimize();
  else if (action === "max") { win.isMaximized() ? win.unmaximize() : win.maximize(); }
  else if (action === "full") win.setFullScreen(!win.isFullScreen());
  else if (action === "close") win.close();
});

// ---------- 迷你小窗（B7 第二步）----------
// 打开小窗时隐藏主窗，界面只留小窗；关掉小窗自动还原主窗（对齐 gui.py 的 open_mini 语义）。
// 小窗没有主窗的页面导航，返回按钮由主进程代发一个 goto 推送。
let mini = null;

async function openMini() {
  if (mini && !mini.isDestroyed()) {
    if (mini.isMinimized()) mini.restore();
    mini.focus();
    return true;
  }
  // 外观是主窗 localStorage 里的数据，小窗是另一个渲染进程——开局注入，省得两套主题不同步
  let appear = "";
  try {
    if (win && !win.isDestroyed()) {
      appear = await win.webContents.executeJavaScript("localStorage.getItem('x12306.appear') || ''");
    }
  } catch { /* 拿不到就用默认 */ }
  mini = new BrowserWindow({
    width: 460,
    height: 420,
    minWidth: 340,
    minHeight: 260,
    frame: false,
    backgroundColor: "#00000000",
    icon: path.join(__dirname, "icon.ico"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      additionalArguments: ["--appear=" + (appear || "")],
    },
  });
  mini.loadFile(path.join(UI_DIR, "mini.html"));
  mini.on("closed", () => {
    mini = null;
    if (win && !win.isDestroyed()) {
      win.show();
      win.focus();
      // 主窗在隐藏期间轮询被浏览器降频，回来先对齐一次数据
      win.webContents.executeJavaScript("window.refreshAll && window.refreshAll()").catch(() => {});
    }
  });
  if (win && !win.isDestroyed()) win.hide();
  return true;
}

ipcMain.handle("mini-open", () => openMini());
ipcMain.handle("mini-close", () => {
  if (mini && !mini.isDestroyed()) mini.close();
  return true;
});
ipcMain.on("mini-win", (e, action) => {
  const w = BrowserWindow.fromWebContents(e.sender);
  if (!w || w.isDestroyed()) return;
  if (action === "min") w.minimize();
  else if (action === "top") {
    w.setAlwaysOnTop(!w.isAlwaysOnTop());
    const on = w.isAlwaysOnTop();
    if (process.env.DSH_DEBUG) console.error("[mini-win] top ->", on, "win=", w.id, "visible=", w.isVisible());
    e.sender.send("mini-pin", on);
  }
  else if (action === "close") w.close();   // closed 事件里会还原主窗
});

function createWindow() {
  win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1180,
    minHeight: 720,
    frame: false,             // 无边框（顶栏自绘）
    backgroundColor: "#00000000",
    icon: path.join(__dirname, "icon.ico"),   // 多尺寸 ico（16…256）
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.loadFile(path.join(UI_DIR, "index.html"));
  // F11 = 全屏切换（无边框窗口自绘顶栏，需要显式绑定）
  win.webContents.on("before-input-event", (_e, input) => {
    if (input.type === "keyDown" && input.key === "F11") win.setFullScreen(!win.isFullScreen());
  });
  // 渲染进程 console 转发到主进程 stdout（诊断用，DSH_DEBUG=1 时开启）
  if (process.env.DSH_DEBUG) {
    win.webContents.on("console-message", (_e, level, message, line, sourceId) => {
      console.log("[renderer]", level, message, line, sourceId);
    });
  }
  win.webContents.on("did-finish-load", () => {
    if (process.env.DSH_DEBUG) console.log("[main] page loaded, resending ready");
    // 页面可能错过早期 ready 推送——重发一次
    win.webContents.send("py-push", { method: "ready", params: { pid: py && py.pid, resend: true } });
  });
}

app.whenReady().then(() => {
  startPython();
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  quitting = true;
  if (py) { try { py.kill(); } catch {} }
  app.quit();
});
