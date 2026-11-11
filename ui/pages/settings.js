/* pages/settings.js —— 设置页（外观 / 窗口 / 邮件通知 / 维护，行级即改即存）。 */
import { S, call, renderPage, registerPage } from "../store.js";
import { esc } from "../components/dom.js";
import { toast } from "../components/toast.js";
import { confirmBox, openModal } from "../components/modal.js";
import { APPEAR_DEF, ACCENTS, applyAppearance } from "../components/appearance.js";

const SEG = (id, items, value) => '<div class="seg seg-compact" id="' + id + '">' +
  items.map(([v, t]) => '<button data-v="' + v + '"' + (String(value) === v ? ' class="on"' : "") + ">" + t + "</button>").join("") + "</div>";

const ROW = (label, control, note) => '<div class="settings-row"><div class="settings-label"><span>' + label + "</span>" +
  (note ? "<small>" + note + "</small>" : "") + '</div><div class="settings-control">' + control + "</div></div>";

const RANGE_ROW = (id, label, min, max, step, value, unit, note) => ROW(label,
  '<div class="settings-range"><input type="range" id="' + id + '" min="' + min + '" max="' + max + '" step="' + step + '" value="' + value + '"><span class="val" id="' + id + 'V">' + value + unit + "</span></div>", note);

const GROUP = (title, hint, body, extra) => '<section class="settings-group card">' +
  '<div class="settings-group-head"><div><h4>' + title + "</h4>" + (hint ? "<span>" + hint + "</span>" : "") + "</div>" + (extra || "") + "</div>" +
  '<div class="settings-list">' + body + "</div></section>";

function appearanceGroupHtml() {
  const a = S.appear || {};
  return GROUP("外观", "主题、字体与主色",
    ROW("主题", SEG("aTheme", [["light", "浅色"], ["dark", "深色"], ["sys", "跟随系统"]], a.theme || "light")) +
    ROW("界面密度", SEG("aDensity", [["compact", "紧凑"], ["normal", "标准"], ["roomy", "宽松"]], a.density || "normal")) +
    ROW("动效", SEG("aMotion", [["on", "开启"], ["off", "关闭"]], a.motion || "on"), "关闭后切换与弹窗不做动画") +
    ROW("字体", '<select id="aFont" class="settings-input">' + [["system", "系统默认"], ["hei", "微软雅黑"], ["song", "宋体"], ["kai", "楷体"], ["mono", "等宽"]].map(([v, t]) => '<option value="' + v + '"' + ((a.font || "system") === v ? " selected" : "") + ">" + t + "</option>").join("") + "</select>") +
    RANGE_ROW("aFs", "字号", 13, 18, 1, a.fs ?? 15, "px") +
    ROW("主色", '<div class="swatches" id="aAccent">' + ACCENTS.map((c) => '<span class="swatch' + (a.accent === c ? " on" : "") + '" data-v="' + c + '" style="background:' + c + '"></span>').join("") + "</div>")
  );
}

function windowGroupHtml() {
  const a = S.appear || {};
  return GROUP("窗口", "模糊、圆角与背景",
    RANGE_ROW("aBlur", "模糊度", 0, 40, 1, a.blur ?? 16, "px") +
    RANGE_ROW("aRadius", "圆角", 6, 28, 1, a.radius ?? 18, "px") +
    RANGE_ROW("aGlass", "玻璃透明度", 15, 100, 1, a.glass ?? 55, "%") +
    RANGE_ROW("aLogh", "日志高度", 5, 22, 0.2, a.logh ?? 8.6, "rem") +
    ROW("背景", '<select id="aWall" class="settings-input">' + [["image", "壁纸"], ["flat", "蓝白"], ["flat-sunset", "晚霞"], ["flat-green", "松绿"], ["custom", "自定义"]].map(([v, t]) => '<option value="' + v + '"' + ((a.wall || "image") === v ? " selected" : "") + ">" + t + "</option>").join("") + "</select>") +
    '<div class="settings-row settings-row-sub" id="aWRow"' + (a.wall === "custom" ? "" : ' hidden') + '><div class="settings-label"><span>图片</span><small>支持链接或本地文件</small></div><div class="settings-control settings-control-wide"><input class="settings-input" id="aWallUrl" placeholder="https://... 或本地路径" value="' + esc(a.custom || "") + '"><button class="btn btn-mini btn-ghost" id="aWallPick">选文件</button><input type="file" id="aWallFile" accept="image/*" hidden></div></div>'
  );
}

