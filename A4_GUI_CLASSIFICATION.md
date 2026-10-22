# A-4 第一步：test_all.py 中 `gui.` 引用分类清单（只读盘点）

- 仓库：worktree `refactor-p4b`，分支 `refactor/p4b-a`，HEAD `8bbff0a`。
- 范围：`tests/test_all.py` 顶部 `from legacy import gui`（第 53 行）牵连的 24 个测试类。
- 计数口径（实测）：24 类共 **194 个 test 方法**（AST 直属方法计数；简报所说"约 197"应含动态/类外口径，差 3，不影响裁决）；`tests/test_all.py` 中含 `gui.` 的行共 **194 行**（`rg -n '\bgui\.'` 行数；按出现次数计 200 次，差额为注释/`gui.py` 字样）。
- 裁决只有三类：**迁（re-point）**＝被测符号在现行生产代码有等价物，改指向后保留；**删（dead）**＝被测的是 legacy tkinter 界面行为，现行产品（Electron UI + rpc）无对应物；**混合**＝类内逐用例裁决。
- "迁/留"在下文区分：**迁**＝需要改 import/符号前缀/patch 目标；**留**＝该用例本来就直接测生产符号（多经 `launcher` 别名＝grabber），零改动，只是与 gui 同类共存。
- 语义核对方法：逐一读 legacy/gui.py 对应定义与候选生产实现，并对关键候选（rpc/common 的 load/update/delete/read_history、appcommon 别名同一性、CONFIG_PATH 默认值）在临时目录实跑验证。

---

## 全局符号映射总表（跨类共用，先看这里）

| legacy 符号 | 生产等价物 | 核对结论 |
|---|---|---|
| `gui.normalize_email_settings` | `appcommon.normalize_email_settings`（`rpc.common.normalize_email_settings` 亦同） | **等价**。legacy 本体即薄委托 appcommon；签名 `(email)`、校验/回退/抛 ValueError 逐字同源。 |
| `gui._mask_id` | `appcommon.mask_id_partial` | **等价（同一函数对象，实测 `is` 为 True）**。⚠️ 不得映射到 `grabber_utils.mask_id`——那是另一口径（先 strip、保前 4 位）：同一输入实测 `mask_id_partial` 给 `110***********1234`，`mask_id` 给 `1101**********1234`，测试期望前者。 |
| `gui.remove_task_from_config` | `appcommon.remove_task_from_config`（`rpc.common` 亦转出） | **等价**。legacy 即薄委托 appcommon（uid 优先、无 uid 只删第一个同名）。 |
| `gui._display_trains` | `appcommon.display_trains`（`grabber._display_trains` 亦同） | **等价（同一函数对象，实测）**。 |
| `gui._display_seat_types` | `appcommon.display_seat_types`（`grabber_utils._display_seat_types` 亦同） | **等价（同一函数对象，实测）**。 |
| `gui.load_config` | `rpc.common.load_config`（api.py 门面转出同一函数） | **部分等价**。返回值/降级口径（缺失/损坏/非 dict → `{}`）一致，机械层同为 `appcommon.read_config_or_empty`；但警告通道不同：legacy 走 `LOG.error` 一次且非 dict 也警告，rpc 只在损坏时向 stderr print 一次、**非 dict 静默**；`_CONFIG_CORRUPT_WARNED` 是两个模块各自独立的全局，复位点不同。`CONFIG_PATH` 默认值实测相同（均为仓库根 `config.json`），但 patch 目标是不同模块的全局。 |
| `gui.save_config` | 无 | **无等价**。rpc 侧没有 standalone save_config（所有写走 update_config_locked）。`monitor.save_config` 不是等价物：带 expect_stamp/损坏指纹守卫、返回 bool、提示走 print，被测对象不同。 |
| `gui.update_config_locked` | `rpc.common.update_config_locked` | **部分等价**。锁内读-改-写、损坏/非 dict 拒写（RuntimeError 文案同源）一致；差异四处：①签名 rpc 多 `expect_stamp=_STAMP_UNSET`（默认不检查）；②返回值 legacy＝mutator 返回值，rpc＝True/False 布尔（实测）；③损坏/非 dict 时 legacy 先 `messagebox.showerror` 再抛，rpc 无弹窗、由 appcommon 直接抛 RuntimeError；④锁超时 legacy 转成友好 `TimeoutError("文件被占用，稍后重试")`＋LOG＋`showwarning`，rpc 原样透传 filelock 的 `TimeoutError("等待文件锁超时：<path>")`。 |
| `gui.update_state_locked` | `rpc.common.update_state_locked` | **部分等价**。机械层同为 `appcommon.update_state_locked`；差异：①临时文件名前缀 legacy `tmp_kind="guisave"`、rpc `"apisave"`（仅文件名，无断言影响）；②legacy 传了 `on_non_dict` 钩子（LOG 警告"非 dict…已按空状态处理"），rpc **未传**，非 dict 时静默按 `{}`；③路径解析 patch 点 legacy 用 `gui.APP_ROOT`、rpc 用 `rpc.common.HERE`（默认均为根目录）；④锁超时转换差异同上。 |
| `gui.delete_task_everywhere` | `rpc.common.delete_task_everywhere`（api.py 门面转出同一函数；`rpc.tasks.rpc_delete_task` 调用之） | **基本等价**（临时目录实跑验证：config 移除＋state 清除＋死引擎走文件路径均一致）。逻辑逐行同源；仅两点差异：引擎通知失败时 legacy 用 `LOG.warning`（含任务名），rpc 用 `_warn` 写 stderr；其内部调用的 update_* 是 rpc 版（差异见上两行）。`app` 契约相同（`engine_thread`/`is_alive()`/`get_ops_engine().note_task_deleted(name)`）。 |
| `gui.read_history_records` | `rpc.common.read_history_records` | **部分等价**。机械层同 appcommon、返回一致（实测 limit 截取/最新在前相同）；差异：legacy 对顶层非 list 有 `LOG.warning`，rpc **未传 `on_non_list`**（静默返回 `[]`）；读失败时 legacy 静默、rpc 走 `_warn` stderr；路径 patch 点 legacy＝`gui.CONFIG_PATH`＋`gui.APP_ROOT`，rpc＝`rpc.common.CONFIG_PATH`＋`rpc.common.HERE`（实测 patch 后者可正确解析 tmp 目录相对 history_file）。 |
| `gui._config_json_corrupt` | 无 | **无等价**。monitor 的损坏指纹机制是"load 时记指纹、写时守卫"，不是独立谓词函数，形状/语义都不同。 |
| `gui._state_tasks_dict` | 无 | **无等价**。`rpc.tasks.rpc_list_monitor_tasks` 内联做了顶层非 dict 防御，但 tasks 节非 dict 时 `(state.get("tasks") or {}).get(...)` 仍会抛，口径并不完全一致。 |
| `gui.format_dates` | 无 | **无等价**。`engine.expand_dates` 只展开日期不做展示格式化；展示在前端 JS。 |
| `gui._build_edited_task` | 无 | **无等价**。`grabber_utils.build_monitor_task` 是新建任务用：生成新 uid 与名字、无 `seats_by_date` 字段、`notify_channels` 固定 `["email"]`；legacy 版是编辑组装：保留原 name/uid/`seats_by_date`/`notify_channels`。`rpc.tasks.rpc_create_task` 编辑＝先建后删，不保留 `seats_by_date`（见风险清单）。 |
| `gui._date_display_text`、`gui._resolve_saved_dates` | 无 | **无等价**。`rpc.tasks._split_date_input` 是入参归一，契约不同（非法返回 `[]` 不抛、倒置自动交换）；展示文本在 JS。 |
| `gui._save_passengers_or_warn` | 无 | **无等价**。`rpc.passengers.rpc_save_passengers` 合同不同（入参 dict、返回 `{ok, error}`），且把 stale（指纹变化）与 refused（不可解密拒写）合并成同一条通用 error 文案，不区分警告/错误两路。 |
| `gui._relogin_ok_via_script` | 无 | **无等价**。现行重登是 `rpc.session.rpc_relogin`：进程内 `browser_order.login()` 异步拉起、返回 dict（ok/running/already），不经 subprocess、不看退出码。`capture_session.main()` 本身的退出码契约还在（由 TestTask65(c) 覆盖），但"跑脚本看退出码"的包装器已无对应物。 |
| `gui.GrabTaskForm / GrabTab / TaskPage / PassengerDialog / PassengerPanel / NewTaskDialog / TaskEditDialog / StartMonitorDialog / QueryHistoryDialog / MonitorApp` | 无 | **全部无等价**：tkinter 类，现行产品为 Electron `ui/`＋`rpc/*`，无 Python 侧对应类。 |
| `gui.tk / gui.ttk / gui.messagebox / gui.simpledialog` | 无 | tkinter 本体引用，随 legacy 一并消失；以它们为 patch 目标的用例随被测类裁决。 |
| `gui.passengers_mod` | `passengers` 模块本身 | 同一模块对象（legacy 只是 `import passengers as passengers_mod`）。引用可直接改 `pax_mod`/`passengers`，但所在用例的裁决取决于被测对象（多为 legacy 对话框，仍删）。 |
| `gui.engine_mod / gui.filelock / gui.appcommon / gui.subprocess` | `engine` / `filelock` / `appcommon` / stdlib `subprocess` 本身 | 同一模块对象，别名转出。patch 这些别名＝patch 真模块；用例裁决取决于被测对象。 |
| `gui.LOG` | `logging.getLogger("monitor")`（与 `appcommon.LOG` 同一对象） | 同一 logger 单例。但"谁往里写、写什么文案"属于各前端语义，见 load_config 等条目。 |
| `gui.CONFIG_PATH / gui.APP_ROOT / gui.HERE` | `rpc.common.CONFIG_PATH / rpc.common.HERE` | 默认值：`CONFIG_PATH` 两边实测相同（根 `config.json`）；注意 `gui.HERE` 是 `legacy/` 自身目录、`gui.APP_ROOT` 才是根，patch 时勿混。rpc 侧没有 APP_ROOT 对应物，相对路径解析靠 `rpc.common.HERE`。 |

