# 12306 抢票项目规则（每条都是实测结论，改前先读）

## 一、网页端席别
- 12306 网页端不卖「无座」：列表页/接口说「无座有票」，确认页（initDc）的 `#seatType_1` 只下发 硬座/硬卧/软卧。勾「无座」必失败，文案「该车次网页端不提供席别 无座（WZ）；可选：硬座(1)」。
- 规则：`ticket.py` 的 `order_seat_code(seat_name, seat_code, train_code)` 把「无座」按**同价席别**改判下单 —— 无座票价跟同车型最低价席别一样：`ORDER_SEAT_ALIAS={"WZ":"1"}`（普速 Z/T/K/数字 → 硬座）、`EMU_SEAT_ALIAS={"WZ":"O"}`（G/D/C 动车组 → 二等座，否则给动车组提「硬座」必失败）。判定/记账/通知仍按「无座」。调用方必须传 train_code（launcher 下单段、browser_order.order_ticket_via_browser、order.py HTTP 路径都已带）。
- 官方同款逻辑（可当依据）：`https://kyfw.12306.cn/otn/resources/merged/queryLeftTicket_end_js.js` 里 `if (tickets_info[0].seat_type=="WZ") { if (V.queryLeftNewDTO.yz_num!="--") { tickets_info[0].seat_type="1"; tickets_info[0].seat_type_name="硬座" } }`；另有 `seatTypeForHB` 里 `WZ:"1_无座"`、`seatTypeCodeForName` 里 `"1":"硬座"`。
- 卧铺字段是「共享」的：官方 DTO 里 `dd.rw_num=c9[23]`（软卧/动卧/一等卧共用）、`dd.gr_num=c9[21]`（高级软卧/高级动卧）、`dd.yw_num=c9[28]`（硬卧/二等卧），p27(yb_num)/p33(srrb_num) 是死字段（1788 行实测恒空，官方也没人读）。`ticket.py` 按本行 p35(seat_types) 命名：含 F→「动卧」（F=动卧，A=高级动卧，I=一等卧，J=二等卧）；DOM 对应列 id 是 `RW_/GR_/YW_`，页面列头写「软卧/动卧」。**p35 是码直接拼接、无分隔符**（实测 `F`/`OF`/`OFAO`/`JOIO`/`3411`），判码只能子串判断（官方也是 `seat_types.indexOf("A")>-1`）。
- 席别能不能自动下单，只看 `ticket.py` 的 `SEAT_CODE_TO_NAME`（不在表里 = 只显示不抢：抢票路径 `launcher.py:426` 剔除并记日志，监控任务创建时开着自动下单也剔除）。「优选一等座」= D 已补上（官方 `seatTypeForHB` 里 `GG:"D_优选一等座"`）；「一等卧/二等卧/高级动卧」官方码是 I/J/A，但**确认页是否下发未实证**，暂不进码表。
- 「首选席别」输入框（launcher 主界面 + 监控任务对话框）：填了就先抢填的席别，首选全都没票才按勾选顺序（`ticket.seat_pick_order()`）；口径 —— 无座=硬座（展开成 无座/硬座：无座有票按无座记账、下单仍同价改判硬座），无法识别的词按硬座处理（`ticket.seat_priority_list()`），输入框下方有即时解析反馈（`ticket.seat_priority_feedback()`）。监控任务里存 `task["seat_priority"]`（归一化后的列表），engine 命中时把首选席别排到最前（`engine.py` 检查循环里的 pri_seats 前置）。
- **「车次=席别」专属规则**（首选席别输入框的升级语法，`ticket.seat_rules_parse()` / `seat_candidates_for()` 是唯一口径，launcher 抢票与 engine 监控共用）：条目用逗号/顿号/分号分隔，条目内席别用斜杠/空白分隔。`K225=硬座/无座` = 该车专属（**∩ 勾选集**后作为该车完整候选，顺序即优先级；勾选集为空视为不限制；交集空 = 该车跳过，**不回退全局**——用户明确指定了）；不带 `=` 的条目 = 其余车次的全局排序偏好（可超出勾选，与旧口径一致；旧值/旧任务里的列表自动兼容）。匹配循环不变：车次点选顺序优先，每趟车用自己的候选，第一个「有票 ∩ 候选」命中即下单整轮结束。与 `seats_by_date` 取交（按 (车次，日期) 各自求交，空格跳过）。feedback 按车次逐行展示并校验（车次不在列表、与勾选无交集都红字提醒）。**为什么取交集不取"写了就算数"**：现有规则"无法识别的词按硬座"+前缀直通组合，`K225=硬坐` 手滑会静默变成可抢硬座；取交集让它变成"跳过+红字"，错不起。
- **不变量：席别候选必须始终与该车实际 avail 求交**（`seat_candidates_for` 的 avail 参数），不允许脱离 avail 谈席别优先级——"高铁+快车混选一个全局勾选自动各取所抢"的性质全靠它。
- 车次列表下方「该车全部席别」明细（`launcher._render_seat_detail`）：席别名来自本车 p35 码串（`ticket.seat_names_all()` + 全量码表 `SEAT_CODE_NAMES_ALL`），**含无票席别**（绿=有票、灰=无票、橙=候补）；p35 不含 WZ，无座固定补上；p37(houbu_train_flag)=1 时无票席别显示「候补」（实测售完车 p35 仍完整：G1305 `9MOO`、D941 `OF`、1461 `3411`，同时 p11=N、p37=1）。p35 为空时按车型兜底 `SEAT_KIND_SEATS`（G/D/C/普速，`ticket.train_seat_kind()`）。没选车次时显示车型参考 `KIND_SEAT_HINT`。码表外的席别（一等卧/二等卧/高级动卧）只显示、不可抢，勾选项标「不能自动下单」。
- 抢票席别区口径（`launcher._rebuild_seats(available, allnames, houbu)`）：选中车次后按**该车提供的全部席别**刷新（各选中车 `seats_all` 并集，主界面 + 明细条同源），**无票席别也照样列成可勾选项**并标「无/候补」——抢票（票没放/已售完）时勾上它，放票或退票回流后按勾选顺序抢；能不能下单仍只由 `SEAT_NAME_TO_CODE` 决定。未选中车次时才退回全量 `SEAT_OPTIONS` 清单。
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

