# -*- coding: utf-8 -*-
"""
12306 车票监控与自动购票系统 —— 交互式命令行入口

功能入口
    python monitor.py          交互式主菜单（创建任务 / 任务管理 / 乘车人管理 / 历史 / 启动监控）
    python monitor.py run      直接启动监控循环（适合放后台 / 终端挂机）
    python monitor.py check    单车次余票速查（免登录）

首次使用三步走
    1. python capture_session.py   —— 在弹出的 Edge 里登录一次 12306（会话持久化）
    2. 菜单 [4] 维护常用乘车人（加密存储），菜单 [1] 交互式创建监控任务
    3. 菜单 [6] 启动监控：命中余票 -> 自动提交订单（不支付）-> 邮件通知

合规说明
    - 余票数据来自 12306 官方公开查询接口；下单走官网同款提交流程
    - 不会绕过验证码（滑块出现即中止并提示人工处理）；不执行支付
"""

import datetime
import json
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))

import engine as engine_mod
import notify as notify_mod
import order as order_mod
import passengers as passengers_mod
import ticket

CONFIG_PATH = os.path.join(HERE, "config.json")

SEAT_CHOICES = ["商务座", "特等座", "一等座", "二等座", "高级软卧",
                "软卧", "动卧", "硬卧", "软座", "硬座", "无座"]


def fresh_engine():
    """每次基于最新配置/状态构建引擎实例。"""
    return engine_mod.MonitorEngine()


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(config):
    # 原子写：写一半被杀不留截断文件
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def pause():
    try:
        input("\n按回车返回主菜单...")
    except (EOFError, KeyboardInterrupt):
        pass


def read(prompt, default=""):
    try:
        v = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return v if v else default


# ----------------------------- 车站与输入辅助 -----------------------------

def pick_station(prompt, name2code):
    """输入站名（支持简称/部分匹配），返回精确站名或 None。"""
    while True:
        raw = read(prompt)
        if raw is None:
            return None
        if raw in name2code:
            return raw
        cands = [n for n in name2code if raw in n]
        if not cands:
            print("  未找到含「{0}」的车站，请重新输入（如：北京 / 上海虹桥 / 广州南）".format(raw))
            continue
        if len(cands) == 1:
            print("  匹配到车站：{0}".format(cands[0]))
            return cands[0]
        print("  找到 {0} 个匹配车站：".format(len(cands)))
        for i, n in enumerate(cands[:20], 1):
            print("    {0:>2}. {1}".format(i, n))
        sel = read("  输入序号选择（回车=全部显示已在上面的重新输入）：")
        if sel is None:
            return None
        if sel.isdigit() and 1 <= int(sel) <= min(len(cands), 20):
            return cands[int(sel) - 1]


def pick_multi(options, prompt, allow_empty=True):
    """从选项里多选（逗号分隔序号）。返回子列表。"""
    while True:
        print("\n  " + prompt)
        for i, op in enumerate(options, 1):
            print("    {0:>2}. {1}".format(i, op))
        raw = read("  输入序号（多个用逗号分隔，回车=%s）：" % ("全部" if allow_empty else "取消"))
        if raw is None or raw == "":
            return list(options) if allow_empty else []
        chosen = []
        try:
            for part in raw.replace("，", ",").split(","):
                part = part.strip()
                if part.isdigit() and 1 <= int(part) <= len(options):
                    chosen.append(options[int(part) - 1])
        except Exception:
            chosen = []
        if chosen:
            return chosen
        print("  输入有误，请重试。")


def input_dates():
    """输入单个日期或日期范围。返回 (dates, date_range)。"""
    print("\n  日期格式：单日  2026-10-15    范围  2026-10-15~2026-10-18")
    while True:
        raw = read("  乘车日期：")
        if raw is None:
            return None, None
        try:
            if "~" in raw:
                a, b = [x.strip() for x in raw.split("~", 1)]
                d0, d1 = datetime.date.fromisoformat(a), datetime.date.fromisoformat(b)
                if d1 < d0:
                    print("  结束日期不能早于开始日期。")
                    continue
                if d0 < datetime.date.today():
                    print("  提示：开始日期已是过去日期（监控时会自动跳过过期日期）。")
                return [], [d0.isoformat(), d1.isoformat()]
            d = datetime.date.fromisoformat(raw)
            if d < datetime.date.today():
                print("  提示：该日期已过期，请确认是否仍要创建任务。")
                if read("  仍要创建？(y/n)：" , "n").lower() != "y":
                    continue
            return [raw], []
        except ValueError:
            print("  日期格式错误，请按 YYYY-MM-DD 格式输入。")


