# 全仓深审报告（2026-10-23 晚，refactor/p4b-a @ d4aab83）

六簇并行通读（RPC 层 / 下单核 / 引擎与抢票 / 支撑模块 / 判定与状态 / 前端与壳），
每条带失败场景；P1 级与现网最重条目由 nimo 亲自复读代码核实。
合计 **52 条：P1×2、P2×23、P3×27**。不含：已登记待裁决的 5 个语义缺口、今晚已修的登录 A / 下单 B1/B2 / 侧栏调整。

## P1（均在门控未上线的判定内核，B 线开门前必修）

1. **TripKey 缺「区间」分量，违反计划书 §1 契约①**（core/domain/tripkey.py:56-61、core/verdict/classifier.py:50-53）
   key 只有日期+车次+乘车人；订单条目本身带 from/to、旧逻辑按区间过滤，新内核丢了这一维。
   场景：已购同车次短区间 → 长区间任务被 precheck 判 BLOCK_PAID 永久买不了；postconfirm 也会把短区间单误判为本次 SUCCESS。对账继承同一误判。
   修：TripKey 增 from/to（站名规范化）并纳入 composite 与比对，构造点同步传区间。

2. **「双读确认」被 60 秒快照缓存架空**（app/order_service.py:64-86、adapters/railway/snapshot.py:31-40）
   双读间隔 2 秒 < 缓存 TTL 60 秒，第二次 fetch 必然命中同一份快照；invalidate() 全仓无调用方。
   docstring 明写的「single 结论永不直接驱动重试」纪律实际不存在。
   场景：首读时订单未落库（NONE）→ 双读实为同读 → RETRY_SAFE 被采纳重试 → 原单已成 → 重复下单/被挡单。
   修：第二次 fetch 前 invalidate（或 fetch 加 force），加测试断言两次真打 provider。

## P2（23 条）

### 现网最重（抢票误报成功）
3. **grabber dup 兜底 else 把 none/cancelled/error 全报成功并发成功邮件**（grabber.py:589-624）
   浏览器判 dup 后回读 classify 只有 paid/unpaid 两档单列，其余全落 else 置 result=(True,…)，_notify_success 无条件调用。
   engine 同名分支对 none/cancelled 是安全重试口径，两侧相反。
   场景：旧单刚被取消或官方查询失败 → 抢票报「成功」+成功邮件、任务终止，用户以为票已到手实际无单可付。
   修：else 拆开——none/cancelled 走安全重试，error/unknown/blocked 落待核对，只有确认有可付订单才许报成功。

### RPC 层
4. **rpc_dispatch 对非字符串 method 直接崩进程**（api.py:230-235、281-282）
   `{"method":123}` → TypeError 冒泡出 stdin 主循环，后端进程退出，引擎/抢票线程全灭。已实跑复现。
   修：分发前 isinstance(method, str) 校验回 bad_request；主循环再兜一层 try。
5. **config/state 子结构形状零防御，手改坏一处核心 RPC 恒崩**（rpc/tasks.py:41-43、197、238-240；rpc/common.py:396）
   state.tasks 改成 list / tasks 混入字符串条目 / task_defaults 写成 list → AttributeError；listMonitorTasks 每 2 秒轮询，监控页恒报错无降级无定位。已实跑复现。
   修：遍历跳过非 dict 条目并 _warn；state.tasks、_task_defaults 非 dict 按 {} 处理。
6. **_grabbers 注册表三线程无锁读写**（rpc/grab.py:79-101、131、271-300）
   armed 线程与分发线程同秒「查活→新建→注册」非原子 → 重复 Grabber，后写覆盖先者成孤儿线程（停不到、结果无人消费）；ticker 删除还可能误删新注册实例。
   修：注册表加锁，查活+注册与删除都进锁；ticker 删除前校验实例同一性。

