# 前端行为检查脚本（R3 批次落盘）

Electron 行为断言脚本，非自动化测试框架的一部分，改动 ui/ 或 electron/ 后手动复跑。

- `r3_run_a_fake_backend.js`：脚本化 ipcMain 假后端 + 生产 preload，驱动真实
  `ui/index.html` / `ui/mini.html`，断言 R1/R3 的前端行为（quiet 轮询、失效弹窗
  不劫持表单、待核对统计与过滤、改名搜索、preload 暴露面、批量删除汇总等）。
- `r3_run_b_main_timeouts.js`：生产 `electron/main.js` + 假 python（`fakepy.js`），
  断言 pyCall 60s/120s 超时与迟到响应丢弃、stderr 行缓冲不乱码。
- `r3_run_b3_backend_crash.js`：假后端崩溃（`fakepy_exit.js`）时 backendDead
  只广播一次、在途 RPC 只 reject 一次。
- `r3_run_b2_spawn_fail.js`：PYTHON 指向不存在文件时启动失败只广播一次。

跑法（在本机有 Electron 的环境）：把脚本里的路径改到本仓 `electron/` 与
`ui/`，用 `ELECTRON_DISABLE_SANDBOX=1 xvfb-run electron <脚本>` 运行，
断言结果打印在 stdout。R3 批次实测：Run A 18/18、Run B 7/7、B3 2/2、B2 2/2。
