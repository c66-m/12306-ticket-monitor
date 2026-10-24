# -*- coding: utf-8 -*-
"""阶段化下单管线（REBUILD_PLAN.md 重构产物）。

推翻 browser_order._order_impl 的单体下单链，按七阶段重建：

    DETECT  检测有票 → 打开网页
    FILL    按预留信息自动填写
    SUBMIT  提交订单
    CONFIRM 确认下单（下单后不立即关闭页面）
    HOLD    驻留观察至锁定页或明确失败（无时限、无回读、无重下）
    LOG     结论返回软件日志
    （2026-10-24 前的 JUDGE 回读 / 重开重下已从本流程移除；
    _judge_official 等函数保留，仅供单测与旧链参考，不再被调用。）

与旧链的关系（双链并存，旧链实跑验证成功后退役）：
- DOM 操作序列由 browser_order._run_fill_submit_confirm 单源共享，
  本文件只做阶段编排、驻留观察与判定，不重复任何选择器。
- 返回值契约与旧链 order_via_browser 一致：(ok, msg, extra)。
  reason 全集：ambiguous / dup(dup_kind) / need_captcha / user_stopped /
  browser_dead / seat_unavailable / alias_no_stock。
"""

import contextlib
import threading
import time

from browser_order import (
    _SLIDE_WAIT_SEC,
    _exclusive,
    _extract_pay_evidence,
    _goto_and_query,
    _hold_pay_page,
    _log,
    _pay_page_hold_seconds,
    _record_timing,
    _run_fill_submit_confirm,
    _safe_close_ctx,
    _slide_up,
    _wait_slide_gone,
    _window_dead_exc,
    launch,
    save_state,
    session_ok,
)
from order import classify_with_time, fetch_unpaid_order_no, session_from_browser_state

# HOLD 快照词表：dup 优先于 fail（与旧链结果循环口径一致，坑 3）
_DUP_KEYS = ("未支付", "未完成订单", "未处理", "已预订", "行程冲突", "已购买")
_FAIL_KEYS = ("订票失败", "出票失败", "余票不足", "系统繁忙")
# 确认提交后的过程提示：12306 在点确认的同一同步栈里就弹出「正在处理，
# 请稍候。」（见 passengerInfo_js.js 的 O()/E() 首句），与本单结果无关。
# 命中即 wait——若照它的固定链接文字判 dup，会在提交结果回来前就关页面
# （2026-10-15 实跑 7 连误判，每次 CONFIRM→dup 只隔 2-5ms）。
_PENDING_KEYS = ("正在处理，请稍候", "订单已经提交", "系统正在处理中")
# 过程提示里的固定指路链接文案（m() 生成、正文里的噪声）：判 dup/fail
# 前先剔除，避免把「指向订单列表的链接」当成「账号已有重复订单」的证据。
_LINK_NOISE = ("查看订单处理情况", "请点击“未完成订单”", '请点击"未完成订单"')
# 提示框容器 #orderResultInfo_id：12306 的过程提示与失败原因都写在这里。
# 读页面时优先取它拼在正文前，保证诊断日志看得到原文（不被长度截断挡掉）。
_READ_JS = ("() => { const b = document.body ? document.body.innerText : '';"
            " const p = document.querySelector('#orderResultInfo_id');"
            " const t = p ? p.innerText.trim() : '';"  # 空容器只有空白，别当提示
            " return t ? ('【提示框】' + t + '\\n' + b) : b; }")
# 付款页成功词：必须是「席位已锁定」的订单界面（2026-10-18 用户口径：
# 以弹出锁定席位的界面为准）。只看到「待支付 / 订单提交成功」这类泛化词
# 不判成功——那可能是旧单列表、过程中提示或错误页；URL 已到 payOrder 但
# 正文还没渲染出锁定界面时判 wait，下一轮再读。
_PAY_LOCK_KEY = "席位已锁定"
_SEAT_SCAN = ("无座", "硬座", "硬卧", "软卧", "二等座", "一等座", "商务座")

_FLOW_DEFAULTS = {"read_poll_seconds": 0.5, "page_wait_seconds": 30.0,
                  "retry_open_times": 1, "lock_confirm_seconds": 3.0}