### 下单核
7. **HOLD 中途 ambiguous 分支回读前未落盘会话**（orderflow.py:420-423）
   B1 给 fail 分支与最终 JUDGE 都加了 _save_state_for_judge，唯独此分支漏了。
   场景：途中刚重登录/cookie 滚动，磁盘仍旧 cookie → 回读失败 → 本可确认的订单降级待核对。
   修：return 前补 _save_state_for_judge 对齐另两处。
8. **读到 order/init 立即回读定案，绕过页面跳转与首轮 8 秒落库等待**（orderflow.py:420-423）
   order/init 是通往 payOrder 的正常中间态，第一轮读页（约 0.5s）即命中转 JUDGE；落库慢于回读窗时页面本会自跳的 success 被提前定成待核对。
   修：order/init 未超时前按 wait 继续观察，仅停滞/deadline 到期才转回读。
9. **JUDGE 回读无总时长上限**（orderflow.py:146-187、order.py:596-690）
   一轮三段查询、每请求 15s、分页上限 25 页、error 再重试 2 轮——最坏分钟级，全程占 profile 独占锁，其它任务等锁 180s 先超时。
   修：JUDGE 设总 deadline（如 45s），分页预算计入总窗。
10. **成功后付款页保留在独占锁内（默认 300s、上限 1200s）**（browser_order.py:779；orderflow.py:234-236；browser_order.py:1839-1841）
    任务 A 进 5 分钟留窗 → B/C 的下单与会话体检全排队，B 的车次在保留期内开售即漏单。
    修：hold 移锁外（结论先落盘再释放锁），或多任务活跃时自动缩短/跳过。
11. **find_duplicate 把席别纳入排除条件，与契约①（key 不含席别）不一致**（order.py:734）
    同行程已有未支付硬座单时再下二等座，本地防重放行 → 被 12306 未完成订单规则拒绝，白耗一次提交。服务端兜底，不会双下单。
    修：未支付防重不以席别排除，席别差异仅记日志。

### 引擎与抢票
12. **CLI「恢复/重置」被引擎防覆盖守卫静默拒绝却打印成功**（monitor.py:658、666 ↔ engine.py:831-838）
    菜单操作未传 force=True，paused/cancelled 任务改不回 monitoring，只记 INFO；CLI 照打「已恢复监控」。
    场景：GUI 暂停后改走 CLI 恢复 → 引擎根本不调度，用户以为在监控实际漏抢。
    修：菜单 1–4 项显式用户操作统一传 force=True。
13. **删除监控任务不中止在途下单**（engine.py:825、1022-1056）
    note_task_deleted 只清 state+墓碑；在途 _order_once 的 should_stop 不感知删除，成功后 _record_success 写历史+发成功邮件无墓碑复检。
    场景：任务下单中用户删除它 → 订单照样提交成功、成功邮件照发，与删除意图相反。
    修：删除合并进 should_stop；_record_success/通知前复检墓碑。
14. **同名监控任务互相遮蔽**（engine.py:1635-1734；grabber_utils.py:290-293）
    调度/状态全按任务名为键，但 build_monitor_task 任务名只含区间/车次/席别——仅日期或乘车人不同的两任务必然同名，引擎不去重不告警。
    场景：两个仅日期不同的任务 → 每轮只投递靠前一条，另一日期静默永不监控；暂停/恢复还串扰。
    修：启动/同步检测重名告警（或调度键改 uid）；任务名至少并入日期。

### 支撑模块
15. **非字符串 password 被 {0!r}/%r 原样回显进错误消息（历史 P1 同型残留）**（notify.py:89、passengers.py:201）
    手改配置把授权码写成无引号数字 → 发送失败消息经 rpc/settings 返回界面、CLI 打印、日志落盘，授权码明文进消息面。
    修：两处只报类型名不报值。
16. **SMTP 授权码走未校验证书的 TLS**（notify.py:112-115）
    SMTP_SSL/starttls 均未传 context，smtplib 默认 CERT_NONE。中间人冒充 smtp 服务器即可截获 login 的授权码。
    修：两处传 ssl.create_default_context()。