---

## 逐类裁决

### 1. TestLauncherUnit（L993–1049，6 用例）— 整类迁（已 re-point，零改动）

- gui.* 符号：无（仅类首注释提到 gui.py；用例全经 `launcher` 别名＝grabber 调用）。
- 符号核对：`launcher.resolve_dates`→`grabber.resolve_dates`（grabber.py:1154）；`launcher.fmt_countdown`/`purpose_of`/`new_grab_task`→grabber 自 `grabber_utils` 转入（grabber.py:59–72 的 `from grabber_utils import ...`，签名一致）；`launcher.search_stations`/`_merge_kind`/`_station_kinds`/`STATION_KIND_PATH`→grabber 本体（914/1022 行）。全部现行生产符号。
- 裁决：**整类迁**。预估改动：无（仅在删除 test_all.py 顶部 import 后，该类不受影响；注释可顺手更新）。

### 2. TestValidateAutoNoModal（L1052–1143，7 用例）— 整类删

- gui.* 符号：`gui.GrabTaskForm`（含 `__new__` 造实例）、`gui.messagebox`（patch 目标）。
- 核对：被测 `GrabTaskForm._validate(auto=...)`/`_validate_fail` 是 legacy 表单方法，无生产等价。现行启动路径 `rpc.grab.rpc_start_grab` 无前置校验，直接建 Grabber；`Grabber._run` 内部校验口径还与 legacy 不同：legacy 对 auto 无乘车人是 **fail-closed**（跳过开抢），`Grabber._run` 是 **fail-open**（记一条提醒后用账号默认乘车人继续）——语义相反，不能 re-point。
- 裁决：**整类删**（7 用例全删）。预估改动：直接删。

### 3. TestRemindNonmodal（L1182–1271，4 用例）— 整类删

- gui.* 符号：`gui.GrabTaskForm`、`gui.tk`、`gui.ttk`、`gui.GrabTaskForm._tick`（源码巡检）。
- 核对：被测 `_notify_nonmodal`（非模态 Toplevel 提醒窗）与 `_tick` 均为 legacy 表单成员。现行产品无提醒窗对应物：到点自动开抢职责已在 `rpc/grab.py` 的 `_armed_loop`（注释自述"原 gui.GrabTaskForm._tick 的到点自动开抢职责"），提醒降级为 `rpc_grab_ticker` 的倒计时文案＋日志推送，机制不同、不可 re-point。
- 裁决：**整类删**（4 用例全删）。预估改动：直接删（类外的 `_FakeTkWidget/_FakeParent` 桩若无其它类共用，一并删）。

### 4. TestGuiUnit（L1274–1294，4 用例）— 混合（迁 3 / 删 1）

- gui.* 符号：`gui.normalize_email_settings`、`gui._mask_id`、`gui.remove_task_from_config`、`gui.format_dates`。
- 逐用例：
  - `test_normalize_email` → **迁**：改指 `appcommon.normalize_email_settings`（或 `rpc.common.` 同名），仅改符号前缀。
  - `test_mask_id` → **迁**：改指 `appcommon.mask_id_partial`。⚠️ 禁止改指 `grabber_utils.mask_id`（口径不同，见总表）。
  - `test_remove_task_by_uid` → **迁**：改指 `appcommon.remove_task_from_config`，仅改前缀。
  - `test_format_dates` → **删**：`gui.format_dates` 无生产等价。
