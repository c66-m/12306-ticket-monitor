# 抢票机制推翻重建 —— 实施计划书

> 裁决依据：用户 2026-10-16 指令「全部推翻重建这样的顺序，并且优化保证不出现相同问题，能够跑的通」。
> 重建顺序（用户指定，依次实现）：
> **检测到有票 → 打开网页按预留信息自动填写 → 自动确认信息无误并确认下单（下单后不立马关闭）→ 依据网页后续变化判定下单成败 → 返回软件日志输出。**

---

---

## 0. 实施状态（2026-10-17 批次）

- [x] Task 1-7：已完成。`orderflow.py` 阶段化管线落地；`_order_impl` 的 fill/submit/confirm 段提取为共享函数 `_run_fill_submit_confirm`（旧链继续调用，选择器单源）；双链并存由 `order_flow_mode` 开关分流（默认新链）。全量 `tests\test_all.py` → Ran 754, OK (skipped=15)。
- [x] 配置段：`config.json`/`config.example.json` 增 `order_flow_mode`、`order_flow`（read_poll_seconds/page_wait_seconds/retry_open_times）；`config_keys.py` CONFIG_KEYS 已含两键。
- [x] Task 8 文档：RULES/README/PRODUCT/HANDOFF 机制描述已更新（2026-10-17）。
- [x] 2026-10-15 夜实跑调试：HOLD 首读把 12306「提交过程提示框」里的固定链接文字**「未完成订单」误判成重复订单** → 每次点确认后 2-5ms 就关页面、永远读不到付款页（7 连误判）。已修 `orderflow.py`：① 过程提示词 `_PENDING_KEYS`（「正在处理，请稍候。」「订单已经提交…」）命中即 wait，判定在 dup/fail 之前；② 判 dup/fail 前先剔除提示框的指路链接文案 `_LINK_NOISE`；③ `_READ_JS` 读页面时把 `#orderResultInfo_id` 提示框原文前置（诊断能看到 12306 的 errMsg）；④ dup/结论日志记录 url + 命中上下文，截断 160→400。全量 760 OK (skipped=15)。
- 铁证：`passengerInfo_js.js` 的 `O()`/`E()` 第一句就是 `m("正在处理，请稍候。",false,"",false,"work")`（在点确认的**同一同步栈**里执行），而 `m()` 在 `ai=false` 时固定往 `#orderResultInfo_id` 写入「查看订单处理情况，请点击“未完成订单”」链接 → 该文本进 body.innerText ⇒ 首读必中。
- [ ] Task 9 实跑：需重新登录（`python browser_order.py login`）+ 有票窗口；成功一次后旧 `_order_impl` 才退役。
- 注意：本计划书 §6 的 `C:\develep\产物\12306` 路径已失效，实际项目目录为 `C:\develep\工作目录\111chanwu\12306`。

## 1. 目标与范围

**目标**：推翻现有 `browser_order._order_impl` 单体下单机制及其驱动接法，重建为按用户指定顺序的阶段化管线（状态机）；付款仍由用户在手机上完成（提交后停在未支付）。重建后不得重现 §2 列出的历史故障。

**范围**：
- 推翻：下单机制的编排与驱动方式（`_order_impl` 单体 → 阶段化管线）、下单成功后的窗口生命周期（提交即关 → 读到「待付款」才关全部页面；首读未获即新开页面重下，双页并存读到即关全部，10s 无果退出）。
- 复用（实测正确资产，推翻它们等于推翻正确架构）：`ticket.py` 免登录轮询（查询很轻）、`order.py` 官方接口回读 + 时间归因（唯一事实来源）、会话/锁/原子写基建（`filelock.py`、`appcommon.py`、`.browser_profile` 独占）、DOM 操作序列（12306 实测结论，选择器别乱改）。
- 退役：新链实跑验证成功后，旧 `_order_impl` 才退役（保留到那一刻）。

## 2. 历史坑清单 → 重建约束（「不出现相同问题」的落点）

每条坑来自 HANDOFF.md §0.98 实测记录，重建必须逐条满足：

