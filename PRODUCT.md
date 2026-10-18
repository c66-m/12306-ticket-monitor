# Product

<!-- impeccable:product-schema 1 -->

## Platform

adaptive — Windows 桌面应用（Electron 壳 + Python 3.12 后端，JSON-RPC over stdin/stdout）。**2026-10-14 界面重构：Electron 单窗口 + 侧边栏 4 页 + 底部日志条**（`ui/` + `api.py`）；旧 Tkinter 版 `gui.py` 已停用但保留在仓库内（测试引用）。不跟随 web 或移动端设计系统。

## Stack

既有代码库已回答：Python 3.12 后端（进程内无 UI 依赖，JSON-RPC over stdio）+ Electron 壳；Playwright + 本机 Edge 负责下单链路。**Python 后端的 UI 依赖仍然禁止**（Pillow/theme.py/liquid.py 已随 2026-10-13 重构删除，不得回归）；前端 `ui/` 为手写 HTML/CSS/JS，不引入第三方 UI 框架。

## Users

个人抢票用户：在放票时刻、节假日出行高峰抢购热门车票，需要长时间常驻监控余票并在命中后自动下单；常同时监控多条路线、多个车次。使用场景包括开抢倒计时（秒级出手）、余票轮询等待、命中自动下单、邮件通知查收。界面为单个 Electron 窗口：「监控任务」「抢票任务」等分页并存，不再有「监控一套、开抢一套」的双开形态。

## Product Purpose

轮询 12306 官方余票接口（免登录轻客户端），命中「有票 ∩ 席别候选」后自动经真实浏览器提交订单（不支付，停在未支付状态）；支持定时开抢、预热、多任务并行与「车次=席别」专属规则。成功 = 用户拿到可支付的未支付订单。

## Positioning

「查询很轻、下单很真」的双段架构：轻客户端高频轮询不占浏览器资源，命中才拉起真实 Edge 下单；下单结果判定不回读页面文本，只信官方订单接口回读 + 时间戳归因（180 秒窗口），「重试被拦」可被反转判定为成功信号，保守分支永不误判成功。 下单编排 2026-10 重建为 orderflow.py 阶段化管线（检测→填单→确认→驻留读到「待付款」→判定→日志），旧链保留到实跑验证后退役。

## Operating Context

- Windows 桌面，常驻运行；全局日志区按事件标签着色（纯 Text tag）；命中后自动下单并发邮件通知。
- **单一界面进程**：Electron 壳（`electron/main.js`）+ `ui/` 前端 + `api.py` RPC 端点，一个窗口承载全部功能（监控任务 / 抢票任务 / 查票 / 乘车人 / 历史 / 设置）；`gui.py`（Tkinter 版）与 `launcher.py` 均只保留业务能力、无界面。共享 config/state/orders 文件（filelock 并发防护）。
- 抢票时刻高度紧张：倒计时到秒、放票窗口操作路径必须最短、误操作成本高。
- 上线方式：双击「启动桌面版.bat」（Electron 壳 → 拉起 `api.py` 后端）；命令行后备 monitor.py 功能以桌面版为准。

## Capabilities and Constraints

功能（来自 README 模块地图与源码）：
- 监控桌面版 `ui/` + `api.py`：常驻轮询监控、四步向导建任务（查询车票→选车次席别→选乘车人→确认）、QuickMonitorDialog 直接监视、历史路线快捷查询、车次卡片席别点选、命中自动下单 + 邮件通知。
- 抢票启动器 launcher.py：倒计时到秒自动出手、多任务独立窗口并行、行程预设、IP 定位、按事件分级着色的日志区、预热（warm_minutes）。
- 命令行后备 monitor.py。

