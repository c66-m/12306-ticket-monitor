# -*- coding: utf-8 -*-
"""阶段化下单管线（REBUILD_PLAN.md 重构产物）。

推翻 browser_order._order_impl 的单体下单链，按七阶段重建：

    DETECT  检测有票 → 打开网页
    FILL    按预留信息自动填写
    SUBMIT  提交订单
    CONFIRM 确认下单（下单后不立即关闭页面）
    HOLD    驻留观察网页后续变化
    JUDGE   依据网页变化判定成败
    LOG     结论返回软件日志

与旧链的关系（双链并存，旧链实跑验证成功后退役）：
- DOM 操作序列由 browser_order._run_fill_submit_confirm 单源共享，
  本文件只做阶段编排、驻留观察与判定，不重复任何选择器。
- 返回值契约与旧链 order_via_browser 一致：(ok, msg, extra)。
  reason 全集：ambiguous / dup(dup_kind) / need_captcha / user_stopped /
  browser_dead / seat_unavailable / alias_no_stock。
"""

import contextlib
import time

from browser_order import (
    _SLIDE_WAIT_SEC,
    _exclusive,
    _extract_pay_evidence,
    _goto_and_query,
    _log,
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
# 付款页成功词：URL 含 payOrder 且正文命中任一才算成功（坑 8：只有付款页
# 判本次成功；正文还没渲染出来时判 wait，下一轮再读）
_PAY_KEYS = ("待支付", "待付款", "支付剩余时间", "席位已锁定", "订单提交成功")
_SEAT_SCAN = ("无座", "硬座", "硬卧", "软卧", "二等座", "一等座", "商务座")

_FLOW_DEFAULTS = {"read_poll_seconds": 0.5, "page_wait_seconds": 10.0,
                  "retry_open_times": 1}


def classify_snapshot(url, body):
    """HOLD 快照分类（纯函数，供单测）。

    返回 success / dup / fail / ambiguous / wait：
    - success: url 含 payOrder 且正文带付款特征词——只有付款页判本次成功。
    - ambiguous: 旧单列表（MyOrderNoComplete）/ 订单初始化页（order/init），
      可能是账号里更早的旧单，不能直接当成功，交官方回读。
    - dup: 账号已有未支付订单等，再重试只会得到同样的拒绝。
    - fail: 余票不足 / 订票失败 / 系统繁忙等。
    - wait: 其余情况继续观察；含点确认后的过程提示（「正在处理，请稍候。」
      「订单已经提交…」）——那时结果还没出来，正文里的「未完成订单」
      只是提示框指向订单列表的链接文字，不是重复订单证据。
    """
    if "payOrder" in url:
        return "success" if any(k in body for k in _PAY_KEYS) else "wait"
    if "MyOrderNoComplete" in url or "order/init" in url:
        return "ambiguous"
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


def _judge_official(date, train, passenger_names, t_submit, logger):
    """官方订单接口回读（JUDGE）。

    unpaid 且下单时间落在本次提交窗口（recent）→ 本次成功（关全部页面后由
    调用方返回）；其余返回 ambiguous 交上层既有闭环（engine/launcher 的
    ambiguous 处理会再次归因，none/cancelled 走安全重试）。
    """
    try:
        cls, ono, raw, recent = classify_with_time(date, train, passenger_names,
                                                   not_before_ts=t_submit)
    except Exception as e:
        cls, ono, raw, recent = "error", "", "官方订单查询异常: %s" % e, None
    judge = {"cls": cls, "order_no": ono, "raw": raw, "recent": bool(recent)}
    _emit(logger, "[流程] JUDGE：官方回读 %s" % _judge_text(judge))
    if cls == "unpaid" and recent:
        order_no = (recent.get("order_no") or ono) if isinstance(recent, dict) else ono
        return True, "订单已生成（未支付）：官方订单接口回读到本次订单 %s" % order_no, {
            "flow_judge": judge, "order_no": order_no, "page_kind": "official"}
    return False, "订单结果未知：官方订单接口回读无法确认本次下单结果", {
        "reason": "ambiguous", "flow_judge": judge}


def _finish_success(entry, url, body, date, train, passenger_names, seat_name, logger):
    """HOLD 读到付款页：取证（页面席别 + 支付证据 + 官方回读单号），返回成功。

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
    try:
        ono = fetch_unpaid_order_no(session_from_browser_state(), date, train,
                                    passenger_names, entry["t_submit"])
    except Exception:
        ono = None
    if ono:
        _emit(logger, "[流程] JUDGE：官方回读到本次订单号 %s" % ono)
    extra = {"passengers": "、".join(entry["picked"]), "seat_on_page": seat_on_page,
             "seat_in_dialog": entry["dlg_seat"], "pay_evidence": pay_evidence,
             "page_kind": "payOrder"}
    if ono:
        extra["order_no"] = ono
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


def _reopen_and_order(ctx, info, date, seat_name, seat_code, passenger_names,
                      purpose, alias_name, purpose_map, should_stop, mark, stage, logger):
    """官方回读确认无 dup 后：新开页面从头重下一单（retry_open_times 次）。

    返回 entry dict（成功提交，加入观察列表）或 (ok, msg, extra)（失败直接
    向上报，与主页面失败同语义）。防重复下单的最后防线在共享的
    _run_fill_submit_confirm 内部：它弹「未处理/未支付订单」即 dup 中止。
    """
    page = ctx.new_page()
    page.set_default_timeout(20000)
    try:
        _goto_and_query(page, info, date, should_stop=should_stop)
    except Exception as exc:
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
    page_wait = max(float(cfg["page_wait_seconds"]), 1.0)
    retry_left = max(int(cfg["retry_open_times"]), 0)
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
            _goto_and_query(page, info, date, should_stop=should_stop)
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
        stage("HOLD")
        _emit(logger, "[流程] HOLD：驻留观察网页变化（每 %.1fs 读 url+正文，"
                      "每页 %.0fs 无果退出）" % (poll, page_wait))
        pages = [{"page": page, "t_submit": t_submit, "picked": res["picked"],
                  "dlg_seat": res["dlg_seat"], "deadline": time.time() + page_wait}]
        first_round = True
        tick = 0
        while pages:
            if _stopped():
                # 已点确认（HOLD 内所有页都已提交过）：结果未知，交官方回读
                return False, ("手动停止：确认已提交但结果未知，"
                                "请先到 12306 核对未完成订单"), {"reason": "ambiguous"}
            for e in list(pages):
                try:
                    url, body = _read_page(e["page"])
                except Exception as exc:
                    if _window_dead_exc(exc):
                        pages.remove(e)
                        continue
                    url, body = "", ""
                snap = classify_snapshot(url, body)
                if snap == "success":
                    _emit(logger, "[流程] HOLD：读到付款页（%.1fs）" % (time.time() - e["t_submit"]))
                    return _finish_success(e, url, body, date, train,
                                           passenger_names, seat_name, logger)
                if snap == "ambiguous":
                    _emit(logger, "[流程] HOLD：落在旧单列表/订单初始化页，走官方回读")
                    return _judge_official(date, train, passenger_names,
                                           e["t_submit"], logger)
                if snap == "dup":
                    return _finish_dup(url, body, logger)
                if snap == "fail":
                    _emit(logger, "[流程] JUDGE：网页判定下单失败")
                    return False, body, None
                # 滑块出现即停交人工（坑 7）：脚本不绕过，等待不计入该页
                # 观察倒计时（顺延 deadline，与旧链结果循环同口径）；
                # tick%4==1 降频查询，与旧链 250ms 结果循环的滑块查询频率对齐
                if tick % 4 == 1 and _slide_up(e["page"]):
                    waited = _wait_slide_gone(e["page"], _SLIDE_WAIT_SEC, stop=_stopped)
                    if waited == "user_stopped":
                        return False, "手动停止：滑块等待期间用户已停止任务", {
                            "reason": "ambiguous", "need_captcha": True}
                    if waited is False:
                        return False, "滑块验证等待超时（%d 秒），请重试" % _SLIDE_WAIT_SEC, {
                            "reason": "ambiguous", "need_captcha": True}
                    e["deadline"] += waited
                    _emit(logger, "[流程] HOLD：滑块已消失（人工 %.1fs，不计入观察倒计时）" % waited)
                if time.time() > e["deadline"]:
                    pages.remove(e)
                    _emit(logger, "[流程] HOLD：页面观察 %.0fs 无果退出" % page_wait)
            if not pages:
                break
            tick += 1
            if first_round:
                # 首读未获：先官方回读防 dup（已生成未完成新订单=成功关全部），
                # 仍无则新开页面重下，新旧页并存各自 0.5s 读、各自 10s 退出
                first_round = False
                # 提交 → 12306 落库有 1-3 秒延迟。提交后立刻回读必然判「结果未知」，
                # 上层据此重开页面从 DETECT 重新下一单 —— 等于每次下单提交两单，
                # 占两次座、各计一次取消，直接喂大「取消次数过多」封禁。
                # 首轮回读前先补足最短落库等待（上限 3s，不放大用户体感）。
                left = (t_submit or 0) + min(3.0, float(page_wait)) - time.time()
                if left > 0:
                    if _stopped():
                        return False, ("手动停止：确认已提交但结果未知，"
                                        "请先到 12306 核对未完成订单"), {"reason": "ambiguous"}
                    time.sleep(left)
                # 官方回读（classify_with_time / fetch_unpaid_order_no）读的是
                # session_from_browser_state() 落盘的那份 cookie，而 save_state 正常
                # 只在 ctx.close() 时写。中间这一段时间里，浏览器里的会话是有效的、
                # 磁盘上的可能是过期的（用户刚在页面上重新登录过就会这样），于是回读
                # 恒报未登录，一次成功下单被判成 ambiguous 再重下一单。回读前先落一次。
                try:
                    save_state(ctx)
                except Exception as exc:
                    _emit(logger, "[流程] 会话落盘失败：%s（官方回读可能读到旧 cookie）" % exc)
                ok, msg, extra = _judge_official(date, train, passenger_names,
                                                 t_submit, logger)
                if ok:
                    return ok, msg, extra
                if retry_left > 0:
                    if _stopped():
                        # 已提交、官方回读未确认：结果未知交上层，绝不带病重下
                        return False, ("手动停止：确认已提交但结果未知，"
                                        "请先到 12306 核对未完成订单"), {"reason": "ambiguous"}
                    retry_left -= 1
                    rep = _reopen_and_order(
                        ctx, info, date, seat_name, seat_code, passenger_names,
                        purpose, alias_name, purpose_map, should_stop, mark, stage, logger)
                    if not isinstance(rep, dict):
                        return rep
                    rep["deadline"] = time.time() + page_wait
                    pages.append(rep)
            time.sleep(poll)
        # 所有观察页超时退出：JUDGE 兜底（坑 8：交官方回读闭环，上层还会再判）
        stage("JUDGE")
        return _judge_official(date, train, passenger_names, t_submit, logger)


def run_orderflow(info, seat_name, seat_code, passenger_names, date,
                  headless=False, purpose="ADULT", warm=None, alias_name=None,
                  purpose_map=None, should_stop=None, flow_cfg=None, logger=None):
    """阶段化下单入口。返回 (ok, msg, extra)，契约与 order_via_browser 一致。

    flow_cfg: config.json 的 order_flow 段（read_poll_seconds /
        page_wait_seconds / retry_open_times），缺省见 _FLOW_DEFAULTS。
    logger: [流程] 阶段日志的落点。engine 传 monitor LOG、launcher 传
        launcher LOG（各自日志文件 + GUI 全局日志区）；None 时兜底 print。

    与旧链 wrapper 同款约束（Task 70a）：stale warm 的 close() 必须在
    @_exclusive 之外处理，否则重入分支的 finally 会把 depth 恢复成
    「本线程仍持有锁」的假象。
    """
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
