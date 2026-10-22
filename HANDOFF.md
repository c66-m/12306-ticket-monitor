# 12306 车票监控与自动购票系统 —— 交接摘要

## 0. 一句话定位
Python 后端 + Electron 桌面壳：查票/监控余票 + 自动抢票（Playwright 驱动真实浏览器下单）。
**2026-10-14 起为 Electron 架构：界面在 `ui/`（HTML/CSS/JS），`api.py` 是 JSON-RPC 后端，
`electron/` 是壳；Tkinter 版 `gui.py` 已降级为 legacy（2026-10-18 移入 `legacy/gui.py`），
不再是入口。**

**先读第 0.95 节（当前架构），第 0.9 节是已停用的 Tkinter 批次，留作历史。**

## 0.95 当前架构：Electron 桌面壳 + JSON-RPC 后端（2026-10-14 起，最新批次）

**旧 Tkinter 界面已停用**：文件在 `legacy/gui.py`（4300 行，2026-10-18 从根目录移入）
供 test_all.py 引用，但不再是入口、不再维护。「启动桌面版.bat」现在启动 Electron。

### 分层

```
ui/            界面（纯 HTML/CSS/JS，无框架）
  index.html   87 行：顶栏 / 侧边栏 4 页 / 主区 / 日志条
  app.js       1671 行：全部页面渲染与交互（S 全局状态 + renderPage + refresh* 轮询）
  style.css    960 行（含玻璃拟态/主题变量/外观设置）
electron/      壳（Node）
  main.js      221 行：spawn python api.py + JSON-RPC 桥 + 窗口（无边框 1440×900）+ win/win-top 通道
  preload.js   27 行：contextBridge 暴露 window.api = { call, onPush, win, top }
api.py         约 1460 行：JSON-RPC 后端（stdin/stdout 逐行 JSON-RPC 2.0，METHODS 37 项）
  ↓ 复用既有业务模块
grabber.py(Grabber/任务库) engine.py(监控引擎) ticket.py(查票) order.py(browser_order.py)/...
```

通信：`window.api.call(method, params)` → preload → `ipcMain.handle("rpc")` → 写 python stdin，
逐行读 stdout 解析 → 回包。后端主动推送走 `push(tag, payload)` → `py-push` 事件 → 前端 `bridge.onPush`。
后端线程共三个：`log_pump`（把 `grabber.LOGQ` 推给前端，调用方别名 `launcher`）、`_armed_loop`（每 2 s 扫抢票任务，
到点自动开抢，`_armed_fired` 防重）、启动即自动起的监控引擎线程（`auto_start_engine=false` 可关）。

### 前端轮询节奏

monitor 2 s / grab 1 s / history 30 s / session 60 s（`ui/app.js` 尾部 setInterval）。

### 引擎启停（曾是真 bug，已修）

`api.py` 全局 `_engine_stop` 保存当前引擎线程的 `threading.Event`；`stop_engine()`：
`set()` → `join(timeout=10)` → 返回 `not alive`（前端据此提示「已停止 / 仍在收尾」）。
`engine.MonitorEngine.run(stop_event=None)` 内的检查点在循环头、due 循环、`stop_event.wait(1)` 三处。

启动软件自动启动引擎（`api.main()` → `_auto_start_engine()`，2026-10-16）：先 `_resume_stopped_tasks()`
把**上次被动暂停**的任务（`message=="引擎已停止"`，`_mark_running_tasks_paused` 的标记）恢复 monitoring，
再 `start_engine()`（顺序不能反：`get_engine()` 首次创建会读 state 文件）。**用户手动暂停的任务不会被拉起**
（message 是别的文案）。`config.json` 里 `auto_start_engine=false` 可关掉整个自动启动。

### 乘车人脱敏（曾是真 bug，已修）

`listPassengers` 返回掩码（`launcher_utils.mask_id` / `mask_mobile`），编辑必须走
`passengerDetail` 取明文；`savePassengers` 有掩码还原守卫（值含 `*` 时回填磁盘旧明文），
防止把掩码写回磁盘。

### api.py 本轮新增的 RPC

`resetTask`（清防重记录并恢复监控）、`renameGrabTask`、`deletePreset`、`pushQueryHistory`、
`ipLocate`、`checkUpdate`、`passengerDetail`。完整 37 项见 `api.py` 末尾的 `METHODS` 表。协议分派已抽成 `rpc_dispatch(line)`，可脱离真管道被测试。

### 启动与调试

- 启动：双击「启动桌面版.bat」（用绝对路径启动 `electron\node_modules\electron\dist\electron.exe`）。
  bat 里两个环境变量是必需的：`set ELECTRON_RUN_AS_NODE=`（否则 electron.exe 会当 node 跑，
  崩在 `TypeError: Cannot read properties of undefined (reading 'handle')`）、
  `set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/`（官方源 TLS 易断，
  报 `npm error at TLSSocket.socketCloseListener`）。
  **bat 必须保持纯 ASCII**：cmd 按 OEM 代码页（本机 936/GBK）解码批处理文件，UTF-8 中文字节会被
  错位拆成命令片段（实测报 `'d' is not recognized` / `'��否则' is not recognized`，并让 npm 找不到
  package.json）。要写说明就写英文注释。
  首次安装依赖在 `electron\` 目录里跑（`pushd "%APP%" → npm install → popd`），
  不能在仓库根跑：根目录没有 package.json，npm 会报 `ENOENT … open …\package.json`。
- 调试日志：`set DSH_DEBUG=1` 打开 main.js 的 `[rpc]` 与 `[renderer]` 转发；默认静默。
- 自动化验证：给 electron.exe 加 `--remote-debugging-port=9222`，再用 CDP
  （`http://127.0.0.1:9222/json/list` + WebSocket）执行 `Runtime.evaluate` / `Page.captureScreenshot`。

## 0.9 2026-10-13 界面重构（历史批次 — 已被上面的 Electron 版取代，仅存档）

**用户指令**：「推翻这个软件所有 ui 及其架构，转为最朴素最简单的 ui 面板」。
三项拍板：单窗口 / 业务功能全保留 / 旧 UI 测试同步改造为新界面的等价测试。

**做了什么**
- `gui.py`（4300 行）：重写为唯一界面。单窗口 + Notebook 页签：
  监控任务 / 抢票任务 / 查票 / 乘车人 / 历史 / 设置；底部常驻全局日志（Text + 事件标签着色）。
  全部原生 ttk 控件；无图片、无动画、无自定义绘制、无主题引擎。
  新建监控任务 = 单个平铺表单（替代旧四步向导 + QuickMonitorDialog 双入口）；
  抢票任务 = 任务列表 + 选中任务的嵌入式 GrabTaskForm（替代旧「任务管理器 + 每任务独立窗口」；
  每个任务一份表单常驻，不显示也照跑 _tick：倒计时/提醒/到点自动开抢）。
- `launcher.py`（1150 行）：删除全部界面类（LauncherApp / TaskManagerPanel / GrabTaskWindow /
  NewMonitorTaskDialog / PassengerDialog / StationEntry / QueryHistoryDialog / main 等），
  保留：Grabber 线程、任务库/配置 IO、车站搜索与类型探测、append_monitor_task、
  merge_trains_from_monitor、check_update、ensure_passengers、resolve_dates（由
  LauncherApp._resolve_dates 静态方法改模块级函数）。Grabber 日志改走 logging 单管道
  （logq 默认 None；界面经 launcher.LOGQ 统一汇总，不再有任务级日志窗）。
- 删除：`theme.py`（1192 行 Pillow 玻璃主题）、`liquid.py`（弹簧动效）、`launcher_widgets.py`
  （自定义日历/车次卡）、`bg_home.jpg`（壁纸）；`launcher_utils.py` 去掉 TrainCard 与
  复选框图标工具（不再依赖 tkinter）。`requirements.txt` 去掉 pillow。
  旧文件备份在 `_undo_backup/*.20261013-2230`（含 theme/liquid/widgets/bg_home）。
- 测试：63 个耦合旧 UI 类的用例同步改造（平移目标：launcher.LauncherApp→gui.GrabTaskForm、
  launcher.TaskManagerPanel→gui.GrabTab、launcher.NewMonitorTaskDialog→gui.NewTaskDialog、
  launcher.QueryHistoryDialog/PassengerDialog→gui.*；patch 目标 launcher.messagebox→gui.messagebox 等）。
  行为断言全部保留，个别实现细节按新结构等价改写（如 GrabTaskWindow._on_close → GrabTaskForm.close）。
  顺手修复：`_save_passengers_or_refresh` 在 expect_stamp 为哨兵值时的 stale 误判
  （现在与 `_save_passengers_or_warn` 同口径跳过乐观检查）。

**验证（2026-10-13 实测）**
- `python -X utf8 -m compileall -q .` 全过。
- `python -B -X utf8 tests\test_all.py` → **Ran 696, failures=4, skipped=15**；
  4 个失败与重构前基线完全一致（Windows 平台差异，见第 7 节），业务零回归。
- GUI 冒烟：`pythonw gui.py` 启动 → 主窗口标题 `12306 车票监控与自动购票系统（桌面版）`、
  进程 Responding、运行 40 秒无 Traceback（monitor/launcher 两个日志均无异常）。
- 注意：启动后抢票表单会按旧行为自动查询余票（默认 2 秒间隔、rail 网络不可用时每轮记
  「[余票] 查询连续 N 次失败」警告）——与旧 LauncherApp 行为一致，非缺陷。

**未做/待办**
- git 未提交（工作树改动：M gui.py/launcher.py/launcher_utils.py/requirements.txt/tests/test_all.py、
  D launcher_widgets.py、?? README 更新等）；提交与否由用户决定。
- 抢票任务页未暴露「自动刷新开关」（与旧实现一致：变量驱动、界面隐藏）。
- 新建监控任务表单不再支持「按日席别」（seats_by_date）：单表单一次选定的席别作用于全部日期；
  旧任务的 seats_by_date 在编辑时原样保留（_build_edited_task）。

## 0.97 2026-10-15/16 后续批次：新增静态设计稿、清理脚本与 FOFA 采集（commit de7186d / f5950eb）

这三个目录是 §0.9/§0.95 之后新加进来的，**此前任何文档都没记过**，容易在下一轮被当成垃圾删掉。

### web-dashboard/（commit de7186d，静态设计稿，与运行时零耦合）
- 内容：`DESIGN.md`（166 行，设计令牌：浅色玻璃拟态 × 弥散分层，`--bg #EDF3FC` / `--glass rgba(255,255,255,.62)` / `--ink #182035` 等）、`index.html`（611 行，单文件静态控制台稿）、`PRODUCT.md`（58 行）、`preview-light.png`(335KB) / `preview-dark.png`(308KB)。
- 性质：**纯静态、零依赖、不发任何网络请求、无后端**（PRODUCT.md 明写）。产物是设计方向稿，**不是** `ui/` 的替代品，也未被 Electron 壳加载。
- 与现有界面的关系：它是 `ui/style.css` 玻璃拟态方向的**演进提案**（同样的材质语言，更彻底的「全域零实线边框 / 层级用阴影写」）。要落地需重写 `ui/` 的样式与部分结构，属独立批次。
- ⚠️ 陷阱：`web-dashboard/PRODUCT.md` 与根 `PRODUCT.md` **同名不同物**；前者是这份设计稿的产品说明，后者是本项目的产品定义。改文档时别改错。

