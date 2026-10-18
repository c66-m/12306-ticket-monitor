# 项目全面检查与测试报告

日期:2026-10-06  ·  基线:a8b4b57(含并行会话的在途改动)

> **2026-10-16 复核：本报告是历史存档**——第一~九节的结论与其成因分析保留价值，但**已不是当前事实基线**。
> 现状：`tests/test_all.py` 为 **726 项**（skipped=15），`python tests/test_all.py` 全绿；架构、文件清单、文件行数以 `HANDOFF.md` 为准，业务规则以 `RULES.md` 为准。
> 本轮复核增量见文末「十、2026-10-16 复核增量」。

## 一、测试执行概况

| 层级 | 数量 | 结果 |
|---|---|---|
| 单元测试(ticket/engine/order/notify/logutil/passengers/launcher/gui) | 38 | 全过 |
| 集成测试(engine 状态机、launcher 任务库并发/坏档) | 7 | 全过 |
| 端到端(mock,不打真实 12306):查询命中→自动下单→防重→通知 | 2 | 全过 |

连跑 4 遍全部稳定。真实 12306 的端到端只能由真实购票承担(合规与数据原因,测试不打线上接口)。

## 二、测试发现并已修复的缺陷

1. **[P1·已修] append_monitor_task 并发追加丢数据**(launcher.py):临时文件名固定 `.launcher`,同进程多线程并发时 Windows 上 `os.replace` 撞上对方打开的句柄直接 PermissionError;测试以 3 线程并发稳定复现。修复:临时名带线程标识 + 新增 `_atomic_replace()`(占用退避重试 3 次)。
2. **[P1·已修] mtime 冲突检测存在 ~15.6ms 盲区**:Windows 文件时间戳按系统时钟量化,同进程多次写落在同一时钟片内 mtime 相同,读-改-写误判"无人写过"互相覆盖(测试中 3 个任务只剩 1 个)。修复:新增 `_CFG_WRITE_LOCK` 进程内串行锁,append_monitor_task 全程持锁;跨进程仍靠 mtime 比对(盲区残留,见"遗留风险")。
3. **[P3·已修] 误导性日志**:passengers.py 两处告警硬编码 "passengers.json",读自定义路径(如测试临时文件)时也这么打。已改为带真实路径。

注:首轮跑出的 7 个失败里有 4 个是测试自身的 bug(闭包未绑定循环变量、把 Handler 当 Logger 用、断言未考虑邮件未启用、oldPassengerStr 格式想当然),修正测试后复跑定位出上述真问题——测试套件本身也经过了一轮自证。

## 三、结构与依赖

- 依赖图(AST 级提取):`ticket/passengers/notify/logutil`(基础层)← `order/browser_order`(下单层)← `engine`(调度层)← `gui/launcher/monitor`(界面层)。**无循环依赖**。分层合理,GUI 不直接碰网络下单细节。
- 2026-10-14 起的实际架构：`ui/`(HTML/CSS/JS) + `api.py`(JSON-RPC 后端) + `electron/`(壳)；`gui.py`(Tkinter) 已降级为 legacy，仅因 `tests/test_all.py` 引用而保留。
- `notify → passengers` 的依赖仅为 SMTP 授权码 DPAPI 加解密,方向正确。
- `_demo_pri.py`、`_pri_test.py` 为本地草稿(已被 .gitignore 的 `_*` 规则排除),建议确认无用后删除。（2026-10-16 复核：仓库内已无这两个文件，建议已执行。）

## 四、配置文件审计

