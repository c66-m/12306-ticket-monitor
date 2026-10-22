/* components/appearance.js —— 外观（主题/壁纸/主色/字体）常量、应用与加载。 */
import { S } from "../store.js";


// ================= 外观设置 =================
export const WALLS = { green: "#E8EBF2", blue: "#E7EEF9", sunset: "#F4ECE6" };
export const APPEAR_KEY = "x12306.appear";
export const APPEAR_DEF = {
  theme: "light", blur: 16, radius: 18, fs: 15,
  wall: "image", custom: "", accent: "#2E6BFF",
  glass: 55, density: "normal", motion: "on", logh: 8.6, font: "system",
};
export const ACCENTS = ["#2E6BFF", "#0EA5A4", "#7C5CFF", "#E2544E", "#C9781E", "#0F766E", "#B91C1C"];
export const FONTS = {
  system: '-apple-system, "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif',
  hei: '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif',
  song: '"SimSun", "Songti SC", "STSong", serif',
  kai: '"KaiTi", "STKaiti", "Kaiti SC", serif',
  mono: '"Cascadia Mono", Consolas, "Microsoft YaHei UI", monospace',
};

export function hexToRgbStr(h) {
  const m = /^#?(..)(..)(..)$/.exec(h || "");
  if (!m) return null;
  return parseInt(m[1], 16) + "," + parseInt(m[2], 16) + "," + parseInt(m[3], 16);
}
export function applyAppearance() {
  const a = S.appear = Object.assign({}, APPEAR_DEF, S.appear || {});
  const rt = document.documentElement;
  rt.dataset.theme = a.theme;
  rt.dataset.density = a.density;
  rt.dataset.motion = a.motion;
  rt.style.setProperty("--blur", a.blur + "px");
  rt.style.setProperty("--radius", a.radius + "px");
  rt.style.setProperty("--fs", a.fs + "px");
  rt.style.setProperty("--log-h", (a.logh || 8.6) + "rem");
  rt.style.setProperty("--font", FONTS[a.font] || FONTS.system);
  // 玻璃透明度：55 → 1.0 系数（白 55% 基准）
  const ga = Math.max(0.15, (Number(a.glass) || 55) / 55);
  rt.style.setProperty("--glass-a", (a.theme === "dark" ? 0.52 : 0.55) * ga);
  const rgb = hexToRgbStr(a.accent);
  if (rgb) {
    rt.style.setProperty("--accent", a.accent);
    rt.style.setProperty("--accent-grad", "linear-gradient(135deg," + a.accent + "cc," + a.accent + ")");
    rt.style.setProperty("--accent-soft", "rgb(" + rgb + ")");
  }
  document.body.dataset.wall = a.wall;
  if (a.wall === "custom" && a.custom) document.body.style.setProperty("--wall-img", "url('" + a.custom + "')");
  else if (a.wall === "flat-sunset") { /* noop */ }
  document.body.style.setProperty("--bg-flat", a.wall === "flat" ? "linear-gradient(140deg,#DCE4F5,#EEF1F8)"
    : a.wall === "flat-sunset" ? "linear-gradient(150deg,#F3E4D6,#EFE9F6)"
    : a.wall === "flat-green" ? "linear-gradient(150deg,#DDEBE2,#EAF3EC)" : "linear-gradient(140deg,#DCE4F5,#EEF1F8)");
  try { localStorage.setItem(APPEAR_KEY, JSON.stringify(a)); } catch (e) { /* 忽略 */ }
}
export function loadAppearance() {
  try { S.appear = Object.assign({}, APPEAR_DEF, JSON.parse(localStorage.getItem(APPEAR_KEY) || "{}")); }
  catch (e) { S.appear = Object.assign({}, APPEAR_DEF); }
  if (S.appear.wall === "flat-sunset" || S.appear.wall === "flat-green") document.body.dataset.wall = "flat";
  if (S.appear.theme === "sys") {
    const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    S.appear.theme = dark ? "dark" : "light";
  }
  applyAppearance();
}