def ask_priority():
    while True:
        raw = read("  任务优先级 1~10（数字越大越优先，默认 5）：", "5")
        if raw.isdigit() and 1 <= int(raw) <= 10:
            return int(raw)
        print("  请输入 1~10 的整数。")


def ask_yes_no(prompt, default="y"):
    v = read(prompt + " (y/n，回车=%s)：" % default, default).lower()
    return v in ("y", "yes", "是")


# ----------------------------- 菜单 1：创建任务 -----------------------------

def menu_create_task():
    print("\n===== 创建监控任务 =====")
    name2code, code2name = ticket.load_station_map()

    from_name = pick_station("  出发站：", name2code)
    if not from_name:
        print("  已取消创建。")
        return
    while True:
        to_name = pick_station("  到达站：", name2code)
        if not to_name:
            print("  已取消创建。")
            return
        if name2code[to_name] != name2code[from_name]:
            break
        print("  到达站不能与出发站相同，请重新输入。")

    dates, date_range = input_dates()
    if dates is None:
        print("  已取消创建。")
        return

    # 实时查询车次列表
    print("\n  正在查询 {0} -> {1} 的车次...".format(from_name, to_name))
    query_date = dates[0] if dates else date_range[0]
    shown = []
    try:
        rows = ticket.query_tickets(name2code[from_name], name2code[to_name], query_date)
    except Exception as e:
        print("  查询失败：{0}".format(e))
        print("  仍可继续创建任务（运行时会自动查询）。")
        trains_choice = read("  车次（多个用逗号分隔，回车=全部车次）：", "")
    else:
        print("\n  共 {0} 趟列车（日期 {1}）：".format(len(rows), query_date))
        print("  " + "-" * 86)
        print("  {0:<8}{1:<16}{2:<22}{3:<10}{4}".format(
            "车次", "出发-到达", "时间", "历时", "当前有余票的席别"))
        shown = []
        for row in rows:
            info = ticket.parse_row(row, code2name)
            seats = " ".join("{0}({1})".format(k, v) for k, v in info["available_seats"].items()) \
                or "暂无可视余票"
            print("  {0:<8}{1:<16}{2:<22}{3:<10}{4}".format(
                info["train_code"],
                info["from_name"] + "-" + info["to_name"],
                info["start_time"] + "-" + info["arrive_time"],
                info["duration"],
                seats[:60]))
            shown.append(info["train_code"])
        print("  " + "-" * 86)
        trains_choice = read("  选择监控车次（多个用逗号分隔，回车=全部车次）：", "")

    trains = [t.strip() for t in trains_choice.replace("，", ",").split(",")
              if t.strip()] if trains_choice else []
    for t in trains:
        if shown and t not in shown:
            print("  注意：车次 {0} 未出现在查询结果中（可能不经过该区间），仍将加入监控。".format(t))

    seat_types = pick_multi(SEAT_CHOICES, "选择监控席别（可多选）：", allow_empty=False)
    if not seat_types:
        print("  未选择席别，已取消创建。")
        return

    # 乘车人选择
    local_passengers = passengers_mod.load_passengers()
    passenger_names = []
    if local_passengers:
        print("\n  本地乘车人（加密库）：")
        for i, p in enumerate(local_passengers, 1):
            mark = "（默认）" if p.get("is_default") else ""
            print("    {0:>2}. {1}{2}  {3}  {4}".format(
                i, p.get("name"), mark, p.get("id_type_code", ""),
                "成人" if p.get("is_adult", True) else "儿童/学生"))
        sel = read("  选择乘车人（多个用逗号分隔，回车=使用默认/全部成人）：", "")
        if sel:
            for part in sel.replace("，", ",").split(","):
                part = part.strip()
                if part.isdigit() and 1 <= int(part) <= len(local_passengers):
                    passenger_names.append(local_passengers[int(part) - 1]["name"])
        else:
            passenger_names = passengers_mod.default_names()
            print("  已选择默认乘车人：{0}".format("、".join(passenger_names) or "（无）"))
    else:
        print("\n  本地乘车人库为空。可不选：下单时将自动使用 12306 账号内已保存的常用乘车人。")
        print("  建议先到主菜单 [4] 添加乘车人。")

    priority = ask_priority()
    auto_order = ask_yes_no("  余票命中时自动下单？", "y")
    stop_after = ask_yes_no("  一次下单成功后自动停止该任务（防止重复购买）？", "y")

    base_name = "{0}-{1} {2} {3}".format(from_name, to_name,
                                         "/".join(trains) if trains else "全部车次",
                                         "/".join(seat_types))
    task_name = read("  任务名称（回车=自动生成）：", base_name) or base_name

    task = {
        "name": task_name,
        "from": from_name,
        "to": to_name,
        "dates": dates,
        "date_range": date_range,
        "trains": trains,
        "seat_types": seat_types,
        "auto_order": auto_order,
        "stop_after_order": stop_after,
        "passenger_names": passenger_names,
        "priority": priority,
        "notify_channels": ["email"],
    }

    config = load_config()
    config.setdefault("tasks", []).append(task)
    save_config(config)
    print("\n  [完成] 任务「{0}」已创建：{1}->{2} 日期 {3} 席别 {4} 乘车人 {5}".format(
        task_name, from_name, to_name,
        dates[0] if dates else "~".join(date_range),
        "/".join(seat_types),
        "、".join(passenger_names) if passenger_names else "账号常用乘车人"))
    if ask_yes_no("\n  是否立即启动监控？", "y"):
        eng = fresh_engine()
        # 新任务默认"已暂停"，用户确认启动时显式置为监控中
        eng.set_task_status(task, "monitoring", "命令行创建并启动", force=True)
        eng.run()


