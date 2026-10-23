#!/usr/bin/env node
// 假 Python 后端（供生产 electron/main.js 经 PYTHON 环境变量拉起）：
// - stderr 按字节脚本写：整行被劈在多字节汉字内部 / 两行挤一个 chunk / 带 \r\n
// - RPC：tickets.query 70s 后回包（验 120s 放宽）；settings.get 62s 后回包
//   （验默认 60s 超时 + 迟到响应丢弃）；其余立即回最小形状。
const readline = require("readline");

const line1 = Buffer.from("[INFO] 中文日志行甲\n", "utf8");
const cut = line1.length - 2; // 劈在「甲」的三字节内部
setTimeout(() => process.stderr.write(line1.subarray(0, cut)), 800);
setTimeout(() => process.stderr.write(line1.subarray(cut)), 1500);
setTimeout(() => process.stderr.write("[WARNING] 警告行乙\r\n[ERROR] 错误行丙\n"), 2200);

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.id === undefined || !msg.method) return;
  const send = (result) => {
    try { process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: msg.id, result }) + "\n"); } catch { /* 已退出 */ }
  };
  const m = msg.method;
  if (m === "tickets.query") { setTimeout(() => send({ trains: [] }), 70000); return; }
  if (m === "settings.get") { setTimeout(() => send({}), 62000); return; }
  if (m === "tasks.list") return send({ tasks: [], engineRunning: false });
  if (m === "passengers.list") return send({ passengers: [], stamp: "s" });
  if (m === "history.list") return send({ records: [] });
  if (m === "problems.list") return send({ logs: [] });
  if (m === "presets.list") return send({ presets: [] });
  send({});
});
