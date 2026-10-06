# 12306 抢票项目规则（每条都是实测结论，改前先读）

## 一、网页端席别
- 12306 网页端不卖「无座」：列表页/接口说「无座有票」，确认页（initDc）的 `#seatType_1` 只下发 硬座/硬卧/软卧。勾「无座」必失败，文案「该车次网页端不提供席别 无座（WZ）；可选：硬座(1)」。
- 规则：`ticket.py` 的 `ORDER_SEAT_ALIAS = {"WZ": "1"}` + `order_seat_code()` 把「无座」同价改判为硬座下单；判定/记账/通知仍按「无座」，日志写明「勾选 无座，网页端同价按 硬座 出票」。
- 官方同款逻辑（可当依据）：`https://kyfw.12306.cn/otn/resources/merged/queryLeftTicket_end_js.js` 里 `if (tickets_info[0].seat_type=="WZ") { if (V.queryLeftNewDTO.yz_num!="--") { tickets_info[0].seat_type="1"; tickets_info[0].seat_type_name="硬座" } }`；另有 `seatTypeForHB` 里 `WZ:"1_无座"`、`seatTypeCodeForName` 里 `"1":"硬座"`。
- 改判后硬座此刻没票 → 不算永久失败：engine 写 7 秒冷却（`reason="alias_no_stock"`），launcher 继续监控不计数。真连硬座都不下发 → `reason="seat_unavailable"`，永久跳过（dedup=`SEAT_UNAVAILABLE`）。

## 二、页面内部状态优先于界面显示
- 下单读的是内部 `window.limit_tickets[i].seat_type`（`getpassengerTickets()` 拼串提交），只改 `<select>` 的 DOM 没有意义。
- 规则：改席别/票种必须走四步 —— jQuery 官方事件链 → `upadateSavePassengerInfo()` → 强写 `limit_tickets` → 读回校验；不一致就中止、不提交。实现在 `browser_order.py` 席别段与票种段，日志有 `[浏览器] 内部状态 limit_tickets = ...`。
- 票种：`limit_tickets[i].ticket_type`：1=成人票、3=学生票。学生档案买成人票会弹问询窗，点 `#dialog_xsertcj_cancel` 取消继续。

## 三、提交链（选择器别乱改）
- `#submitOrder_id`（DOM click）→ `checkOrderInfo` → `getQueueCount` → 核对窗 `checkticketinfo_id` → `#qr_submit_id` 约 3 秒倒计时（class 从 `btn92` 变 `btn92s` 才可点）→ `confirmSingleForQueue` → `payOrder/init`。
- 规则：200ms 轮询 `#qr_submit_id` 的 class 含 `btn92s` 再点；等不到就用可见确认控件兜底。`#slide_passcode` 一出现就返回 `need_captcha`，不绕验证码。
- 取证：点核对窗前抄窗内原文（`#checkticketinfo_id / #lay-box_id / #orderResultInfo_id / #popup / #confirmDiv`）→ 日志 `[浏览器] 核对窗原文：…`；到支付页再扫席别 → `seat_on_page`/`seat_in_dialog` 一并回传。**以订单详情为准**。

## 四、会话与锁
- `tk`/`uamtk`/`JSESSIONID` 是 session cookie，浏览器一关就没：只能经 `browser_order.launch()` 启动（内部 `ctx.add_cookies(.browser_state.json)`）。直接 `launch_persistent_context(.browser_profile)` 打开是未登录态，点「预订」不跳转。
- 登录态失效判据：`https://kyfw.12306.cn/otn/index/initMy12306Api` 被跳到 `/otn/passport?...`（页面「您好，请登录注册」）→ 先重新登录，别去查别处。
- profile 独占：同进程线程锁 + 跨进程 `.browser_profile.lock`；登录/体检/下单都必须走 `exclusive()`。强杀 python 会留下孤儿浏览器窗口（页面还是旧代码），对账前先确认有没有新进程在跑。

## 五、干活规矩
- 改完所有文件必须 `py_compile` 过一遍；测试用临时目录/临时 state，跑完删干净，别污染 `state.json`、`logs/order_timing.jsonl`、`order_history.json`。
- 动 `launcher_config.json` 的测试先备份（`shutil.copy2` → `.pbak`），结束还原。
- 改完提醒重启才生效：`Get-CimInstance Win32_Process | ? { $_.CommandLine -match "gui\.py|launcher\.py" } | % { Stop-Process -Id $_.ProcessId -Force }`
- 下单优先级按界面点选顺序；开抢前按 `warm_minutes` 预热；耗时看 `logs/order_timing.jsonl`（`python browser_order.py timing`）。
- 待办：K225 2026-10-09 长葛→确山 的未支付订单该去「未完成订单」确认或放弃。
