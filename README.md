# 12306 车票监控与自动抢票系统

桌面版监控 + 抢票启动器：轮询 12306 官方余票接口，命中后自动提交订单（**不支付**），支持定时开抢、预热、多任务并行与「车次=席别」席别规则。

## 结构思路

整个系统的架构由三个实测事实决定：

**一、查询可以很轻，下单必须很真。**
余票查询是免登录的纯 HTTP 接口（仅需先 GET `leftTicket/init` 拿 JSESSIONID），高频轮询毫无压力；而下单请求携带两类纯 HTTP 客户端生成不了的东西——网易易盾注入的设备指纹 `window.json_ua`、阿里云滑动验证凭证（`sessionId/sig`），服务端会把缺失这些的请求伪装成「余票不足 / 系统繁忙」直接拒绝。于是链路被切成两段：**`ticket.py` 用轻客户端高频轮询，命中后 `browser_order.py` 才拉起真实 Edge 完成下单**——贵重的浏览器资源只在真正要出手时占用。

**二、页面信号不可信，订单真相只在官方接口。**
提交订单后充满模糊状态：跳转丢失、确认页超时、会话被登出、页面显示「已有人支付」——这些都可能发生在订单**实际已创建**之后。因此下单结果判定不回读页面文本，只回读官方订单接口（`order.py`）：未支付列表 + 未出行列表 + 历史列表三处合查，并用订单的下单时刻 `order_date` 与本次提交时刻做**时间戳归因**——只有落在本次提交窗口（默认 180 秒）内的订单才算「本次生成」，防止把账号里的旧单误当新单。

**三、「重试被拦」是成功信号，不是失败信号。**
提交结果未知时，重试若被 12306 判「已有该订单」，说明第一把其实成功了。归因逻辑把这个信号反转：官方回读 unpaid 且下单时间对得上 → 判定本次提交成功并入账；官方确认无订单 → 才允许安全重试（有限次）；行程冲突 → 永不判成功（保守停任务交人工）。既避免重复下单，又不丢单。

### 数据流

```
ticket.py（免登录轻客户端轮询余票）
   │  命中「有票 ∩ 席别候选」
   ▼
engine.py / launcher.py（设防重记录 → 发起下单）
   │
   ▼
browser_order.py（真实 Edge：提交订单 → 排队 → 停在未支付）
   │  结果已知 / 未知（ambiguous）
   ▼
order.py（官方接口回读 + 时间戳归因，唯一事实来源）
   │
   ▼
state.json / orders.json / 购票历史 / 邮件通知
```

## 模块地图

### 入口层（2026-10-14 Electron 架构：ui + api.py + electron）

| 模块 | 规模 | 职责 |
|---|---|---|
| `ui/index.html` + `ui/app.js` + `ui/style.css` | 主窗 87 / 1671 / 960 行 | **唯一界面（纯 HTML/CSS/JS，无框架）**：顶栏（会话胶囊 / 监控开关 / 窗口键：置顶 · 小窗 · 最小化 · 最大化 · 关闭）+ 侧边栏 = 监控任务 / 抢票任务 / 乘车人 / 历史 / 设置 + 主区 + 底部全局日志。渲染、轮询、外观设置全在 app.js |
| `ui/mini.html` + `ui/mini.js` + `ui/mini.css` | 小窗 3 文件 | **抢票小窗（B7 第二步）**：460×420 无边框，只显示账号 / 进行中任务 / 抢票日志；打开时主窗隐藏，关闭自动还原。外观经 `--appear` 启动参数继承主窗 |
| `electron/main.js` + `electron/preload.js` | 221 / 27 行 | **Electron 壳**：spawn `python api.py`、JSON-RPC 桥（60 s 超时）、无边框窗口 1440×900、窗口通道 win/win-top、F11、`DSH_DEBUG` 日志开关；preload 暴露 `window.api = { call, onPush, win, top }` |
| `api.py` | 1259 行 | **JSON-RPC 后端（界面后端 = 唯一入口）**：stdin/stdout 逐行 JSON-RPC 2.0，METHODS 38 项（api.py:1174-1211）；引擎启停、日志泵、抢票定时调度、config/state 原子写 |
| `launcher.py` | 1165 行 | **抢票业务模块（无界面）**：Grabber 抢票线程、任务库（grab_tasks.json）、配置读写、车站搜索/类型探测、与监控系统联动（append_monitor_task / merge_trains_from_monitor）、更新检查 |
| `gui.py` | 4317 行 | **legacy（已停用）**：旧 Tkinter 单窗口界面，不再是入口；仅因 `tests/test_all.py` 深度引用（288 处）原地保留。新功能一律改 `ui/` |
| `monitor.py` | 1127 行 | **命令行后备入口**：交互式菜单（任务/乘车人/历史/启动监控），适合无 GUI 环境，功能以桌面版为准 |