### tools/clean_profile.py（commit f5950eb）
- 清 `.browser_profile/` 的可再生产物（BrowserMetrics / Crashpad / Cache / Code Cache / GPUCache / EdgeCoupons / 各种 *_diagnostic.log / Singleton* 等 8+7 组名单），**保留登录会话载体**（`Default/Network/Cookies`、`Local Storage`、`Session Storage`、`Preferences`、`Secure Preferences`、`Login Data`、根 `Local State` / `Last Version`）。
- 无参 = 干跑只列清单；`--force` 才真删。用 `filelock.file_lock(.browser_profile.lock, timeout=0.2)` 抢同一把字节锁——**抢不到即说明浏览器在跑，直接返回 2 不删任何东西**。
- 实测（2026-10-16）：干跑 21 项 / 244.9 MB → `--force` 后 profile 从 **511 文件 / 247.7 MB 降到 226 文件 / 2.8 MB**，会话载体全部保留。需要腾空间时先关掉下单浏览器再跑。

### runs/（commit f5950eb）
- `fofa_client.py`（101 行）：FOFA 资产采集脚本（单线程 + 最小间隔 + 指数退避 `[5,15,45]` + 结果缓存，`HARD_BUDGET = 30`，读环境变量 `FOFA_KEY`，`User-Agent: redteam-recon/1.0`）。**与抢票主链路完全无关**，是独立的侦察工具。
- `fofa-*.jsonl` 三个结果文件（234B / 0 / 0）也被一起提交了。注意 commit 标题写的是「FOFA 采集脚本」，与目录名 `runs/` 对不上，grep 时按 `fofa` 找。

### .browser_profile/ 现状
- 已 gitignore；清理见上。**内含登录态**，删了要重新 `python browser_order.py login`。

### tests/TEST_REPORT.md（历史存档 + 已加复核抬头）
- 5.3KB，抬头写「日期:2026-10-06 · 基线:a8b4b57 · 测试套件(47 项)」，内容为当时的二次自我检查报告（append_monitor_task 并发丢数据、mtime 15.6ms 盲区、误导性日志等**已修项**）。
- 当前基线是 **726 项**（2026-10-16 复核；本报告写于 47 项时代，早于 Electron 迁移与三轮重构），**不要拿它的数字当现状**；保留只为追溯那几个并发 bug 的成因分析。

## 0.99 本批（2026-10-17）：下单机制推翻重建（REBUILD_PLAN.md）

- 用户指令「推翻之前全部下单操作重新设计」→ `_order_impl` 单体的编排重建为 `orderflow.py` 七阶段管线（DETECT/FILL/SUBMIT/CONFIRM/HOLD/JUDGE/LOG）。
- 核心变化：提交后驻留——每 0.5s 读 page.url+body，读到「待付款」才关全部页面返回成功；首读未获先官方回读防 dup，再新开页面重下（retry_open_times=1），新旧页并存各自观察 10s；任一页读到失败词/dup 词/订单列表页即判即出；滑块出现即停交人工（等待不计入倒计时）；只认 payOrder 付款页为本次成功。
- DOM 序列（填单/提交/确认、limit_tickets 四步强写、核对窗取证）提取为 `browser_order._run_fill_submit_confirm` 共享函数，旧链继续调用——选择器单源。
- 分流：`order_flow_mode` 开关（config.json 默认 true）在 `browser_order.order_ticket_via_browser` 与 `launcher._run` 两处；旧 `_order_impl` 保留到 Task 9 实跑成功后退役。
- 配置：`order_flow` 段 read_poll_seconds=0.5/page_wait_seconds=10/retry_open_times=1；键入 config_keys.py 防漂移。
- 测试：tests/test_all.py 新增 TestOrderflowClassifySnapshot/TestOrderflowDispatch/TestOrderflowE2EMock 共 14 项；全量 754 OK (skipped=15)。
- **2026-10-15 夜实跑踩到的真 bug（已修）**：HOLD 首读把 12306「提交过程提示框」里的固定链接文字「未完成订单」当成重复订单 → 每次点确认后 2-5ms 就判 dup 关页面，永远读不到付款页。根因是 `passengerInfo_js.js` 的 `O()`/`E()` 第一句同步 `m("正在处理，请稍候。",false,"",false,"work")`，而 `m(ai=false)` 固定写入「查看订单处理情况，请点击“未完成订单”」链接。修复：`orderflow.py` 加 `_PENDING_KEYS`（过程提示→wait）、`_LINK_NOISE`（判 dup/fail 前剔除链接文案）、`_READ_JS`（提示框原文前置）、dup 日志记 url+上下文、结论截断 160→400；全量 760 OK (skipped=15)。
- **软件启动自动启动引擎（2026-10-16 用户要求）**：`api.main()` 在 `push("ready")` 之前调 `_auto_start_engine()`（`api.py` 新增 `_resume_stopped_tasks` / `_auto_start_engine` 两函数）——先把上次被动暂停的任务恢复 monitoring，再起引擎；`config.json` 新增 `auto_start_engine`（默认 true，键入 `config_keys.py` 防漂移）。测试 `TestAutoStartEngine` 5 项（只恢复「引擎已停止」标记、tasks 非 dict 不抛、resume→start 顺序、开关关闭时不调引擎、main 在 ready 前调自动启动）；全量 765 OK (skipped=15)。
- 待办：Task 9 实跑（需重新登录 + 有票窗口）；实跑成功后退役旧链。

## 0.98 本批（2026-10-16）：config/state 收敛 + 文档订正 + 会话失效判定修复 + 任务级自动下单开关

- **config/state 家族跨文件重复已收敛**（原第 10 节待办，用户 2026-10-16 裁决「消掉」）：
  `load_config / save_config / update_config_locked / update_state_locked / normalize_email_settings / read_history_records` 原在 `api.py`、`gui.py`、`monitor.py` 各有一份，现机械层单源到 `appcommon.py` 新分节「config.json 共享 plumbing」（appcommon.py:534 起）：
  `EMAIL_RE`（严格版）/ `_STAMP_UNSET` / `state_path_of` / `history_path_of` / `read_config_or_empty` / `_config_refusal` / `write_config_locked` / `update_config_locked`（`on_corrupt`/`on_non_dict`/`return_result` 钩子）/ `update_state_locked`（注入 `read_state`）/ `normalize_email_settings` / `read_history_records`（`on_non_list`/`on_error` 钩子）。
  **策略差异全部以参数/回调注入，未拉平**（RULES.md §七第 2 条硬约束）：monitor 的坏档指纹拒写 + CLI print、gui 的 messagebox + `guisave` + `return_result=True`、api 的 stderr 出口 + `apisave`、launcher 的路径解析。
  邮箱正则统一取**严格**口径（旧 `api.EMAIL_RE` 宽松，会让 gui 拒收的地址从桌面版设置页落进 config.json）。
  `state_file` / `history_file` 两键的路径解析在 api/gui/monitor/launcher 四处统一走 `appcommon.state_path_of / history_path_of`。
- **monitor.update_config_locked 新增损坏/非 dict 的 CLI 提示**（旧版静默 `return False`）——仍在锁内、仍在 mutator 之前拒绝，只是补一句人话；无测试断言旧文案。
- **顺带修掉一条「真打网络」的测试**（本轮全量跑暴露）：`TestTask77Order.test_classify_with_time_no_session_error_path` 在 `session=None` 时真去读项目里的 `.browser_state.json`，只要本机有登录态就会打真 12306 订单接口、结论随登录状态漂移（RULES.md 明写测试不打真实接口）。现改为 mock `order.session_from_browser_state` 抛 RuntimeError，断言语义不变并补一条文案断言。
- **回归**：`python -X utf8 -m compileall -q .` 干净；`python -X utf8 tests\test_all.py` → **Ran 711 tests, OK (skipped=15)**。代码改动 5 个文件（api.py / appcommon.py / gui.py / launcher.py / monitor.py），文档订正 5 个（RULES / HANDOFF / README / PRODUCT / tests/TEST_REPORT）。
- **本轮踩到的坑**：改造 `api.load_config` 时内部闭包 `_broken` 给模块级 `_CONFIG_CORRUPT_WARNED` 赋值忘了再声明 `global` → 名字退化成局部变量 → `UnboundLocalError`，被只覆盖该路径的 `test_api_load_config_gbk_still_returns_empty` 逮到。**模块级哨兵在嵌套函数里赋值，每层都要 `global`。**

### 会话失效被误判成瞬时故障（2026-10-16 修，本轮 CDP 冒烟时发现）

**症状**：浏览器模式下任务无限重试、永不下单，界面却只显示「监控中」。
**根因链**：踢会话时 12306 是 302 到 `/otn/passport?redirect=...`（**不是** `/login`），落点页是登录门户 HTML →
`session_ok` 里 `json.loads(raw)` 抛 `JSONDecodeError` → 旧代码把它兜进 `"校验异常: %s"`；
而 `engine.py` 的 `permanent = not ok and not who.startswith("校验异常")` 把「校验异常」整类当瞬时故障 →
`set_task_status(..., "failed")` 永不触发。`order.py:341-343` 的注释早就点过这个病（「只认 /login 会把真失效误判成临时故障 -> 无限重试、永不下单」），
纯 HTTP 版 `order.verify_session` 判对了，浏览器版漏了。
**修法**（判据与 `order.verify_session` 同口径）：
- `browser_order.session_ok`：goto/evaluate 单独一层 try（异常仍=「校验异常」，瞬时）；随后先看落点 `page.url` 含 `/login`|`/passport`、
  再看正文 `raw` 含「请先登录」/前 500 字含「未登录」，命中即 `(False, "已跳转登录门户：<url>")`；顺序在 `json.loads` 之前。
- 新增 `browser_order._PERMANENT_FAILURE_MARKS = ("已跳转登录门户", "接口返回 status=false")` 与 `session_failure_is_permanent(who)`：
  **白名单**判定（旧代码是 `startswith("校验异常")` 前缀黑名单），默认方向取「重试」——误标 failed 要人工恢复，代价更大，未知文案一律按瞬时。
- `engine.py:1342` 改为 `permanent = (not ok) and browser_order.session_failure_is_permanent(who)`。
  全库只有 engine 拿 `permanent` 做任务状态分支（launcher/api/monitor/gui 只看 `ok`），所以只动这一处。
**测试**：`tests/test_all.py` 的 `TestSessionCheckTransient` 加 5 例（门户落点 / 门户正文 / goto 超时 / 无门户标志的风控 HTML / 门户文案到引擎必须标 failed），跑真实 `session_ok` 而非 mock 文案。
**实跑取证**：失效会话下 `session_ok` 真实返回 `(False, "已跳转登录门户：https://kyfw.12306.cn/otn/passport?redirect=/otn/index/initMy12306Api")`，`session_failure_is_permanent` = True。
**回归**：`compileall` 干净；`tests\test_all.py` → **Ran 716 tests, OK (skipped=15)**。

**教训**：把「异常」整类当瞬时故障的兜底，会把真失效吞掉——兜底判据要用**确认失效的白名单**，不要用「排除了异常前缀就算永久」。

### 停止后日志仍在刷 + 无限重试（2026-10-15 修，用户截图报告）

**症状**：点了「停止」，全局日志栏仍每 2 秒一条 ERR「任务「…」抢票失败：浏览器会话不可用（校验异常: Expecting value: line 1 column 1 (char 0)）」，连刷十几条不停；且该会话失效永不把任务标 failed。