17. **全写路径无 fsync，原子写不防断电**（appcommon.py:231、208；全仓生产代码零 fsync）
    tmp 写完不 fsync、replace 后不 fsync 目录。刚写完即断电/崩溃 → config/state/orders/passengers/session 任一写入可能整体丢失或回滚，无报错。
    修：atomic_write_json 内 tmp fsync + replace 后父目录尽力 fsync（Windows 分支处理）；日志类高频小写可豁免。

### 判定与状态（B 线基础设施）
18. **dualwrite 的 ROOT 少剥一层，生产双写静默永不执行**（adapters/store/dualwrite.py:23-24）
    DATA_DIR 算成 …/adapters 而非仓库根；无 T12306_DATA_DIR 时 _is_data_target 恒 False，mirror 静默 return 无告警；测试因设了 env 全绿恰好掩盖。已实跑证实。
    后果：B-2 将来拿双写一致性做开门证据会建立在空副本上。
    修：ROOT 改三层与 db.HERE 对齐，补一条不带 env 的守卫单测。
19. **reconcile 把 blocked 写回后永久退出复核集**（app/reconcile_service.py:30、106-118）
    PENDING_CLASSIFIES 只有 {unpaid, unknown, error}；被写成 blocked 的记录此后不再复查，其订单后来支付也永不触发 order.paid；report 还把 blocked 计入 none，口径误。
    修：BLOCKED 按待决处理；none 计数只计 NONE。
20. **scheduler peek/pop 非原子竞态可吞掉周期任务**（app/scheduler.py:95-112）
    锁内 peek 取 name、锁外比较、再加锁 heappop 却丢弃弹出值按旧 name 执行——两步间有更早到期项推入时，pop 弹走的是新任务条目，该任务永不再调度、无日志。
    修：以 pop 元组为准并校验 name，或 peek+pop 同一临界区。

### 前端与壳
21. **call() 无条件 toast+记日志，后端重启期间错误刷屏**（ui/store.js:9-18；ui/app.js:128-131；ui/pages/grab.js:352-368）
    后端重启退避最长 30s 内，1s/2s 轮询每次失败都刷新 toast 并写一行日志，400 行缓冲冲掉真实记录。
    修：call() 加 quiet 选项，后台轮询走 quiet，用户操作保留现状。
22. **登录失效弹窗劫持单一 #modal，销毁正在编辑的表单**（ui/components/modal.js:5-11；ui/components/session.js:87；ui/app.js:29-32）
    填任务表单/乘车人编辑/删除确认期间来失效事件 → openModal 无条件替换内容，未保存输入无声丢失。
    修：有未关闭业务弹窗时失效提示降级 toast+会话卡红字，表单关后再弹。
23. **ambiguous 任务在 mini 小窗被过滤不可见**（ui/mini.js:70-71）
    rows 只取 success 与 monitoring/retrying；B2 的待核对终态在小窗直接消失（.st-ambiguous 样式已备但用不上），守小窗的用户以为任务没了、不会去核对。
    修：ambiguous 与 success 一样作为「需关注结果」置顶展示。
24. **关动效（motion=off）时 animationend 永不触发**（ui/style.css:39-41；ui/store.js:84-91；ui/pages/settings.js:84-90）
    旧页面只在 animationend 里移除 → 关动效后每次切页堆一层旧页面（重复 id、倒计时写到不可见旧节点）；设置面板点关闭永远撤不掉 .show。
    修：清理动作加超时兜底或 motion=off 时走同步移除。
25. **乘车人保存不查后端结果级失败，指纹冲突假报「已保存」**（ui/pages/pax.js:109、22、35、40）
    后端按 expect_stamp 拒写返回 {ok:false} → 前端照样关窗+toast 已保存+刷新，编辑静默丢失（settings 的 saveSettings 有处理，pax 没对齐）。
    修：四处 savePassengers 统一包装查 r.ok/r.error，失败报错不关弹窗。

## P3（27 条，摘要）