- 全部 10 个 JSON(config/config.example/launcher_config/launcher_config.example/grab_tasks/state/order_history/station_kind/station_name/station_index)解析通过;真实 passengers.json 为 DPAPI 密文且完整。
- ~~**[P2] 仓库缺 README.md / requirements.txt / LICENSE**~~ **已解决(2026-10-16 复核)**：README.md、requirements.txt、requirements-dev.txt、LICENSE 均已在库(git ls-files 证实)。历史事实：依赖为 `requests`(必需)、`playwright`(浏览器下单)、`cryptography`(非 Windows 加密兜底)。
- ~~**[P3] config.example.json 未文档化两个代码会读的键**~~ **已解决**：模板已含 `order_mode`(http/browser)与 `browser_headless`。
- **[P3] SMTP 授权码迁移未完成**:notify.py 已支持 DPAPI 加密(`dpapi1:` 前缀),但现网 config.json 里的授权码仍是明文——在通知设置界面点一次"保存"即自动加密。旧明文在迁移前照常工作(向后兼容已测)。**2026-10-16 复核仍成立**（config.json 里 password 仍是明文，等用户在设置页点一次保存）。

## 五、性能

import gui 0.21s(一次性);config.json 单次读取 0.07ms——GUI tick 每 2 秒的多次读取开销可忽略;车站索引 3404 站缓存加载 4ms;模糊搜索单次 1.06ms。**未发现性能问题**。长跑内存风险(日志区膨胀、缓冲无界)在此前轮次已修。

## 六、兼容性

Windows 优先(msvcrt 跨进程锁、DPAPI),非 Windows 路径均有降级:锁退化为进程内 RLock(代码有注释)、加密退化为 Fernet/明文并明示告警。Python 3.12 语法兼容。无 shell=True、无 pickle、无 eval/exec(Playwright 的 page.evaluate 是在浏览器里执行 JS,属设计内)。未发现命令注入面(subprocess 全列表参数)。

## 七、安全隐患

- 敏感文件全部在 .gitignore 内(config/session_cookies/passengers/state/order_history/launcher_config/grab_tasks/logs/.browser_profile),git ls-files 复核无泄漏。
- 原子写临时文件名(`*.tmp*`、`*.guisave*`、`*.launcher*`、`state.json.bad*`)已被 .gitignore 覆盖。
- 残留明文:SMTP 授权码(见四)、session_cookies.json 与 .browser_state.json 明文存登录态——属设计内(本机使用),gitignore 兜底。

## 八、遗留风险(知情接受,暂不处理)

1. **跨进程 config.json 读-改-写仍有 mtime 盲区**:监控 GUI 与启动器分属两个进程,同时在 ~15.6ms 窗口内各写一次仍可能丢一方(同进程已用锁消除)。彻底解需跨进程文件锁,两进程都要接入,建议与并行会话正在进行的 filelock.py 工作合并处理。
2. mock 端到端覆盖状态机逻辑,不覆盖真实页面 DOM/接口变更——这条防线是 RULES.md 的实测结论 + 真实购票演练。
3. engine 与 launcher 的两个驱动循环语义不同,维持各自实现(此前已论证不宜合并)。

## 九、并行工作提示

检查进行期间检测到另一会话的大功能改动在途(无座按车型同价改判、首选席别、车次席别明细,见 RULES.md 新增条目)。本报告的测试套件已在该在途代码上跑通,间接验证了其与既有逻辑的兼容性;涉及 launcher.py/passengers.py 的本轮修复留在工作树,待该功能收口后一并提交。

## 十、2026-10-16 复核增量

本轮代码变更（不在原报告检查范围内，单独记录以免与上文混淆）：