**这是两个独立缺陷**（上一节只修了 `check_session_if_needed` 那条判定路径，这两条都在**下单路径/线程收尾**上）：

- **缺陷 A：下单路径的会话失效仍被判瞬时**。`browser_order._order_impl`（browser_order.py:817-823）冷启动会话校验失败返回「浏览器会话不可用（%s）。请先运行：python browser_order.py login」，`engine._run_task` 的会话分支（engine.py:1170）只匹配「会话已失效」/「未找到会话文件」（那是 http 模式 `order.py` 的文案），browser 模式的文案匹配不上 → 落到兜底 `continue` → **每趟车重试一遍、永不 failed、停止也停不住**。
  修法：判据扩为 `("会话已失效" in msg or "未找到会话文件" in msg or browser_order.session_failure_is_permanent(msg))`，命中即 `set_task_status(task, "failed", "登录会话失效，请点界面上的「重新登录」后恢复任务")` + `return True, False`；engine.py:43 顶层 import browser_order（原本只在 check_session_if_needed 里局部 import）。
  文案保留「登录会话失效」串：`check_session_if_needed` 靠它做会话恢复后的自动重启监控，gui.py:4036 靠 `[错误] 登录会话失效` 触发重登弹窗。
- **缺陷 A 的文案层**：兜底分支（engine.py:1189）原来也打「任务「…」抢票失败」，让人以为任务已终止，其实每轮都在重试 → 改为 `LOG.warning("[重试] 任务「%s」本轮未成功：%s", name, msg)`。
- **缺陷 B：停止后在途任务不停**。`run()` 的 `stop_event` 只出现在 run() 内部（engine.py:1513/1535/1556），`_run_task` 看不见它；`executor.shutdown(wait=False)` 只停止提交新任务，**在途的一轮会跑完**（本例 18 趟车 ≈ 36 秒日志）。
  修法：`run()` 里 `self._stop_event = stop_event`；`_run_task` 用 `getattr(self, "_stop_event", None)` 兜底取（测试有 6 处直接调 `_run_task` 不经 run()），在 `for date in dates:` 与 `for seat_name in hit_seats:` 两个循环头检查 `stop.is_set()` 即 `return True, False`。
- **launcher 侧同样补判据**（launcher.py:493-503）：`Grabber._run` 拿到 `ok=False` 时不打「抢票失败」也不标 failed，会一路走到 fail_streak（要凑满 `MAX_ORDER_FAILS=5` 次、中间白等 5×3 秒）→ 现在 `session_failure_is_permanent(msg)` 命中即 `self.result = (False, "登录会话已失效（%s）。请点界面上的「重新登录」后重新开抢" % msg)` 并 return。
- **api.stop_engine 文案**（api.py:254）：join 超时时的「仍在收尾（在途任务未结束）」→「仍在收尾（在途任务跑完当轮即停）」。

**测试**：新增 `TestRunTaskStopAware`（置位 stop 后不再查询 / 无 `_stop_event` 时老路径仍跑）、`TestOrderPathSessionDead`（门户文案→failed 且 message 带「重新登录」/ status=false→failed / 「校验异常」→ 仍 monitoring）、`TestTask55ExcStreakStop` 加两例（launcher 门户文案→立即收尾且零等待 / 瞬时文案→继续重试）。
**回归**：`compileall` 干净；`tests\test_all.py` → **Ran 723 tests, OK (skipped=15)**。

**教训**：同一个「会话失效」在**三条路径**上各判一次（`check_session_if_needed` / 下单结果 / launcher 兜底），修一条不等于修完；判据要单源（`session_failure_is_permanent`），文案要能被恢复逻辑认出来。

### 有票却不自动下单：任务级开关在界面上「隐形」（2026-10-16 修，用户截图报告）

**症状**（用户截图 + 原文「有票点击启动任务也在监控但是没反应」）：任务卡状态「监控中」、引擎运行中，「已抢到 0」，「全部任务 1」，全局日志停在 `[启动] 监控引擎已启动` 不再增长。

**取证：不是引擎没跑，也不是没票**。`state.json` 的 `last_poll` 每轮都在更新（引擎确实在轮询）；`order_history.json` 尾部有 17:54:43 一条 `result: "hit_no_order"` / `message: "余票命中（任务未开启自动下单）"` / `notify: email:成功`。
根因是该任务 `config.json` 里 `"auto_order": false`：`engine._run_task` 的 `if not auto_order:` 分支只写 dedup `NO_ORDER` + 发提醒邮件 + 记历史，然后 `continue`；之后每轮同一 key 命中 dedup → `[跳过] …已记录(不自动下单)`，静默。

**结构性缺口（真缺陷）**：`rpc_list_monitor_tasks` 不返回 `auto_order`，任务编辑弹窗在 2026-10-13 重构时把四个开关移走（只留「设置 → 任务默认值」，且**只对新建任务生效**）→ **老任务的 `auto_order=false` 在界面上既看不见、也改不回来**，用户只能看到「有票没反应」。

**修法**：
- `api.py:350`：`rpc_list_monitor_tasks` 补 `"auto_order": bool(t.get("auto_order", True))`——缺省必须与引擎 `task.get("auto_order", True)` 同口径，否则老任务缺字段会被界面误标「仅提醒」。
- `ui/app.js`：任务卡新增「仅提醒」徽章（`t.auto_order === false` 且非管理模式时渲染；`ui/style.css:529` 的 `.no-order` 用 warn 色）；任务编辑弹窗加回「自动下单」勾选框（`id=fAuto`，放进 `.check-row` 复用通用选中态绑定，无需额外事件代码），保存时**始终**回传 `p.auto_order`（后端 `_req_on` 显式传优先，缺省才回落到「任务默认值」）。
- **从「仅提醒」改回自动下单要顺手清防重**：旧的 `NO_ORDER` dedup 会继续挡住下单（`empty_task_dedup` 只能走引擎，手改 `state.json` 会被 `_merge_state_for_save` 从内存复活）。故保存后对 `t.auto_order === false && p.auto_order` 的情况补一次 `resetTask`，再 `setTaskStatus` 还原编辑前状态——`resetTask` 会把任务拉回 monitoring，不还原就成了「编辑一下任务自己跑起来」。

**测试**：新增 `TestPerTaskAutoOrder`（listMonitorTasks 暴露 auto_order 且缺字段按 True / createTask 带回 auto_order / build_monitor_task 保留 auto_order；旧任务字段缺失时的界面语义）。
**回归**：`compileall` 干净；`tests\test_all.py` → **Ran 726 tests, OK (skipped=15)**。

**教训**：① 「有票没反应」先看 `order_history.json` 有没有 `hit_no_order`——命中过就说明引擎是好的，问题在下单开关。② 功能开关从 UI 移除但后端仍消费时，必须留一个**只读标识**，否则用户无法区分「没票」和「不下单」。③ 测试里 `start_now` 走默认 True 会经 `get_engine()` 单例写项目**真实** `state.json`（mock `start_engine` 拦不住）——调 `rpc_create_task` 必须显式 `start_now=False`。

## 1. 位置与仓库现状（2026-10-16 复核）
- 当前家：C:\develep\产物\12306（本项目唯一工作副本）。
- 旧位置 C:\develep\deepseek harness\工作目录\12306-probe 已删除（robocopy 复制 → 校验 MISSING=0 → 验证通过 → 删源）。
- 嵌套评审 worktree p1-review：**2026-10-15 已用 `git worktree remove` 移除**（内容早已并入 main），`git worktree list` 现只剩主树 190a919 [main]。同理 `_undo_backup/`、`_ann/`、根 `node_modules/` 均已删除。
- remote：https://github.com/c66-m/12306-ticket-monitor.git
- 分支/HEAD：**不在此写死 sha，跑命令看实况** —— `git status -sb`、`git log --oneline origin/main..main`。
  （旧版本这里记了「main = 60f4947 / ahead 4」之类的快照，提交后当场就自指过期，所以删掉；数字类的记录一律现查。）
  已推送基线：origin/main = **190a919**（2026-10-16 记录）。其后本地新增的是文档订正 + config/state 收敛批次（见 §0.98）。是否 push 是外向操作，**待裁决**。
- 工作树：共 **60 个跟踪文件**。历史在途改动**已全部提交**，包括：Electron 三件套（api.py / ui/ / electron/）、
  5 个过期 md 与 .scratch/（按用户裁决删除后一并提交）、launcher_widgets.py（已删除）、
  `web-dashboard/preview-*.png`（190a919 入库，`.gitignore` 已开白名单 `!web-dashboard/preview-*.png`）。
  新目录 web-dashboard/、runs/、tools/clean_profile.py 均已入库（见 §0.97）。
  __pycache__/ 按 .gitignore 忽略，本地存在但不入库。
  **工作树干净**：本轮 10 个文件的改动（HANDOFF / RULES / README / PRODUCT / api.py / appcommon.py / gui.py / launcher.py / monitor.py / tests/TEST_REPORT.md）已随 `92370ed` 提交，见 §0.98。
- fix/p1-critical（e4a090a）已并入 main（git merge-base --is-ancestor e4a090a main → true），本地无该分支、仅远程存在。
- 桌面快捷方式：C:\Users\Admini\Desktop\12306车票监控系统.lnk → C:\develep\产物\12306\启动桌面版.bat
  （不变，但 bat 内容已改为启动 Electron，见第 0.95 节）。
- 项目内无硬编码绝对路径，所有模块用 HERE = os.path.dirname(os.path.abspath(__file__))；启动桌面版.bat 用
  `%~dp0electron` 拼绝对路径启动（首次运行先 pushd 进去 npm install）→ 整体搬家安全。
  注意 bat **不能依赖 cwd**：cmd 启动批处理时 cwd 可能是继承来的任意目录，`cd /d` 曾因此没能生效。
- Electron 依赖只装在 electron/node_modules（已 gitignore）；electron/package.json 是唯一 Node 清单。

## 2. 模块地图（体积为本次实测；职责含前序会话结论）