- 预估改动：3 例仅改符号前缀；1 例直接删。

### 5. TestConfigKeys（L1765–1888，5 用例）— 整类迁（保留，仅动一处清单）

- gui.* 符号：无函数符号；唯一牵连是 `_PRODUCT_FILES`（L1771）含字符串 `"legacy/gui.py"`，扫描面把 legacy 当产品文件。
- 核对：5 个用例测的是 `config_keys` 键集与各产品文件实际读键的一致性，与 gui 函数无关。
- 裁决：**整类迁（保留）**。预估改动：仅 `_PRODUCT_FILES` 去掉 `"legacy/gui.py"` 一项，且**必须与删 legacy/gui.py 同一步**——文件删了但清单还在，`_scan` 的 `open()` 会 FileNotFoundError；清单先删则在过渡期少扫一个文件（无害但口径不一）。

### 6. TestLockedRMW（L5085–5197，5 用例）— 混合（迁 4 / 删 1）

- gui.* 符号：`gui.update_config_locked`、`gui.update_state_locked`、`gui.load_config`、`gui.save_config`；helper `_patch_gui_paths` patch `gui.CONFIG_PATH`/`gui.APP_ROOT`。
- 逐用例：
  - `test_gui_update_config_locked_concurrent_no_lost_update` → **迁**：改指 `rpc.common.update_config_locked`；断言只验并发不丢更新（机械层同 appcommon），返回值差异不涉断言。改动：patch 目标 `gui.CONFIG_PATH`→`rpc.common.CONFIG_PATH`，`APP_ROOT` patch 不再需要。
  - `test_gui_update_state_locked_concurrent_no_lost_update` → **迁**：改指 `rpc.common.update_state_locked`；改动：patch `rpc.common.CONFIG_PATH`＋`rpc.common.HERE`（指 tmp，替代 APP_ROOT patch）；tmp_kind 差异（guisave/apisave）不涉断言。
  - `test_gui_save_config_still_writes_standalone` → **删**：`gui.save_config` 无生产等价（见总表）。
  - `test_monitor_save_config_holds_file_lock`、`test_monitor_update_config_locked_concurrent_no_lost_update` → **留**：测 monitor 本体，零 gui 改动（仅与 gui 同类共存）。
- 预估改动：2 例改 patch 目标＋符号前缀；1 例删；2 例不动。

### 7. TestDeleteTaskClearsState（L5758–5886，9 用例）— 整类迁

- gui.* 符号：`gui.delete_task_everywhere`（2 例直调）；其余 7 例只经 helper `_patch_gui_paths` patch `gui.CONFIG_PATH`/`gui.APP_ROOT`，被测对象是 `engine_mod.MonitorEngine`。
- 核对：`rpc.common.delete_task_everywhere` 与 legacy 版逻辑逐行同源（实跑验证 config/state/引擎通知三路一致）；差异仅失败日志通道（LOG.warning vs stderr `_warn`），不涉断言。helper 的 gui patch 对 engine 用例其实无关（引擎以显式 `config_path=cfg` 构造）。
- 逐用例：`test_delete_task_removes_config_and_state_entries`、`test_delete_task_notifies_live_engine` → **迁**（改指 `rpc.common.delete_task_everywhere`，patch 目标改 `rpc.common.CONFIG_PATH`；测试 config 里 `state_file` 是绝对路径，故 APP_ROOT/HERE patch 不需要）；其余 7 例（note_task_deleted 系列）→ **迁（helper 清理）**：helper 去掉 gui patch、只建文件即可，断言不动。
- 预估改动：改 patch 目标＋符号前缀；无需改断言。

### 8. TestReloginScriptResult（L5889–5919，3 用例）— 整类删

- gui.* 符号：`gui._relogin_ok_via_script`、`gui.subprocess`（patch 目标）。
- 核对：被测包装器无生产等价（见总表）。3 例（退出码 1/0/超时）测的全是"subprocess 跑 capture_session.py 看 returncode"这一已退役机制；现行 `rpc_relogin` 是进程内异步＋dict 合同，形状完全不同。
- 裁决：**整类删**（3 用例全删）。预估改动：直接删。

### 9. TestTask55AutoFired（L6040–6116，5 用例）— 整类删

- gui.* 符号：`gui.GrabTaskForm`（`__new__` 造实例后调 `start_grab(auto=...)`）。
- 核对：被测语义"校验通过后才置 `auto_fired`、失败回滚、下个 tick 重试"长在 legacy 表单的 `_tick`＋`start_grab` 上。现行 `rpc/grab._armed_loop` 语义**相反**：先把 tid 加入 `_armed_fired` 再 `rpc_start_grab`，无校验、无回滚、无重试——没有可 re-point 的等价物，这批用例删后该语义在现行代码中无任何覆盖（见风险清单）。
- 裁决：**整类删**（5 用例全删）。预估改动：直接删。

### 10. TestFileLockTimeout（L6119–6231，9 用例）— 混合（留 7 / 删 2）

- gui.* 符号：`gui.update_config_locked`、`gui.update_state_locked`、`gui.messagebox`（patch 目标）、`gui.load_config`（patch 目标）。
- 逐用例：
  - engine 三例（load/save/sync state 超时）、`test_append_history_timeout_skips_record`（appcommon）、`test_launcher_append_monitor_task_timeout_user_message`（grabber）、monitor 两例 → **留**（7 例，测生产符号，零 gui 改动）。
  - `test_gui_update_config_locked_timeout_warns_user`、`test_gui_update_state_locked_timeout_warns_user` → **删**：断言的是 legacy 的超时转换（友好文案 `TimeoutError("文件被占用，稍后重试")`＋`messagebox.showwarning`）。rpc 版两函数原样透传 filelock 的 `TimeoutError("等待文件锁超时：…")` 且无弹窗——不等价（见总表）。
- 预估改动：删 2 例；其余不动。

### 11. TestTask65InteractionLoginChain（L7260–7455，13 用例）— 混合（留 12 / 删 1）