- RPC：reconcileHistory 的 limit 未捕获 ValueError（rpc/orders.py:101，与同族口径不一致）；startEngine/stopEngine 未列入慢方法，启停可冻结全量 RPC 数秒至 10s（api.py:198-201）；stdin 主循环单行无长度上限（api.py:281-282）。
- 下单核：HTTP 链 confirm 非 JSON 时同 token 重发、首次已落单会被误判失败（order.py:833-845，非默认链）；find_duplicate 异常被 print 后继续下单、防重静默失效（order.py:1003）；等 profile 锁整段阻塞、停止信号最长 180s 不响应（browser_order.py:324/360、orderflow.py:579）；warm 模式重下失败未关页、野页面残留预热 ctx（orderflow.py:253-277）；下单成功记账的乘车人未点名时落空串（browser_order.py:2222）。
- 引擎：dup 判 unpaid 且 stop_after_order=False 时同轮逐席别重复走浏览器下单（engine.py:1120-1140）；任何 config 变更让全部活动任务立即提前轮询一轮、作废失败退避（engine.py:1703-1707）；seats_by_date 形状未消毒，手改坏时每轮内部异常静默永不恢复（engine.py:950、963-966）；ambiguous_retries 只增不减跨目标累计，与「连续多次」文案不符（grabber.py:411、566-578）。
- 支撑：save_passengers 落盘不收紧权限，明文降级时证件数据 0644 裸奔（passengers.py:456）；保存被指纹守卫放弃时仍用 print 告警、GUI 下不可见（passengers.py:445）；note_station_missing 无节流，查不到站即全量强制下载、可随轮询无限重复（ticket.py:548）；capture_session 登录成功落盘后裸 browser.close() 可把成功变失败退出码（capture_session.py:288、262）。
- 判定：scheduler 下次到期用执行前旧 now 锚定、长任务会立即连跑（app/scheduler.py:107-112）；classify_snapshot 按快照顺序短路、UNKNOWN 靠前会盖过 PAID（core/verdict/classifier.py:97-103）；migrate 备份承诺 md5 校验但只 copy2 从不校验（adapters/store/migrate.py:10-12、86-101）；find_earliest_unpaid 缺 order_ts 时 TypeError（已复现，当前无调用方，潜伏）（core/verdict/snapshot_view.py:66-76）。
- 前端：pyCall 60s 超时只在前端 reject、后端不取消、迟到响应丢弃（electron/main.js:52-59、126-145）；主进程同一次子进程失败 error/exit 双路处理、退避叠加，stderr 按 chunk 广播会截断/乱码（electron/main.js:102-118、147-153、158-172）；refreshPax 无内部守卫、后端抖动时任务表单弹窗打不开（ui/pages/pax.js:124-129 等）；改名残留——弹窗标题仍「新建监控任务」、搜旧名不再命中该页（ui/pages/monitor.js:345；ui/store.js:56-63）；监控统计条不单列 ambiguous（ui/pages/monitor.js:104-110）；preload 映射表整体暴露 window.api、onPush 无退订（electron/preload.js:63-88）；管理模式批量删除不查逐项结果、部分失败仍报全删（ui/pages/monitor.js:239、248）。

## 契约核对（判定簇必答项）

- ① 防重 key：**违反**（见 P1-1，缺区间；不含席别一半符合；另下单核 find_duplicate 反在席别上排除，见 P2-11）
- ② in-flight 租约先回读：本簇无实现（不在 core/app/adapters）；其「放行前真实回读」原则被双读缓存架空（见 P1-2）
- ③ UNKNOWN 恒停+人工出口：**符合**（classifier→UNKNOWN、verdict 恒 HALT_MANUAL、task_sm AMBIGUOUS 终态仅人工可出，三侧一致）
- ④ 时间戳归因：**基本符合**（窗口参数化 [t−5s, t+180s] 且要求全乘车人命中；缺口仅 P3 的 find_earliest_unpaid 潜伏缺陷）
- ⑤ 落盘先脱敏：**符合**（attempts 台账、对账写回、事件/邮件 payload 逐一核过，未发现未脱敏落盘点）