| 文件 | 行数（2026-10-16 实测，node 口径） | 职责 |
|---|---|---|
| ui/index.html | 87 | **当前界面骨架**：顶栏（会话胶囊 / 拖动区 / 监控开关 / 窗口键 min·max·置顶·close）+ 侧边栏 4 页 + 主区 + 底部日志条 |
| ui/app.js | 1671 | **当前界面全部逻辑**：S 全局状态、call()/toast()/openModal()/showDD()、renderPage 与各页渲染、refresh* 轮询、外观设置、搜索联想、push 订阅 |
| ui/style.css | 960 | 当前界面样式（玻璃拟态 + 主题变量 + 外观设置项 + .eye-wrap/.g-result/.wb.on 等本轮新增） |
| electron/main.js | 221 | Electron 壳：spawn python api.py、JSON-RPC 桥（60 s 超时）、无边框窗口 1440×900、win/win-top 通道、F11、DSH_DEBUG 日志开关 |
| electron/preload.js | 27 | contextBridge：window.api = { call, onPush, win, top } |
| api.py | 约 1460 | **JSON-RPC 后端**：stdin/stdout 逐行 JSON-RPC 2.0，METHODS 37 项；引擎启停（_engine_stop）、日志泵 log_pump、抢票定时调度 _armed_loop、config/state 原子写与文件锁 |
| grabber.py | 1188 | **抢票业务模块（无界面）**：Grabber、任务库/配置 IO、车站搜索/类型探测、append_monitor_task、merge_trains_from_monitor、check_update、ensure_passengers、resolve_dates。**2026-10-18 由 launcher.py 改名**，调用方用 `import grabber as launcher` 别名 |
| launcher_utils.py | 290 | 纯业务工具（席别表/掩码 mask_id·mask_mobile/归一化/时间/车型/IP 定位 ip_locate/build_monitor_task）；无 tkinter 依赖 |
| engine.py | 1621 | 监控引擎：多任务调度、自适应频率、防重三层、历史/重试冷却；run(stop_event=None) 支持优雅退出 |
| browser_order.py | 1708 | Playwright 真浏览器下单全流程（_order_impl 分 8 段 mark()） |
| ticket.py | 823 | 查票 + parse_row 竖线行解析 + 席别/偏好规则 |
| order.py | 1042 | 订单查询/归因/session 载入与校验（唯一事实来源） |
| monitor.py | 1140 | 命令行后备入口（无 GUI 环境；功能以桌面版为准） |
| passengers.py | 496 | 乘车人（_passengers_key 为 Fernet 密钥文件，非 Windows 落明文 0o600） |
| capture_session.py | 305 | 采集登录态 cookie（Playwright 打开浏览器人工登录一次） |
| probe_login.py | 520 | 登录链路诊断（域名连通性/二维码/会话有效性；只诊断不绕过） |
| appcommon.py | 708 | 跨模块机械层：日期区间解析、原子文件写、state.json 读容错、PII 脱敏口径（mask_order_no / mask_one_name / mask_names / mask_id_partial）、北京时间（beijing_tz）、展示归一化（normalize/display 车次与席别） |
| filelock.py | 144 | 跨进程文件锁（msvcrt 字节锁） |
| logutil.py | 132 | 按天滚动日志 handler（保持 logs/<prefix>_YYYYMMDD.log 命名） |
| notify.py | 147 | 邮件通知（SMTP SSL） |
| station_db.py | 152 | 全量车站本地库（3404 站）；`rebuild` 合成、`check` 校验三份同源车站文件 name↔code 漂移（非零退出） |
| config_keys.py | 22 | 配置键唯一定义处（测试防漂移） |
| legacy/gui.py | 4300 | **legacy（已停用）**：旧 Tkinter 单窗口界面，2026-10-18 移入 `legacy/`；不再是任何入口，仅因 tests/test_all.py 深度引用（288 处）而保留；新功能一律改 ui/ |
| launcher_widgets.py / theme.py / liquid.py | 已删除 | 2026-10-13 随 Tkinter 界面推翻；`_undo_backup/` 备份目录已于 2026-10-15 清理 |
| tools/gen_icon.py、tools/gen_icon_emoji.py | — | 图标生成脚本（原根目录 _icon_gen*.py，2026-10-14 归位） |

数据/状态文件：config.json、launcher_config.json、state.json、orders.json、order_history.json、passengers.json、grab_tasks.json、session_cookies.json、.browser_state.json、.browser_profile/（Playwright 持久化 profile，含登录态）、stations_db.json(622855B, 2026-10-15 重建)、station_index.json(234485B)、station_name.json(160853B)、station_kind.json(2068B)、logs/。（`_undo_backup/` 已于 2026-10-15 清理。）

## 3. 12306 领域知识（硬核，改代码前必读）
- queryLeftTicket 竖线行：3=车次码，11=可否购买；席别计数位置 20 gg_num(优选一等座)、21 gr_num、22 qt_num、23 rw_num、24 rz_num、25 tz_num、26 wz_num、27 yb_num、28 yw_num、29 yz_num、30 ze_num、31 zy_num、32 swz_num、33 srrb_num、34 yp_ex、35 seat_types、37 houbu_train_flag（有候补标记）。
- 官方席别码：9商务座 P特等座 D优选一等座 M一等座 O二等座 A高级动卧 6高级软卧 F动卧 I一等卧 J二等卧 4软卧 3硬卧 2软座 1硬座 WZ无座。
- 共享字段（一个位置对应多席别）：rw_num(p23)=软卧/动卧/一等卧；gr_num(p21)=高级软卧/高级动卧；yw_num(p28)=硬卧/二等卧 → 具体是哪种由 train_seat_kind() 的车型推断。
- 死字段：p22/p24/p27/p33（1788 样本恒空，别用）。
- p35 seat_types 是无分隔符码串（形如 "OFAO"），只能子串判断，不能切分。
- 网页端不卖无座：ORDER_SEAT_ALIAS = {"WZ": "1"}（普速无座→硬座）、EMU_SEAT_ALIAS（G/D/C 无座→二等座）。
- 候补：p37 非 0 → 该席别可候补（GUI 三态着色：有票/候补/售完）。

## 4. 下单结果判定（已落地的核心逻辑，别回退）
- order.classify_with_time(date, train, passenger_names, not_before_ts=None, session=None) → (cls, ono, raw, recent)；底层 find_recent_order(orders, date, train_code, passenger_names, not_before_ts, window_sec=180)：本行程 + 乘客交集 + 订单时间 ≥ 本次提交时刻 −180s，取最新。
- 归因口径（灵魂）：unpaid + 命中 recent → 认定「本次已提交订单（未支付），下单时间 X」；unpaid 无 recent → 「账号存在该行程更早的未支付订单，非本次提交」；paid → 「官方确认已支付」。
- dup 判定：页面文本/URL 判 dup 不可靠（成功页正文本身含「未支付/已预订/已购买」等字样）。必须先排除 dup_kind == "行程冲突"（那是真失败，提示去「未完成订单」查证），其余回读归因。
- ambiguous 三态：none/cancelled → 允许安全重试（ambiguous_retries 上限 3）；unpaid 且有 recent → 判本次成功并停任务；其余（unpaid 无 recent / blocked / error / unknown）→ 失败停任务交人工。
- engine 侧：_order_once 调用前记 attempt_ts = time.time()；_record_success 收的是 "、".join(p_names) 字符串（内部再 .split("、")）。
- engine 启动恢复复核 orders.json 仍用三元组 classify_order_status(...)，不参与归因。
- 规则已写进 RULES.md（「下单结果判定」章节）。

## 5. 席别选择规则
- ticket.py：seat_priority_list（认不出→硬座）、seat_priority_expanded（无座→无座+硬座）、seat_pick_order(checked, priority, avail=None)、seat_rules_parse(value)（train=seat 语法）、seat_candidates_for(train_code, checked, priority, avail=None)、seat_priority_feedback(text, checked=None, trains=None)（即时解析反馈）。
- 口径：先抢手填首选席别；勾选项只作回退；无座按硬座处理；认不出按硬座；UI 要给即时反馈。
- per-train 绑定已实现：train=seat 语法（如 K225=硬座/无座），无前缀=全局回退。
- 主抓票循环（launcher）：外层车次、内层席别，取第一个「有票且不在 bad 集合」的 (code, seat) 即下单。
- 自动下单资格只看 SEAT_NAME_TO_CODE；不在码表里的席别标记「（不能自动下单）」并跳过。

## 6. 抢票场景显示口径（已交付）
未放票时显示该车次拥有的全部席别（ticket.seat_names_all(codes, available=None, train_code=None) + launcher _rebuild_seats(available, allnames, houbu) / _on_cards_changed / _render_seat_detail 三态着色），无票席别也能勾，放票/退票回流后按勾选顺序抢。

## 7. 测试与验证口径
- 语法检查：python -X utf8 -m compileall -q .
- 全量测试：python -X utf8 tests\test_all.py → 现状 **Ran 726, OK (skipped=15)**（2026-10-16 复核，零失败）。
  历史：曾一度为 696/5/15、701/4/15，那批 Windows 平台差异失败（test_append_history_shape_invalid_abandon_rereads、
  test_cookie_path_absolute_passthrough、test_menu_notify_routes_password_through_protect_secret、
  test_load_session_relative_path_resolves_against_here、test_save_session_relative_path_resolves_against_here）
  已全部修复转绿。
  测试输出定位法：`python -X utf8 tests\test_all.py > $env:TEMP\t.txt 2>&1` 后 `Select-String -Pattern '^Ran \d+ tests|^OK|^FAILED'`（直接接管道会被尾部 CLI 菜单干扰）。
- 每次改动后必须实跑：compileall → tests → 相关界面冒烟。
- **界面冒烟（当前 Electron 版）**：双击「启动桌面版.bat」（或直接跑 electron.exe .），
  加 `--remote-debugging-port=9222` 后用 CDP 连 `http://127.0.0.1:9222/json/list`，
  `Runtime.evaluate` 断言 `document.querySelector('#content')` 等节点、`Page.captureScreenshot` 存图。
  后台启动 electron 时要 `Start-Process -RedirectStandardOutput/-RedirectStandardError`，
  不要挂在 job 里（job 结束会 SIGTERM 掉主进程，表现为 EPIPE + main.js:19 的 crash 弹窗）。

## 8. 环境与操作要点（踩过的坑）
- Python：C:\Users\Admini\AppData\Local\Programs\Python\Python312\python.exe；GUI 用 pythonw。
- PowerShell 5.1 读 UTF-8(无 BOM) 的 .ps1 会把中文路径解成乱码（Get-ChildItem 静默返回 0 项）→ 用内联命令（tools.pwsh）或写带 BOM 的脚本；控制台里中文显示成乱码只是解码问题，文件本身没事。
- 界面冒烟（当前）：见第 7 节 CDP 套路；legacy gui.py 若需冒烟才用 Start-Process pythonw -ArgumentList 'gui.py' + MainWindowTitle 断言（标题「12306 车票监控与自动购票系统（桌面版）」）。
- 状态写入一律 tmp + os.replace 原子替换；文件锁用 filelock.py（msvcrt 字节锁）；.browser_profile.lock 是跨进程独占（并发任务会在下单步串行化）。
- 截图/窗口枚举靠 pythonw + Get-Process MainWindowTitle + P/Invoke。

## 9. 当前运行状态
- 登录态已失效：order.verify_session(order.session_from_browser_state()) → (False, '已跳转登录门户：https://kyfw.12306.cn/otn/passport?redirect=/otn/index/initMy12306Api', True)。第三位 True = 服务端明确判失效（非网络故障）。
  浏览器版同口径（2026-10-16 实测）：browser_order.session_ok(ctx) → (False, '已跳转登录门户：https://kyfw.12306.cn/otn/passport?redirect=/otn/index/initMy12306Api')，session_failure_is_permanent → True。
- cookie 文件(.browser_state.json 4721B / session_cookies.json 1593B)搬迁前后逐字节一致 → 自然过期，不是搬家造成。抢票前需重新登录一次。
- 车站文件一致性（2026-10-15 实测）：`python station_db.py check` → 漂移 0 处；stations_db.json 已 rebuild（3404 站，待识别 3313），三份同源文件时间戳漂移消除。
- 免登录链路正常（本次实测）：ticket.load_station_map() 3404 站；ticket.query_tickets('SHH','GZQ','2026-10-16') → 26 车次；parse_row → G1305 上海南→广州新塘，seat_names_all → ['商务座','一等座','二等座','无座']；train_seat_kind('G1305')=G、train_seat_kind('K225')=普速。
- verify_session(session) 语义：返回 (ok, who, permanent)；非 JSON 响应重试 3 次，避免把 12306 临时繁忙误判为登录失效。