| # | 历史故障 | 重建约束 |
|---|---|---|
| 1 | 会话失效被误判瞬时 → 无限重试永不失败 | 判据单源 `session_failure_is_permanent`（白名单），检测/下单/兜底三路径统一 |
| 2 | 停止后在途任务停不住、日志刷屏 | `stop_event` 贯穿状态机每阶段检查点，任意阶段可中断 |
| 3 | 有票不自动下单且界面静默 | `auto_order` 全程可见；关开关时显式日志 `hit_no_order` |
| 4 | 提交结果未知被盲重试 → 重复下单 | 防重三层 + ambiguous 三态 + 180s 时间归因，全部保留 |
| 5 | 席别静默买错 | `limit_tickets` 四步强写（事件链→save→强写→读回校验）+ 核对窗/付款页双取证 |
| 6 | 浏览器关闭丢成功信号 | 读到「待付款」才返回，不再提交即关；首读未获即新开页面重下，新开前官方回读快查防重复下单；成功时关闭所有页面 + `browser_dead`/`ambiguous` 标因 |
| 7 | 滑块验证 | 出现即停交人工、不绕过；等待不计入结果倒计时 |
| 8 | 旧单/未完成订单列表被误判本次成功 | 只有 `payOrder` 付款页才判本次成功，其余一律 ambiguous 走回读 |
| 9 | 界面状态与引擎脱节 | 每阶段节点日志带统一 tag，状态变化即时可观测 |

## 3. 新机制管线（顺序 = 用户指定）

```
ticket.py 轮询余票（免登录轻客户端）
   │ 命中「有票 ∩ 席别候选」
   ▼
engine.py / launcher.py（防重三层 → 调用新链）
   ▼
orderflow.py —— 阶段化状态机（新文件，重建主体）
   STAGE_DETECT   余票确认（复用 ticket 命中结果，阶段可跳过）
   STAGE_FILL     打开真实 Edge，按预留乘车人/席别自动填写（复用现有 DOM 序列）
   STAGE_SUBMIT   #submitOrder_id → checkOrderInfo → getQueueCount
   STAGE_CONFIRM  核对窗取证 → 自动确认信息无误 → #qr_submit_id 倒计时自动点击
   STAGE_HOLD     每 0.5s 读页：读到待付款关全部页面返回；首读未获即新开页面重下；10s 无果退出
   STAGE_JUDGE    依据网页后续变化 + order.py 官方回读，判定下单成败
   STAGE_LOG      各阶段节点 + 最终结果落日志（全局日志区 + logs/ 文件）
```

待付款识别（STAGE_HOLD 核心机制，用户指定）：
提交确认后**每 0.5s 获取当前网页信息（page.url + body.innerText）**：
- 读到**「待付款」**（付款页 payOrder + 待支付状态）→ **下单成功**：**关闭所有页面**，返回软件日志输出；
- **第一次读取未获得待付款** → 不干等，直接**新开一个页面重新下单**（新开前先官方接口快查：已生成未完成新订单则判成功、关全部页面返回，防重复下单）；原页面与新页面并存，均每 0.5s 继续读；
- 新旧页面**任一读到待付款**（原页面或新开页面）→ **关闭所有页面**，返回软件输出成功；
- 任一页面读到余票不足 / 占座失败 / 行程冲突 → 失败或 dup，关全部按现有口径输出；未完成订单列表 / 订单初始化页 → ambiguous 交官方接口回读后输出；
- 页面**等待 10s 无果 → 退出**（双页均无果后整体退出，ambiguous 兜底输出）；
- 滑块 #slide_passcode 出现 → 即停交人工，不绕过。

## 4. 文件计划

| 文件 | 动作 | 内容 |
|---|---|---|
| `orderflow.py` | **新建** | 阶段化状态机（约 500-600 行）：阶段枚举、stop 感知、驻留器、判定融合、日志 tag；复用 browser_order 的 DOM 序列与 order.py 归因 |
| `config.json` / `config_keys.py` | 修改 | 新增 `order_flow` 配置段：`read_poll_seconds=0.5`、`page_wait_seconds=10`、`retry_open_times=1`；键进 `config_keys.py` 防漂移 |
| `engine.py` / `launcher.py` | 修改 | 调用点切到 `orderflow`（新增 `order_flow_mode` 开关，默认新链；旧链保留到实跑验证后退役） |
| `tests/test_all.py` | 修改 | 新增 `TestOrderFlow*` 系列 + §2 九条坑的回归用例 |
| `RULES.md` / `README.md` / `HANDOFF.md` / `PRODUCT.md` | 修改 | 更新机制描述、门禁条款（重建即授权动 _order_impl 相关区域）、记录本轮批次 |

## 5. 任务拆解

### Task 1：状态机骨架（orderflow.py）
- 文件：新建 `orderflow.py`
- 步骤：阶段枚举与转换表 → stop 感知的 run 循环 → 每阶段 mark() 计时点 → 日志 tag 规范（`[流程]` 前缀）
- 验收：`python -X utf8 -m compileall -q .` 干净；单测 `TestOrderFlowSkeleton` 全绿