- gui.* 符号：仅 `test_warn_relogin_http_waits_and_writes_back` 用到 `gui.MonitorApp`、`gui._relogin_ok_via_script`（patch 目标）、`gui.subprocess`、`gui._RESULT_QUEUE`。
- 逐用例：其余 12 例分别测 `probe_login`（脱敏/二维码确认）、`capture_session`（退出码/最终校验/goto 提示）、`browser_order`（类名精确匹配/浏览器锁恢复）、`launcher._is_soft_fail`（grabber 转出 grabber_utils）→ **留**（零 gui 改动）。
  - `test_warn_relogin_http_waits_and_writes_back` → **删**：被测 `MonitorApp._warn_relogin_http`＋脚本重登＋结果队列回写 UI 全是 legacy 链路；现行 `rpc_relogin` 机制完全不同。
- 预估改动：删 1 例；其余不动。

### 12. TestTask74Launcher（L8662–8923，14 用例）— 混合（留 3 / 删 11）

- gui.* 符号：`gui.GrabTaskForm`、`gui.GrabTab`、`gui.QueryHistoryDialog`、`gui.messagebox`。
- 留（测 grabber 本体，零 gui 改动）：
  - `test_search_stations_kind_rank_case_insensitive`（`launcher.search_stations`）；
  - `test_load_launcher_config_preserves_synced_trains`（`launcher.load_launcher_config`）；
  - `test_history_item_text_skips_non_dict`（`launcher._history_item_text`）。
- 删（全是 GrabTaskForm/GrabTab/QueryHistoryDialog 的方法行为）：
  - `test_start_grab_no_disk_write_on_validate_fail`、`test_start_grab_saves_on_validate_pass`、`test_validate_fail_warn_once`、`test_validate_fail_warn_per_reason`、`test_validate_fail_warn_resets_after_pass`（`_validate/start_grab/_validate_fail`，且校验语义与现行 Grabber 不同，见第 2 节）；
  - `test_ui_to_lc_spinbox_non_numeric`（`_ui_to_lc` 的 Tk 变量读数兜底；现行对应逻辑在 JS 的 `Number(...)||10` 与 rpc_save_grab_task 的 float 转换报错，无 Python 等价物）；
  - `test_tick_remind_non_numeric_no_crash`（`_tick`）；
  - `test_test_session_error_routed_to_log`（`GrabTaskForm._test_session`；现行会话探测是 `rpc.session.probe_session/rpc_check_session`，返回 dict 而非日志队列，无对应方法）；
  - `test_delete_task_stops_grabber_thread`（`GrabTab._delete_task` 先 `form.close(stop_thread=True)`；⚠️ 现行 `rpc.grab.rpc_delete_grab_task` 只删任务库记录、**不停** `_grabbers[tid]` 线程，语义缺口见风险清单）；
  - `test_on_window_closed_keeps_ok`（`GrabTab._on_window_closed`；Electron 无此生命周期，状态回写在 `rpc_grab_ticker`，形状不同）；
  - `test_history_pick_aligned_with_junk_in_middle`（`QueryHistoryDialog` 列表对齐；现行历史在前端 JS＋rpc_list_presets，无对话框类）。
- 预估改动：删 11 例（含类内 `_make_app` 桩的大部分）；留 3 例不动。

### 13. TestTask75Gui（L8926–9104，13 用例）— 混合（迁 1 / 删 12）

- gui.* 符号：`gui.load_config`、`gui._CONFIG_CORRUPT_WARNED`、`gui.LOG`、`gui.main`、`gui.tk`、`gui._build_edited_task`、`gui.TaskPage`、`gui._date_display_text`、`gui._resolve_saved_dates`、`gui.update_state_locked`、`gui.engine_mod`/`gui.filelock`/`gui.appcommon`（patch 别名）。
- 逐用例：
  - `test_load_config_corrupt_returns_empty` → **迁**：改指 `rpc.common.load_config`（只断言返回 `{}`，与警告通道无关）；改动：patch 目标改 `rpc.common.CONFIG_PATH`，标志复位改 `rpc.common._CONFIG_CORRUPT_WARNED`。
  - `test_load_config_corrupt_warns_once` → **删**：断言落在 legacy 独有的 LOG 警告通道（"解析失败"只记一次）；rpc 走 stderr print，通道与文案都不同。⚠️ 判断题，见"拿不准"。
  - `test_main_corrupt_config_friendly_prompt_and_starts` → **删**：`gui.main()`＋Tk 启动链。
  - `test_build_edited_task_preserves_seats_by_date`、`test_build_edited_task_no_seats_by_date` → **删**：`_build_edited_task` 无等价物；且其保住的 `seats_by_date` 在现行编辑链（rpc_create_task→build_monitor_task）根本不产出——删后该字段保留行为无覆盖，见风险清单。
  - `test_refresh_caches_date_text_no_warning_spam`、`test_refresh_cache_invalidates_on_config_change` → **删**：`TaskPage.refresh` 的日期文本缓存是 legacy 树表行为。
  - `test_date_display_text_non_contiguous`、`test_date_display_text_contiguous_single_empty`、`test_resolve_saved_dates_keeps_original_when_untouched`、`test_resolve_saved_dates_parses_edited`、`test_resolve_saved_dates_illegal_raises` → **删**：两纯函数无生产等价（`_split_date_input` 契约不同：非法不抛、倒置交换）。
  - `test_update_state_locked_non_dict_state` → **删**：机械结果（非 dict 按 `{}` 后 mutator 落盘）在 appcommon 层存在，但断言包含 legacy 独有的 LOG 警告（"非 dict"）与 gui 的 patch 契约（`gui.engine_mod.load_state_file` 等）；rpc 版未传 `on_non_dict`、无此警告。⚠️ 判断题，见"拿不准"。
- 预估改动：1 例改 patch 目标＋前缀；12 例直接删。

### 14. TestTask80GuiPassengersSave（L9564–9587，2 用例）— 整类删

- gui.* 符号：`gui._save_passengers_or_warn`、`gui.passengers_mod`（patch 目标）、`gui.messagebox`。
- 核对：被测 helper 的"失败弹 error/成功不弹窗"无生产等价；`rpc_save_passengers` 返回 dict 错误合同，且 stale/refused 不分（见总表）。
- 裁决：**整类删**（2 用例全删）。预估改动：直接删。

### 15. TestTask84LauncherP3（L9949–10118，13 用例）— 混合（留 10 / 删 3）

- gui.* 符号：`gui.update_config_locked`、`gui.PassengerDialog`（含 `_save`/`_delete`）、`gui.passengers_mod`。
- 留（测 grabber/grabber_utils 本体，零 gui 改动，10 例）：
  - (a) `test_normalize_trains_*` 四例＋`test_grabber_run_wires_normalize_trains`＋`test_merge_trains_from_monitor_string_not_char_split`（`launcher._normalize_trains`/`Grabber._run`/`merge_trains_from_monitor`）；
  - (c) `test_mask_pii_text_masks_order_no`、`test_mask_names`、`test_ensure_passengers_log_masks_names`（`launcher._mask_pii_text`/`_mask_names`/`ensure_passengers`）；
  - (d) 中 `test_ensure_passengers_refusal_no_false_success_log`（`launcher.ensure_passengers`）。
