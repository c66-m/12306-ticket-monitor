# 12306 车票监控与自动购票系统 —— 交接摘要

## 0. 一句话定位
Python 后端 + Electron 桌面壳：查票/监控余票 + 自动抢票（Playwright 驱动真实浏览器下单）。
**2026-10-14 起为 Electron 架构：界面在 `ui/`（HTML/CSS/JS），`api.py` 是 JSON-RPC 后端，
`electron/` 是壳；`gui.py`（Tkinter 版）已降级为 legacy，仍在仓库内但不再是入口。**

**先读第 0.95 节（当前架构），第 0.9 节是已停用的 Tkinter 批次，留作历史。**

## 0.95 当前架构：Electron 桌面壳 + JSON-RPC 后端（2026-10-14 起，最新批次）

**旧 Tkinter 界面（gui.py）已停用**：文件仍在仓库内（4323 行，2026-10-16 实测）供 test_all.py 引用，但不再是入口、
不再维护。「启动桌面版.bat」现在启动 Electron。

### 分层

```
ui/            界面（纯 HTML/CSS/JS，无框架）
  index.html   87 行：顶栏 / 侧边栏 4 页 / 主区 / 日志条
  app.js       1671 行：全部页面渲染与交互（S 全局状态 + renderPage + refresh* 轮询）
  style.css    960 行（含玻璃拟态/主题变量/外观设置）
electron/      壳（Node）
  main.js      221 行：spawn python api.py + JSON-RPC 桥 + 窗口（无边框 1440×900）+ win/win-top 通道
  preload.js   27 行：contextBridge 暴露 window.api = { call, onPush, win, top }
api.py         1262 行：JSON-RPC 后端（stdin/stdout 逐行 JSON-RPC 2.0，METHODS 38 项）
  ↓ 复用既有业务模块
launcher.py(Grabber/任务库) engine.py(监控引擎) ticket.py(查票) order.py(browser_order.py)/...
```

通信：`window.api.call(method, params)` → preload → `ipcMain.handle("rpc")` → 写 python stdin，
逐行读 stdout 解析 → 回包。后端主动推送走 `push(tag, payload)` → `py-push` 事件 → 前端 `bridge.onPush`。
后端线程共三个：`log_pump`（把 `launcher.LOGQ` 推给前端）、`_armed_loop`（每 2 s 扫抢票任务，
到点自动开抢，`_armed_fired` 防重）、按需启动的监控引擎线程。

### 前端轮询节奏

monitor 2 s / grab 1 s / history 30 s / session 60 s（`ui/app.js` 尾部 setInterval）。

### 引擎启停（曾是真 bug，已修）

`api.py` 全局 `_engine_stop` 保存当前引擎线程的 `threading.Event`；`stop_engine()`：
`set()` → `join(timeout=10)` → 返回 `not alive`（前端据此提示「已停止 / 仍在收尾」）。
`engine.MonitorEngine.run(stop_event=None)` 内的检查点在循环头、due 循环、`stop_event.wait(1)` 三处。

### 乘车人脱敏（曾是真 bug，已修）

`listPassengers` 返回掩码（`launcher_utils.mask_id` / `mask_mobile`），编辑必须走
`passengerDetail` 取明文；`savePassengers` 有掩码还原守卫（值含 `*` 时回填磁盘旧明文），
防止把掩码写回磁盘。

### api.py 本轮新增的 RPC

`resetTask`（清防重记录并恢复监控）、`renameGrabTask`、`deletePreset`、`pushQueryHistory`、
`ipLocate`、`checkUpdate`、`passengerDetail`。完整 38 项见 `api.py` 的 METHODS 表（api.py:1177-1214）。协议分派已抽成 `rpc_dispatch(line)`（api.py:1217），可脱离真管道被测试。

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

### tests/TEST_REPORT.md（陈旧，仅存档）
- 5.3KB，抬头写「日期:2026-10-06 · 基线:a8b4b57 · 测试套件(47 项)」，内容为当时的二次自我检查报告（append_monitor_task 并发丢数据、mtime 15.6ms 盲区、误导性日志等**已修项**）。
- 当前基线是 **711 项**，该报告早于 Electron 迁移与三轮重构，**不要拿它的数字当现状**；保留只为追溯那几个并发 bug 的成因分析。