- **config/state 家族跨文件重复已收敛**（HANDOFF 待办项）：`load_config / save_config / update_config_locked / update_state_locked / normalize_email_settings / read_history_records` 原先在 `api.py`、`gui.py`、`monitor.py` 各有一份，现机械层单源到 `appcommon.py`（新分节「config.json 共享 plumbing」）。各前端的**策略差异以参数或回调注入，未拉平**：monitor 的坏档指纹拒写(`damaged_fingerprint`)与 CLI print、gui 的 messagebox 与 `return_result=True`、api 的 stderr 出口、state.json 的 `tmp_kind`(apisave/guisave/monsave)。
- **邮箱正则统一取严格口径**：旧 `api.EMAIL_RE` 是宽松版，会让 gui 拒收的地址从桌面版设置页落进 config.json；现四处同源 `appcommon.EMAIL_RE`。
- **`state_file` / `history_file` 路径解析单源**：`appcommon.state_path_of / history_path_of`，api / gui / monitor / launcher 四处统一。
- **RULES.md 第七节门禁订正**：launcher.py 实测 1164 行（旧文「约 3000 行」）、其 `python launcher.py` 兼容条款失效（已无 `__main__`）、`browser_order._order_impl` 实测 599 行（`browser_order.py:726`→`:1324`）。
- **浏览器模式会话失效被误判成瞬时故障（已修）**：12306 踢会话是 302 到 `/otn/passport?redirect=...`（不是 `/login`），落点页是登录门户 HTML，`session_ok` 的 `json.loads` 抛 `JSONDecodeError` 被兜进通用的「校验异常」，而 `engine.py` 用 `startswith("校验异常")` 前缀黑名单把它当瞬时 → 任务永不标 `failed`、无限重试、永不下单。现改为落点/正文门户标志先判（顺序在 `json.loads` 之前）+ 确认失效白名单 `session_failure_is_permanent`。全库只有 engine 用 permanent 做任务状态分支。
- **停止后日志仍在刷 + 会话失效无限重试（2026-10-15 修，用户截图报告）**：上面那条改的是 `check_session_if_needed` 的判定路径；**下单路径**与**线程收尾**上还有两个独立缺陷。
  其一：`browser_order._order_impl` 返回「浏览器会话不可用（…）」既不匹配「会话已失效」也不匹配「未找到会话文件」，engine._run_task 落到兜底 `continue` → 每趟车重试一遍、永不标 `failed`；现判据接 `browser_order.session_failure_is_permanent`，命中即 failed 并提示「重新登录」。
  其二：`run()` 的 `stop_event` 原本只在 run() 内部可见，`_run_task` 感知不到停止，`executor.shutdown(wait=False)` 之后在途一轮仍跑满（18 趟车约 36 秒日志）；现 `run()` 暴露 `self._stop_event`，`_run_task` 在两个循环头 `getattr` 兜底查 `is_set()` 即退出。
  launcher 侧（`Grabber._run`）同样接判据，避免撞满 `MAX_ORDER_FAILS=5` 才停；engine 兜底分支的「抢票失败」改为「[重试] …本轮未成功」，与真终止区分。
- 回归（config/state 收敛与三处会话失效修复批次）：`python -X utf8 -m compileall -q .` 干净；`python -X utf8 tests\test_all.py` → **Ran 723 tests, OK (skipped=15)**。
- **任务级「自动下单」开关在界面上隐形（已修）**：任务 `auto_order=false` 时引擎只记历史（`hit_no_order`）+ 发提醒邮件、不下单（`engine._run_task` 的 `if not auto_order:` 分支），而 `rpc_list_monitor_tasks` 不返回该字段、编辑弹窗又没有这个开关（2026-10-13 重构时移走，只剩「设置 → 任务默认值」且仅对新建生效）→ 老任务的这一项在界面上既看不见也改不回来，表现即「有票没反应」。现补 API 字段（缺省与引擎同口径 `True`）、任务卡「仅提醒」徽章（`.no-order`）、编辑弹窗勾选框；从「仅提醒」改回自动下单时顺手 `resetTask` 清旧 `NO_ORDER` 防重（`resetTask` 会把任务拉回 monitoring，故再 `setTaskStatus` 还原编辑前状态）。
- **顺带修掉一条「真打网络」的测试**：`TestTask77Order.test_classify_with_time_no_session_error_path` 在 `session=None` 时真读项目 `.browser_state.json`，本机有登录态就会打真 12306 订单接口、结论随登录状态漂移（违反 RULES.md「测试不打真实接口」）；现 mock `order.session_from_browser_state` 抛 RuntimeError。
- 回归（本批末）：`compileall` 干净；`python -X utf8 tests\test_all.py` → **Ran 726 tests, OK (skipped=15)**；新增 `TestPerTaskAutoOrder` 3 例。