- 删（3 例）：
  - `test_update_config_locked_damaged_config_friendly_abort`：核心拒写行为（RuntimeError＋不覆写）在 rpc/appcommon 成立，但断言含 `messagebox.showerror` 一次——按"弹窗为共同主断言"判删。⚠️ 判断题，见"拿不准"。
  - `test_passenger_dialog_save_refusal_shows_error_not_success`、`test_passenger_dialog_delete_refusal_no_refresh`：`PassengerDialog._save/_delete` legacy 对话框行为，无等价物。
- 预估改动：删 3 例；其余不动。

### 16. TestTask89GuiLeftovers（L10606–10726，9 用例）— 混合（迁 3 / 删 6）

- gui.* 符号：`gui._display_trains`、`gui.TaskEditDialog`、`gui.StartMonitorDialog`、`gui.TaskPage.refresh`（源码巡检）、`gui.PassengerPanel`、`gui.passengers_mod`。
- 迁（3 例）：`test_display_trains_string_is_single_not_char_split`、`test_display_trains_list_and_illegal_shapes`、`test_trains_roundtrip_no_pollution`——被测 `gui._display_trains` 与 `appcommon.display_trains` 是同一函数对象，改前缀即可；第三例后半段的"保存解析表达式"是测试内复刻的字符串拆分，不依赖 legacy 类。
- 删（6 例）：
  - `test_edit_dialog_load_wiring_uses_normalized_trains`、`test_task_row_display_wiring_uses_normalized_trains`：`inspect.getsource` 巡检 legacy 类源码，随类消失；
  - `test_passenger_dialog_save_abandons_on_external_write`、`test_passenger_dialog_save_refusal_shows_error_not_warning`、`test_passenger_dialog_save_success`、`test_passenger_dialog_refresh_takes_stamp`：`PassengerPanel._save_passengers_or_refresh/refresh` 是 legacy 包装（返回 `(ok, reason)`＋messagebox＋refresh），`rpc_save_passengers` 合同不同，不等价。
- 预估改动：3 例仅改符号前缀；6 例直接删。

### 17. TestTask90LauncherPassengerDialogStamp（L10729–10861，7 用例）— 整类删

- gui.* 符号：`gui.PassengerDialog`（含 `__init__` 源码巡检）、`gui._save_passengers_or_warn`、`gui.passengers_mod`。
- 核对：全类测 legacy 快速编辑对话框的指纹（expect_stamp）接线。底层乐观并发语义在生产侧是 `passengers.save_passengers(expect_stamp=...)` 本体＋`rpc_save_passengers` 的 stamp 透传（rpc/passengers.py:57–60：`p.expect_stamp`→`save_passengers(..., expect_stamp=stamp)`，null→`_STAMP_UNSET`），但被测的对话框包装（`_save_passengers_or_refresh` 返回 `(ok, reason)`、保存成功刷新 `_pax_stamp`、messagebox 分路）无对应物。`test_save_passengers_or_warn_accepts_expect_stamp` 虽只测 helper 的 stamp 透传，helper 本身仍是 legacy 专属。
- 裁决：**整类删**（7 用例全删）。预估改动：直接删。注：删后"UI 层 stamp 透传"在 Python 侧无直接 pin（rpc 侧透传靠 TestRpcNamespace 等其它覆盖的范围之外，建议知悉即可）。

### 18. TestTask94GuiStartupRobustness（L11490–11604，9 用例）— 混合（迁 5 / 删 4）

- gui.* 符号：`gui.load_config`、`gui._CONFIG_CORRUPT_WARNED`、`gui.LOG`、`gui._config_json_corrupt`、`gui.read_history_records`；patch `gui.CONFIG_PATH`/`gui.APP_ROOT`。
- 迁（5 例，只断言返回值、不碰 LOG 通道）：
  - `test_load_config_latin1_bytes_returns_empty`、`test_load_config_valid_dict_unchanged` → 改指 `rpc.common.load_config`，patch 改 `rpc.common.CONFIG_PATH`，标志复位改 `rpc.common._CONFIG_CORRUPT_WARNED`；
  - `test_read_history_records_non_list_number_returns_empty`、`test_read_history_records_valid_list_unchanged`、`test_read_history_records_missing_file_returns_empty` → 改指 `rpc.common.read_history_records`，patch 改 `rpc.common.CONFIG_PATH`＋`rpc.common.HERE`（实测可正确解析 tmp 下相对 `history_file`）。
- 删（4 例）：
  - `test_load_config_gbk_bytes_returns_empty`：虽只断言返回值，但同例还断言 LOG ERROR 含"UTF-8"——rpc 的 stderr 文案（"[api] config.json 解析失败，按空配置"）不含此信息，不等价；
  - `test_load_config_non_dict_list_returns_empty`：断言 LOG 指出"不是 dict/对象"，rpc 对非 dict 静默；
  - `test_config_json_corrupt_detects_gbk_and_non_dict`：`_config_json_corrupt` 无等价物；
  - `test_read_history_records_non_list_dict_returns_empty`：断言 LOG 警告"不是列表"，rpc 未传 `on_non_list`、静默。
- 预估改动：5 例改 patch 目标＋前缀（含 helper `_reset_corrupt_flag` 改指 rpc 模块）；4 例直接删。

### 19. TestTask96LauncherP3（L11714–11926，8 用例）— 整类删

- gui.* 符号：`gui.PassengerDialog`、`gui.NewTaskDialog`、`gui.GrabTab`、`gui.GrabTaskForm`、`gui.passengers_mod`。
- 逐组核对：
  - `test_second_save_no_false_alarm`：`PassengerDialog._save_passengers_or_refresh` 保存后刷新 `_pax_stamp` 的对话框状态机，rpc 模型下 stamp 由前端每回传入，无对应物；
  - `test_prio_spinbox_non_numeric_uses_default`、`test_prio_spinbox_numeric_still_works`：`NewTaskDialog.create` 的 TclError 兜底。注：`rpc.tasks.rpc_create_task` 有相近的优先级解析（非法→5、越界→5），但被测的是对话框读 IntVar 抛错＋弹窗路径，形状不同，不作 re-point；
  - `test_add_row_skips_non_dict`、`test_add_row_dict_still_renders`：`GrabTab._add_row` 展示侧跳过；现行对应防线已前移到加载入口 `grabber.load_grab_tasks` 的 sanitize（由 TestTask104 的生产用例覆盖），行渲染在前端 JS；
  - `test_sync_from_lc_dirty_values`、`test_sync_from_lc_clean_values_unchanged`：`GrabTaskForm._sync_from_lc`；现行 presets sanitize 在 `grabber.load_launcher_config`、数值字段在 rpc 层各自防御，无此同步方法；
  - `test_grab_task_form_close_syncs_ui`：`GrabTaskForm.close` 的关窗同步，Electron 无此生命周期。
