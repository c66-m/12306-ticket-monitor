# 项目全面检查与测试报告

日期:2026-10-06  ·  基线:a8b4b57(含并行会话的在途改动)  ·  测试套件:`tests/test_all.py`(47 项,标准库 unittest,`python tests/test_all.py` 可复跑)

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
- `notify → passengers` 的依赖仅为 SMTP 授权码 DPAPI 加解密,方向正确。
- `_demo_pri.py`、`_pri_test.py` 为本地草稿(已被 .gitignore 的 `_*` 规则排除),建议确认无用后删除。

## 四、配置文件审计

- 全部 10 个 JSON(config/config.example/launcher_config/launcher_config.example/grab_tasks/state/order_history/station_kind/station_name/station_index)解析通过;真实 passengers.json 为 DPAPI 密文且完整。
- **[P2] 仓库缺 README.md / requirements.txt / LICENSE**(git ls-files 证实)。依赖实际为 `requests`(必需)、`playwright`(浏览器下单)、`cryptography`(非 Windows 加密兜底)——不写 requirements.txt,新机器部署全靠猜。
- **[P3] config.example.json 未文档化两个代码会读的键**:`order_mode`(http/browser)与 `browser_headless`。模板补上即可。
- **[P3] SMTP 授权码迁移未完成**:notify.py 已支持 DPAPI 加密(`dpapi1:` 前缀),但现网 config.json 里的授权码仍是明文——在通知设置界面点一次"保存"即自动加密。旧明文在迁移前照常工作(向后兼容已测)。

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
