/* pages/settings.js —— 设置页（气泡 + 放大面板：外观 / 邮件通知）。 */
import { S, call, renderPage, registerPage, registerClosePanelHook } from "../store.js";
import { esc } from "../components/dom.js";
import { toast } from "../components/toast.js";
import { confirmBox } from "../components/modal.js";
import { APPEAR_DEF, ACCENTS, applyAppearance } from "../components/appearance.js";


// ================= 设置页 =================
// 设置气泡：只保留外观 / 邮件通知（其余设置项已按用户要求彻底移除）
export const SET_BUBBLES = [
  { key: "appear", ic: "🎨", name: "外观", desc: "主题、模糊、圆角、主色与背景" },
  { key: "mail", ic: "📧", name: "邮件通知", desc: "SMTP 服务器与订单通知" },
];

export function renderSettings(root) {
  root.innerHTML =
    '<div class="sec-head"><h3>设置</h3><span class="more">点击气泡查看与修改对应设置</span></div>' +
    '<div class="set-wrap set-bubbles">' + SET_BUBBLES.map((b) => {
      const st = statusOfSetting(b.key);
      return '<div class="set-bubble" data-set="' + b.key + '" title="点击打开">' +
        '<span class="b-ic">' + b.ic + "</span>" +
        '<span class="b-name">' + b.name + "</span>" +
        '<span class="b-desc">' + b.desc + "</span>" +
        (st ? '<span class="b-status">' + st + "</span>" : "") +
        "</div>";
    }).join("") + "</div>";
  root.querySelectorAll(".set-bubble").forEach((b) => {
    b.onclick = () => openSettingPanel(b.dataset.set, b);
  });
}

// 气泡底部小状态（邮件启用与否等）
export function statusOfSetting(key) {
  const s = S.settings || {};
  if (key === "mail") return s.notify_enabled ? "已启用" : "未启用";
  return "";
}

// 打开设置面板：从气泡位置放大到主区正中（15/20 ≈ 75%），关闭时缩回气泡
export function openSettingPanel(key, bubbleEl) {
  const panel = document.getElementById("setPanel");
  if (!panel) return;
  const html = settingsPanelHtml(key);
  if (!html) return;
  // 记录气泡中心坐标，作为放大/缩回的动画原点
  if (bubbleEl) {
    const r = bubbleEl.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const pr = panel.getBoundingClientRect();
    panel.style.setProperty("--ox", (cx - pr.left) + "px");
    panel.style.setProperty("--oy", (cy - pr.top) + "px");
  }
  // 清掉上一次关闭动画的残留监听（否则打开动画结束会被误判成关闭完成）
  panel.onanimationend = null;
  panel.classList.remove("closing", "done");
  panel.innerHTML =
    '<div class="set-panel-head"><h3>' + panelTitle(key) + "</h3>" +
    '<button class="btn btn-mini btn-ghost btn-panel-close" id="setPanelClose">✕ 关闭</button></div>' +
    '<div class="set-panel-content">' + html + "</div>";
  panel.classList.add("show");
  // 背景模糊遮罩：1s 渐变（透明→模糊）
  const bd = document.getElementById("setBackdrop");
  if (bd) bd.classList.add("show");
  panel.querySelector("#setPanelClose").onclick = closeSettingPanel;
  bindSettingsPanel(panel, key);
}

export function closeSettingPanel() {
  const panel = document.getElementById("setPanel");
  if (!panel) return;
  panel.onanimationend = null;
  // 背景模糊遮罩同步淡出
  const bd = document.getElementById("setBackdrop");
  if (bd) bd.classList.remove("show");
  // 未在展示状态时直接复位，不挂动画（goto 切页等场景）
  if (!panel.classList.contains("show")) {
    panel.classList.remove("closing", "done");
    return;
  }
  // 退出动画：缩小回气泡原点，动画结束后才真正隐藏
  panel.classList.add("closing");
  panel.onanimationend = (e) => {
    if (e.animationName === "setpop-out") {
      panel.classList.remove("show", "closing", "done");
      panel.onanimationend = null;
    }
  };
}