- 裁决：**整类删**（8 用例全删）。预估改动：直接删。

### 20. TestTask99CaptureSessionP3（L12285–12401，9 用例）— 混合（留 7 / 删 2）

- gui.* 符号：`gui._relogin_ok_via_script`（2 例）；另含字符串 patch 目标 `"legacy.gui.subprocess.run"`（L12296、L12304）。
- 留（7 例，测 capture_session/order 本体，零 gui 改动）：Ctrl+C 优雅取消、相对/绝对路径解析（load/save session）、final_verify 失败时关/不关已断开浏览器、取消路径关浏览器返回 1。
- 删（2 例）：`test_relogin_script_timeout_covers_script_total`、`test_relogin_script_timeout_still_returns_false`——同 TestReloginScriptResult，脚本包装器无等价物；其超时口径（`capture_session.GUI_SUBPROCESS_TIMEOUT`）随包装器退役。
- 预估改动：删 2 例（两处 `"legacy.gui.subprocess.run"` 字符串随之消失）；其余不动。

### 21. TestTask101CrossModuleP3（L12541–12727，16 用例）— 混合（留 13 / 删 3）

- gui.* 符号：仅 (f) 组用 `gui._state_tasks_dict`、`gui.load_state`（patch 目标）、`gui.LOG`。
- 留（13 例，测 grabber/ticket/order/browser_order 本体）：(a) station_kinds 并发写 tmp 唯一、(b) get_station_index 失败不缓存/成功缓存、(d) 未知席别码 warn-once、(e) 点名乘车人部分/零命中告警、(g) WarmSession 关锁语义、(h) browser_order 脱敏 helpers。
- 删（3 例）：`test_f_nondict_state_safe`、`test_f_nondict_tasks_section_safe`、`test_f_dict_state_passthrough`——`_state_tasks_dict` 无生产等价（见总表；rpc_list_monitor_tasks 的内联防御口径还不完全一致）。
- 预估改动：删 3 例；其余不动。

### 22. TestTask103LauncherGuiP3（L12730–12870，8 用例）— 混合（迁 3 / 删 5）

- gui.* 符号：`gui.GrabTaskForm`、`gui.simpledialog`、`gui.tk`、`gui.ttk`、`gui._display_seat_types`、`gui.update_config_locked`。
- 迁/留（3 例）：
  - `test_b_display_seat_types_unit` → **留**：测的是 `launcher._display_seat_types`（grabber_utils 转出 appcommon），零 gui 改动；
  - `test_c_display_seat_types_unit` → **迁**：`gui._display_seat_types` 改指 `appcommon.display_seat_types`（同一函数对象），仅改前缀；
  - `test_d_update_config_locked_dict_still_works` → **迁**：改指 `rpc.common.update_config_locked`（只断言落盘结果，返回值差异不涉断言），patch 改 `rpc.common.CONFIG_PATH`。
- 删（5 例）：
  - (a) 三例 `test_a_save_preset_*`：`GrabTaskForm._save_preset` 写回前归一化无等价物——`rpc.grab.rpc_save_preset` 直接落盘入参，**不做** trains/seat_types 归一化（污染防护只剩消费侧 `_display_*`），见风险清单；
  - `test_b_rebuild_seats_dirty_seat_types_default_checked`：`GrabTaskForm._rebuild_seats` 的 Tk 勾选重建；
  - `test_d_update_config_locked_rejects_non_dict`：拒写核心（RuntimeError＋mutator 不执行＋不覆写）在 rpc 成立，但断言含 `messagebox.showerror` 一次——按共同主断言口径判删。⚠️ 判断题，见"拿不准"。
- 预估改动：1 例改前缀、1 例改 patch 目标、1 例不动；5 例直接删。

### 23. TestTask104LoadSanitize（L12873–13017，8 用例）— 混合（留 4 / 删 4）

- gui.* 符号：`gui.GrabTab`（`_find`/`_on_window_closed`）、`gui.GrabTaskForm`（`_current_preset`/`_save_preset`）、`gui.simpledialog`。
- 留（4 例，测加载入口 sanitize 本体，零 gui 改动）：`test_grab_tasks_load_drops_non_dict`、`test_launcher_config_load_drops_non_dict_presets`、`test_launcher_config_non_list_presets_becomes_empty`、`test_clean_files_unchanged_no_warnings`（`launcher.load_grab_tasks`/`load_launcher_config`）。
- 删（4 例）：
  - `test_find_safe_after_sanitized_load`：数据来自生产 loader（已由留例覆盖），但断言落在 legacy `GrabTab._find` 上（rpc 侧查找是各 rpc 函数内联 `next(...)`，无此方法）；
  - `test_on_window_closed_safe_and_destroy_reached`：`GrabTab._on_window_closed` 关窗生命周期；
  - `test_current_preset_safe_after_sanitized_load`：`GrabTaskForm._current_preset`（现行预设选择在前端 JS）；
  - `test_save_preset_safe_after_sanitized_load`：`GrabTaskForm._save_preset`（同第 22 节 (a)）。
- 预估改动：删 4 例；其余不动。

### 24. TestTask106BareStringHardening（L13020–13189，8 用例）— 混合（迁/留 4 / 删 4）

- gui.* 符号：`gui.GrabTaskForm`、`gui.NewTaskDialog`（含 `__init__` 源码巡检）、`gui._display_seat_types`。
- 迁/留（4 例）：
  - `test_grabber_run_dirty_seat_types_single_not_char_split`、`test_grabber_run_clean_seat_types_unchanged` → **留**：测 `launcher.Grabber._run` 本体，零 gui 改动；
  - `test_gui_2892_passenger_names_dirty_string_preselects` → **迁**：用例本体只是拿 `gui._display_seat_types` 做调用点契约计算，改指 `appcommon.display_seat_types`（同一函数对象）即可；
  - `test_display_helper_passenger_names_contract` → **迁**：其中 `gui._display_seat_types` 改指 appcommon；`launcher._display_seat_types` 部分不动。