## 10. 未决 / 待办（没有授权就别动）
- ~~main ahead 8：是否 git push~~ **已完成**：HEAD=190a919 已推送，与 origin/main 同步（见第 1 节）。
- ~~交接文档校正未提交~~ **已完成**：3f683ff（HANDOFF/README）+ 671324a（PRODUCT.md）已提交，工作树干净。**下次 push 时这两个 commit 会一起上去**。
- ~~5 个 Windows 平台失败测试是否补 skipif 守卫~~ 已全部修复转绿（现 726 OK）。
- ~~浏览器模式会话失效被误判成瞬时故障（引擎永不标 failed、无限重试）~~ **已修（2026-10-16，见 §0.98 末节）**：session_ok 先判落点/正文的门户标志（顺序在 json.loads 之前），引擎改用白名单 session_failure_is_permanent；新增 5 条测试。
- ~~**停止后日志仍在刷 + 会话失效无限重试**（2026-10-15 用户截图报告）~~ **已修（2026-10-15，见 §0.98 末节「停止后日志仍在刷」）**：这是与上一条**不同的两条路径**（下单路径 + 线程收尾）。
  其一：browser_order 的「浏览器会话不可用（…）」匹配不上 engine 的会话判据 → 落兜底 continue → 每趟车重试、永不 failed；现接 session_failure_is_permanent，launcher 侧（Grabber._run）同样接，避免撞满 MAX_ORDER_FAILS=5 才停。
  其二：engine 的 stop_event 原本只在 run() 内部可见，_run_task 感知不到停止，stopEngine 后在途一轮仍跑满（用户截图里 17:23:02→17:23:13 每 2 秒一条 ERR 就是这么来的）；现 run() 暴露 self._stop_event，_run_task 在两个循环头 getattr 兜底查 is_set() 即退出。
  **注意：需重启桌面端生效**（入口是 Electron 壳 + 子进程 api.py）。
- ~~**有票却不自动下单：任务级开关在界面上「隐形」**（2026-10-15 用户截图报告「有票点击启动任务也在监控但是没反应」）~~ **已修（2026-10-16，见 §0.98 末节）**：`rpc_list_monitor_tasks` 不返回 `auto_order`，编辑弹窗在 2026-10-13 重构时移走四个开关（只剩「设置 → 任务默认值」且只管新建）→ 老任务 `auto_order=false` 时界面完全看不出来。现补 API 字段 + 卡片「仅提醒」徽章 + 编辑弹窗勾选框，改回自动下单时顺手 `resetTask` 清旧 `NO_ORDER` 防重。**界面改动需重启桌面端生效。**
- 一等卧/二等卧/高级动卧（官方码 I/J/A）是否进下单码表（长期未决）。
- 重新登录后做一次真实下单核对（核对窗席别 vs 订单详情，需登录实跑）。
- ~~**config/state 家族跨文件重复尚未收敛**~~ **已完成（2026-10-16，见 §0.98；策略以参数/回调注入保留，未拉平）**（用户 2026-10-16 裁决「消掉」）：load_config / save_config / update_config_locked / update_state_locked / normalize_email_settings / read_history_records 在 api.py、gui.py、monitor.py 各有一份。实测三份不是机械重复而是**能力分层**（monitor 有 FileNotFoundError 分支 + `_DAMAGED_STAMP` 指纹拒写 + expect_stamp；gui 有 messagebox 弹窗与 `guisave` 临时名；api 最简但有 expect_stamp），收敛必须**参数化保留策略**（RULES.md §七 第 2 条是硬约束），落点是 appcommon.py（已具 state.json 共享 plumbing 分节，含 read_state_or_none / `_read_history_list`）。逐字等价可无痛下沉的只有 `load_state` 与 `normalize_email_settings`（后者须统一选 gui 的**严格** EMAIL_RE，api 现为宽松版）。
- 两条驱动循环（launcher Grabber 一次性 vs engine MonitorEngine 常驻）是否重构——语义不同，暂缓到真实抢票跑通之后。
- ~~**RULES.md §七 两条门禁的数字与措辞已过期**~~ **已订正（2026-10-16）**：
  (a) launcher.py 实测 **1164 行**（旧文「约 3000 行」已删）；
  (b) 「门面保 `python launcher.py` 兼容」条款删除，改为明写「已于 2026-10-13 停止支持（launcher.py 无 `__main__`），门面只需保 `import launcher`」；
  (c) `browser_order._order_impl` 599 行（`browser_order.py:726` → 下一顶层定义 `:1324`）已写进 RULES。
- ~~**拆 launcher.py 的《拆分清单》已删**~~ 已在 RULES 里注明「原稿已删，真拆时重新起草」，不留悬空引用。
- `web-dashboard/` 设计稿是否落地到 `ui/`（见 §0.97）：它是玻璃拟态方向的演进提案（全域零实线边框、层级用阴影写），落地需重写 ui/ 的样式与部分结构，属独立批次。
- ~~`tests/TEST_REPORT.md` 陈旧~~ **已裁决并完成（2026-10-16）：保留 + 加存档抬头**（历史成因分析有价值），文末追加「十、2026-10-16 复核增量」记录本轮代码变更与回归。
- ~~5 个 md + .zip + .scratch/ 如何处置~~ **已裁决（2026-10-16）：删除**，已提交。
- ~~PRODUCT.md 视觉口径冲突~~ **已裁决（2026-10-16）：以落地的 Electron 玻璃拟态为准**，PRODUCT.md §Brand Commitments 已改写。

## 10.9 2026-10-14 本轮批次（Electron 化后续 + 清单 17 项）

来源文档：`12306-桌面端-未完成清单与待办.md`（根目录，本轮全部条目已处理）。截至本轮结束：

- **A 组（认知错误）**：A1 停止引擎假停止已修（api.py `_engine_stop` + join(10)；前端按布尔返回提示）；
  A2 抢票结果渲染已修（后端 grabTicker 回读 result_ok/result_msg/result_at 并落盘 grab_tasks.json，
  前端卡片加 `.g-result` 行）；A3 乘车人证件号眼睛切换；A4 列表脱敏 + `passengerDetail` 取明文。
- **B 组（缺失能力）**：B1 IP 定位（抢票页工具栏「📍 定位」+ 设置页「关于」卡）；B2 检查更新
  （设置页「关于」卡 + 启动静默检查；注意 `launcher_config.json:41 update_url` 仍为空串，
  永远返回未配置）；B3 重置防重（监控任务右键「♻ 重置防重」→ `resetTask`）；B4 删除预设
  （抢票页预设下拉「🗑 删除预设…」）；B5 抢票任务改名（卡片「✎ 改名」→ `renameGrabTask`）；
  B6 查询历史（抢票页工具栏「🕘 查询历史」+ `pushQueryHistory` 自动记账到
  `launcher_config.json.query_history` 最多 10 条）；B7 窗口置顶 + **完整小窗**（顶栏图钉；
  小窗见下面第 0.96 节）。
- **C1**：config.json 里 3 条脏任务（`{"name":"任务X"}`）已用 deleteTask 清掉。
- **D 组**：D1 根目录 111 个临时文件已清理（`_icon_gen*.py` 归位到 tools/ 改名
  gen_icon.py·gen_icon_emoji.py，其余 `_cdp_*.mjs`/`_*.png`/`_*.txt` 全删）；
  D2 本文档已改写为 Electron 架构（第 0.95、2 节）；D3「启动桌面版.bat」已补
  `set ELECTRON_RUN_AS_NODE=` 与 `set ELECTRON_MIRROR=…`；D4 main.js 的 `[rpc]`/renderer 转发已加
  `DSH_DEBUG` 开关；D5 见 D3 的镜像行；D6 `gui.py` **保留原地**并标注 legacy
  （移动会破坏 tests/test_all.py 的 288 处引用与 TestConfigKeys._PRODUCT_FILES 产品文件清单）。

## 0.96 抢票小窗（ui/mini.html，2026-10-14 补 B7 第二步）

给「只看抢票结果」的场景做的小窗口，规格对齐 Tkinter 版 `gui.py:3637-3770 MiniWindow`：
460×420（min 340×260）无边框、玻璃底。入口是主窗顶栏的 `#btnMini`（`.winbtns` 里，图钉左侧）。

结构：顶栏（标题 / 引擎状态 / 置顶 / 回主窗 / 最小化 / 关闭）→ 账号芯片 + 启停按钮 → 任务区 → 日志区。

- **任务区**（`renderTasks`）：只显示 `success`（已抢到，永远排最前）与 `monitoring/retrying`，
  其余状态一律不出现；`lastSig` 签名去重，2 s 轮询不重绘。
- **日志区**（`feedLog`）：正则抽时间戳，只收含 `MINI_TAGS` 的行
  `["[有票]","[抢到]","[错误]","[冷却]","[防重]","[跳过]","[会话]","[状态]"]`
  （对齐 `gui.py:3639 MINI_LOG_TAGS`——引擎启动横幅、每轮 `[运行]` 播报、`[配置]` 同步这类噪声全部过滤）；
  `[抢到]`→accent 加粗、`[有票]`→warn、`[错误]`→err；上限 400 行，超了从头部裁 60 行。
- **主窗联动**：打开小窗 → `win.hide()`；关闭小窗 → `win.show()+focus()` 并执行一次
  `window.refreshAll()`。**主窗隐藏期间渲染进程被浏览器降频，轮询会变慢**，所以关小窗必须补一次对齐
  （`ui/app.js` 为此把 `refreshAll` 挂到了 `window`；小窗靠 2 s 轮询自己拉数据，不依赖主窗推送）。
- **推送**：主进程用 `broadcast()` 给所有窗口发 `py-push`——小窗开着时主窗是隐藏的，
  只发给主窗的话小窗收不到日志。
- **外观**：小窗是另一个渲染进程，`file://` 下 localStorage 不保证共享，所以打开时主进程用
  `additionalArguments: ["--appear=<主窗 localStorage JSON>"]` 注入，preload 解析成 `window.api.appearArgs`；
  小窗字号固定比主窗小 1 px。
- **通道**：`window.api.mini()` / `miniClose()` / `miniWin("min"|"top"|"close")` / `onMiniPin(cb)`。
  注意 `setAlwaysOnTop` 之后 `isAlwaysOnTop()` 在个别环境有约 1 s 的读取延迟（实测连续点击时
  第一次回报可能是旧值），所以置顶态以主进程回报为准、不要在前端自己取反。

## 11. 上手禁忌（改代码前必看）
- 当前界面是 ui/ + api.py（Electron）。**别再去改 legacy/gui.py 加功能**（停用代码，只为测试而存在）。
- ticket.py parse_row 的位置魔数索引不要动——RULES.md 与全部席别推导都依赖它。
- 两条驱动循环语义不同，别合并；RULES.md 的工作流定义别绕过。
- 原子写/锁的现有差异是修过的并发 bug（filelock 路径、bad 文件重命名、mtime 二次探测、engine 的 deepcopy 直写回退），抽取公共层时必须参数化保留，不许拉平。
- 日期区间解析现有口径：max_span_days=5（最多 6 个日期），多处入口已统一，别改口径。
- 项目文档：RULES.md（业务规则）、README.md、PRODUCT.md、HANDOFF.md（本文件）。拆 grabber.py 的清单要求见 RULES.md §七。

