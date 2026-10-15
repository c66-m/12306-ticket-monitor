const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("api", {
  // 请求-响应：window.api.call("queryTickets", {...})
  call: (method, params) => ipcRenderer.invoke("rpc", method, params),
  // 订阅 Python 主动推送（日志/状态/后端死亡）
  onPush: (cb) => {
    ipcRenderer.on("py-push", (_e, payload) => cb(payload));
  },
  // 无边框窗口控制：min / max / close
  win: (action) => ipcRenderer.send("win", action),
  // 置顶开关：resolve 为切换后的置顶状态
  top: () => ipcRenderer.invoke("win-top"),
  // 迷你小窗：打开 / 关闭 / 小窗内的窗口动作（min|top|close|main）
  mini: () => ipcRenderer.invoke("mini-open"),
  miniClose: () => ipcRenderer.invoke("mini-close"),
  miniWin: (action) => ipcRenderer.send("mini-win", action),
  // 小窗置顶状态回执
  onMiniPin: (cb) => { ipcRenderer.on("mini-pin", (_e, on) => cb(on)); },
  // 小窗开局参数：主进程用 additionalArguments 把主窗的外观 JSON 传过来
  // （localStorage 在 file:// 下不保证跨页共享，preload 是唯一可靠的传递点）
  appearArgs: (() => {
    const tag = "--appear=";
    const a = process.argv.find((x) => x.startsWith(tag));
    return a ? a.slice(tag.length) : "";
  })(),
});