# 锁定座位页静置确认窗（秒）：进入 payOrder 后先在原页静置再下结论。
# 12306 点确认后要 2–8 秒才渲染出「席位已锁定」，期间正文可能还是旧页或
# 过程提示；窗内不重下、不关页、不判 dup/fail。2026-10-24 用户口径：
# 锁定页出现后稳 3 秒再判成功，故默认与上限均为 3 秒。
_LOCK_CONFIRM_MIN = 1.0
_LOCK_CONFIRM_MAX = 3.0


def classify_snapshot(url, body):
    """HOLD 快照分类（纯函数，供单测）。

    返回 success / dup / fail / ambiguous / wait：
    - success: 正文出现「席位已锁定」——以锁定座位的订单界面为唯一成功
      口径（2026-10-18 用户要求），不看 URL（真实锁定页 URL 为
      confirmPassenger/initDc；payOrder 分支保留兼容）。
    - ambiguous: 旧单列表（MyOrderNoComplete）/ 订单初始化页（order/init），
      可能是账号里更早的旧单，不能直接当成功，交官方回读。
    - dup: 账号已有未支付订单等，再重试只会得到同样的拒绝。
    - fail: 余票不足 / 订票失败 / 系统繁忙等。
    - wait: 其余情况继续观察；含点确认后的过程提示（「正在处理，请稍候。」
      「订单已经提交…」）——那时结果还没出来，正文里的「未完成订单」
      只是提示框指向订单列表的链接文字，不是重复订单证据。
    """
    if "MyOrderNoComplete" in url or "order/init" in url:
        return "ambiguous"
    if _PAY_LOCK_KEY in body:
        # 「席位已锁定」即成功口径（2026-10-18 用户口径），不看 URL：真实
        # 锁定页 URL 是 confirmPassenger/initDc 而非 payOrder，且其温馨提示
        # 「逾期未支付，系统将取消本次交易」恰好命中 dup 词表——2026-10-24
        # 实跑 D290 曾把刚下成功的单判成 dup，引发逐轮重复下单。
        return "success"
    if "payOrder" in url:
        return "wait"
    if any(k in body for k in _PENDING_KEYS):
        # 提交过程提示（「正在处理，请稍候。」等）：结果还没出来，继续观察
        return "wait"
    clean = body
    for noise in _LINK_NOISE:
        clean = clean.replace(noise, "")
    if any(k in clean for k in _DUP_KEYS):
        return "dup"
    if any(k in clean for k in _FAIL_KEYS):
        return "fail"
    return "wait"


def _emit(logger, text):
    """[流程] 日志：显式 logger（engine=monitor / launcher=launcher）落
    各自日志文件并经 Queue 进 GUI 全局日志区；无 logger 时兜底 print。"""
    if logger is not None:
        logger.info(text)
    else:
        _log(text)


def _read_page(page):
    """读 page.url + body.innerText（HOLD 每轮快照，坑 6 双取证来源之一）。"""
    url = page.url
    try:
        body = page.evaluate(_READ_JS)
    except Exception:
        body = ""
    return url, body


def _judge_text(judge):
    cls, ono, recent = judge.get("cls"), judge.get("order_no"), judge.get("recent")
    raw = (judge.get("raw") or "")[:80]
    if ono:
        return "cls=%s order_no=%s recent=%s raw=%s" % (cls, ono, recent, raw)
    return "cls=%s recent=%s raw=%s" % (cls, recent, raw)


# 官方回读重试口径：首次回读落空多半不是「真没下单」——订单落库有秒级
# 延迟、查询接口也会抖动。有界重试：error（查询失败）/ none（没查到）时
# 最多再查 _JUDGE_RETRY_ROUNDS 轮、间隔 _JUDGE_RETRY_INTERVAL 秒
# （默认 2 轮 × 6 秒，新增等待 ≤12 秒）；查到 unpaid 立即停，其余分类
# （paid / cancelled / blocked 等已是定论）不重试。
_JUDGE_RETRY_ROUNDS = 2
_JUDGE_RETRY_INTERVAL = 6.0
# 回读总时限（秒，审查第 9 条）：一轮 classify_with_time 串行三段查询、
# 每请求 15s、分页上限 25 页，12306 整体变慢时单次回读可达分钟级，且
# 全程占着 profile 独占锁、其它任务等锁先超时。总时限封顶：透传给
# classify_with_time 做段内截断，本函数在重试决策前再检查一次，超限
# 按 error 收尾（没查完不能当「确认无单」的 none）。
_JUDGE_DEADLINE_SECONDS = 45.0


