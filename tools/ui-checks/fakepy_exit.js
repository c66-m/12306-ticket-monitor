#!/usr/bin/env node
// 假后端崩溃版：2.5s 后以 code 3 退出；settings.get 永不回包（制造在途 RPC），
// 其余立即回最小形状。验第 2 条：exit 路径单次处理（一个 backendDead、
// 在途 RPC 只 reject 一次且文案带 code）。
const readline = require("readline");
setTimeout(() => process.exit(3), 2500);
const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.id === undefined || !msg.method) return;
  if (msg.method === "settings.get") return; // 永不回包
  const send = (result) => {
    try { process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: msg.id, result }) + "\n"); } catch { /* 已退出 */ }
  };
  const m = msg.method;
  if (m === "tasks.list") return send({ tasks: [], engineRunning: false });
  if (m === "passengers.list") return send({ passengers: [], stamp: "s" });
  if (m === "history.list") return send({ records: [] });
  if (m === "problems.list") return send({ logs: [] });
  if (m === "presets.list") return send({ presets: [] });
  send({});
});