# ----------------------------- 菜单 2：任务列表 -----------------------------

def menu_task_list():
    config = load_config()
    tasks = config.get("tasks") or []
    if not tasks:
        print("\n  暂无任务。请先在菜单 [1] 创建。")
        return
    eng = fresh_engine()
    print("\n===== 任务列表与状态 =====")
    print("-" * 110)
    print("  {0:<3}{1:<26}{2:<14}{3:<20}{4:<14}{5:<14}{6:<8}{7}".format(
        "#", "任务", "区间", "日期", "车次", "席别", "优先级", "状态"))
    print("-" * 110)
    for i, t in enumerate(tasks, 1):
        dates = engine_mod.expand_dates(t)
        status = eng.task_status(t)
        print("  {0:<3}{1:<26}{2:<14}{3:<20}{4:<14}{5:<14}{6:<8}{7}".format(
            i, (t.get("name") or "")[:26],
            (t["from"] + "-" + t["to"])[:14],
            (dates[0] + ("..." if len(dates) > 1 else ""))[:20],
            ("/".join(t.get("trains") or []) or "全部")[:14],
            ("/".join(t.get("seat_types") or []))[:14],
            t.get("priority", 5),
            engine_mod.STATUS_LABELS.get(status, status)))
        msg = (eng.state["tasks"].get(t["name"], {}).get("message", ""))
        if msg:
            print("      └ {0}".format(msg[:80]))
    print("-" * 110)
    running = [t for t in tasks if eng.task_status(t) == "monitoring"]
    if running:
        print("  监控中任务轮询间隔估算（基准 %ss，自适应）:" % eng.base_interval)
        for t in running:
            print("    {0:<26} 约 {1:.0f}s".format(t["name"][:26], eng.task_interval(t)))


# ----------------------------- 菜单 3：任务操作 -----------------------------