## 12. 下一次开工第一步建议
1. cd C:\develep\产物\12306（**测试必须在项目目录下跑**：tests 用相对路径读产品文件，换目录会 FileNotFoundError 假失败）
2. git status -sb + `git log --oneline origin/main..main` 看实况（本文档不记 sha；是否 push 待裁决）
3. python -X utf8 -m compileall -q .
4. python -X utf8 tests\test_all.py（预期 807 OK / skipped=15；行数为量级参考，精确值用 wc -l）
5. 改界面/后端就去 ui/ 与 api.py（**别再去 legacy/gui.py 加功能**，它是 legacy）；改完按第 7 节做一次 CDP 界面冒烟
6. 需要下线实跑就先重新登录，再动代码。

## 13. 2026-10-18 数据目录分离（本批变更，已过 800 测试）

**动机**：根目录原先平铺 18 个 .py + 11 个数据 json + 3 个车站大库 + 多个 .lock，
代码与数据混杂；`%SystemDrive%` 幽灵目录也是这类问题的副产物（里面有搜狗输入法的
活数据，本次未动）。

**结构**：

```
（根目录）  代码 + config.json + launcher_config.json + .browser_profile/ + logs/
data/      运行时数据：orders / state / order_history / passengers / grab_tasks /
           session_cookies / .browser_state + 车站四库（stations_db / station_index /
           station_name / station_kind）
```

**单一解析口径** `appcommon.data_path(name)`：
1. 绝对路径直通（测试注入）；
2. `data/<name>` 存在 → 新布局；
3. 根目录 `<name>` 存在 → 旧布局兼容回退；
4. 都不存在 → 落 `data/<name>`。

`appcommon.state_path_of / history_path_of` 改走 `_resolve_data_file`：base_dir 是
项目根时用 data_path，是临时目录时严格落临时目录（测试隔离）。

**必须知道的坑（本批踩过）**：
- `ticket.py::_abs_cache_path` 原先直接拼 `__file__` 目录，车站缓存刷新会把
  `station_name.json / station_index.json` **写回根目录**，抵消数据分离。已改走 data_path。
- `engine.orders_path` 做成实例属性：`_run_task` 的「官方回读未支付」分支会 upsert
  订单，测试用 `object.__new__` 构造引擎时若不注入 `orders_path`，假单会写进真实库
  （本批真的发生过，已从迁移备份恢复）。
- 测试入口新增**全局数据隔离**：`appcommon.DATA_DIR` 重定向到临时副本（只复制车站
  四库，不复制隐私数据）；保险名单 `_REAL_FILES` 补了 `orders.json`。
- 故意不迁：`config.json`（用户手改的配置）、`.browser_profile/`（换路径 = 丢登录态
  与设备指纹）、`logs/`（测试锁定目录形态）。

**迁移与回滚**：`python tools/migrate_data_dir.py --dry-run` / `--rollback`；
本次备份在 `data_migration_backup/20261018-113333/`（含隐私数据，已在 .gitignore）。

**依赖锁定**：`requirements.txt` 按实际环境回填 `==`（requests 2.33.1 /
playwright 1.58.0 / cryptography 50.0.2）。升级 playwright 必须跟跑
`python -m playwright install chromium`，否则浏览器二进制与库版本不匹配。

**调试产物出库**：`runs/fofa-*.jsonl` 已 `git rm --cached`（本地文件保留），
`.gitignore` 增加 `runs/*.jsonl` 与 data/ 精确忽略规则（车站库仍入库）。

## 14. 2026-10-18 第二批：legacy 归档 + 抢票模块改名

**动机**：根目录 18 个 .py 平铺，已停用的 `gui.py`（4300 行）与活跃模块混放，
容易被误当活代码维护；`launcher.py` 原名暗示「启动器」，实际是抢票业务模块
（启动器界面 2026-10-13 已删除），名字与职责不符。

**变更**：

1. `gui.py` → `legacy/gui.py`（新建 `legacy/` 包）。文件内 **`HERE`（legacy/ 自身）
   与 `APP_ROOT`（项目根）分离**：`CONFIG_PATH` / `log_dir` / `state_path_of` /
   `history_path_of` / `capture_session.py` 路径全部改用 `APP_ROOT`。
   测试：`import gui` → `from legacy import gui`；4 处 `patch.object(gui, "HERE")`
   → `"APP_ROOT"`；2 处字符串式 `mock.patch("gui.subprocess.run")` →
   `"legacy.gui.subprocess.run"`；`_PRODUCT_FILES` 里 `gui.py` → `legacy/gui.py`。
2. `launcher.py` → `grabber.py`，`launcher_utils.py` → `grabber_utils.py`。
   调用方统一用别名（`import grabber as launcher` / `import grabber_utils as
   launcher_utils`），数百处 `launcher.xxx` 调用点**零改动**。
   内部 `import launcher` 全部改 `import grabber as launcher`（`station_db` 三处、
   测试六处）；测试 `patch.dict(sys.modules, {"launcher": ...})` → `{"grabber": ...}`
   （否则导入真实模块 → 离线空索引提前返回）。
3. 文档：README 模块地图 / HANDOFF 当前架构与模块地图同步；**历史批次记录不动**
   （改历史会让「当时发生了什么」失真）。

**验证口径**：`python tests/test_all.py` → 800 tests / OK / skipped=15；
真实 `data/` 全文件 md5 零变化。

**遗留**：`%SystemDrive%/` 目录仍在（内含搜狗输入法活数据，属外部程序副作用，
不属本项目产物，未动）；建议在确认无程序依赖后手工删除。

## 15. 2026-10-18 第三批：工程化收尾（pyproject + 一键自检）

**动机**：前两批改造（数据分离、模块改名）靠人工核对完成，容易在后续改动中退化；
项目也没有 `pyproject.toml`，Python 版本下限与工具口径都是口口相传。

**新增**：

1. `pyproject.toml`：声明 `requires-python >=3.9`（代码未用 match / PEP 604 /
   3.11+ 标准库，实测 3.12）、运行时依赖（与 requirements.txt 同源）、ruff 检查
   范围（排除 legacy/electron/产物目录）、pytest 收集路径。**故意不声明
   `[build-system]`**——本项目不打包分发（Electron 直接 spawn `api.py`）。
2. `tools/check.py`（270 行）：一键自检 5 项 —— 语法编译 / 遗留引用 /
   数据完整性 / 车站库一致性 / 全量测试。`--quick` 跳过测试（约 1 秒），
   退出码 0/1，可直接接 CI。

**自检当场抓到的真 bug**：`station_db.check()` 读车站四库时用
`os.path.join(HERE, name)`，数据迁到 data/ 后直接报「文件缺失」——这是数据分离
漏掉的第三处（前两处：`ticket._abs_cache_path` 车站缓存写回根目录、
`engine.orders_path` 不可注入）。修复：新增 `appcommon.resolve_data_file(base_dir, name)`
公开函数（项目根走 data_path、临时目录严格本地），`station_db.check` 改用之；
`state_path_of` / `history_path_of` 同步复用同一实现。

**为什么「遗留引用」这一项值得留着**：它把三批改造的约束变成可执行检查——
`import launcher` / `from gui import` / `os.path.join(HERE, "*.json")` 一旦在
新代码里复活，自检直接红。白名单只允许 4 个用户配置文件
（config / launcher_config 及其 example）。

**验证口径**：`python tools/check.py` → 5/5 OK（800 tests, skipped=15）。

## 16. 2026-10-18 登录窗口「一直刷新」根因与修法

**现象**：点「立即登录」后登录页反复刷新，扫码/输密码来不及（此前还出现过
「弹出两个登录页」）。

**根因**：`login()` 用「探针页」校验登录态——一进循环就 `ctx.new_page()` 再导航
`CHECK_URL`。在可见浏览器里 `new_page()` 是**真的多开一个窗口**（实测
`--app` 启动 pages=1，`new_page()` 后 =2）；未登录时该导航被 302 到登录门户，
于是既出现第二个登录页，又每 15 秒重复一次（用户看到的就是页面不停刷新）。

**修法**：登录期间**不再开任何新页面、不做整页导航**。校验改为在登录页内用
同源 `fetch('/otn/index/initMy12306Api')`（`_page_session_ok`）：不导航、不开窗、
不刷新；未登录时落点 URL 带 `/passport` 即判为未登录。兜底节拍保持 15 秒，
只是不再需要探针页。

**验证**：
- 真实浏览器（临时 profile，35 秒覆盖 20 秒兜底点）：页面数恒为 1、
  `framenavigated` 事件 0 次、URL 始终停在 `login.html`。
- 登录相关 11 个用例 + 全量 807 tests 通过。

**顺带修掉的数据事故**：`GRAB_TASKS_PATH` 等**模块级路径常量在 import 时就定死**，
测试重定向 `appcommon.DATA_DIR` 对它们无效，导致测试把真实
`data/grab_tasks.json` 清空成 `{"tasks": []}`（已从迁移备份恢复 3 条任务）。
修法：测试隔离时一并重绑这些常量；并在 `__main__` 结尾对真实数据做一次
全量 md5 比对，任何漂移直接退出码 1（不再依赖用例是否继承 `TempDirCase`）。

## 17. 2026-10-18 登录页 ERR_FAILED：移除文档级导航拦截

**现象**：登录窗口停在「嗯… 无法访问此页面」，地址为
`https://kyfw.12306.cn/otn/login/userLogin`，错误码 `ERR_FAILED`。

**根因**：`login()` 在登录页上挂了 `page.route("**/*")`，把「首次之后的 document
请求」一律 `route.abort()`。12306 的登录流程本身要导航到 `/otn/login/userLogin`
（账号登录 / 登录后中转），被 abort 后 Chromium 直接渲染错误页——窗口从此不可用。

**为什么当初会加这条拦截**：2026-10-17 反馈「刷新太快，妨碍扫码」，当时真正的
原因是探针页每 2 秒导航一次 `CHECK_URL`（第二个窗口在闪）。探针页已在 §16 删除
后，实测登录页 30 秒内零文档导航、零周期请求——拦截只剩副作用。

**改动**：删除 `_no_reload` 路由与 `allowed` 计数，登录页不再挂任何拦截；
`--app=` 与 `need_nav` 的首次导航逻辑不变。测试同步：删掉两条拦截规则用例，
新增「登录页不得挂文档拦截」的回归用例。

**验证**：
- 真实浏览器：直接 `goto /otn/login/userLogin`、以及用 JS 从登录页跳该地址，
  均正常落在 12306 页面上，不再出现「无法访问此页面」。
- 806 tests OK (skipped=15)；真实 data/ 与 config.json md5 未变。

## 18. 2026-10-18 编辑任务被「重建」且跑不起来

**现象**：在任务上改日期后，列表里多出一条同名任务；新任务一点就失败
「监控日期已全部过期，任务自动停止」。

**根因（两个叠加）**：

1. **没有原地编辑接口**：编辑 = 删旧建新。但任务名由「线路+车次+席别」生成，
   改日期后名字**完全一样**，前端 `t.name !== r.name` 恒为假 → 旧任务不删，
   列表出现两条同名任务。
2. **区间起止可以颠倒**：真实数据里出现 `date_range=['2026-10-19','2026-10-18']`。
   前端 `dateSpan` 对倒置区间返回 `[]`，落盘成 `dates=[]` 的空日期任务；引擎
   `expand_dates` 对倒置区间直接跳过 → 一条日期都没有 → 「已全部过期」。
   更麻烦的是 `state.json` 以**任务名**为键，同名重建会继承旧任务的 failed 状态，
   新任务于是也永远跑不起来。