- 删（4 例）：
  - `test_3145_load_preset_dirty_string_no_substring`、`test_3145_load_preset_clean_list_unchanged`：`GrabTaskForm._load_preset` 的 Tk 勾选回填（现行预设载入在前端 JS）；
  - `test_3374_import_from_main_dirty_string_no_substring`：`GrabTaskForm.open_new_monitor_task`＋`NewTaskDialog.__init__` 源码巡检，legacy 链路；
  - `test_2326_sync_from_lc_dirty_string_no_substring`：`GrabTaskForm._sync_from_lc`（同第 19 节）。
- 预估改动：2 例改前缀；2 例不动；4 例直接删。

---

## 全仓其它 `legacy` 出现（生产＋测试＋tools 扫描结果）

真正的代码级 import 只有一处，其余为字符串、规则、文档与假阳性：

| 位置 | 形态 | 处置 |
|---|---|---|
| `tests/test_all.py:53` | `from legacy import gui`（唯一 import） | 24 类处理完后删除（前置动作 1）。 |
| `tests/test_all.py:1771` | `_PRODUCT_FILES` 含 `"legacy/gui.py"` | 去掉该项，与删文件同一步（前置动作 2）。 |
| `tests/test_all.py:12296, 12304` | `mock.patch("legacy.gui.subprocess.run", ...)` 字符串 | 在 TestTask99 的 2 个删除用例内，随用例消失，无独立动作。 |
| `tools/check.py:72–75` | `LEGACY_REF_PATTERNS` 两条 gui 规则，文案指向 `from legacy import gui` | 删 legacy 后规则文案失效：改为"禁止复活 `import gui`"口径或删除这两条（前置动作 3）。注：该检查跳过 tests/，不拦截 test_all.py 的 import。 |
| `tools/check.py:42` | `SKIP_DIRS` 含 `"legacy"` | 可清理（留着无害）。 |
| `tools/check.py:96` | `check_compile` docstring 提"（含 legacy/…）" | 顺手改文案，非阻塞。 |
| `pyproject.toml:34` | ruff 排除列表含 `"legacy"` | 可清理（留着无害）。 |
| `legacy/__init__.py` | legacy 包说明 | 随目录一并删除。 |
| `grabber.py:10` | docstring 提"legacy/gui.py"（指 grabber 的来源说明） | 文档性提及，删后更新文案，非阻塞。 |
| `appcommon.py:769` | 注释"（api / legacy/gui 共用）" | 同上，文案更新，非阻塞。 |
| `README.md`（53/79/99/125 行附近）、`HANDOFF.md`（多处）、`REFACTOR_PLAN_V2.md`、`全系统重构实施规划书.md`、`.gitignore:47` | 文档/注释提及 | 文档更新，非阻塞；README/HANDOFF 的模块表在删后应同步。 |
| `core/state/task_sm.py`、`core/state/order_lifecycle.py` 的 `from_legacy/to_legacy`；`tests/unit/test_core.py` 的 `test_from_legacy` 等；`tests/unit/test_rpc_namespace.py` 的 "legacy method" | **假阳性**：分别是状态机"旧字符串状态"桥接方法名、RPC 旧扁平方法名，与 legacy 包无关 | **不要动**。 |

生产代码（非 tests/tools）对 legacy 包的 import 实测为 **零**——删 legacy 不触碰任何生产 import。

---

## 汇总表

| # | 类 | 行号 | 用例 | 裁决 | 迁/留 | 删 |
|---|---|---|---|---|---|---|
| 1 | TestLauncherUnit | 993–1049 | 6 | 整类迁（已 re-point） | 6 | 0 |
| 2 | TestValidateAutoNoModal | 1052–1143 | 7 | 整类删 | 0 | 7 |
| 3 | TestRemindNonmodal | 1182–1271 | 4 | 整类删 | 0 | 4 |
| 4 | TestGuiUnit | 1274–1294 | 4 | 混合 | 3 | 1 |
| 5 | TestConfigKeys | 1765–1888 | 5 | 整类迁（仅动 _PRODUCT_FILES） | 5 | 0 |
| 6 | TestLockedRMW | 5085–5197 | 5 | 混合 | 4 | 1 |
| 7 | TestDeleteTaskClearsState | 5758–5886 | 9 | 整类迁 | 9 | 0 |
| 8 | TestReloginScriptResult | 5889–5919 | 3 | 整类删 | 0 | 3 |
| 9 | TestTask55AutoFired | 6040–6116 | 5 | 整类删 | 0 | 5 |
| 10 | TestFileLockTimeout | 6119–6231 | 9 | 混合 | 7 | 2 |
| 11 | TestTask65InteractionLoginChain | 7260–7455 | 13 | 混合 | 12 | 1 |
| 12 | TestTask74Launcher | 8662–8923 | 14 | 混合 | 3 | 11 |
| 13 | TestTask75Gui | 8926–9104 | 13 | 混合 | 1 | 12 |
| 14 | TestTask80GuiPassengersSave | 9564–9587 | 2 | 整类删 | 0 | 2 |
| 15 | TestTask84LauncherP3 | 9949–10118 | 13 | 混合 | 10 | 3 |
| 16 | TestTask89GuiLeftovers | 10606–10726 | 9 | 混合 | 3 | 6 |
| 17 | TestTask90LauncherPassengerDialogStamp | 10729–10861 | 7 | 整类删 | 0 | 7 |
| 18 | TestTask94GuiStartupRobustness | 11490–11604 | 9 | 混合 | 5 | 4 |
| 19 | TestTask96LauncherP3 | 11714–11926 | 8 | 整类删 | 0 | 8 |
| 20 | TestTask99CaptureSessionP3 | 12285–12401 | 9 | 混合 | 7 | 2 |
| 21 | TestTask101CrossModuleP3 | 12541–12727 | 16 | 混合 | 13 | 3 |
| 22 | TestTask103LauncherGuiP3 | 12730–12870 | 8 | 混合 | 3 | 5 |
| 23 | TestTask104LoadSanitize | 12873–13017 | 8 | 混合 | 4 | 4 |
| 24 | TestTask106BareStringHardening | 13020–13189 | 8 | 混合 | 4 | 4 |
| | **合计** | | **194** | 整类迁 3 / 整类删 7 / 混合 14 | **99** | **95** |

### 删 legacy/gui.py 前的前置动作清单