def _judge_official(date, train, passenger_names, t_submit, logger,
                    retry_rounds=None, retry_interval=None, deadline_seconds=None,
                    picked=None):
    """官方订单接口回读（JUDGE）。

    unpaid 且下单时间落在本次提交窗口（recent）→ 本次成功（关全部页面后由
    调用方返回）；其余返回 ambiguous 交上层既有闭环（engine/launcher 的
    ambiguous 处理会再次归因，none/cancelled 走安全重试）。

    回读带 _JUDGE_RETRY_* 口径的有界重试；多轮后仍落空时 flow_judge.cls
    保留最后一轮的分类——error（查询失败）与 none（确认无单）可分辨，
    上层据此决定是否允许重下。retry_rounds / retry_interval 可注入（测试用，
    classify_with_time 本身语义不变）。

    deadline_seconds：回读总时限（默认 _JUDGE_DEADLINE_SECONDS=45s）。
    转成绝对时刻透传 classify_with_time（deadline_ts，三段查询内部截断），
    本函数每轮重试前再核一次总耗时，超限按 error 收尾、不再发起新查询。

    picked：本次实际勾选的乘车人（FILL 阶段 res["picked"]）。成功时写进
    extra["passengers"]，与 _finish_success 的 entry["picked"] 口径对齐——
    任务未点名时 picked 是账号默认乘车人，只靠上层任务名单兜底会落空串；
    不传 picked 时不写该键，留给上层既有兜底。
    """
    if retry_rounds is None:
        retry_rounds = _JUDGE_RETRY_ROUNDS
    if retry_interval is None:
        retry_interval = _JUDGE_RETRY_INTERVAL
    if deadline_seconds is None:
        deadline_seconds = _JUDGE_DEADLINE_SECONDS
    deadline_ts = time.time() + max(0.0, float(deadline_seconds))
    attempt = 0
    while True:
        try:
            cls, ono, raw, recent = classify_with_time(
                date, train, passenger_names, not_before_ts=t_submit,
                deadline_ts=deadline_ts)
        except Exception as e:
            cls, ono, raw, recent = "error", "", "官方订单查询异常: %s" % e, None
        judge = {"cls": cls, "order_no": ono, "raw": raw, "recent": bool(recent)}
        _emit(logger, "[流程] JUDGE：官方回读 %s" % _judge_text(judge))
        if cls == "unpaid" and recent:
            order_no = (recent.get("order_no") or ono) if isinstance(recent, dict) else ono
            extra = {"flow_judge": judge, "order_no": order_no,
                     "page_kind": "official"}
            if picked:
                extra["passengers"] = "、".join(picked)
            return True, "订单已生成（未支付）：官方订单接口回读到本次订单 %s" % order_no, extra
        if cls not in ("error", "none") or attempt >= retry_rounds:
            return False, "订单结果未知：官方订单接口回读无法确认本次下单结果", {
                "reason": "ambiguous", "flow_judge": judge}
        if time.time() >= deadline_ts:
            _emit(logger, "[流程] JUDGE：回读超过总时限 %.0fs，按查询失败收尾"
                          % deadline_seconds)
            judge = {"cls": "error", "order_no": ono,
                     "raw": "官方回读超过总时限 %.0fs" % deadline_seconds,
                     "recent": False}
            return False, "订单结果未知：官方订单接口回读无法确认本次下单结果", {
                "reason": "ambiguous", "flow_judge": judge}
        attempt += 1
        _emit(logger, "[流程] JUDGE：回读%s，%.0fs 后重试（%d/%d）"
                      % ("查询失败" if cls == "error" else "暂无本次订单",
                         retry_interval, attempt, retry_rounds))
        if retry_interval > 0:
            # 睡眠不越过总时限：到点立刻进下一轮，由轮首检查收尾
            time.sleep(min(retry_interval,
                           max(0.0, deadline_ts - time.time())))


# order/init 停滞判据（审查第 8 条）：order/init 是通往 payOrder 的正常
# 中间态，不能一读到就转回读定案。快照（url+正文）连续 _INIT_STALL_TICKS
# 轮与上一轮完全一致，视为页面已停滞（不再自行跳转），才允许转 JUDGE；
# 快照还在变就继续观察，本页 deadline 到期则走最终 JUDGE 兜底。
_INIT_STALL_TICKS = 2