def menu_task_ops():
    config = load_config()
    tasks = config.get("tasks") or []
    if not tasks:
        print("\n  暂无任务。")
        return
    menu_task_list()
    raw = read("\n  输入要操作的任务序号（回车=返回）：", "")
    if not raw or not raw.isdigit() or not (1 <= int(raw) <= len(tasks)):
        return
    idx = int(raw) - 1
    task = tasks[idx]
    eng = fresh_engine()
    status = eng.task_status(task)
    print("\n  任务：{0}（当前状态：{1}）".format(task["name"],
          engine_mod.STATUS_LABELS.get(status, status)))
    print("   1. 暂停监控       2. 恢复监控（继续抢票）")
    print("   3. 取消任务       4. 重置状态并清除防重记录（换乘车人/席别后重新开始）")
    print("   5. 删除任务")
    op = read("  选择操作（回车=返回）：", "")
    if op == "1":
        eng.set_task_status(task, "paused", "用户手动暂停")
        print("  已暂停。")
    elif op == "2":
        eng.set_task_status(task, "monitoring", "用户手动恢复")
        print("  已恢复监控。启动监控（菜单 6）后即开始轮询。")
    elif op == "3":
        if ask_yes_no("  确认取消任务「%s」？取消后不再监控" % task["name"], "n"):
            eng.set_task_status(task, "cancelled", "用户手动取消")
            print("  已取消（任务保留在列表中，可删除）。")
    elif op == "4":
        if ask_yes_no("  确认重置「%s」状态并清除其防重下单记录？" % task["name"], "n"):
            eng.empty_task_dedup(task)
            eng.set_task_status(task, "monitoring", "已重置")
            print("  已重置为监控中，防重记录已清除。")
    elif op == "5":
        if ask_yes_no("  确认从配置中删除任务「%s」？" % task["name"], "n"):
            del config["tasks"][idx]
            save_config(config)
            print("  已删除任务。")


# ----------------------------- 菜单 4：乘车人管理 -----------------------------

def _mask_id(id_no):
    if not id_no or len(id_no) < 8:
        return id_no or ""
    return id_no[:3] + "*" * (len(id_no) - 7) + id_no[-4:]


def menu_passengers():
    passengers = passengers_mod.load_passengers()
    while True:
        print("\n===== 乘车人管理（数据加密存储于本地） =====")
        if not passengers:
            print("  （空）")
        for i, p in enumerate(passengers, 1):
            mark = "【默认】" if p.get("is_default") else ""
            print("  {0:>2}. {1} {2}{3}  证件:{4} {5}  手机:{6}".format(
                i, p.get("name"), mark,
                "" if p.get("is_adult", True) else "(非成人)",
                passengers_mod.ID_TYPE_NAMES.get(p.get("id_type_code"), p.get("id_type_code", "?")),
                _mask_id(p.get("id_no")),
                (p.get("mobile") or "")[:3] + "****" + (p.get("mobile") or "")[-4:]
                if p.get("mobile") else "未填"))
        print("  1. 添加乘车人   2. 编辑   3. 删除   4. 设为默认乘车人   0. 返回")
        op = read("  选择操作：", "")
        if op == "0" or op == "":
            return
        if op == "1":
            name = read("  姓名：", "")
            if not name:
                print("  姓名不能为空。")
                continue
            print("  证件类型：")
            for code, label in passengers_mod.ID_TYPE_NAMES.items():
                print("    {0}. {1}".format(code, label))
            id_type = read("  证件类型代码（回车=1 二代身份证）：", "1")
            id_no = read("  证件号码：", "")
            mobile = read("  手机号（可空）：", "")
            is_default = ask_yes_no("  设为默认乘车人？", "n")
            is_adult = ask_yes_no("  是否成人？", "y")
            passengers.append({
                "name": name, "id_type_code": id_type or "1", "id_no": id_no,
                "mobile": mobile, "is_default": is_default, "is_adult": is_adult,
            })
            passengers_mod.save_passengers(passengers)
            print("  已添加并加密保存。")
        elif op == "2":
            raw = read("  编辑第几位：", "")
            if raw.isdigit() and 1 <= int(raw) <= len(passengers):
                p = passengers[int(raw) - 1]
                name = read("  姓名（%s）：" % p.get("name"), p.get("name"))
                id_no = read("  证件号码（保持不填=不变）：", "") or p.get("id_no")
                mobile = read("  手机号：", p.get("mobile"))
                is_default = ask_yes_no(
                    "  设为默认乘车人？(当前：%s)" % ("是" if p.get("is_default") else "否"),
                    "y" if p.get("is_default") else "n")
                p.update({"name": name, "id_no": id_no, "mobile": mobile,
                          "is_default": is_default})
                passengers_mod.save_passengers(passengers)
                print("  已更新并加密保存。")
        elif op == "3":
            raw = read("  删除第几位：", "")
            if raw.isdigit() and 1 <= int(raw) <= len(passengers):
                if ask_yes_no("  确认删除该乘车人？", "n"):
                    del passengers[int(raw) - 1]
                    passengers_mod.save_passengers(passengers)
                    print("  已删除。")
        elif op == "4":
            raw = read("  设为默认的第几位：", "")
            if raw.isdigit() and 1 <= int(raw) <= len(passengers):
                for p in passengers:
                    p["is_default"] = False
                passengers[int(raw) - 1]["is_default"] = True
                passengers_mod.save_passengers(passengers)
                print("  已设为默认乘车人（自动下单时优先使用）。")