### 核心链路

| 模块 | 规模 | 职责 |
|---|---|---|
| `ticket.py` | 823 行 | **余票查询与解析**（免登录）：车站码表、queryG 竖线字段解析、p20~p33 逐席别判余票；席别命名（卧铺共享字段、p35 码串）、「车次=席别」规则解析与候选求交 |
| `browser_order.py` | 1708 行 | **浏览器下单**（Playwright）：`#submitOrder_id` → checkOrderInfo → getQueueCount → 核对窗 → `#qr_submit_id` 倒计时 → confirmSingleForQueue → 停在支付页；席别/票种走官方事件链强写内部状态并回读校验；滑块一出现即中止 |
| `order.py` | 1042 行 | **订单事实来源**：官方订单接口回读（未支付 / 未出行 / 历史三列表）、状态分类（paid/unpaid/cancelled/none/blocked/unknown/error）、时间戳归因（`classify_with_time`、`find_recent_order`）、纯 HTTP 下单后备链路 |
| `engine.py` | 1621 行 | **监控引擎**：多任务调度与优先级、自适应轮询频率、防重复下单三层、断点续连、下单决策（含 dup/ambiguous 的官方回读归因分支） |

### 基础层

| 模块 | 规模 | 职责 |
|---|---|---|
| `appcommon.py` | 533 行 | 跨界面共享机械层：日期区间解析、原子文件写（临时名 + replace 重试）、state.json 读容错、PII 脱敏口径（`mask_order_no` / `mask_one_name` / `mask_names` / `mask_id_partial`）、北京时间（`beijing_tz`）、展示归一化（normalize/display 车次与席别）单一实现 |
| `filelock.py` | 144 行 | 跨进程文件锁（msvcrt 字节锁）：launcher 与 gui/engine 是独立进程却共改 config/state.json，原子替换挡不住并发读改写 |
| `logutil.py` | 132 行 | 按天滚动日志 handler，保持 `logs/<prefix>_YYYYMMDD.log` 命名习惯（标准库 handler 会改变命名，故自实现） |
| `config_keys.py` | 22 行 | 配置键唯一定义处：代码读取的键 ⊆ example 模板键（测试防漂移回归） |
| `tools/gen_icon.py`、`tools/gen_icon_emoji.py` | — | 图标生成脚本（产出 ui/appicon.png、electron/icon.ico） |
| `notify.py` | 147 行 | 邮件通知：订单提交成功后发送购票详情（SMTP SSL） |
| `passengers.py` | 496 行 | 乘车人管理：DPAPI（Windows）/ Fernet / 明文三级加密，按环境自动选最安全可用者 |
| `station_db.py` | 152 行 | 全量车站本地库（3404 站）：在库=运营中；车型标签来自实际查询学习。`rebuild` 从车站索引+车型表合成，`check` 校验三份同源车站文件的 name↔code 漂移（非零退出） |
| `capture_session.py` | 305 行 | 会话抓取：Playwright 打开 Edge 人工登录一次，Cookie 持久化到 `session_cookies.json` |
| `probe_login.py` | 520 行 | 登录链路诊断：域名连通性、二维码申请/轮询、会话有效性验证（只诊断，不绕过） |