def _save_state_for_judge(ctx, logger):
    """官方回读前先落盘会话：回读读的是 session_from_browser_state() 落盘的
    那份 cookie，不落盘可能读到旧 cookie（原理见 _run_orderflow_core 首轮
    回读前的注释）。落盘失败不中断判定，只记日志（与首轮口径一致）。"""
    try:
        save_state(ctx)
    except Exception as exc:
        _emit(logger, "[流程] 会话落盘失败：%s（官方回读可能读到旧 cookie）" % exc)


# 后台回读线程的 join 上限（秒）：fetch 单发请求自带 http_timeout，正常
# 数秒内返回；超时按「本次成功、单号待查」收尾，不阻塞成功结论。
_JUDGE_JOIN_TIMEOUT = 40.0


def _start_judge_thread(entry, page, date, train, passenger_names, logger):
    """锁定页一出现就后台启动官方订单号回读（2026-10-24 用户口径：静置
    等待期间后台自动核对并更新软件日志，不让用户盯着静屏等结果）。

    回读是纯 HTTP（session 读自落盘的 browser_state），与页面无关，可在
    后台线程进行；save_state 留在主线程（Playwright 上下文不跨线程）。
    结果由 _finish_success join 后取用；流程若从其它出口结束，daemon 线程
    随进程退出，不阻塞。"""
    try:
        ctx = page.context
    except Exception:
        ctx = None      # 测试替身页无 context：跳过落盘（真实页不受影响）
    if ctx is not None:
        _save_state_for_judge(ctx, logger)

    def _run():
        try:
            entry["judge_order_no"] = fetch_unpaid_order_no(
                session_from_browser_state(), date, train,
                passenger_names, entry["t_submit"])
            if entry.get("judge_order_no"):
                _emit(logger, "[流程] JUDGE：官方回读到本次订单号 %s"
                              % entry["judge_order_no"])
        except Exception as exc:
            entry["judge_error"] = str(exc)[:160]
            _emit(logger, "[流程] JUDGE：订单号回读失败（%s），不影响本次成功判定"
                          "（可到 12306「未完成订单」核对）" % str(exc)[:120])

    entry["judge_thread"] = threading.Thread(target=_run, daemon=True)
    entry["judge_thread"].start()


def _finish_success(entry, url, body, date, train, passenger_names, seat_name, logger,
                    headless=False, stop=None):
    """HOLD 读到「席位已锁定」界面：取证（页面席别 + 支付证据 + 官方回读单号），返回成功。

    关全部页面不在此处显式做：冷启动由 ExitStack callback（_safe_close_ctx）
    在 return 时关，warm 由 launcher finally warm.close() 收尾，重开的新页
    同属 ctx 一并关闭——行为满足坑 8「关全部页面」。
    """
    seat_on_page = next((kw for kw in (seat_name,) + _SEAT_SCAN if kw and kw in body), "")
    pay_evidence = _extract_pay_evidence(body)
    warn = ""
    if seat_on_page and seat_on_page != seat_name:
        warn = "（注意：页面显示席别为 %s，与期望的 %s 不一致，请核对订单）" % (
            seat_on_page, seat_name)
    if entry["dlg_seat"] and entry["dlg_seat"] != seat_name:
        warn += "（核对窗曾显示席别 %s，请以订单详情为准）" % entry["dlg_seat"]
    ono = None
    th = entry.get("judge_thread")
    if th is not None:
        # 回读已在锁定页出现时后台启动（_start_judge_thread）：join 取结果；
        # 超时按「本次成功、单号待查」收尾，不改变成功结论
        th.join(timeout=_JUDGE_JOIN_TIMEOUT)
        ono = entry.get("judge_order_no")
        if th.is_alive():
            _emit(logger, "[流程] JUDGE：后台订单号回读 %.0fs 未完成，先按已提交"
                          "成功收尾（可到 12306「未完成订单」核对）"
                          % _JUDGE_JOIN_TIMEOUT)
    else:
        try:
            # 用户可能刚在页面上重新登录过：先落盘会话，再回读，避免读到旧 cookie
            try:
                save_state(entry["page"].context)
            except Exception:
                pass
            ono = fetch_unpaid_order_no(session_from_browser_state(), date, train,
                                        passenger_names, entry["t_submit"])
        except Exception as e:
            _emit(logger, "[流程] JUDGE：订单号回读失败（%s），不影响本次成功判定"
                          "（可到 12306「未完成订单」核对）" % str(e)[:120])
    if ono:
        _emit(logger, "[流程] JUDGE：官方回读到本次订单号 %s" % ono)
    extra = {"passengers": "、".join(entry["picked"]), "seat_on_page": seat_on_page,
             "seat_in_dialog": entry["dlg_seat"], "pay_evidence": pay_evidence,
             "page_kind": "payOrder"}
    if ono:
        extra["order_no"] = ono
    # 成功页就是「席位已锁定」的订单界面：headless 没有窗口可看，直接跳过；
    # 否则保留窗口供核对与支付（关窗 / 手动停止 / 超时才继续）。
    if not headless:
        hold = _pay_page_hold_seconds()
        if hold > 0:
            _hold_pay_page(entry.get("page"), hold, stop)
    return True, "已提交订单（未支付）：%s%s" % (url, warn), extra


