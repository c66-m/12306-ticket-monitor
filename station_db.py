# -*- coding: utf-8 -*-
"""全量车站本地库：合并官方车站索引与车型学习缓存，落为 stations_db.json。

12306 客售系统只收录运营中(可购票)车站——在库即运营中，停运/关闭车站
官方列表里不存在，因此「是否运营」以在库为准。车型（高铁/动车/普速）来自
station_kind.json 的实际查询学习，未学习过的标「待识别」，随使用自动补全。

用法：
    python station_db.py rebuild           # 重建/更新 stations_db.json
    python station_db.py rebuild --force   # 空索引时仍强制落盘（清空旧库，慎用）
    python station_db.py query 长葛     # 命令行速查
    python station_db.py check            # 校验四份车站文件一致性（漂移非零退出）
"""

import appcommon
import json
import os
import sys
import time

appcommon.ensure_utf8_stdio()

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = appcommon.data_path("stations_db.json")


def rebuild(force=False):
    import grabber  # 复用 get_station_index / station_kind / search_stations
    idx = grabber.get_station_index()
    if not idx and not force:
        # Task 82 (P3)：车站索引为空（离线/无缓存降级）时拒绝落盘——不得以
        # count=0 静默覆盖已有 stations_db.json（种子库曾被实际清零）。
        # 需要清空重建的显式场景，请用 `python station_db.py rebuild --force`。
        msg = ("stations_db.json 重建失败：车站索引为空（可能离线或无缓存），"
               "为保护已有车站库已拒绝写入，旧库已保留；网络恢复后重试，"
               "或使用 `python station_db.py rebuild --force` 显式覆盖。")
        grabber.log("[车站] " + msg)
        print(msg)
        return None
    kinds = grabber.load_station_kinds()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = [{"name": st["name"], "code": st["code"], "py": st["py"],
             "spy": st["spy"], "kind": kinds.get(st["code"], "待识别"),
             "status": "运营中", "updated": now}
            for st in idx]
    db = {"_说明": "12306 客售全量车站(在库=运营中);kind 为车型学习缓存,"
                 "未学习标待识别,查询使用会自动补全;更新: python station_db.py rebuild",
          "version": 1, "updated": now, "count": len(rows), "stations": rows}
    tmp = DB_PATH + ".tmp%s" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DB_PATH)
    if not rows and force:
        # --force 显式覆盖的空库："共 0 站（运营中）"是虚假表述，改发警告。
        print("警告：已按 --force 显式写入空车站库（0 站），旧库已被覆盖。")
        return db
    known = sum(1 for r in rows if r["kind"] != "待识别")
    print("stations_db.json：共 %d 站（运营中），已识别车型 %d 站，更新于 %s"
          % (len(rows), known, now))
    unknown = [r for r in rows if r["kind"] == "待识别"]
    if unknown:
        # 未知 kind 不静默定死：明确提示用户核对确认（首次查询使用时会自动
        # 学习补全，但用户应知道哪些站尚未识别）。
        sample = "、".join(r["name"] for r in unknown[:20])
        more = " 等" if len(unknown) > 20 else ""
        print("注意：%d 个车站车型「待识别」（如：%s%s），已在库中标记待识别；"
              "首次查询使用时会自动学习补全，请核对确认。"
              % (len(unknown), sample, more))
    return db


def search(text, limit=20):
    import grabber
    return grabber.search_stations(text, limit=limit)


def check():
    """校验四份车站文件一致性，漂移时返回非零码（重构前基线 / CI 用）。

    硬失败(1)：文件缺失或解析失败；station_name.json / station_index.json /
    stations_db.json 的 name↔code 集合不一致——三份同源于 station_name.js，
    任一分叉都说明被单点刷新过，此时迁移消费者或删文件都会丢站。
    软提示(0)：stations_db.json 的 kind 快照落后于 station_kind.json；
    rebuild 才收编，滞后是正常中间态，只报数不改退出码。
    """
    data = {}
    for name in ("station_name.json", "station_index.json",
                 "stations_db.json", "station_kind.json"):
        try:
            # 2026-10-18：数据文件已迁入 data/，走统一解析（HERE 是项目根时落 data/，
            # 测试把 HERE 换成临时目录时严格落临时目录）。
            with open(appcommon.resolve_data_file(HERE, name), "r",
                      encoding="utf-8-sig") as f:
                data[name] = json.load(f)
        except Exception as e:
            print("不一致：%s 读取失败：%r" % (name, e))
            return 1
    try:
        pairs = {
            "station_name.json":
                set(data["station_name.json"].get("name2code", {}).items()),
            "station_index.json":
                {(s["name"], s["code"])
                 for s in data["station_index.json"].get("stations", [])},
            "stations_db.json":
                {(s["name"], s["code"])
                 for s in data["stations_db.json"].get("stations", [])},
        }
    except Exception as e:
        print("不一致：车站文件格式异常（需人工核对）：%r" % (e,))
        return 1
    base = pairs["station_index.json"]
    drift = 0
    for name in ("station_name.json", "stations_db.json"):
        diff = sorted(pairs[name] - base) + sorted(base - pairs[name])
        drift += len(diff)
        if diff:
            print("不一致：%s 相对 station_index.json 差 %d 条" % (name, len(diff)))
            for n, c in diff[:5]:
                print("    %s %s" % (n, c))
    stations = data["stations_db.json"].get("stations", [])
    kinds = data["station_kind.json"]
    if not isinstance(stations, list) or not isinstance(kinds, dict):
        # 形状被手改坏时返回「不一致」，而不是 KeyError/AttributeError 带崩 CLI
        print("不一致：stations_db.json / station_kind.json 结构异常")
        return 1
    stale = [s for s in stations if isinstance(s, dict)
             and s.get("kind") == "待识别" and kinds.get(s.get("code"))]
    if stale:
        print("提示：stations_db.json 有 %d 站仍标「待识别」而 station_kind.json "
              "已有答案（rebuild 后收编）：%s"
              % (len(stale), "、".join("%s=%s" % (s.get("name"), kinds[s.get("code")])
                                       for s in stale[:5])))
    if drift:
        print("name↔code 漂移 %d 处：迁移消费者或删除车站文件前必须先对齐。" % drift)
        return 1
    print("车站文件一致性：name↔code 漂移 0 处；stations_db.json %d 站"
          "（待识别 %d，updated %s）"
          % (len(stations),
             sum(1 for s in stations if s.get("kind") == "待识别"),
             data["stations_db.json"].get("updated")))
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "rebuild":
        rebuild(force="--force" in args[1:])
    elif args and args[0] in ("check", "--check"):
        sys.exit(check())
    elif args and args[0] == "query":
        import grabber  # 延迟导入：import station_db 时不拖入 GUI 模块
        for st in search(" ".join(args[1:]) or "", limit=20):
            kind = grabber.load_station_kinds().get(st["code"], "待识别")
            print("{name} {code} {kind}".format(kind=kind, **st))
    else:
        print(__doc__)