## 功能入口

| 入口 | 命令 | 定位 |
|---|---|---|
| 桌面版（唯一界面，Electron） | 双击「启动桌面版.bat」（用绝对路径启动 `electron\node_modules\electron\dist\electron.exe`） | 「监控任务」页常驻轮询；「抢票任务」页定时开抢/预热/多任务并行；顶栏搜索框免登录速查 |
| 调试（带日志） | 同 bat，先 `set DSH_DEBUG=1` | 打开主进程 `[rpc]`、渲染进程 `[renderer]` 转发 |
| 命令行后备 | `python monitor.py` | 无 GUI 环境的后备入口（功能以桌面版为准） |
| 登录 | `python capture_session.py` / `python browser_order.py login` | 浏览器登录一次，会话持久化 |

> 2026-10-13 起 `launcher.py` 不再是独立入口（只保留业务能力）。
> 2026-10-14 起界面为 `ui/` + `api.py`（Electron）；`gui.py`（Tkinter 版）已停用但保留在仓库内（测试引用）。
> 「启动桌面版.bat」里的 `set ELECTRON_RUN_AS_NODE=` 与 `set ELECTRON_MIRROR=` 两行不要删（原因见 bat 内注释）。
> bat 文件必须保持**纯 ASCII**：cmd 按 OEM 代码页（本机 936/GBK）解码批处理，UTF-8 中文注释会错位成命令片段。
> 依赖装在 `electron/node_modules`，首次安装由 bat 自己在 electron 目录里执行（根目录没有 package.json）。

## 部署三步

1. 安装依赖：`pip install -r requirements.txt`，然后 `playwright install msedge`（或使用本机已装 Edge/Chrome）。
2. 登录：运行 `python browser_order.py login`，在弹出的浏览器里完成 12306 登录（扫码或账密+滑块），会话存入 `.browser_profile/`。
3. 建任务：桌面版「监控任务」页新建任务，或在「抢票任务」页配置车次/席别/乘车人，勾选自动下单。

## 「首选席别」语法

- 逗号分隔，先填的先抢：`硬座, 硬卧`
- 指定车次专属规则：`K225=硬座/无座, K1969=硬卧`（只作用该趟车，与勾选席别取交集，交集空=该车跳过）
- 无座=硬座（普速）/二等座（动车组）同价改判；认不出的词按硬座处理

## 关键机制速览

- **防重复下单三层**：本地 state.json 记录组合 → 下单前官方接口查重 → 成功后按 `stop_after_order` 停任务。
- **归因窗口**：订单 `order_date` ≥ 本次发起时刻 − 180 秒才算「本次生成」；解析失败走保守分支。
- **queryMyOrder 三列表**（2026-10 实测）：未支付=独立接口 `queryMyOrderNoComplete`；未出行（G）窗口按**下单日期**过滤；历史（H）按**乘车日期**过滤且 EndDate ≤ 昨天（含今天会返回空）。
- **席别同价改判**：网页端不卖无座，下单时按同价席别改判（普速→硬座、动车组→二等座），记账与通知仍按「无座」。
- **验证码**：滑块出现即中止并提示人工完成，不绕过。

## 合规声明

- 余票数据来自 12306 官方公开查询接口；下单走官网同款页面流程。
- **不绕过任何验证码**（滑块出现时保留浏览器等人工完成）；**不执行支付**（订单提交后 45 分钟内请自行到 App/网页支付）。
- 请遵守 12306 使用条款，控制查询频率，勿用于倒卖车票。

## 数据安全

- 乘车人证件号：Windows DPAPI 加密存储；SMTP 授权码：DPAPI 加密（`dpapi1:` 前缀）。
- 登录 Cookie（`.browser_profile/`、`session_cookies.json`）、配置与历史均为本地文件且已在 `.gitignore` 排除，请勿上传或分享。