def _finish_dup(url, body, logger):
    """任一页读到重复订单特征：关全部页面返回 dup（dup_kind 区分行程冲突）。"""
    clean = body
    for noise in _LINK_NOISE:
        clean = clean.replace(noise, "")
    dup_kind = next(k for k in _DUP_KEYS if k in clean)
    i = body.find(dup_kind)
    hit = body[max(0, i - 60):i + 60].replace("\n", " ")
    _emit(logger, "[流程] JUDGE：网页判定重复订单（%s）url=%s 原文=%s"
          % (dup_kind, url[:120], hit))
    return False, body, {"reason": "dup", "dup_kind": dup_kind, "dup_url": url}


def _close_page_quietly(page):
    """收尾关页（异常吞掉）：warm 模式 ctx 长存，失败页（可能停在确认页）
    不关会一直挂在预热现场里，后续复用时多一张无人看管的页；冷启动由
    ExitStack 在 return 时统一关，提前关无副作用。关页失败不掩盖原始
    失败结论，故吞异常。"""
    try:
        page.close()
    except Exception:
        pass


def _reopen_and_order(ctx, info, date, seat_name, seat_code, passenger_names,
                      purpose, alias_name, purpose_map, should_stop, mark, stage, logger):
    """官方回读确认无 dup 后：新开页面从头重下一单（retry_open_times 次）。

    返回 entry dict（成功提交，加入观察列表）或 (ok, msg, extra)（失败直接
    向上报，与主页面失败同语义）。防重复下单的最后防线在共享的
    _run_fill_submit_confirm 内部：它弹「未处理/未支付订单」即 dup 中止。
    失败分支 return 前先关掉新开的页（见 _close_page_quietly）。
    """
    page = ctx.new_page()
    page.set_default_timeout(20000)
    try:
        _goto_and_query(page, info, date, should_stop=should_stop)
    except Exception as exc:
        _close_page_quietly(page)
        if _window_dead_exc(exc):
            return False, "浏览器窗口异常关闭: %s: %s" % (
                type(exc).__name__, str(exc)[:180]), {"reason": "browser_dead"}
        return False, "浏览器下单异常: %s: %s" % (type(exc).__name__, str(exc)[:180]), None
    mark("query")
    stage("FILL")
    res = _run_fill_submit_confirm(
        ctx, page, info, date, seat_name, seat_code, passenger_names,
        purpose=purpose, alias_name=alias_name, purpose_map=purpose_map,
        should_stop=should_stop, mark=mark, stage_cb=stage)
    if not res["ok"]:
        _close_page_quietly(page)
        return False, res["msg"], res["extra"]
    _emit(logger, "[流程] HOLD：新开页面重下已提交，新旧页并存各自观察")
    return {"page": page, "t_submit": res["t_submit"], "picked": res["picked"],
            "dlg_seat": res["dlg_seat"]}


