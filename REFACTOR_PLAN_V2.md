# 改造计划书 V2（P4-B 剩余项 + B 线门控）

- 日期：2026-10-23
- 基线：main `598250e`（P0–P4 重构已落地）
- 施工分支：`refactor/p4b-a`（基于 origin/main，main 不直改）
- 前置文档：REBUILD_PLAN.md（P0–P4 已完成部分）、HANDOFF.md、RULES.md

## 0. 基线事实（2026-10-23 实测，Linux 校验环境）

| 项 | 实测 |
|---|---|
| ui/app.js | 108,807 字节（≈106KB），ui/ 下仅此一个 JS |
| api.py | 72,153 字节，METHODS 表 39 个 RPC 方法（实测，计划初稿的 37 与单测 docstring 的 41 均已过时），命名空间已就位 |
| electron/preload.js | 1,290 字节 |
| tests/test_all.py | 855 个用例；tests/unit、tests/contract、tests/integration 已部分分层且全绿 |
| tools/ | check.py、shadow_report.py、verify_dualwrite.py 均已存在 |
| 基线测试 | venv（playwright==1.58.0）下 test_all 855 项：仅 3 个已知非代码失败——`test_secret_roundtrip`、`test_passengers_public_secrets_api`（均系非 Windows 平台无 DPAPI 的平台性失败）、`test_atomic_write_json_basic_and_unique_tmp`（已知 flaky，复跑可过）；skipped=4 |

**回归口径（全程适用）**：每一步完成后失败集不得大于上述基线集；`tools/check.py` 静态四项（编译/遗留引用/数据/车站库）必须保持 OK；分层测试必须保持全绿。

**基线补充（2026-10-23 A-3a 期间背靠背实测）**：另有 2 个环境/竞态敏感项，pristine 与施工分支同签名出现，不计入回归：`test_main_calls_auto_start_before_ready`（看门狗线程推送竞态，两边各约 1/6 概率失败）；`test_log_emit_flushes`（测试按本地日期拼日志文件名、logutil 按北京时间，跨日界时窗内可失败）。判读失败集时先剔除这两项与上表 3 项再比对。

## 1. 已定契约（施工不得破坏）

以下四条是判定准确性的既定契约，A/B 线施工触及相关代码时必须保持，并由黄金用例与不变式测试守住：

1. **防重粒度**：已购阻断 key = 日期+车次+区间+乘车人，**不含席别**（席别只是下单参数）。任务只是 key 的容器，fulfilled 终态按 key 生效，不得按任务整体锁死。
2. **in-flight 锁带租约**：同 key 同时只允许一个下单流程；锁设超时自动过期，过期后必须先走官方回读确认才放行，禁止直接放行。
3. **UNKNOWN 恒停 + 人工出口**：未识别文案/超时无结果 → 恒判 UNKNOWN 保守停；停后必须通知用户并提供界面一键「人工确认继续」；多任务同时触发时通知聚合发送，禁止逐条轰炸。
4. **时间戳归因保留**：只认提交时间窗内的订单为本次生成，窗口参数化进 config，不得在拆分 order.py / orderflow.py 时丢失。
5. **脱敏**：任何落盘的判定快照、影子样本、结构化日志，乘车人/凭证字段必须先过 `core/domain/masking`。本项目有授权码进持久日志的 P1 前科，此条为硬约束。

## 2. A 线（不依赖影子，立即执行）

每项一个独立提交；提交前跑该项验收；失败集不得扩大。

### A-1 ui/ ES Modules 拆分
- 内容：`ui/app.js`（108KB）拆为 `ui/pages/*.js` + `ui/store.js` + `ui/components/*.js`，原生 ESM、无构建步骤；`ui/index.html`、`ui/mini.html` 的脚本引用同步改。
- 约束：纯前端结构搬迁，**不改任何行为、不改 RPC 调用名与参数**；全局函数的跨文件引用改为显式 import/export，不留隐式全局。
- 验收：全部新文件 `node --check` 通过；模块图完整性脚本校验（import 路径全部可达、无循环）；在本机（Linux）尽力做 Electron 无头冒烟——**最终验收以用户 Windows 机真启动为准：五个页面逐个点开 + 一次真实 RPC 往返成功**（loadFile 下 ESM 的路径/协议坑只能真机排除）。

### A-2 preload 旧名→新名映射
- 内容：`electron/preload.js` 挂 `_NS_TO_OLD` 反向映射（C4 第二步）：前端可用旧方法名调用，preload 转新命名空间名。
- **push 推送通道本期不做**。原因：api.py 为 HTTP 请求-响应，push 的传输（SSE/WebSocket）、事件序号、断线重连后全量快照三件事未定案；本地单用户场景现有轮询够用。待三件事写成设计并评审后再排期，不阻塞 A 线其余项。

### A-3a api.py 按域拆分（行为不变）
- 内容：39 方法按域落 7 个文件（tasks/grab/orders/passengers/settings/history/session），`api.py` 变启动壳 + 门面：**re-export 全部旧符号**，外部 import 路径零变化。
- 验收：`tests/unit/test_rpc_namespace.py` 等 52 个相关 unit 测试**零改动**全过；全量回归失败集不扩大。若拆分被迫改测试，先停下来修耦合，不硬拆。

### A-3b 统一错误协议（与 A-3a 分开提交、分开上线）
- 内容：RPC 错误统一为 `{code, message, evidence}`；api 侧与前端（含 A-1 拆分后的各 page 模块）、preload 同批适配。
- 约束：协议变更是行为变更，单独提交；上线前前端所有调用点的错误处理逐个核对，不留旧形状假设。
- evidence 口径（施工定案，rpc/common.py 的 reply() docstring 为准）：白名单仅三类——方法名 `method`、异常类名 `exc_type`、原因类别常量 `reason`（如 `invalid_json`/`not_object`）；**禁止**放入请求 params 的任何内容（乘车人等 PII），message 文案与旧版逐字不变。

