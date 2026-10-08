# -*- coding: utf-8 -*-
"""全量车站本地库：合并官方车站索引与车型学习缓存，落为 stations_db.json。

12306 客售系统只收录运营中(可购票)车站——在库即运营中，停运/关闭车站
官方列表里不存在，因此「是否运营」以在库为准。车型（高铁/动车/普速）来自
station_kind.json 的实际查询学习，未学习过的标「待识别」，随使用自动补全。

用法：
    python station_db.py rebuild        # 重建/更新 stations_db.json
    python station_db.py query 长葛     # 命令行速查
"""

import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "stations_db.json")


def rebuild():
    import launcher  # 复用 get_station_index / station_kind / search_stations
    idx = launcher.get_station_index()
    kinds = launcher.load_station_kinds()
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
    import launcher
    return launcher.search_stations(text, limit=limit)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "rebuild":
        rebuild()
    elif args and args[0] == "query":
        import launcher  # 延迟导入：import station_db 时不拖入 GUI 模块
        for st in search(" ".join(args[1:]) or "", limit=20):
            kind = launcher.load_station_kinds().get(st["code"], "待识别")
            print("{name} {code} {kind}".format(kind=kind, **st))
    else:
        print(__doc__)
