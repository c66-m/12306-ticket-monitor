const { contextBridge, ipcRenderer } = require("electron");

// ---- RPC 方法名映射（P4-B A-2 / C4 第二步）----
// _NS_TO_OLD：新命名空间名 -> 平台旧名，与 api.py 中同名表逐条一致（后端为权威，
// 增删方法时两处同步改）。前端历史调用点全部使用旧名；preload 在 call() 里把
// 旧名翻译为新命名空间名再发给后端，新名则原样直通。后端 rpc_dispatch 对
// 新旧名都可解析（METHODS / _NS_METHODS），翻译不改变分发结果。
const _NS_TO_OLD = {
  "tasks.list": "listMonitorTasks",
  "tasks.create": "createTask",
  "tasks.set_status": "setTaskStatus",
  "tasks.delete": "deleteTask",
  "tasks.pause_all": "pauseAll",
  "tasks.resume_all": "resumeAll",
  "problems.list": "listProblemLogs",
  "problems.clear": "clearProblemLogs",
  "grab.save": "saveGrabTask",
  "grab.delete": "deleteGrabTask",
  "grab.rename": "renameGrabTask",
  "grab.start": "startGrab",
  "grab.stop": "stopGrab",
  "grab.result": "grabResult",
  "grab.ticker": "grabTicker",
  "tickets.query": "queryTickets",
  "seats.priority_feedback": "seatPriorityFeedback",
  "stations.search": "searchStation",
  "presets.list": "listPresets",
  "presets.save": "savePreset",
  "presets.delete": "deletePreset",
  "history.push_query": "pushQueryHistory",
  "history.list": "listHistory",
  "history.reconcile": "reconcileHistory",
  "network.ip_loc": "ipLocate",
  "update.check": "checkUpdate",
  "passengers.list": "listPassengers",
  "passengers.save": "savePassengers",
  "passengers.detail": "passengerDetail",
  "passengers.sync_12306": "sync12306Passengers",
  "session.check": "checkSession",
  "session.relogin": "relogin",
  "session.account": "getAccount",
  "settings.get": "getSettings",
  "settings.save": "saveSettings",
  "settings.test_email": "testEmail",
  "engines.start": "startEngine",
  "engines.stop": "stopEngine",
  "system.shutdown": "shutdown",
};

// 旧名 -> 新名：由 _NS_TO_OLD 反转生成（不手写第二份表，避免两表漂移）。
const _OLD_TO_NS = {};
for (const ns of Object.keys(_NS_TO_OLD)) _OLD_TO_NS[_NS_TO_OLD[ns]] = ns;

// 对外传入的方法名解析：旧名转新名；新名与未知名字原样透传
// （未知名字由后端回「未知方法」错误，与映射前行为一致，preload 不拦截）。
function _resolveMethod(method) {
  return _OLD_TO_NS[method] || method;
}

contextBridge.exposeInMainWorld("api", {
  // 请求-响应：window.api.call("queryTickets", {...})，旧名自动转新名；
  // 也可直接传新名，如 window.api.call("tickets.query", {...})
  call: (method, params) => ipcRenderer.invoke("rpc", _resolveMethod(method), params),
  // 名称映射表（与 api.py 的 _NS_TO_OLD 一致），供前端查名/调试
  _NS_TO_OLD: _NS_TO_OLD,
  _OLD_TO_NS: _OLD_TO_NS,
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