def _run_orderflow_core(info, seat_name, seat_code, passenger_names, date,
                        headless, purpose, warm, alias_name, purpose_map,
                        should_stop, mark, stage, flow_cfg, logger):
    """锁内七阶段管线本体。"""
    cfg = dict(_FLOW_DEFAULTS)
    for k, v in (flow_cfg if flow_cfg else {}).items():
        if v is not None and k in _FLOW_DEFAULTS:
            cfg[k] = v
    # 配置下限防忙循环：poll=0 会 sleep(0) 打满 CPU，等待窗 0 会一轮即退
    poll = max(float(cfg["read_poll_seconds"]), 0.1)
    # page_wait_seconds / retry_open_times 自 2026-10-24 起不再限制 HOLD：
    # 锁定页没出来前无限等，故这里不再取用；_FLOW_DEFAULTS 仍保留两键，
    # 仅为旧 config.json 兼容（cfg 循环只认 _FLOW_DEFAULTS 里的键）。
    try:
        lock_confirm = float(cfg["lock_confirm_seconds"])
    except (TypeError, ValueError):
        lock_confirm = _FLOW_DEFAULTS["lock_confirm_seconds"]
    lock_confirm = max(_LOCK_CONFIRM_MIN, min(lock_confirm, _LOCK_CONFIRM_MAX))
    train = (info or {}).get("train_code")

    def _stopped():
        return should_stop is not None and should_stop()

    from playwright.sync_api import sync_playwright

    reuse = warm is not None
    with contextlib.ExitStack() as stack:
        # warm 是抢锁之前拿到的（最长等 180s）。等待期间页面可能已被上一位使用者
        # 关掉：不复验就会拿到死 page，后续操作抛 TargetClosedError，被上层泛化
        # 成 browser_dead——其实会话完全正常，只是需要重开一次。
        _usable = getattr(warm, "usable", None)
        if reuse and _usable is not None and not _usable():
            _emit(logger, "[流程] 复用的浏览器页面已失效，改为冷启动")
            # 只回收浏览器资源、不释放 profile/线程锁（锁属外层 finally）；
            # 不关就重开同一 profile 会双实例抢锁（2026-10-19 审查）。
            try:
                warm.close(release_locks=False)
            except Exception:
                pass
            reuse = False
        own_ctx = not reuse
        if reuse:
            p, ctx, page = warm.p, warm.ctx, warm.page
        else:
            p = stack.enter_context(sync_playwright())
            ctx = launch(p, headless=headless)
            stack.callback(_safe_close_ctx, ctx)  # 任何 return 都会关全部页面
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(20000)
        mark("open")
        stage("DETECT")
        if own_ctx:
            ok, who = session_ok(ctx, page=page)
            if not ok:
                return False, "浏览器会话不可用（%s）。请先运行：python browser_order.py login" % who, None
            hit = _goto_and_query(page, info, date, should_stop=should_stop)
            if not hit:
                # 查询命中判定没等到「预订」：后续 FILL 在无按钮的页面上点，
                # 只会把「页面没起来/无票」误报成同一句话（2026-10-19 实测）。
                if _stopped():
                    return False, "手动停止：车票页查询阶段用户已停止", {
                        "reason": "user_stopped"}
                try:
                    closed = page.is_closed()
                except Exception:
                    closed = False
                if closed:
                    return False, "浏览器窗口异常关闭", {"reason": "browser_dead"}
                return False, ("车票页未就绪或 %s 当前不可预订（未等到「预订」按钮）；"
                               "下一轮重试" % train), {"reason": "not_ready"}
        else:
            warm.refresh(info)
        mark("goto")
        mark("query")
        stage("FILL")
        res = _run_fill_submit_confirm(
            ctx, page, info, date, seat_name, seat_code, passenger_names,
            purpose=purpose, alias_name=alias_name, purpose_map=purpose_map,
            should_stop=should_stop, mark=mark, stage_cb=stage)
        if not res["ok"]:
            return False, res["msg"], res["extra"]
        t_submit = res["t_submit"]
        # 2026-10-24 用户口径：下单后只等座位锁定页。锁定页出现后稳
        # lock_confirm 秒才判成功；页面明确报失败直接返回失败；其余
        # 快照（过程提示 / 旧单列表 / 初始化页）一律继续等，不设观察
        # 时限、不转官方回读、不新开页重下——此前「没等锁定页出来就
        # 返回重下、而订单其实已生成待付款」的问题即由此而来。
        # 唯一提前出口：手动停止、滑块超时、浏览器窗口关闭。
        stage("HOLD")
        _emit(logger, "[流程] HOLD：驻留观察网页变化（每 %.1fs 读 url+正文，"
                      "只等锁定页或明确失败）" % poll)
        e = {"page": page, "t_submit": t_submit, "picked": res["picked"],
             "dlg_seat": res["dlg_seat"], "lock_seen_at": None,
             "success_since": None}
        tick = 0
        while True:
            if _stopped():
                return False, ("手动停止：确认已提交但结果未知，"
                                "请先到 12306 核对未完成订单"), {"reason": "ambiguous"}
            try:
                url, body = _read_page(page)
            except Exception as exc:
                if _window_dead_exc(exc):
                    return False, "浏览器窗口异常关闭", {"reason": "browser_dead"}
                url, body = "", ""
            snap = classify_snapshot(url, body)
            on_lock = "payOrder" in url
            if on_lock and e["lock_seen_at"] is None:
                e["lock_seen_at"] = time.time()
                _emit(logger, "[流程] HOLD：进入锁定座位页，静置确认 %.1fs"
                              "（期间不重下、不关页）" % lock_confirm)
            # 锁座确认窗：刚进入 payOrder 的 lock_confirm 秒内正文可能
            # 还是旧页/半渲染，只认「席位已锁定」，不判 dup/fail。
            in_confirm = (on_lock and e["lock_seen_at"] is not None
                          and time.time() - e["lock_seen_at"] < lock_confirm)
            if snap == "success":
                if e["success_since"] is None:
                    e["success_since"] = time.time()
                    _emit(logger, "[流程] HOLD：读到「席位已锁定」，静置 %.1fs"
                                  "确认订单状态保持不变" % lock_confirm)
                    # 静置期间后台启动官方回读并实时落日志（2026-10-24 用户口径）
                    _start_judge_thread(e, page, date, train,
                                        passenger_names, logger)
                elif time.time() - e["success_since"] >= lock_confirm:
                    _emit(logger, "[流程] HOLD：锁定界面稳定 %.1fs（提交后 %.1fs）"
                          % (lock_confirm, time.time() - e["t_submit"]))
                    return _finish_success(e, url, body, date, train,
                                           passenger_names, seat_name, logger,
                                           headless=headless, stop=_stopped)
            else:
                # 锁定信号消失（跳走/半渲染）：重新计时，避免瞬时误判
                e["success_since"] = None
                if in_confirm:
                    pass          # 确认窗内一律 wait，交给下一轮
                elif snap == "dup":
                    if not on_lock:
                        # 重复订单特征（已有未支付订单）：本单未新生成，
                        # 直接返回 dup 交上层处理，绝不重下撞出第二单。
                        return _finish_dup(url, body, logger)
                    # 锁定页里的「未完成订单」多是过程提示的链接文字，
                    # 不能据此关页：继续等锁定词或显式失败
                elif snap == "fail":
                    # 页面明确报失败（出票失败 / 订票失败 / 余票不足 /
                    # 系统繁忙）：直接返回失败，原文即软件显示的文案。
                    _emit(logger, "[流程] HOLD：网页报下单失败，直接返回")
                    return False, body, {"reason": "fail"}
                # wait / ambiguous：过程提示、旧单列表、初始化页——
                # 都只是「还没出结果」，继续等，不转回读、不重下。
            # 滑块出现即停交人工（坑 7）：脚本不绕过。
            # tick%4==1 降频查询，与旧链 250ms 结果循环的滑块查询频率对齐
            if tick % 4 == 1 and _slide_up(page):
                waited = _wait_slide_gone(page, _SLIDE_WAIT_SEC, stop=_stopped)
                if waited == "user_stopped":
                    return False, "手动停止：滑块等待期间用户已停止任务", {
                        "reason": "ambiguous", "need_captcha": True}
                if waited is False:
                    return False, "滑块验证等待超时（%d 秒），请重试" % _SLIDE_WAIT_SEC, {
                        "reason": "ambiguous", "need_captcha": True}
                _emit(logger, "[流程] HOLD：滑块已消失（人工 %.1fs）" % waited)
            tick += 1
            time.sleep(poll)


