/* components/log.js —— 全局日志区（#logs）渲染与写入。 */
import { $, esc } from "./dom.js";


// 日志
export function makeLogLine(line, tag) {
  const d = new Date();
  const ts = d.toTimeString().slice(0, 8);
  const tagMap = { ok: "OK", warn: "WARN", err: "ERR", info: "INFO" };
  const t = tagMap[tag] || "INFO";
  const div = document.createElement("div");
  div.className = "log-line t-" + (tag || "info");
  div.innerHTML = '<span class="ts">' + ts + '<span class="tag">' + t + '</span><span>' + esc(line) + '</span>';
  return div;
}

export function pushLog(line, tag) {
  const logs = $("#logs");
  if (!logs) return;
  logs.appendChild(makeLogLine(line, tag));
  while (logs.children.length > 400) logs.firstChild.remove();
  logs.scrollTop = logs.scrollHeight;
}

// 批量日志：一次 DocumentFragment 插入、只滚一次（后端 150ms 攒批）
export function pushLogBatch(items) {
  const logs = $("#logs");
  if (!logs || !items || !items.length) return;
  const frag = document.createDocumentFragment();
  for (const it of items) frag.appendChild(makeLogLine(it.line || "", it.tag));
  logs.appendChild(frag);
  while (logs.children.length > 400) logs.firstChild.remove();
  logs.scrollTop = logs.scrollHeight;
}