## 七、结构重构门禁（2026-10-07 起，重构批次 649db10..0dd5c57 落地后的约定）
- 单点口径清单，改这些语义必须同时核对三个调用方：`appcommon.parse_date_range`（日期区间，gui/monitor/launcher 五个入口）、`appcommon.atomic_write_json / replace_with_retry / write_state`（原子写，全库 11 处）、`appcommon.read_state_or_none / quarantine_corrupt`（state.json 三写方 engine/gui/launcher 共享 plumbing）、`config_keys.py`（配置键 ⊆ example 模板，tests 有防漂移回归）。
- 各写方的防护语义是**参数**不是噪音，别「统一」掉：engine 的 deepcopy 快照 + 直写兜底（`fallback_direct=True`）、launcher 的 `_CFG_WRITE_LOCK` + mtime 二次检测、gui 的 guisave 临时名。Windows 读侧 `read_state_or_none` 有 PermissionError 退避重试（与写方 os.replace 撞车窗口）。
- **拆 launcher.py（约 3000 行）门禁**：必须等一次真实抢票验证通过 + 工作树无在途功能；拆前单独出拆分清单（4+ 文件 + launcher.py 门面保 `import launcher` / `python launcher.py` 兼容）给用户确认。
- **拆 browser_order._order_impl（约 579 行）门禁**：必须等「重新登录 + 真实下单取证核对窗原文」的验证窗口；拆时 `mark()` 计时点原样保留、日志格式一行不动。
- 两项拆分做完前，禁止往这两个函数/文件里加新功能（新功能先进 ticket.py/appcommon.py 层）。