def run_orderflow(info, seat_name, seat_code, passenger_names, date,
                  headless=False, purpose="ADULT", warm=None, alias_name=None,
                  purpose_map=None, should_stop=None, flow_cfg=None, logger=None):
    """阶段化下单入口。返回 (ok, msg, extra)，契约与 order_via_browser 一致。

    flow_cfg: config.json 的 order_flow 段（read_poll_seconds /
        page_wait_seconds / retry_open_times / lock_confirm_seconds），
        缺省见 _FLOW_DEFAULTS；其中 page_wait_seconds /
        retry_open_times 自 2026-10-24 起不再影响 HOLD（仅配置兼容）。
    logger: [流程] 阶段日志的落点。engine 传 monitor LOG、launcher 传
        launcher LOG（各自日志文件 + GUI 全局日志区）；None 时兜底 print。

    与旧链 wrapper 同款约束（Task 70a）：stale warm 的 close() 必须在
    @_exclusive 之外处理，否则重入分支的 finally 会把 depth 恢复成
    「本线程仍持有锁」的假象。
    """
    # 兼容传 Event 的调用方：统一下沉为可调用（is_set）。契约仍以回调为准。
    if should_stop is not None and not callable(should_stop):
        should_stop = getattr(should_stop, "is_set", None)
    try:
        if warm is not None and not warm.usable():
            try:
                warm.close()
            except Exception:
                pass
            warm = None
        if warm is not None:
            try:
                ok, who = session_ok(warm.ctx, page=warm.page)
            except Exception as e:
                ok, who = False, "预热会话校验异常: %s" % str(e)[:100]
            if not ok:
                _log("  [浏览器] 预热期会话已失效（%s），关闭预热现场改走冷启动" % who)
                try:
                    warm.close()
                except Exception:
                    pass
                warm = None
    except Exception as e:
        if _window_dead_exc(e):
            return False, "浏览器窗口异常关闭: %s: %s" % (
                type(e).__name__, str(e)[:180]), {"reason": "browser_dead"}
        return False, "浏览器下单异常: %s: %s" % (type(e).__name__, str(e)[:180]), None
    return _run_orderflow_locked(
        info, seat_name, seat_code, passenger_names, date, headless=headless,
        purpose=purpose, warm=warm, alias_name=alias_name, purpose_map=purpose_map,
        should_stop=should_stop, flow_cfg=flow_cfg, logger=logger)