function mailGroupHtml() {
  const s = S.settings || {};
  const email = s.email || {};
  return GROUP("邮件通知", "SMTP 发信，改动自动保存",
    ROW("启用", '<label class="switch"><input type="checkbox" id="sEnabled" data-mail' + (email.enabled ? " checked" : "") + '><span></span></label>', "抢票成功与订单变化时发信") +
    ROW("SMTP 服务器", '<input class="settings-input" id="sHost" data-mail value="' + esc(email.smtp_host || "") + '" placeholder="smtp.qq.com">') +
    ROW("端口", '<input class="settings-input" id="sPort" data-mail type="number" value="' + esc(email.smtp_port || 465) + '">') +
    ROW("发件邮箱", '<input class="settings-input" id="sUser" data-mail value="' + esc(email.username || "") + '">') +
    ROW("授权码", '<input class="settings-input" id="sPwd" data-mail type="password" value="' + esc(email.password || "") + '" placeholder="******">', "QQ 邮箱请填授权码，不是登录密码") +
    ROW("收件人", '<input class="settings-input" id="sTo" data-mail value="' + esc((email.to || []).join(",")) + '" placeholder="多个邮箱用逗号分隔">'),
    '<div class="settings-group-actions"><span class="status ' + (s.notify_enabled ? "succ" : "canc") + '" id="mailStatus">' + (s.notify_enabled ? "已启用" : "未启用") + '</span><button class="btn btn-mini btn-ghost" id="setPanelHelp">帮助</button></div>'
  ).replace("</section>", '<div class="settings-group-foot"><span id="mailSaveState">修改任意项即保存</span><button class="btn btn-mini btn-ghost" id="sTest">发送测试邮件</button></div></section>');
}

function maintGroupHtml() {
  return GROUP("维护", "本地偏好与缓存",
    ROW("恢复默认外观", '<button class="btn btn-mini btn-ghost" id="aReset">恢复</button>', "主题、模糊、圆角与布局") +
    ROW("清空本地数据", '<button class="btn btn-mini btn-ghost danger-ghost" id="aWipe">清空</button>', "不影响服务器上的任务与配置")
  );
}

export function renderSettings(root) {
  root.innerHTML = '<div class="settings-page">' +
    '<div class="settings-head"><div><h3>设置</h3><p>外观、窗口和邮件通知，改动即时保存</p></div><span class="settings-auto"><i></i>自动保存</span></div>' +
    '<div class="settings-scroll"><div class="settings-grid">' +
      appearanceGroupHtml() + windowGroupHtml() + mailGroupHtml() + maintGroupHtml() +
    "</div></div></div>";
  bindAppearPanel(root);
  bindMailPanel(root);
}

function readMailForm(p) {
  return {
    enabled: p.querySelector("#sEnabled").checked,
    smtp_host: p.querySelector("#sHost").value.trim(),
    smtp_port: Number(p.querySelector("#sPort").value) || 465,
    username: p.querySelector("#sUser").value.trim(),
    password: p.querySelector("#sPwd").value,
    from: p.querySelector("#sUser").value.trim(),
    to: p.querySelector("#sTo").value.split(/[,，\s]+/).filter(Boolean),
  };
}