1. 上表 24 类全部处置完毕（迁的改完、删的删完），`tests/test_all.py` 中不再有任何 `gui.` 引用与 `"legacy.gui..."` patch 字符串。
2. 删 `tests/test_all.py:53` 的 `from legacy import gui`。
3. `TestConfigKeys._PRODUCT_FILES`（L1771）去掉 `"legacy/gui.py"`——与删文件同一步（先删清单、文件还在，无害；先删文件、清单还在，`_scan` 会 FileNotFoundError）。
4. `tools/check.py` 的 `LEGACY_REF_PATTERNS` 两条 gui 规则改口径（禁止复活）或删除；`SKIP_DIRS` 的 `"legacy"`、`pyproject.toml:34` 的 `"legacy"` 排除项可一并清理。
5. 删 `legacy/` 目录（含 `__init__.py` 与 `gui.py`）。
6. 删后验证：全量测试＋`tools/check.py` 全绿；README/HANDOFF 模块表、grabber.py:10 docstring、appcommon.py:769 注释的 legacy 提及做一轮文案更新（非阻塞，可随后）。

---

## 语义不等价清单（风险所在，全列）

以下符号/行为在现行生产代码中**没有语义等价物**，相关用例只能删、不能 re-point；其中标注 ⚠️ 的是"删测试会同时掩盖现行代码的真实行为差异/缺口"，删前应知悉：

1. `gui.update_config_locked` vs `rpc.common.update_config_locked`：返回值（mutator 结果 vs bool）、`expect_stamp` 签名、损坏/非 dict 无弹窗、锁超时不做友好转换（rpc 透传 filelock 原始 `TimeoutError("等待文件锁超时：…")`，legacy 是 `TimeoutError("文件被占用，稍后重试")`＋LOG＋showwarning）。
2. `gui.update_state_locked` vs `rpc.common.update_state_locked`：`on_non_dict` 的 LOG 警告缺失（rpc 静默按 `{}`）、tmp_kind 不同（guisave/apisave）、超时转换缺失、路径 patch 点不同（APP_ROOT vs HERE）。
3. `gui.load_config` vs `rpc.common.load_config`：警告通道不同（LOG.error vs stderr print）、非 dict 时 rpc 静默（legacy 有警告）、`_CONFIG_CORRUPT_WARNED` 为两模块独立状态。
4. `gui.read_history_records` vs `rpc.common.read_history_records`：顶层非 list 时 rpc 静默（未传 `on_non_list`），legacy 有 LOG 警告；读失败时 rpc 多一条 stderr `_warn`。
5. `gui.save_config`：rpc 侧不存在；`monitor.save_config` 语义不同（指纹守卫/expect_stamp/bool/print），不可互替。
6. ⚠️ `gui._mask_id` 的映射陷阱：只能映射 `appcommon.mask_id_partial`；`grabber_utils.mask_id` 口径不同（实测输出不同），误映射会让用例假绿/假红。
7. `gui.format_dates`、`gui._date_display_text`、`gui._resolve_saved_dates`：无等价物（`rpc.tasks._split_date_input` 契约不同：非法不抛、倒置交换）。
8. ⚠️ `gui._build_edited_task` 与 `seats_by_date`：现行编辑链（`rpc_create_task`→`grabber_utils.build_monitor_task`）根本不产出 `seats_by_date` 字段，编辑即丢失该字段；HANDOFF.md:121 仍声称"编辑时原样保留（_build_edited_task）"，与现行代码不符。删掉这 2 例后，该丢失行为无任何测试 pin。
9. `gui._config_json_corrupt`：无等价谓词（monitor 是指纹守卫机制，形状不同）。
10. `gui._state_tasks_dict`：无等价物；`rpc_list_monitor_tasks` 的内联防御对 tasks 节非 dict 并不完备（仍可能抛 AttributeError）。
11. `gui._save_passengers_or_warn` 与乘车人保存分路：`rpc_save_passengers` 把 stale 与 refused 合并为同一条通用 error，legacy 区分 showwarning（stale）/showerror（refused）两路——用户可见的失败原因粒度在现行产品已变粗。
12. `gui._relogin_ok_via_script`：脚本退出码机制整体退役，现行 `rpc_relogin` 为进程内异步 dict 合同，无对应物。
13. ⚠️ `GrabTaskForm._validate` 的 auto 无乘车人 fail-closed：现行 `Grabber._run` 为 fail-open（提醒后用账号默认乘车人继续），`rpc_start_grab` 无前置校验——与 Task 27/55 修复的语义相反，删用例后无覆盖。
14. ⚠️ `rpc/grab._armed_loop` 的 `auto_fired` 时机：先置位再启动、无校验无回滚无重试，与 Task 55 修复的语义相反（见第 9 节）。
15. ⚠️ `GrabTab._delete_task` 的"删任务先停抢票线程"：现行 `rpc_delete_grab_task` 只删任务库记录，不停 `_grabbers[tid]`——运行中线程可能继续为已删任务下单，删用例后无覆盖。
16. ⚠️ `GrabTaskForm._save_preset` 的写回前归一化：`rpc_save_preset` 不做 trains/seat_types 归一化，裸字符串会原样落盘（消费侧 `_display_*` 有归一兜底，但落盘数据本身可被污染）。
17. PassengerDialog/Panel 的指纹状态机（保存成功后刷新 `_pax_stamp`、`_save_passengers_or_refresh` 返回 `(ok, reason)` 三分）：rpc 模型下由前端每回传 `expect_stamp` 承担，Python 侧无对应物，Task 89/90/96 的这批回归 pin 删后无替代覆盖。

## 拿不准的类（判断题，已按上文口径裁决，但口径可议）

1. **TestTask75Gui**（`test_load_config_corrupt_warns_once`、`test_update_state_locked_non_dict_state`）：机械结果在 rpc/appcommon 层有等价物，断言却落在 legacy 独有的 LOG 警告通道上。本文按"断言对象即被测语义"判删；若产品意图是 rpc 侧也应有同等可见的警告（如给 `rpc.common.load_config` 补 `non_dict` 钩子、给 `update_state_locked` 补 `on_non_dict`），则应先补生产代码再判迁。这取决于对 rpc 警告通道的产品要求，我无法从代码单独判定。
2. **TestTask84LauncherP3 的 (b) 例与 TestTask103LauncherGuiP3 的 (d) 例**：损坏/非 dict 拒写的核心断言（RuntimeError＋mutator 不执行＋不覆写）在 rpc 侧全部成立，只有 `messagebox.showerror` 一条断言不成立。本文按"弹窗为共同主断言"判删；若接受去掉弹窗断言，两例可改判迁（改 patch 目标到 `rpc.common.CONFIG_PATH`）。这是口径选择题。
3. **TestLockedRMW 的 `test_gui_save_config_still_writes_standalone`**：本文判删（rpc 无 save_config）。另一选项是改指 `monitor.load_config/save_config` 对，但被测对象就从 gui 变成 monitor、且 monitor 版带指纹守卫语义，属"换被测对象"而非 re-point；是否接受取决于对这条回归 pin 的定位。