export function panelTitle(key) {
  const b = SET_BUBBLES.find((x) => x.key === key);
  return b ? b.name : key;
}

// 各面板 HTML（保留全部原字段与 id；事件在 bindSettingsPanel 统一绑定）
export function settingsPanelHtml(key) {
  const s = S.settings || {};
  const email = s.email || {};
  const a = S.appear || {};
  const wallOpts = [["image", "壁纸"], ["flat", "蓝白"], ["flat-sunset", "晚霞"], ["flat-green", "松绿"], ["custom", "自定义"]];
  if (key === "appear") return (
    '<div class="card glass glass-surface"><div class="appear-row"><label>主题</label><div class="seg" id="aTheme">' +
    ["light|浅色", "dark|深色", "sys|跟随系统"].map((x) => { const p = x.split("|"); return '<button data-v="' + p[0] + '"' + (a.theme === p[0] ? ' class="on"' : "") + ">" + p[1] + "</button>"; }).join("") +
    "</div></div>" +
    '<div class="appear-row"><label>模糊度</label><input type="range" id="aBlur" min="0" max="40" step="1" value="' + (a.blur ?? 16) + '"><span class="val" id="aBlurV">' + (a.blur ?? 16) + "px</span></div>" +
    '<div class="appear-row"><label>圆角</label><input type="range" id="aRadius" min="6" max="28" step="1" value="' + (a.radius ?? 18) + '"><span class="val" id="aRadiusV">' + (a.radius ?? 18) + "px</span></div>" +
    '<div class="appear-row"><label>玻璃透明度</label><input type="range" id="aGlass" min="15" max="100" step="1" value="' + (a.glass ?? 55) + '"><span class="val" id="aGlassV">' + (a.glass ?? 55) + "%</span></div>" +
    '<div class="appear-row"><label>字号</label><input type="range" id="aFs" min="13" max="18" step="1" value="' + (a.fs ?? 15) + '"><span class="val" id="aFsV">' + (a.fs ?? 15) + "px</span></div>" +
    '<div class="appear-row"><label>字体</label><select id="aFont" class="grow">' +
    [["system", "系统默认"], ["hei", "微软雅黑"], ["song", "宋体"], ["kai", "楷体"], ["mono", "等宽"]].map(([v, t]) => '<option value="' + v + '"' + ((a.font || "system") === v ? " selected" : "") + ">" + t + "</option>").join("") +
    "</select></div>" +
    '<div class="appear-row"><label>界面密度</label><div class="seg" id="aDensity">' +
    ["compact|紧凑", "normal|标准", "roomy|宽松"].map((x) => { const p = x.split("|"); return '<button data-v="' + p[0] + '"' + ((a.density || "normal") === p[0] ? ' class="on"' : "") + ">" + p[1] + "</button>"; }).join("") +
    "</div></div>" +
    '<div class="appear-row"><label>动效</label><div class="seg" id="aMotion">' +
    ["on|开启", "off|关闭"].map((x) => { const p = x.split("|"); return '<button data-v="' + p[0] + '"' + ((a.motion || "on") === p[0] ? ' class="on"' : "") + ">" + p[1] + "</button>"; }).join("") +
    "</div></div>" +
    '<div class="appear-row"><label>日志高度</label><input type="range" id="aLogh" min="5" max="22" step="0.2" value="' + (a.logh ?? 8.6) + '"><span class="val" id="aLoghV">' + (a.logh ?? 8.6) + "rem</span></div>" +
    '<div class="appear-row"><label>主色</label><div class="swatches" id="aAccent">' +
    ACCENTS.map((c) => '<span class="swatch' + (a.accent === c ? " on" : "") + '" data-v="' + c + '" style="background:' + c + '"></span>').join("") +
    "</div></div>" +
    '<div class="appear-row"><label>背景</label><div class="seg" id="aWall">' +
    wallOpts.map(([v, t]) => '<button data-v="' + v + '"' + (a.wall === v ? ' class="on"' : "") + ">" + t + "</button>").join("") +
    "</div></div>" +
    '<div class="appear-row" id="aWRow"' + (a.wall === "custom" ? "" : ' style="display:none"') + '><label>图片</label><input class="grow" id="aWallUrl" placeholder="https://... 或本地路径" value="' + esc(a.custom || "") + '"><button class="btn btn-mini btn-ghost" id="aWallPick">选文件</button><input type="file" id="aWallFile" accept="image/*" style="display:none"></div>' +
    '<div class="form-actions"><button class="btn btn-ghost" id="aReset">恢复默认外观</button><button class="btn btn-ghost" id="aWipe">清空本地数据</button></div></div>'
  );
  if (key === "mail") return (
    '<div class="card glass glass-surface"><div class="sec-head"><h3>邮件通知（SMTP）</h3><span class="status ' + (s.notify_enabled ? "succ" : "canc") + '">' + (s.notify_enabled ? "已启用" : "未启用") + "</span></div>" +
    '<div class="form-row"><label>启用</label><label class="check"><input type="checkbox" id="sEnabled"' + (email.enabled ? " checked" : "") + ">发送订单通知邮件</label></div>" +
    '<div class="form-row"><label>SMTP 服务器</label><input id="sHost" value="' + esc(email.smtp_host || "") + '" placeholder="smtp.qq.com"></div>' +
    '<div class="form-row"><label>端口</label><input type="number" id="sPort" value="' + esc(email.smtp_port || 465) + '"></div>' +
    '<div class="form-row"><label>发件邮箱</label><input id="sUser" value="' + esc(email.username || "") + '"></div>' +
    '<div class="form-row"><label>授权码</label><input type="password" id="sPwd" value="' + esc(email.password || "") + '" placeholder="******"></div>' +
    '<div class="form-row"><label>收件人（逗号分隔）</label><input id="sTo" value="' + esc((email.to || []).join(",")) + '"></div>' +
    '<div class="form-actions"><button class="btn btn-ghost" id="sTest">发送测试邮件</button>' +
    '<button class="btn btn-call" id="sSave">保存设置</button></div></div>'
  );
  return "";
}