function syncMailStatus(p, emailNew) {
  if (!S.settings) S.settings = {};
  const old = S.settings.email || {};
  S.settings.email = Object.assign({}, old, emailNew, {
    password: old.password || (emailNew.password ? "******" : ""),
  });
  S.settings.notify_enabled = !!emailNew.enabled;
  const text = emailNew.enabled ? "已启用" : "未启用";
  const badge = p.querySelector("#mailStatus");
  if (badge) {
    badge.textContent = text;
    badge.className = "status " + (emailNew.enabled ? "succ" : "canc");
  }
  const state = p.querySelector("#mailSaveState");
  if (state) {
    state.textContent = "已保存 " + new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    state.classList.add("saved");
    clearTimeout(state._timer);
    state._timer = setTimeout(() => { state.classList.remove("saved"); state.textContent = "修改任意项即保存"; }, 2400);
  }
}

function mailHelpHtml() {
  return '<h3>邮件通知设置帮助</h3><div class="mail-help">' +
    '<p>启用邮件通知后，抢票成功与订单状态变化会发送到收件人邮箱。授权码不是邮箱登录密码，按下面步骤单独获取。</p>' +
    '<h4>一、获取 QQ 邮箱授权码</h4><ol>' +
    '<li>电脑浏览器打开 <b>mail.qq.com</b>，登录要作为发件人的 QQ 邮箱。</li>' +
    '<li>点右上角「设置」→「账号与安全」→「安全设置」。</li>' +
    '<li>找到「IMAP/SMTP 服务」或「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务」，点「开启」。</li>' +
    '<li>按提示完成短信验证，页面会显示 16 位授权码；它只显示一次，先复制保存。</li>' +
    '<li>回到本页，把授权码填到「授权码」，不要填 QQ 登录密码。</li></ol>' +
    '<h4>二、详细邮件设置</h4>' +
    '<table><thead><tr><th>邮箱</th><th>SMTP 服务器</th><th>端口</th></tr></thead><tbody>' +
    '<tr><td>QQ 邮箱</td><td>smtp.qq.com</td><td>465（SSL）</td></tr>' +
    '<tr><td>163 邮箱</td><td>smtp.163.com</td><td>465（SSL）</td></tr>' +
    '<tr><td>126 邮箱</td><td>smtp.126.com</td><td>465（SSL）</td></tr>' +
    '<tr><td>Gmail</td><td>smtp.gmail.com</td><td>587（STARTTLS）</td></tr>' +
    '<tr><td>Outlook</td><td>smtp.office365.com</td><td>587（STARTTLS）</td></tr>' +
    '</tbody></table>' +
    '<p>发件邮箱和授权码必须属于同一个邮箱账号；收件人可以填多个，用逗号分隔。保存后点「发送测试邮件」，失败时先检查授权码是否过期、SMTP 服务是否开启、端口是否被防火墙拦截。</p>' +
    '</div><div class="form-actions"><button class="btn btn-call" id="mailHelpOk">知道了</button></div>';
}

export function bindMailPanel(p) {
  // 每个字段 change 立即保存；串行队列保证快速连续修改时不会带着旧 stamp
  // 并发提交（后一次冲突会把前一次刚填的字段吞掉）。
  let saveChain = Promise.resolve();
  const runSave = async (quiet) => {
    const emailNew = readMailForm(p);
    if (!await saveSettings({ email: emailNew })) return false;
    syncMailStatus(p, emailNew);
    if (!quiet) toast("邮件设置已保存");
    return true;
  };
  const save = (quiet) => {
    const task = saveChain.then(() => runSave(quiet), () => runSave(quiet));
    saveChain = task.catch(() => false);
    return task;
  };
  p.querySelectorAll("[data-mail]").forEach((el) => el.addEventListener("change", () => save(true)));
  const help = p.querySelector("#setPanelHelp");
  if (help) help.onclick = () => {
    const { modal, close } = openModal(mailHelpHtml(), "business");
    modal.querySelector("#mailHelpOk").onclick = close;
  };
  p.querySelector("#sTest").onclick = async () => {
    const emailNew = readMailForm(p);
    if (!emailNew.username || !emailNew.to.length) {
      toast("请先填写发件邮箱和收件人", "err");
      return;
    }
    // 测试前先保存当前表单，避免测到旧配置（旧逻辑只读磁盘）。
    if (!await save(true)) return;
    toast("正在发送测试邮件…");
    const r = await call("testEmail");
    const msg = (r && (r.message || r.error)) || (r && r.ok ? "测试邮件已发送" : "测试邮件发送失败");
    toast(msg, r && r.ok ? "" : "err");
  };
}