@_exclusive(lock_timeout=180)
def _run_orderflow_locked(info, seat_name, seat_code, passenger_names, date,
                          headless=False, purpose="ADULT", warm=None,
                          alias_name=None, purpose_map=None, should_stop=None,
                          flow_cfg=None, logger=None):
    """run_orderflow 的锁内本体（stale warm 已在外层 wrapper 处理）。"""
    tm = {}
    t0 = time.perf_counter()
    t_last = [t0]

    def mark(name):
        now = time.perf_counter()
        tm[name] = round(now - t_last[0], 3)
        t_last[0] = now

    def stage(name):
        mark("stage_" + name.lower())
        _emit(logger, "[流程] %s" % name)

    try:
        ok, msg, extra = _run_orderflow_core(
            info, seat_name, seat_code, passenger_names, date, headless=headless,
            purpose=purpose, warm=warm, alias_name=alias_name,
            purpose_map=purpose_map, should_stop=should_stop,
            flow_cfg=flow_cfg, mark=mark, stage=stage, logger=logger)
    except RuntimeError:
        raise  # 抢锁失败等：交给调用方决定重试节奏
    except Exception as e:
        if _window_dead_exc(e):
            ok, msg, extra = False, "浏览器窗口异常关闭: %s: %s" % (
                type(e).__name__, str(e)[:180]), {"reason": "browser_dead"}
        else:
            ok, msg, extra = False, "浏览器下单异常: %s: %s" % (
                type(e).__name__, str(e)[:180]), None
    if alias_name:
        extra = dict(extra or {})
        extra.setdefault("alias_seat", alias_name)
        extra.setdefault("selected_seat", seat_name)
        if ok:
            msg = "%s（勾选 %s，网页端同价按 %s 下单）" % (msg, seat_name, alias_name)
    _emit(logger, "[流程] LOG：本次下单结论 ok=%s %s" % (ok, (msg or "")[:400]))
    _record_timing(tm, time.perf_counter() - t0, ok, msg, info, seat_name, warm)
    return ok, msg, extra