硬约束（改 UI 不得破坏）：
- 不绕过验证码（滑块出现即中止交人工）、不执行支付（提交后停在未支付）。
- 席别候选必须与该车实际余票求交；「车次=席别」规则语法（ticket.py 唯一口径）。
- 下单结果判定只信 order.py 官方回读 + 时间戳归因；防重复下单三层。
- 并发文件写走 appcommon 原子写 + filelock；config_keys.py 键唯一定义。
- Python 后端进程内不得有 UI 依赖（Pillow/theme.py/liquid.py 已删除，不得回归）；界面是 `ui/` 手写 HTML/CSS/JS，不引入第三方前端框架。

## Brand Commitments

无既有品牌资产与对外承诺。**视觉方向以 2026-10-14 落地的 Electron 界面为准：液态玻璃（glassmorphism）**——半透明玻璃层 + `backdrop-filter` 模糊 + 大圆角，衬在 `wallpaper.jpg` 照片壁纸上；浅色 / 深色双主题。
- 外观参数单点在 `ui/style.css` 的 `:root` 变量（`--ink*` 前景、`--glass-rgb/--glass-a` 玻璃基质、`--accent` 品牌蓝、`--ok/--warn/--err` 语义色、`--blur/--radius/--fs/--gap` 外观参数），由设置页实时注入根元素；不得在别处硬编码视觉常量。
- 主题三态 light / dark / 跟随系统；密度 compact / roomy；动效可整体关闭（`data-motion="off"` 一键停全部 animation/transition）；壁纸默认照片，可切 flat 渐变 / 自定义 / 无。
- 玻璃只做承载层：车次、时间、余票、状态等抢票时刻的关键信息一律实心高对比呈现，半透明层不得压过它。
- 动效克制（命中脉冲、下拉展开、toast 入场），一律短时长；抢票时刻的关键动作不得被动画或过渡拖慢。
- 无障碍：`--fs` 可调字号，关闭动效即刻生效；语义色不单独承担含义（必配文字标签，命中/异常可分辨）。
- 仍禁止：前端不引入第三方 UI 框架（`ui/` 手写 HTML/CSS/JS）；Python 后端不得回归 UI 依赖（Pillow/theme.py/liquid.py）。
- 新增 UI 一律加到 `ui/` 里；gui.py 与 launcher.py 不得重新长回界面代码。

## Evidence on Hand

- 源码：`ui/app.js` + `ui/index.html` + `ui/style.css`（当前界面）、`api.py`（RPC 端点，1222 行）、`electron/main.js`（壳）、gui.py（4296 行，**已停用的 Tkinter 界面**，仅因 tests 深度引用而保留）、launcher.py（1164 行，纯业务）、monitor.py、ticket.py、engine.py、order.py、browser_order.py、appcommon.py 等。
- 文档：README.md（模块地图/机制/合规）、RULES.md（实测结论）、HANDOFF.md（0.9 节=本次重构记录）。
- 截图：无留档（旧版视觉截图已随 2026-10-15 根目录清理移除；需要时用 CDP `Page.captureScreenshot` 现拍当前界面）。
- 无品牌资产、无对外商用素材；不得虚构用户评价、价格、市场数据。

## Product Principles

1. 真相优先：界面从不承诺未确认的状态——下单结果只信官方接口回读，UI 如实呈现不确定。
2. 监控是主体：常驻、低打扰、命中即响应；界面服务于长时间注视与等待，减少视觉疲劳。
3. 出手时刻零摩擦：倒计时/放票窗口操作路径最短，关键动作（建任务、开抢、确认）一眼可达。
4. 透明可审计：任务状态、席别候选、订单归属、日志分级全部可追溯，用户知道系统在做什么。
5. 合规边界不可僭越：不绕验证码、不支付；UI 不诱导规避，反而清晰标注状态（如「待支付」）。

## Accessibility & Inclusion

未建立产品级特殊需求（无既存标准要求）。抢票场景用户长时间注视 + 紧张时刻：注意对比度、信息密度、字号与状态可分辨性（如色盲安全的命中/异常状态区分），科幻装饰不得降低文本可读性。