### A-4 测试四层迁移 + legacy 清理
- 前置：先全仓 grep 生成**真实引用清单**（谁 import legacy、共几处、每处测什么），以清单为准估工作量与排期（此前口头数字 49 用例/198 行与仓库实测对不上，不作依据）。
- 分类处理（禁止「等价改写」一刀切）：
  1. 测通用工具（脱敏/时间/解析）→ 迁 `tests/unit/`；
  2. 测已废弃 UI 行为 → **直接删**（保的是死代码不是业务）；
  3. 测业务流程 → 迁 `tests/integration/`；报文解析类 → `tests/contract/`。
- legacy 删除条件（三条全满足才删）：引用清单清零；删除后 check.py 遗留引用检查与全量回归不退化；删除动作用独立提交并在提交信息标注回滚点（上一提交 hash）。launcher 别名同理。
- 一次提交只做一件事：迁移一个测试类 = 一个提交。

## 3. B 线（门控，条件满足才启动）

**双门分离**：判定线与存储线证据互不通用，分别开门，禁止捆绑上线。

### B-1 判定线（门槛：影子样本数，不是日历天）
- **前置修复（已实证的存量 bug）**：`rpc/orders.py` 的 `_reconcile_tick` 调用从未定义的 `_classify_session()`，每次执行必 NameError 并被 try/except 吞成「[对账] 本轮跳过」——即现行代码的后台对账守护实际从未跑过。对账守护正式上线前必须先修复此 bug 并加一条「tick 真正执行」用例，否则 B-1 的对账部分是假上线。
- 启动条件：影子判定事件数达到约定量（建议 ≥200 次），且成功/拦截/UNKNOWN 三类决策都有真实样本、连续统计零分歧。`tools/shadow_report.py` 须先补：样本数与决策分布输出（无此输出不得判读「零分歧」）；影子样本落盘前过脱敏（见 §1.5）。
- 内容：verdict 影子转正；fulfilled 按 dedup_key 接入 engine 任务终态（§1.1）；对账守护正式上线（间隔进 config、走全局限流器；首次上线间隔保守取值，不写死 5 分钟）。

### B-2 存储线（门槛：双写一致性证据，与影子无关）
- 启动条件：`tools/verify_dualwrite.py` 连续多日输出一致（行数、校验和、抽样字段比对全过）。
- 步骤（分三步、每步可回退）：① JSON 停写、SQLite 单写（JSON 文件保留只读）；② 观察期（建议 ≥7 天日常使用无异常）；③ 才删 filelock/原子写重试/坏档隔离代码。禁止 ①③ 同一次提交。

### B-3 浏览器子进程结构化协议（P2-6/7）
- 启动条件：实跑验证窗口（真实下单链路可观察）。
- 内容：子进程出口切换 `^12306-RPC^` 标记行协议，或结果写独立文件（优先文件，与 capture_session 既有做法一致）；**超时且无结果恒判 UNKNOWN 停手**（§1.3）；order_service 接协议后旧出口保留一个版本周期才删。

## 4. 执行纪律

- 分支施工、逐项提交、逐项验收；main 由用户决定何时合入。
- 每项完成后跑：`python tools/check.py` + 分层测试 + test_all，失败集与 §0 基线比对并留记录。
- B 线任一门槛未达，不提前施工、不口头放行；A 线做完即停下评估，B 线按证据另行启动。
- 施工中发现与本计划书冲突的事实，先更新计划书再继续，不默默偏离。

## 5. 执行状态（2026-10-23 本轮收尾，分支 refactor/p4b-a）

- A-1 ✅（d350d0a）：ui 拆 16 个 ES Module，符号零丢失、import 图无环，Electron 33.4.11 无头冒烟过；**最终验收待用户 Windows 真机**（五页逐个点开＋一次真实 RPC 往返）。
- A-2 ✅（a9b7e33）：preload 映射 39 对（实测数，非初稿 37）；push 仍按计划暂缓。
- A-3a ✅（5f23741）：api.py 1719 行→门面 278 行＋rpc/ 八文件，AST 逐字对账 0 差异（别名提交后的限定名差异除外）；门面 `__setattr__` 传播垫片经独立评审实跑验证。
- A-3b ✅（8bbff0a）：信封 `{code,message,evidence}` 四场景落地，前端零感知（main.js 转 Error(message)），11 个协议用例更新。
- A-4 ◐（8edd23f／6a82e89／ed92649）：gui 引用三遍处置完成——re-point 23 例、删纯死 UI 74 例（855→781）、launcher 别名清除（仓外 `api.launcher` 随之消失，有意不兼容）。**未完两件**：① 钉住 5 个现行语义缺口的 19 例 HOLD 保留（清单见 A4_GUI_CLASSIFICATION.md 风险清单 #8/#13/#14/#15/#16），等用户裁决（修生产代码恢复旧语义，或接受新语义后删 pin）；legacy/gui.py 因此**尚未删除**，删 legacy 的前置动作清单在分类文件文末。② test_all 其余用例逐类迁四层属长尾，本轮只处理了 gui 耦合部分，后续增量推进。
- 独立评审（598250e..ed92649，六个面）：**GO-with-notes**，零 Critical/Important，3 条 Minor 均为文档口径对齐（已随本节回填）。
- B 线：门控未开，本轮未施工；`_classify_session` 存量 bug 仍按 B-1 前置修复登记在案（A-3a 原样保留）。