## 1. 位置与仓库现状（2026-10-16 复核）
- 当前家：C:\develep\产物\12306（本项目唯一工作副本）。
- 旧位置 C:\develep\deepseek harness\工作目录\12306-probe 已删除（robocopy 复制 → 校验 MISSING=0 → 验证通过 → 删源）。
- 嵌套评审 worktree p1-review：**2026-10-15 已用 `git worktree remove` 移除**（内容早已并入 main），`git worktree list` 现只剩主树 190a919 [main]。同理 `_undo_backup/`、`_ann/`、根 `node_modules/` 均已删除。
- remote：https://github.com/c66-m/12306-ticket-monitor.git
- 分支/HEAD：main = **190a919**（chore(web-dashboard): 预览图入库，.gitignore 开白名单），**与 origin/main 完全同步（ahead 0）**。
  上一轮记的「ahead 8 未推送」已过时：那 8 个 commit 连同 190a919 均已推送。
  已用 `git ls-remote origin refs/heads/main` 校验远端真实值为 `190a919`（不是只信本地记录）。
- 工作树：共 **60 个跟踪文件**。历史在途改动**已全部提交**，包括：Electron 三件套（api.py / ui/ / electron/）、
  5 个过期 md 与 .scratch/（按用户裁决删除后一并提交）、launcher_widgets.py（已删除）、
  `web-dashboard/preview-*.png`（190a919 入库，`.gitignore` 已开白名单 `!web-dashboard/preview-*.png`）。
  新目录 web-dashboard/、runs/、tools/clean_profile.py 均已入库（见 §0.97）。
  __pycache__/ 按 .gitignore 忽略，本地存在但不入库。
  **当前唯一未提交项**：本文件的 `M HANDOFF.md`（即本轮校正，等用户决定是否提交）。
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
| api.py | 1262 | **JSON-RPC 后端**：stdin/stdout 逐行 JSON-RPC 2.0，METHODS 38 项；引擎启停（_engine_stop）、日志泵 log_pump、抢票定时调度 _armed_loop、config/state 原子写与文件锁 |
| launcher.py | 1168 | **抢票业务模块（无界面）**：Grabber、任务库/配置 IO、车站搜索/类型探测、append_monitor_task、merge_trains_from_monitor、check_update、ensure_passengers、resolve_dates |
| launcher_utils.py | 290 | 纯业务工具（席别表/掩码 mask_id·mask_mobile/归一化/时间/车型/IP 定位 ip_locate/build_monitor_task）；无 tkinter 依赖 |
| engine.py | 1621 | 监控引擎：多任务调度、自适应频率、防重三层、历史/重试冷却；run(stop_event=None) 支持优雅退出 |
| browser_order.py | 1708 | Playwright 真浏览器下单全流程（_order_impl 分 8 段 mark()） |
| ticket.py | 823 | 查票 + parse_row 竖线行解析 + 席别/偏好规则 |
| order.py | 1042 | 订单查询/归因/session 载入与校验（唯一事实来源） |
| monitor.py | 1133 | 命令行后备入口（无 GUI 环境；功能以桌面版为准） |
| passengers.py | 496 | 乘车人（_passengers_key 为 Fernet 密钥文件，非 Windows 落明文 0o600） |
| capture_session.py | 305 | 采集登录态 cookie（Playwright 打开浏览器人工登录一次） |
| probe_login.py | 520 | 登录链路诊断（域名连通性/二维码/会话有效性；只诊断不绕过） |
| appcommon.py | 533 | 跨模块机械层：日期区间解析、原子文件写、state.json 读容错、PII 脱敏口径（mask_order_no / mask_one_name / mask_names / mask_id_partial）、北京时间（beijing_tz）、展示归一化（normalize/display 车次与席别） |
| filelock.py | 144 | 跨进程文件锁（msvcrt 字节锁） |
| logutil.py | 132 | 按天滚动日志 handler（保持 logs/<prefix>_YYYYMMDD.log 命名） |
| notify.py | 147 | 邮件通知（SMTP SSL） |
| station_db.py | 152 | 全量车站本地库（3404 站）；`rebuild` 合成、`check` 校验三份同源车站文件 name↔code 漂移（非零退出） |
| config_keys.py | 22 | 配置键唯一定义处（测试防漂移） |
| gui.py | 4323 | **legacy（已停用）**：旧 Tkinter 单窗口界面；不再是任何入口，仅因 tests/test_all.py 深度引用（288 处）而原地保留；新功能一律改 ui/ |
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
- 全量测试：python -X utf8 tests\test_all.py → 现状 **Ran 711, OK (skipped=15)**（2026-10-16 复核，零失败）。
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
- cookie 文件(.browser_state.json 4721B / session_cookies.json 1593B)搬迁前后逐字节一致 → 自然过期，不是搬家造成。抢票前需重新登录一次。
- 车站文件一致性（2026-10-15 实测）：`python station_db.py check` → 漂移 0 处；stations_db.json 已 rebuild（3404 站，待识别 3313），三份同源文件时间戳漂移消除。
- 免登录链路正常（本次实测）：ticket.load_station_map() 3404 站；ticket.query_tickets('SHH','GZQ','2026-10-16') → 26 车次；parse_row → G1305 上海南→广州新塘，seat_names_all → ['商务座','一等座','二等座','无座']；train_seat_kind('G1305')=G、train_seat_kind('K225')=普速。
- verify_session(session) 语义：返回 (ok, who, permanent)；非 JSON 响应重试 3 次，避免把 12306 临时繁忙误判为登录失效。

