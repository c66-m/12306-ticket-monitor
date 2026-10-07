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
    return db


def search(text, limit=20):
    import launcher
    return launcher.search_stations(text, limit=limit)


import launcher  # query 分支也需要


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "rebuild":
        rebuild()
    elif args and args[0] == "query":
        for st in search(" ".join(args[1:]) or "", limit=20):
            kind = launcher.load_station_kinds().get(st["code"], "待识别")
            print("{name} {code} {kind}".format(kind=kind, **st))
    else:
        print(__doc__)