### Task 2：配置段
- 文件：`config_keys.py`、`config.example.json`、`config.json`
- 步骤：新增三个键 → 默认值注入 → 防漂移测试
- 验收：`tests` 配置键漂移用例通过

### Task 3：填单/提交/确认三阶段（复用 DOM 序列）
- 文件：`orderflow.py`
- 步骤：把现有 `_order_impl` 的 fill/submit/confirm 段按阶段重排（选择器、limit_tickets 四步强写、滑块即停逻辑原样保留），阶段间插 stop 检查点
- 验收：离线 mock 冒烟（临时目录，跑完删）

### Task 4：0.5s 读页 + 首读未获即新开重下（STAGE_HOLD）
- 文件：`orderflow.py`
- 步骤：提交后每 0.5s 读当前页面 → 读到「待付款」关全部页面返回 → 第一次读取未获得待付款则新开页面重新下单（新开前官方回读防 dup），新旧页面并存各自 0.5s 读 → 旧页面读到待付款关全部返回 → 各自 10s 无果退出 → 失败页 / 旧单列表即判即出 → 各自日志
- 验收：mock 页面验证读到待付款关全部返回 + 首读未获即新开重下 + 旧页读到关全部 + 双页 10s 无果退出 + 防 dup 回读

### Task 5：判定融合（STAGE_JUDGE）
- 文件：`orderflow.py`、`order.py`（不改归因逻辑，仅提供复用入口）
- 步骤：页面证据（pay_evidence/seat_on_page）+ `classify_with_time` 归因 → 结果三元组落日志
- 验收：`TestOrderFlowJudge` 全绿（ambiguous 三态、dup 行程冲突、时间归因窗口）

### Task 6：engine/launcher 切调用点
- 文件：`engine.py`、`launcher.py`
- 步骤：`order_flow_mode` 开关默认新链；返回值契约与旧链一致（ok/msg/extra），不动防重三层
- 验收：全量测试 `Ran >= 726, OK`

### Task 7：测试与回归
- 文件：`tests/test_all.py`
- 步骤：§2 九条坑每条至少 1 个回归用例（会话白名单、stop 贯穿、auto_order 可见、ambiguous 三态、席别强写、驻留三出口、滑块即停、payOrder 唯一成功页、日志 tag）
- 验收：全量测试 OK；`TEST_REPORT.md` 追加本轮记录

### Task 8：文档更新
- 文件：RULES/README/HANDOFF/PRODUCT
- 步骤：机制描述改为阶段化管线；门禁条款更新（重建完成前 _order_impl 冻结，重建后退役）；记录本轮批次与坑对策表

### Task 9：实跑验证（需重新登录）
- 前置：HANDOFF §9 登录态已失效，先 `python browser_order.py login`（扫码登录一次）
- 步骤：建真实任务（确山→长葛 K1970/K926 硬座/无座，2026-10-17）→ 命中后观察：自动填单 → 自动确认 → 驻留付款页不关闭 → 手机 App 支付 → 网页变化侦测 → 日志完整
- 验收：order_history.json 有 SUBMITTED/paid 记录；logs 当日文件含 `[流程]` 各阶段节点；核对窗原文取证齐全

## 6. 验证口径（跑得通 = 四层全过）

1. `cd C:\develep\产物\12306`（测试必须在项目目录跑，否则 FileNotFoundError 假失败）
2. `python -X utf8 -m compileall -q .` 零输出
3. `python -X utf8 tests\test_all.py` → `Ran >= 726, OK (skipped=15)` + 新增全绿
4. 离线 mock 端到端（模拟有票 → 全管线 → 驻留 → 判定 → 日志，临时目录跑完删）
5. Task 9 真实窗口验证

## 7. 风险与默认值

- 真实验证依赖：重新登录 + 有票时机（任务日期 2026-10-17）；无票则仅完成 1-4 层，实跑留待有票窗口。
- 12306 选择器变更风险：DOM 序列沿用 2026-10 实测结论，若官方改版需重取证（记录在 RULES）。
- 旧链退役节奏：新链实跑成功一次后才删 `_order_impl`，之前双链并存（开关切换）。
- 配置默认值（用户可改）：`read_poll_seconds=0.5`（读页间隔）；`page_wait_seconds=10`（单页无果上限，超时退出）；`retry_open_times=1`（首读未获后新开页面重下次数）。