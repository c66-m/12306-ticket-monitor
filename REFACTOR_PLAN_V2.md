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