// 设置控件就地同步：保存后只刷新值，不重建整页（避免滑块/选中态闪一下）。
export function syncAppearControls(p) {
  const a = S.appear || {};
  const setSeg = (id, value) => {
    const box = p.querySelector("#" + id);
    if (!box) return;
    box.querySelectorAll("button[data-v]").forEach((b) => b.classList.toggle("on", b.dataset.v === String(value)));
  };
  setSeg("aTheme", a.theme || "light");
  setSeg("aDensity", a.density || "normal");
  setSeg("aMotion", a.motion || "on");
  p.querySelectorAll("#aAccent .swatch").forEach((x) => x.classList.toggle("on", x.dataset.v === a.accent));
  const setRange = (id, key, unit, def) => {
    const el = p.querySelector("#" + id), lab = p.querySelector("#" + id + "V");
    if (!el) return;
    const v = a[key] ?? def;
    el.value = v;
    if (lab) lab.textContent = v + unit;
  };
  setRange("aBlur", "blur", "px", 16);
  setRange("aRadius", "radius", "px", 18);
  setRange("aGlass", "glass", "%", 55);
  setRange("aFs", "fs", "px", 15);
  setRange("aLogh", "logh", "rem", 8.6);
  const font = p.querySelector("#aFont");
  if (font) font.value = a.font || "system";
  const wall = p.querySelector("#aWall");
  if (wall) wall.value = a.wall || "image";
  const row = p.querySelector("#aWRow");
  if (row) row.hidden = a.wall !== "custom";
  const url = p.querySelector("#aWallUrl");
  if (url) url.value = a.custom || "";
}

export function bindAppearPanel(p) {
  // 主题切换走 View Transition：整页 0.28s 交叉淡入，比瞬间换色自然。
  // 关闭动效（motion=off）时退回直接应用，尊重用户设置。
  const save = (animateTheme) => {
    const run = () => { applyAppearance(); syncAppearControls(p); };
    if (animateTheme && S.appear.motion !== "off" && typeof document.startViewTransition === "function") {
      try { document.startViewTransition(run); return; } catch (e) { /* 忽略并直接应用 */ }
    }
    run();
  };
  p.querySelectorAll("#aTheme button").forEach((b) => b.onclick = () => { S.appear.theme = b.dataset.v; save(true); });
  const bindRange = (id, key, unit) => {
    const el = p.querySelector("#" + id), lab = p.querySelector("#" + id + "V");
    if (!el) return;
    el.oninput = () => { S.appear[key] = Number(el.value); lab.textContent = el.value + unit; applyAppearance(); };
    el.onchange = () => save();
  };
  bindRange("aBlur", "blur", "px"); bindRange("aRadius", "radius", "px"); bindRange("aFs", "fs", "px");
  bindRange("aGlass", "glass", "%"); bindRange("aLogh", "logh", "rem");
  const aFont = p.querySelector("#aFont");
  if (aFont) aFont.onchange = () => { S.appear.font = aFont.value; save(); };
  [["aDensity", "density"], ["aMotion", "motion"]].forEach(([bid, key]) => {
    p.querySelectorAll("#" + bid + " button").forEach((b) => b.onclick = () => { S.appear[key] = b.dataset.v; save(); });
  });
  p.querySelectorAll("#aAccent .swatch").forEach((b) => b.onclick = () => { S.appear.accent = b.dataset.v; save(); });
  const wall = p.querySelector("#aWall");
  if (wall) wall.onchange = () => { S.appear.wall = wall.value; save(); };
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