**改动**：

- `api.py`
  - `_split_date_input`：区间倒置时自动交换并记一条警告；
  - `rpc_create_task`：日期为空直接返回 `{ok: false, error: ...}`，不再造出必失败的任务；
  - `rpc_delete_task`：支持 `uid` 精确删除（同名任务按名字只能删到第一条）；
  - `rpc_list_monitor_tasks`：返回 `uid`，供前端精确删除。
- `ui/app.js`
  - 保存前校验「结束日期不能早于开始日期」；
  - `createTask` 返回 error 时提示并**保留弹窗**（否则会继续删旧任务）；
  - 编辑成功后**无条件按 uid 删旧任务**（不再靠名字差异判断）；
  - 删除后按编辑前的运行态显式回设新任务状态（monitoring / paused），
    避免继承旧任务的 failed。
- 数据：清理用户库里的 3 条同名任务（1 条空日期 + 2 条重复）→ 保留
  `dates=['2026-10-19']` 一条，状态 failed → paused。备份在
  `/tmp/config_before_dedup.json`、`/tmp/state_before_dedup.json`。

**验证**：真实浏览器（mock RPC）7/7 —— 倒置日期被拦、createTask 恰好一次、
deleteTask 带 uid、状态被回设、提交日期为新选值；全量 809 tests OK；
`tools/check.py --quick` 5/5。

## 19. 2026-10-18 结束日期自动跟随开始日期

**需求**：改「开始日期」时，结束日期自动识别为同一天；原来设置的区间（结束在
开始之后）自动清除。

**实现**（`ui/app.js` 的 `openMonitorTaskForm`）：
- `#fD1` 的 `change` 事件里把 `#fD2` 的值直接设为开始日期，同时把 `#fD2` 的
  `min` 设为开始日期（日期控件里选不到更早的日期）。
- 打开弹窗时也先把 `min` 设成当前开始日期，但不覆盖已有值——任务本来就是
  区间（dates 展开多天）时仍显示首尾两天。

**效果**：单日任务两者一致；想设区间就手动改结束日期；再动开始日期则区间被
重置为单日，不会残留「起 > 止」的倒置区间（`api._split_date_input` 的自动交换
作为后端兜底仍然保留）。

**验证**：真实浏览器（真实 app.js + mock RPC）7/7 —— 改开始后结束跟随、
手设区间可行、再改开始后区间被清除、保存提交 `dates=[该日]` 且 `date_range=[]`；
编辑流程原有 7 项回归仍全绿；`tools/check.py --quick` 5/5。

## 20. 2026-10-18 下单成功后保留「席位已锁定」付款页

**需求**：网页下单成功后要停在 12306 的订单确认页（「席位已锁定，请在 xx 分
xx 秒内完成支付」），让人亲眼确认下单成功并直接支付。

**原因**：`_order_impl` 判定到 `payOrder` 后立即返回，`ExitStack` 的
`_safe_close_ctx` 随即关窗——用户只看到窗口一闪。

**改动**：
- `browser_order._hold_pay_page(page, seconds, should_stop)`：留在付款页，直到
  **用户关窗 / 手动停止 / 超时**；测试替身没有 `is_closed` 时直接返回，不会挂住单测。
- `_order_impl` 成功分支：`headless=False` 时先按配置保留付款页，再返回成功。
- `browser_order._pay_page_hold_seconds()`：读 config.json——
  `keep_pay_page`（false 关闭）、`pay_page_keep_seconds`（默认 300，上限 1200 = 座位锁定 20 分钟）。
- `config_keys.CONFIG_KEYS` 与 `config.example.json` 同步登记这两个键
  （`TestConfigKeys` 会做「代码读取键 ⊆ 模板键」的回归）。

**注意**：保留期间浏览器仍持有 profile 独占锁——这正是「浏览器正在用」的诚实
语义，其它下单/体检会排队；关窗或超时后自动释放。要恢复旧行为把
`keep_pay_page` 设为 false 即可。

**验证**：
- 真实 Playwright 页面：2 秒档实测 2.1s 返回；已关闭页面 0.0s 立即返回。
- 单测 `TestKeepPayPage` 4 项（配置解析 / 测试替身不阻塞 / 关窗即返 / 手动停止即返）。
- 全量 813 tests OK (skipped=15)。

## 21. 2026-10-18 「下单成功」口径收紧为出现「席位已锁定」界面

**需求**：只有网页真的弹出「席位已锁定，请在 xx 分 xx 秒内完成支付」的订单
界面，才算本次下单成功。

**旧口径的问题**：两条链路都只看 URL——
- 旧链 `_order_impl`：URL 含 `payOrder` 即判成功（页面空白/被踢也算成功）；
- 新链 `orderflow.classify_snapshot`：URL 含 payOrder + 正文命中任一泛化词
  （`待支付` / `待付款` / `支付剩余时间` / `席位已锁定` / `订单提交成功`），
  「待支付」这类词在旧单列表或错误页同样会出现。

**改动**：
- `browser_order._wait_pay_locked()`：pages 到 payOrder 后轮询正文（最长 10 秒），
  **必须出现「席位已锁定」**才判成功；期间可被「停止」打断；超时按 ambiguous
  交官方回读，绝不误报成功。
- `orderflow._PAY_KEYS` → 单一 `_PAY_LOCK_KEY = "席位已锁定"`：URL 到 payOrder
  但界面没锁定的一律继续观察（wait），不再用泛化词提前判成功。
- `orderflow._finish_success()` 也接入付款页保留（§20）：关窗 / 停止 / 超时才继续。

**说明**：若页面始终没出现锁定界面，但官方订单接口回读到本次新订单
（`_judge_official`），仍按成功上报——那是比页面更硬的证据。

**验证**：`TestOrderflowClassifySnapshot` 更新为「泛化词 → wait、锁定词 →
success」；新增 `test_pay_url_without_locked_page_is_ambiguous`（URL 到 payOrder
但正文为空 → ambiguous）；全量 813+ tests。

## 22. 2026-10-19 锁定座位页静置确认：不再「锁座成功后闪退重购」

**需求**：进入「席位已锁定」页后先稳定停留 1–2 秒确认，期间订单状态不变；
只有拿到明确的锁座成功/失败结果才继续，禁止跳回重新购票流程。

**实测根因**（用户录屏两段 + 全局日志 21:38:12–14）：
- 点确认后页面先显示「订单已经提交，系统正在处理中，请稍候。」，12306 要
  2–8 秒才渲染 payOrder 锁定页（视频 2：24s 起提示，34s 才出锁定页）。
- HOLD 首读只等 3 秒就官方回读；提交结果未落库 → ambiguous → 立刻
  `_reopen_and_order` 新开页再下一单（日志同一秒出现「新开页面重下已提交」）。
- 第二单被 12306 拒绝「出票失败/未完成订单」，页面判定 dup，流程收尾关页——
  用户看到的就是「锁座成功后立即闪退并重新购票」。

**改动**（orderflow.py）：
- `order_flow.lock_confirm_seconds`（默认 1.5s，钳制 1–2s）：进入 payOrder 后
  静置确认，期间不重下、不关页、不判 dup/fail；锁定词稳定 ≥ 确认窗才
  `_finish_success`（同时顺延该页 deadline，避免确认中被超时打断）。
- `first_round` 重下闸门：任一页仍显示过程提示（`_PENDING_KEYS`）或在
  payOrder 上时，官方回读 + 新开页重下整体顺延（保持 first_round=True），
  状态明确后才允许走老逻辑。
- 锁定页上的 dup 词（多为过程提示里的「未完成订单」链接）不再关页。
- 成功判定仍是「URL 到 payOrder + 正文出现席位已锁定」，不放松。

**验证**：新增 4 项回归（锁定页至少静置 5 次轮询才成功 / 处理中禁止重下 /
锁定页永不重下 / 锁定页瞬态 dup 噪声忽略）；orderflow 相关 21 项全绿；
全量 823 项 1 失败为既有未提交改动（engine 去 dedup 与
`test_engine_load_state_timeout_skips_not_quarantine` 断言冲突），与本改动无关。

## 23. 2026-10-19 编辑任务改日期后任务消失：按名删全部同名

**需求/现象**：老任务在编辑弹窗里改日期保存后，任务从列表里直接消失。

**根因**：编辑 = 前端「先 `createTask` 再 `deleteTask(旧 uid)`」（无原地编辑
接口）。任务名由「线路+车次+席别」生成、**不含日期**，改日期后新旧同名。
`api.remove_task_from_config` 旧实现按名字删掉**全部同名任务**，刚建的新任务
一起被删 → 列表归零。`legacy/gui.remove_task_from_config` 早已是按 uid / 只删
第一条的语义，api 这份是漂移出来的重复实现。

**改动**（api.py）：
- `remove_task_from_config`：有 uid 且命中 → 按 uid 精确删除；无 uid 的旧任务
  → 只删第一个同名条目（append 追加的新任务在末尾，第一条正好是旧任务）。
- 批量删除按名字仍可用：多次调用逐条删，不会误删新建任务。

**验证**：新增 3 项回归（按 uid 删同名兄弟保留新任务 / 无 uid 只删第一条 /
`rpc_delete_task` 真实链路保留新建任务）；`TestMonitorTaskEdit` 6 项全绿；
全量 827 项 1 失败仍为既有 engine 去 dedup 断言冲突，与本改动无关。

## 24. 2026-10-19 代码审查 P0 整改 + 去重精简

**触发**：全项目审查（并发/下单/校验/性能四线）后按 P0 顺序整改，同时合并
api 与 legacy/gui 重复的删任务逻辑。

**P0 修复**：
- `appcommon.update_state_locked`：读实现吞异常返回 `{}` 时复核原始 JSON，
  解析失败即拒写（旧行为会把空状态整份覆写，丢全部任务 status/retry）。
- `orderflow`：官方回读只有 `cls ∈ {none, cancelled}` 才允许新开页重下；
  `error/unknown/blocked` 一律放弃自动重下（旧逻辑会把已成功订单再提交一次）。
- `browser_order._run_fill_submit_confirm`：确认按钮始终未启用（clicked=False）
  改为返回 `not_submitted`，不再 `ok=True` 诱使 HOLD 空等后重下。
- `api.rpc_save_settings`：新增授权码按 `email` 自身判断，不再被旧配置判空删掉；
  未提交密码时沿用旧值。
- `api.rpc_save_passengers`：检查 `save_passengers` 返回值；`expect_stamp`
  缺省/None 归一为哨兵（旧行为把它当「文件必须不存在」，文件已存在时静默拒绝）。
- `api.start_engine`：新增 `_engine_start_lock`，判定+建线程+赋值原子化。
- `api.rpc_shutdown` + `electron/main.js`：退出先 RPC 收尾（停引擎落盘）再 kill，
  超时 5s 兜底；避免状态丢失与 Playwright 孤儿进程占 profile 锁。
- `orderflow`：warm 锁内失效时 `warm.close(release_locks=False)`，避免同
  profile 双上下文。
- `order.order_ticket` / `browser_order.order_ticket_via_browser` 透传
  `should_stop`，引擎 `_order_once` 传入 `_stop_event`（滑块/付款页驻留可中断）。

