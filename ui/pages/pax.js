/* pages/pax.js —— 乘车人页（列表/同步/编辑表单）。 */
import { S, call, renderPage, hitSearch, registerPage } from "../store.js";
import { $, esc } from "../components/dom.js";
import { toast } from "../components/toast.js";
import { openModal, confirmBox } from "../components/modal.js";
import { showDD } from "../components/dd.js";


// 保存乘车人的统一收口（与 settings.saveSettings 同口径）：后端按
// expect_stamp 乐观并发拒写时返回 {ok:false,error} 而不抛异常，必须查
// 返回值——不查就会照常关弹窗+假报「已保存」，编辑静默丢失。
// 成功：同步后端回的新 stamp 并返回 true；失败：toast 报错返回 false，
// 调用方不得关弹窗/报成功。RPC 本身抛错（后端未就绪）时 call() 已 toast，
// 这里只兜底返回 false。
async function savePaxList(passengers) {
  let r;
  try {
    r = await call("savePassengers", { passengers, expect_stamp: S.paxStamp });
  } catch (e) { return false; }
  if (r && (r.ok === false || r.error)) { toast(r.error || "保存失败", "err"); return false; }
  if (r && r.stamp) S.paxStamp = r.stamp;
  return true;
}

// ================= 乘车人页 =================
export function renderPax(root) {
  root.innerHTML =
    '<div class="sec-head"><h3>乘车人</h3><span style="display:flex;gap:8px">' +
    '<button class="btn btn-mini btn-ghost" id="pSync">从 12306 同步</button>' +
    '<button class="btn btn-mini btn-call" id="pNew">＋ 新增</button></span></div>' +
    '<div class="card glass glass-surface" style="flex:1"><div class="pax" id="pList">' + S.pax.map(paxRow).join("") + "</div></div>";

  root.querySelector("#pNew").onclick = () => openPaxForm();
  root.querySelector("#pSync").onclick = async () => {
    const r = await call("sync12306Passengers");
    if (r.error) return toast(r.error, "err");
    // 落盘成功才报「已同步」；失败时 savePaxList 已 toast 报错，只刷新对齐
    if (await savePaxList(r.passengers || [])) toast("已同步 " + (r.passengers || []).length + " 位");
    refreshPax();
  };
  root.querySelectorAll("#pList .p").forEach((el) => {
    el.ondblclick = () => { const p = S.pax.find((x) => x.name === el.dataset.name); if (p) openPaxForm(p); };
    el.oncontextmenu = (e) => {
      e.preventDefault();
      const p = S.pax.find((x) => x.name === el.dataset.name);
      if (!p) return;
      showDD([
        { i: 0, label: "✎ 编辑", fn: () => { openPaxForm(p); } },
        { i: 1, label: p.is_default ? "已是默认" : "☆ 设为默认", fn: async () => {
          if (p.is_default) return;
          await savePaxList(S.pax.map((x) => Object.assign({}, x, { is_default: x.name === p.name })));
          refreshPax();
        } },
        { i: 2, label: "🗑 删除", danger: true, fn: async () => {
          if (await confirmBox("删除", "删除乘车人「" + p.name + "」？")) {
            await savePaxList(S.pax.filter((x) => x.name !== p.name));
            refreshPax();
          }
        } },
      ], e.clientX, e.clientY);
    };
  });
}

export function paxRow(p) {
  if (!hitSearch(p.name, p.id_no || "", p.mobile || "")) return "";
  const idt = ID_TYPE_NAMES[p.id_type_code] || p.id_type_code || "";
  return '<div class="p" data-name="' + esc(p.name) + '"><span>👤 <b>' + esc(p.name) + "</b>" +
    (p.is_adult ? "" : ' <span class="muted">(未成年)</span>') + "</span>" +
    (p.is_default ? '<span class="def">默认</span>' : "") +
    '<span class="idno">' + esc(idt + " " + (p.id_no || "")) + "</span></div>";
}