## 建议修复批次

- **R1 现网 P2（20 条）**：第 3–17、21–25 条——都在当前运行路径上，先修。
- **R2 B 线内核（P1×2 + 第 18–20 条 + 判定簇 P3）**：B-1/B-2 开门前置，与 B 线门控合并验收。
- **R3 P3 长尾**：其余 P3 批量收。

## R1 完成记录（2026-10-23 深夜）

20 条全部修复，独立评审 GO-with-notes（零 NO-GO）。提交：6f95c6e（下单核）/43e338d（前端）/dc836fa（支撑）/b3e44c2（RPC）/7670bcc（引擎）/cb92340（测试），已 push（远端 HEAD cb92340）。全量 863 例，失败集=基线 3 项（DPAPI×2＋自动启动竞态项），新增测试 44 例全绿。评审四条 note 登记为 R3 增补：
- N1：第 9 条 deadline 是软顶——单个在途请求仍可吃满其 15s 超时，最坏 ≈60s；若要硬顶 45s 需把单请求 timeout 与剩余预算联动。
- N2：第 14 条只做到告警＋建名改进，存量同名任务在用户改名前仍互相遮蔽；调度键 uid 化留待后续。
- N3：第 5 条 `_warn_shape_once` 的 detail 用 %r 回显了坏条目原文（config 任务数据、非凭证，低风险，口径一致性清理）。
- N4：第 17 条 fallback_direct 直写路径的内容本身未单独 fsync（只有事后目录 fsync），在尽力而为范围内，备查。

## R2 完成记录（2026-10-23 深夜）

9 条全部修复，独立评审 GO-with-notes（零 NO-GO；两条 P1 的测试均为真 provider/反例级构造）。提交：判定内核九项 + engine 影子比对接线补区间，远端 HEAD 50f4806。tests/unit 69 例全绿、verify_dualwrite 与 shadow_report 均通过、全量 863 例失败集仍为基线集。评审新 note 登记为 R3 增补：
- N5：reconcile 把 BLOCKED 纳入复核集后，blocked 记录每轮对账都会重复 fire `order.reconciled`——启用 email 通知时形成周期性邮件；需给该事件加边沿触发（仅 classify 变化时 fire）。
- 备查（不修）：TripKey 与 find_duplicate 的站名空白归一差异（TripKey 更宽容，无口径冲突）；migrate 中止后磁盘残留不完整备份目录（有 errors 报告兜底）；report 中 cancelled 不再计入 none（与「none 只计 NONE」方向一致，summary 数字与旧版不可直接对比）。

## R3 完成记录（2026-10-23 深夜）——52 条全闭环

P3 长尾 23 条 + 评审 note 4 条（N1/N3/N4/N5）全部修复，独立评审 GO-with-notes（零 NO-GO）。新增后端测试 50 例、前端行为断言 29 条（脚本落盘 tools/ui-checks/）。评审提出的两点已当场处理：① 统计条 ambOnly 过滤死角（待核对归零时 chip 消失但过滤仍在、列表永久空白）已加自愈复位；② 前端断言脚本已落盘。其余 note 级残余（均已在代码/评审中声明、可接受）：stdin 行长恰界丢弃、分片等锁总时长口径（两把锁共用一个 timeout，最坏等待减半、方向更严）、站名节流 check-then-set 非原子、spawn 后运行时 error 的守卫边角、N2 同名任务调度键 uid 化（报告允许「告警」方案，已按告警实现，uid 化为可选增强、未排期）。

**总账**：深审 52 条（P1×2、P2×23、P3×27）经 R1/R2/R3 三批全部关闭；分支自 d4aab83 起共新增测试 111 例（863→910 主套件 + unit 52→72），最终全量失败集仍为基线集（DPAPI 两项 + atomic flaky + 自动启动竞态项，均非代码问题）。