**精简**：`remove_task_from_config` 收敛到 appcommon 单源，api 与 legacy/gui
改为委托（删除两处 20+ 行重复实现）；密码/指纹判断合并为单次取值。

**验证**：新增 6 项回归（损坏 state 拒写 / 合法空对象放行 / 回读不可判定不重下 /
首次 SMTP 授权码保留 / 乘车人保存失败上报 / null stamp 按不检查）；全量
833 项 1 失败仍为既有 engine 去 dedup 断言冲突。`TestTask52SliderAmbiguous`
的「未点确认」用例已同步为新语义（not_submitted）。

## 25. 2026-10-19 代码审查 P1 批次：异常路径、输入校验与缓存

**修复**：
- orders.json 瞬时 `OSError`（占用/扫描）不再当坏档隔离——返回空库且
  `write_ok=False`，写方跳过覆写；只有 JSON 损坏才挪档。
- HOLD 首轮官方回读最短等待 3s → 8s（12306 落库/渲染 2–8s，提前判无单会重下）。
- 成功收尾先 `save_state(page.context)` 再回读订单号；回读失败记日志，不再静默。
- 归因窗口 `window_sec` 180s → 30s：2 分钟前的上一轮旧单不再被判本次成功；
  RULES.md 同步。
- `saveGrabTask`：必填 `id`，数值参数校验（`poll_seconds="abc"` 不再让抢票
  线程 `float()` 猝死）；`deleteGrabTask` 缺 id 直接拒绝（防清空无 id 老任务）。
- `createTask`：`seat_types`/`passengers` 字符串归一（不再逐字符拆成「二/等/座」），
  `pax_purpose` 非 dict 归空。
- `date_range` 列表形态补 ISO 校验（非 ISO 不再落盘后被引擎静默丢弃）。
- `setTaskStatus`：状态白名单 + `tasks` 非 dict 安全重建；grabber 轮询间隔
  `float()` 包 try。
- `engine._setup_logging` 摘除 handler 时 `close()`（文件句柄泄漏）。
- `probe_session` 缓存对 http 模式生效（前端与看门狗不再各打一轮 3×15s）。
- `ticket._read_json_cache` 按 `(mtime, size)` 记忆化（3300 站不再每 2s 重解析）。

**验证**：新增 10 项回归（瞬时占用不隔离 / 非 ISO 区间拒绝 / 字符串席别不拆字 /
抢票任务非法数值拒绝 / 缺 id 拒绝删除 / 非法状态拒绝 / 缓存命中与失效 /
上一轮旧单不归因）；全量 841 项 1 失败仍为既有 engine 去 dedup 断言冲突。

## 26. 2026-10-19 审查 P1 性能批：慢 RPC 分流、日志攒批、抢票页增量刷新

- `api.rpc_dispatch(line, threaded=False)`：main 的真管道传 `threaded=True`，
  `_SLOW_METHODS`（queryTickets / checkSession / relogin / sync12306Passengers /
  reconcileHistory / testEmail / ipLocate / getAccount）交给 daemon 短线程执行，
  `BoundedSemaphore(6)` 满则退化为同步（背压不丢请求）；测试/协议层默认同步，
  行为与返回顺序不变。
- `log_pump`：150ms 攒批推送——单条仍是 `{line,tag}`，多条用 `{lines:[...]}`；
  前端 `pushLogBatch` 用 DocumentFragment 一次插入、只滚一次，会话失效正则仍
  对整批文本生效。
- `refreshGrab`：新增 `grabSig`（除倒计时外的展示字段）；签名不变时只原地更新
  `.g-count`，不再每秒 `innerHTML` 重建整页。
- `logutil._trim_problem_log`：超过上限 1.2 倍才裁剪，避免贴着上限时每条日志
  都全量读+重写。
- 验证：新增「慢方法不阻塞分发线程」；全量 842 项 1 失败仍为既有 engine 去
  dedup 断言冲突；`node --check` 通过。

## 27. 2026-10-19 审查 P2 批：边界收口与测试基线转绿

- `grabber.append_monitor_task` 临时文件名改为 `<kind><pid>-<tid>`，纳入
  `appcommon.sweep_stale_tmp` 清理（旧名 `.launcher<tid>` 永远清不掉）。
- `electron/main.js`：`pyCall` 计时器存入 pending，响应/启动失败/进程退出
  统一 `clearTimeout`；`DSH_DEBUG` 对 savePassengers/passengerDetail/
  saveSettings 只打参数键名，不再输出证件号/手机号/授权码明文。
- `api.rpc_query_tickets`：站码必须 3 位大写字母、日期过 `fromisoformat`、
  purpose 限定 ADULT/0X00（非法回落 ADULT）。
- `api.rpc_list_history`：limit 强转保护 + [1,1000] 钳制（负数不再取最旧段）。
- `station_db.check`：stations/kinds 形状守卫，异常形状返回 1 而非抛栈。
- `appcommon.normalize_email_settings`：`to` 为字符串时按分隔符拆分，不再
  逐字符校验报「a、@、b…」。
- `grabber_utils.build_monitor_task`：seats/trains=None、字符串布尔、
  字符串乘车人/日期、非 dict pax_purpose、非法 priority 全部归一；
  `api._req_on` 同步字符串布尔语义（"false" 不再变 True）。
- 测试：新增 `TestP2InputGuards` 4 项 + query/list 校验 2 项；
  `test_engine_load_state_timeout_skips_not_quarantine` 对齐「去 dedup」
  （磁盘字节原样、内存丢弃 dedup）。全量 **847 OK (skipped=15)**，基线转绿。

## 28. 2026-10-19 状态合并外部写入优先 + 落盘节流放宽

- `engine._status_seen`：记录「上次见到的磁盘 status/message」基线。`_save_state`
  合并时，磁盘值与基线不同 ⇒ 外部进程（GUI/launcher）改过，status/message
  保留磁盘值；磁盘未变 ⇒ 内存（引擎最新调度结果）优先。修复「用户暂停被引擎
  内存旧状态覆盖回 monitoring」。
- `_apply_merged_status`：外部值写盘后同步回内存条目（就地改、保引用），
  否则下一轮 `_save_state` 又会拿旧内存覆盖回去。
- `_sync_state` 重载后重置基线；`_save_state` 成功后更新基线。
- `_STATE_FLUSH_INTERVAL` 2s → 5s：例行字段（last_poll/message）落盘频率减半，
  重要状态变更仍立即落盘。
- 测试：新增 3 项（外部暂停优先 / 磁盘未变时内存优先 / `_save_state` 端到端
  保留外部 paused）；原 `test_memory_wins_task_conflict` 按新语义改名并调整。
  全量 **850 OK (skipped=15)**。

## 29. 2026-10-19 收尾：去重单源、死代码与隐藏窗口轮询

- `appcommon.drop_dedup_state`：防重清理下沉为单点实现（dedup 节 + 早期平铺
  「|」键）。`engine._drop_dedup` 改为委托；`grabber.append_monitor_task` 的
  state 写路径改用它——旧代码只 pop `dedup` 节，早期平铺键会被原样写回。
- 删除 `api.save_config` 死函数（无调用点，且绕过指纹/坏档守卫）。
- `log_pump`：push 包 try——前端断开时日志泵不再静默死亡。
- `rpc_dispatch`：慢方法线程启动失败时归还信号量名额，避免名额泄漏。
- `ui/app.js`：窗口隐藏（迷你小窗/最小化）时跳过 1s/2s 定时轮询，
  `visibilitychange` 回到前台立即 `refreshAll()` 对齐数据。
- 测试：新增 `test_drop_dedup_state_clears_section_and_flat_keys`；
  全量 **851 OK (skipped=15)**。

## 31. 2026-10-19 网页有「预订」脚本却报没有

**现象**（截图）：12306 网页 K558 无座=有、显示「预订」；客户端 22:53:37
DETECT → FILL → 22:53:38 报「页面上没有 K558 的『预订』按钮」。

**根因（两处）**：
1. 同车次在 `#queryLeftTable` 里可能有多行（主行/展开行）。旧点击逻辑找到
   第一条 `<strong>K558</strong>` 就 break，那一行没有 `<a>预订` 便直接判失败；
   而查询命中判定 `_QUERY_HIT_JS` 用的是 `.some()`（任意一行有预订即可）→
   命中通过、点击失败，1 秒内出现自相矛盾的结果。
2. 失败文案没有页面取证，无法区分「无票/候补」「DOM 结构变了（a→button）」
   「页面根本没起来」。

**改动**：
- 点击改为遍历所有 K558 行，取第一条**真有**「预订」的 `<a>`；找不到再走
  词界兜底，与命中判定同口径。
- 失败返回页面取证：`url/title`、行数、同车次行数、首行正文片段。
- 冷启动 `_goto_and_query` 未命中不再硬跑 FILL：返回 `not_ready` /
  `user_stopped` / `browser_dead`，不再把「页面没起来」误报成「没按钮」。
- 测试：新增取证回归；全量 **853 OK (skipped=15)**。

**补充（同日 23:0x）**：用户日志仍显示旧文案，核对进程启动时间 22:53:34
早于本次改动 22:56:46——跑的是旧后端。另加两层硬化：
- 「预订」识别同时认 `<a>` 与 `<button>`（12306 前端灰度可能换标签）。
- 失败现场落盘 `logs/booking_fail_<车次>_<时间>.json/.png`：URL、标题、
  表格 id、前 40 行文本与链接文本、`#queryLeftTable` 的 HTML 片段（20KB）
  与截图；测试替身无 `screenshot` 属性时跳过，不污染 logs/。

## 32. 2026-10-19 回归修复：Event 被当成回调传

**现象**：23:09 恢复任务后立刻报
`浏览器下单异常: TypeError: 'Event' object is not callable`。

**根因**：上一批把 `should_stop` 贯穿到引擎下单链时，`engine._order_once`
传的是 `threading.Event` 对象本身；下游按回调调用 `should_stop()` → TypeError。

**改动**：
- `engine._order_once` 改传 `ev.is_set`（可调用）；
- `orderflow.run_orderflow` 入口做兼容归一：非 callable 时取 `.is_set`，
  防止其它调用方再犯；
- 新增 2 项回归（引擎传参必须是可调用 / run_orderflow 接受 Event）；
  全量 **855 OK (skipped=15)**。

## 33. 2026-10-20 安全整改：授权码加密、会话文件 ACL、耗时记录噪声

- `config.json` 的 `notify.email.password` 已用 DPAPI 加密为 `dpapi1:`
  （379 字符），`notify.secret_of` 往返校验通过；设置页仍显示 `******`，
  保存设置不会覆盖已加密值。全量 git 历史扫描该串 **0 命中**（从未提交）。
- ACL 收紧：`data/` 与 `.browser_profile/`（706 个文件）去掉继承，仅保留
  当前用户 / Administrators / SYSTEM；原有 `BUILTIN\Users:(RX)` 与
  `Authenticated Users:(M)` 已移除。目录 ACE 带 `(OI)(CI)`，后续原子写的
  新文件自动继承同一权限。
- `order_timing.jsonl`：新增 `browser_order.TIMING_PATH`；`TempDirCase`
  统一 patch 到临时文件，mock 下单不再污染真实记录。历史 1473 条中
  1287 条 G101 mock 已归档为 `.bak-20261020` 并清理，剩 186 条真实车次。
- 全量 **855 OK (skipped=15)**。