// 面板事件绑定（原 renderSettings 的绑定逻辑整体迁移，查询范围改 panel）
export function bindSettingsPanel(panel, key) {
  if (key === "appear") bindAppearPanel(panel);
  if (key === "mail") bindMailPanel(panel);
}

export function bindMailPanel(p) {
  p.querySelector("#sSave").onclick = async () => {
    const emailNew = {
      enabled: p.querySelector("#sEnabled").checked,
      smtp_host: p.querySelector("#sHost").value.trim(),
      smtp_port: Number(p.querySelector("#sPort").value) || 465,
      username: p.querySelector("#sUser").value.trim(),
      password: p.querySelector("#sPwd").value,
      from: p.querySelector("#sUser").value.trim(),
      to: p.querySelector("#sTo").value.split(/[,，\s]+/).filter(Boolean),
    };
    if (!await saveSettings({ email: emailNew })) return;
    toast("设置已保存");
    // 保存后重开面板：refreshSettings 会重渲染气泡，面板浮层独立保留
    closeSettingPanel();
    openSettingPanel("mail");
  };
  p.querySelector("#sTest").onclick = async () => {
    toast("正在发送测试邮件…");
    const r = await call("testEmail");
    toast((r.message || "") + (r.ok ? "" : "（失败）"), r.ok ? "" : "err");
  };
}