## 10. 未决 / 待办（没有授权就别动）
- ~~main ahead 8：是否 git push~~ **已完成**：HEAD=190a919 已推送，与 origin/main 同步（见第 1 节）。剩余：HANDOFF.md 本轮校正是否提交。
- ~~5 个 Windows 平台失败测试是否补 skipif 守卫~~ 已全部修复转绿（711 OK）。
- 一等卧/二等卧/高级动卧（官方码 I/J/A）是否进下单码表（长期未决）。
- 重新登录后做一次真实下单核对（核对窗席别 vs 订单详情，需登录实跑）。
- **config/state 家族跨文件重复尚未收敛**（用户 2026-10-16 裁决「消掉」）：load_config / save_config / update_config_locked / update_state_locked / normalize_email_settings / read_history_records 在 api.py、gui.py、monitor.py 各有一份。实测三份不是机械重复而是**能力分层**（monitor 有 FileNotFoundError 分支 + `_DAMAGED_STAMP` 指纹拒写 + expect_stamp；gui 有 messagebox 弹窗与 `guisave` 临时名；api 最简但有 expect_stamp），收敛必须**参数化保留策略**（RULES.md §七 第 2 条是硬约束），落点是 appcommon.py（已具 state.json 共享 plumbing 分节，含 read_state_or_none / `_read_history_list`）。逐字等价可无痛下沉的只有 `load_state` 与 `normalize_email_settings`（后者须统一选 gui 的**严格** EMAIL_RE，api 现为宽松版）。
- 两条驱动循环（launcher Grabber 一次性 vs engine MonitorEngine 常驻）是否重构——语义不同，暂缓到真实抢票跑通之后。
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
- 当前界面是 ui/ + api.py（Electron）。**别再去改 gui.py 加功能**（legacy，只为测试而存在）。
- ticket.py parse_row 的位置魔数索引不要动——RULES.md 与全部席别推导都依赖它。
- 两条驱动循环语义不同，别合并；RULES.md 的工作流定义别绕过。
- 原子写/锁的现有差异是修过的并发 bug（filelock 路径、bad 文件重命名、mtime 二次探测、engine 的 deepcopy 直写回退），抽取公共层时必须参数化保留，不许拉平。
- 日期区间解析现有口径：max_span_days=5（最多 6 个日期），多处入口已统一，别改口径。
- 项目文档：RULES.md（业务规则）、README.md、PRODUCT.md、HANDOFF.md（本文件）。拆 launcher.py 的清单要求见 RULES.md §七。

## 12. 下一次开工第一步建议
1. cd C:\develep\产物\12306（**测试必须在项目目录下跑**：tests 用相对路径读产品文件，换目录会 FileNotFoundError 假失败）
2. git status -sb 确认 HEAD 仍是 190a919（与 origin/main 同步，见第 1 节）；预期只有本文件可能是脏的
3. python -X utf8 -m compileall -q .
4. python -X utf8 tests\test_all.py（预期 711 OK / skipped=15）
5. 改界面/后端就去 ui/ 与 api.py（**别再去 gui.py 加功能**，它是 legacy）；改完按第 7 节做一次 CDP 界面冒烟
6. 需要下线实跑就先重新登录，再动代码。
