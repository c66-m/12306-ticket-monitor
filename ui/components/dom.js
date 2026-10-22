/* components/dom.js —— 最基础的 DOM 工具（$ / esc），不依赖任何模块。 */


// ================= 基础组件 =================
export const $ = (sel, root) => (root || document).querySelector(sel);
export const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