export async function openPaxForm(edit) {
  let t = edit ? Object.assign({}, edit) : {};
  // 列表接口返回的是掩码证件号/手机号，编辑时必须取回明文（否则保存会把掩码写进磁盘）
  let detailFailed = false;
  if (edit && edit.name) {
    try {
      const d = await call("passengerDetail", { name: edit.name });
      if (d && d.ok && d.passenger) t = Object.assign({}, edit, d.passenger);
      else detailFailed = true;
    } catch (e) { detailFailed = true; /* call 已提示 */ }
  }
  // 取不回明文时绝不能沿用列表里的掩码：那张掩码被原样保存一次，真证件号就永久没了。
  // 清空敏感字段逼用户重填，比静默写坏安全。
  if (detailFailed) {
    t = Object.assign({}, t, { id_no: "", mobile: "" });
    toast("未能读取原证件号，请重新填写", "err");
  }
  const { modal, close } = openModal(
    "<h3>" + (edit ? "编辑乘车人" : "新增乘车人") + "</h3>" +
    '<div class="form-row"><label>姓名</label><input id="pName" value="' + esc(t.name || "") + '"></div>' +
    '<div class="form-grid">' +
    '<div class="form-row"><label>证件类型</label><select id="pIdType">' +
    Object.entries(ID_TYPE_NAMES).map(([k, v]) => '<option value="' + k + '"' + ((t.id_type_code || "1") === k ? " selected" : "") + ">" + esc(v) + "</option>").join("") +
    "</select></div>" +
    '<div class="form-row"><label>证件号</label><div class="eye-wrap">' +
    '<input id="pIdNo" type="password" value="' + esc(t.id_no || "") + '">' +
    '<button class="eye" type="button" id="pIdEye" title="显示 / 隐藏证件号">👁</button></div></div>' +
    "</div>" +
    '<div class="form-row"><label>手机号</label><input id="pMobile" value="' + esc(t.mobile || "") + '"></div>' +
    '<div class="form-actions"><button class="btn btn-ghost" id="pCancel">取消</button>' +
    '<button class="btn btn-call" id="pSave">保存</button></div>');

  modal.querySelector("#pCancel").onclick = close;
  const eye = modal.querySelector("#pIdEye");
  if (eye) eye.onclick = () => {
    const i = modal.querySelector("#pIdNo");
    i.type = i.type === "password" ? "text" : "password";
    eye.classList.toggle("on", i.type === "text");
  };
  modal.querySelector("#pSave").onclick = async () => {
    const name = modal.querySelector("#pName").value.trim();
    if (!name) return toast("请填写姓名", "err");
    const id_no = modal.querySelector("#pIdNo").value.trim();
    if (!id_no) return toast("请填写证件号", "err");
    const rec = {
      name, id_type_code: modal.querySelector("#pIdType").value,
      id_no, mobile: modal.querySelector("#pMobile").value.trim(),
      is_default: edit ? edit.is_default : S.pax.length === 0,
      is_adult: edit ? edit.is_adult : true,
    };
    const list = edit ? S.pax.map((x) => (x.name === edit.name ? rec : x)) : S.pax.concat([rec]);
    // 保存失败（指纹冲突等）不关弹窗、不报已保存：错误已由 savePaxList
    // toast，用户可改后重试或取消，表单输入不丢。
    if (!await savePaxList(list)) return;
    close(); toast("已保存"); refreshPax();
  };
}

export async function refreshPax() {
  // 内部吞错保底（与 history/errlog 刷新函数同口径）：后端抖动/重启时保留
  // 旧缓存与 stamp、不抛。否则抢票/监控表单打开前的预拉取一失败，异常会
  // 把整个开窗流程打断（表单根本打不开）。失败原因 call() 已 toast。
  try {
    const r = await call("listPassengers");
    S.pax = r.passengers || [];
    S.paxStamp = r.stamp;
    if (S.page === "pax") renderPage();
  } catch (e) { /* 保留旧缓存，等下一轮刷新 */ }
}
export const ID_TYPE_NAMES = { "1": "二代身份证", "2": "一代身份证", "C": "港澳通行证", "G": "台湾通行证", "B": "护照", "H": "永久居留证", "L": "其他" };

registerPage("pax", renderPax);