# ----------------------------- 菜单 5：历史记录 -----------------------------

def menu_history():
    config = load_config()
    path = os.path.join(HERE, config.get("history_file", "order_history.json"))
    if not os.path.exists(path):
        print("\n  暂无购票历史记录。")
        return
    try:
        with open(path, encoding="utf-8") as f:
            history = json.load(f)
    except Exception as e:
        print("  读取历史失败：%s" % e)
        return
    print("\n===== 购票历史与通知记录（共 %d 条） =====" % len(history))
    label = {"success": "下单成功", "dup": "防重跳过", "failed": "下单失败",
             "hit_no_order": "命中未下单"}
    for r in reversed(history[-50:]):
        print("-" * 100)
        print("  {0}  {1}".format(r.get("time"), r.get("task", "")))
        print("  {0} {1}  {2}->{3}  席别:{4}  乘车人:{5}".format(
            r.get("date", ""), r.get("train", ""), r.get("from", ""), r.get("to", ""),
            r.get("seat", ""), "、".join(r.get("passengers") or [])))
        if r.get("order_no"):
            print("  订单号:{0}".format(r["order_no"]))
        print("  结果:{0}   {1}".format(
            label.get(r.get("result"), r.get("result")), r.get("message", "")))
        if r.get("notify"):
            print("  通知:{0}".format(r["notify"]))
    print("-" * 100)


# ----------------------------- 菜单 7：通知设置 -----------------------------

def menu_notify():
    config = load_config()
    email = config.setdefault("notify", {}).setdefault("email", {
        "enabled": True, "smtp_host": "smtp.qq.com", "smtp_port": 465,
        "username": "", "password": "", "from": "", "to": []})
    print("\n===== 邮件通知设置（SMTP 授权码） =====")
    print("  当前：enabled=%s host=%s port=%s user=%s" % (
        email.get("enabled"), email.get("smtp_host"), email.get("smtp_port"),
        email.get("username")))
    email["smtp_host"] = read("  SMTP 服务器（回车=%s）：" % email.get("smtp_host", "smtp.qq.com")) \
        or email.get("smtp_host", "smtp.qq.com")
    port = read("  端口（回车=%s）：" % email.get("smtp_port", 465), str(email.get("smtp_port", 465)))
    email["smtp_port"] = int(port) if port.isdigit() else 465
    email["username"] = read("  发件邮箱：", email.get("username"))
    email["password"] = read("  邮箱授权码（非登录密码）：", email.get("password"))
    email["from"] = read("  发件人地址（回车=发件邮箱）：", "") or email.get("username")
    to_raw = read("  收件人（多个用逗号分隔）：", ",".join(email.get("to") or []))
    email["to"] = [x.strip() for x in to_raw.replace("，", ",").split(",") if x.strip()]
    save_config(config)
    print("  已保存。")
    if ask_yes_no("  是否发送测试邮件验证？", "y"):
        ok, msg = notify_mod.send_email(email, "测试邮件：12306 监控系统",
                                        "这是一封测试邮件，收到说明邮件通知可用。")
        print("  %s" % msg)


# ----------------------------- 菜单 8：登录会话 -----------------------------