export function bindAppearPanel(p) {
  const save = () => {
    applyAppearance();
    const content = document.getElementById("content");
    if (content) renderSettings(content);
  };
  p.querySelectorAll("#aTheme button").forEach((b) => b.onclick = () => { S.appear.theme = b.dataset.v; save(); });
  const bindRange = (id, key) => {
    const el = p.querySelector("#" + id), lab = p.querySelector("#" + id + "V");
    if (!el) return;
    el.oninput = () => { S.appear[key] = Number(el.value); lab.textContent = el.value + "px"; applyAppearance(); };
    el.onchange = () => save();
  };
  bindRange("aBlur", "blur"); bindRange("aRadius", "radius"); bindRange("aFs", "fs");
  const bindRange2 = (id, key, unit) => {
    const el = p.querySelector("#" + id), lab = p.querySelector("#" + id + "V");
    if (!el) return;
    el.oninput = () => { S.appear[key] = Number(el.value); lab.textContent = el.value + unit; applyAppearance(); };
    el.onchange = () => save();
  };
  bindRange2("aGlass", "glass", "%"); bindRange2("aLogh", "logh", "rem");
  const aFont = p.querySelector("#aFont");
  if (aFont) aFont.onchange = () => { S.appear.font = aFont.value; save(); };
  [["aDensity", "density"], ["aMotion", "motion"]].forEach(([bid, key]) => {
    p.querySelectorAll("#" + bid + " button").forEach((b) => b.onclick = () => { S.appear[key] = b.dataset.v; save(); });
  });
  p.querySelectorAll("#aAccent .swatch").forEach((b) => b.onclick = () => { S.appear.accent = b.dataset.v; save(); });
  p.querySelectorAll("#aWall button").forEach((b) => b.onclick = () => {
    S.appear.wall = b.dataset.v; applyAppearance();
    p.querySelector("#aWRow").style.display = b.dataset.v === "custom" ? "" : "none";
    p.querySelectorAll("#aWall button").forEach((x) => x.classList.toggle("on", x === b));
  });
  const wurl = p.querySelector("#aWallUrl");
  if (wurl) wurl.onchange = () => { S.appear.custom = wurl.value.trim(); save(); };
  const wfile = p.querySelector("#aWallFile");
  if (wfile) {
    p.querySelector("#aWallPick").onclick = () => wfile.click();
    wfile.onchange = () => {
      const f = wfile.files && wfile.files[0];
      if (!f) return;
      const fr = new FileReader();
      fr.onload = () => { S.appear.custom = String(fr.result); save(); toast("背景已更新"); };
      fr.readAsDataURL(f);
    };
  }
  p.querySelector("#aReset").onclick = () => { S.appear = Object.assign({}, APPEAR_DEF); save(); toast("外观已恢复默认"); };
  p.querySelector("#aWipe").onclick = async () => {
    if (!await confirmBox("清空本地数据？", "仅清除本机保存的外观偏好与缓存，不影响服务器上的任务与配置。")) return;
    try { localStorage.clear(); } catch (e) { /* 忽略 */ }
    toast("已清空本地数据"); setTimeout(() => location.reload(), 600);
  };
}

export async function refreshSettings() {
  try {
    S.settings = await call("getSettings");
    S.settingsStamp = (S.settings && S.settings.stamp) || null;
    if (S.page === "settings") renderPage();
  } catch (e) { /* ignore */ }
}

// 设置页保存的统一收口：带上乐观并发基线，冲突（抢票进程/另一窗口已改）
// 时后端返回 {ok:false} 且不落盘 —— 提示并使本地状态回落到磁盘真实值。
export async function saveSettings(params) {
  const r = await call("saveSettings", Object.assign({ expect_stamp: S.settingsStamp }, params));
  if (r && r.ok === false) { toast(r.error || "保存失败", "err"); refreshSettings(); return false; }
  if (r && r.stamp) S.settingsStamp = r.stamp;
  return true;
}

registerPage("settings", renderSettings);
registerClosePanelHook(closeSettingPanel);
