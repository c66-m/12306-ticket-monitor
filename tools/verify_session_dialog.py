# -*- coding: utf-8 -*-
"""会话失效弹窗去重的一次性验证（真实 Chromium 加载真实 ui/app.js）。

覆盖两个触发来源（引擎日志 / 看门狗推送）与「稍后」记忆：
  1. 首次 sessionExpired → 弹
  2. 弹窗打开时重复推送 → 不叠加
  3. 点「稍后」→ 关闭
  4. 再推 sessionExpired → 不弹（核心诉求）
  5. 引擎日志再报失效 → 不弹
  6. 会话恢复（refreshSession ok）→ 复位标记
  7. 恢复后再次失效 → 重新弹
"""

import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(r"C:/develep/工作目录/111chanwu/12306")
URL = (ROOT / "ui" / "index.html").as_uri()

MOCK = """
window.api = {
  call: (method) => Promise.resolve(({
    listMonitorTasks: {tasks: [], engineRunning: false, engine_alive: false},
    listGrabTasks: {tasks: []},
    grabTicker: {running: false},
    listPassengers: {passengers: []},
    listHistory: {records: []},
    listPresets: {presets: []},
    getSettings: {},
    checkSession: {ok: true},
    getAccount: {name: "测试"},
    checkUpdate: {has_new: false}
  })[method] || {}),
  onPush: (cb) => { window.__push = cb; },
  win: () => Promise.resolve(),
  top: () => Promise.resolve(false),
  mini: () => Promise.resolve()
};
"""

VISIBLE = """() => {
  const ov = document.querySelector("#overlay");
  const modal = document.querySelector("#modal");
  return !!ov && !ov.classList.contains("hidden") && /登录已失效/.test(modal.textContent || "");
}"""

PUSH_EXPIRED = """() => window.__push({method: "sessionExpired",
                                     params: {message: "已跳转登录门户"}})"""
PUSH_DEAD_LOG = """() => window.__push({method: "log",
                                       params: {line: "[错误] 登录会话失效（已跳转登录门户）", tag: "err"}})"""


def main() -> int:
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(("  [PASS] " if cond else "  [FAIL] ") + name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.add_init_script(MOCK)
        page.goto(URL)
        page.wait_for_timeout(900)

        page.evaluate(PUSH_EXPIRED)
        page.wait_for_timeout(120)
        check("首次 sessionExpired 弹窗", page.evaluate(VISIBLE))

        page.evaluate(PUSH_EXPIRED)
        page.wait_for_timeout(120)
        check("弹窗打开时重复推送不叠加", page.evaluate(VISIBLE))

        page.click("[data-a=later]")
        page.wait_for_timeout(120)
        check("点「稍后」关闭弹窗", not page.evaluate(VISIBLE))

        page.evaluate(PUSH_EXPIRED)
        page.wait_for_timeout(120)
        check("点稍后后再推 sessionExpired 不弹", not page.evaluate(VISIBLE))

        page.evaluate(PUSH_DEAD_LOG)
        page.wait_for_timeout(120)
        check("点稍后后引擎日志再报也不弹", not page.evaluate(VISIBLE))

        page.evaluate("() => refreshSession()")
        page.wait_for_timeout(250)
        check("会话恢复后复位「已忽略」",
              page.evaluate("() => typeof S !== 'undefined' && !S._expiredDismissed"))

        page.evaluate(PUSH_EXPIRED)
        page.wait_for_timeout(120)
        check("恢复后再次失效重新提示", page.evaluate(VISIBLE))

        browser.close()

    failed = [n for n, ok in results if not ok]
    print("\n结果：%d/%d 通过" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