def menu_session():
    print("\n===== 登录会话 =====")
    config = load_config()
    cookie_path = os.path.join(HERE, config.get("session_cookies_file", "session_cookies.json"))
    if os.path.exists(cookie_path):
        try:
            s = order_mod.load_session(config.get("session_cookies_file"))
            ok, who = order_mod.check_login(s)
            print("  会话文件：%s" % cookie_path)
            print("  当前状态：%s" % ("有效（%s）" % who if ok else "失效（%s）" % who))
        except Exception as e:
            print("  会话校验异常：%s" % e)
    else:
        print("  尚未保存会话。")
    print("  1. 重新登录（弹出 Edge，扫码/账号密码登录后自动保存 Cookie）")
    print("  2. 查看账号已保存的乘车人")
    op = read("  选择操作（回车=返回）：", "")
    if op == "1":
        script = os.path.join(HERE, "capture_session.py")
        print("  启动会话抓取脚本...登录后请回到本窗口。")
        subprocess.call([sys.executable, script], cwd=HERE)
    elif op == "2":
        try:
            s = order_mod.load_session(config.get("session_cookies_file"))
            ok, who = order_mod.check_login(s)
            if not ok:
                print("  会话无效：%s" % who)
            else:
                ps = order_mod.get_passengers(s)
                print("  账号已保存乘车人 %d 位：" % len(ps))
                for p in ps:
                    print("    %s（%s）" % (
                        p["name"], "成人" if p.get("is_adult", True) else "非成人"))
        except Exception as e:
            print("  查询失败：%s" % e)


# ----------------------------- 菜单 9：余票速查 -----------------------------

def menu_quick_check():
    print("\n===== 余票速查（免登录） =====")
    name2code, code2name = ticket.load_station_map()
    from_name = pick_station("  出发站：", name2code)
    if not from_name:
        return
    to_name = pick_station("  到达站：", name2code)
    if not to_name:
        return
    date = read("  日期（YYYY-MM-DD，回车=今天）：", datetime.date.today().isoformat())
    try:
        rows = ticket.query_tickets(name2code[from_name], name2code[to_name], date)
    except Exception as e:
        print("  查询失败：%s" % e)
        return
    print("\n  {0} -> {1}  日期 {2}  共 {3} 趟".format(from_name, to_name, date, len(rows)))
    print("  " + "-" * 90)
    for row in rows:
        info = ticket.parse_row(row, code2name)
        seats = " ".join("{0}({1})".format(k, v) for k, v in info["available_seats"].items()) \
            or "暂无可视余票"
        print("  {0:<8}{1:<12}{2:<16}{3:<10}{4}".format(
            info["train_code"], info["from_name"] + "-" + info["to_name"],
            info["start_time"] + "-" + info["arrive_time"], info["duration"], seats))
    print("  " + "-" * 90)


# ----------------------------- 主菜单 -----------------------------

MENU = [
    ("1", "创建监控任务（选择日期 / 车次 / 席别 / 乘车人）", menu_create_task),
    ("2", "任务列表与实时状态", menu_task_list),
    ("3", "任务操作（暂停 / 恢复 / 取消 / 重置 / 删除）", menu_task_ops),
    ("4", "乘车人管理（加密存储，支持默认乘车人）", menu_passengers),
    ("5", "购票历史与通知记录", menu_history),
    ("6", "启动监控（后台循环抢票）", lambda: fresh_engine().run()),
    ("7", "通知设置（邮件 SMTP）", menu_notify),
    ("8", "登录会话（重新登录 / 校验 / 查看乘车人）", menu_session),
    ("9", "单车次余票速查", menu_quick_check),
    ("0", "退出", None),
]


def main_menu():
    while True:
        print("\n" + "=" * 56)
        print("  12306 车票监控与自动购票系统")
        print("=" * 56)
        for key, label, _ in MENU:
            print("  [{0}] {1}".format(key, label))
        print("=" * 56)
        choice = read("  请选择：", "")
        if choice is None or choice == "0":
            print("  再见。")
            return
        for key, label, func in MENU:
            if choice == key and func:
                try:
                    func()
                except KeyboardInterrupt:
                    print("\n  已中断，返回主菜单。")
                break
        else:
            print("  无效选项。")
        if choice not in ("6",):
            pause()


def main():
    args = sys.argv[1:]
    if args and args[0] == "run":
        fresh_engine().run()
        return
    if args and args[0] in ("check", "query"):
        menu_quick_check()
        return
    main_menu()


if __name__ == "__main__":
    main()