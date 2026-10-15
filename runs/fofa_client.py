#!/usr/bin/env python3
"""FOFA 采集（限速安全）：单线程 + 最小间隔 + 指数退避 + 结果缓存。"""
import argparse, base64, hashlib, json, os, sys, time, urllib.error, urllib.parse, urllib.request

KEY = os.environ["FOFA_KEY"]
API = "https://fofa.info/api/v1/search/all"
FIELDS = "host,ip,port,title,domain,server,protocol"
RETRY_BACKOFF = [5, 15, 45]
HARD_BUDGET = 30


def fetch(query, page, size, timeout=30):
    qs = urllib.parse.urlencode({
        "key": KEY, "qbase64": base64.b64encode(query.encode()).decode(),
        "size": size, "page": page, "fields": FIELDS,
    })
    req = urllib.request.Request(API + "?" + qs, headers={"User-Agent": "redteam-recon/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"error": True, "errmsg": body[:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--size", type=int, default=100)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    slug = hashlib.md5(args.query.encode()).hexdigest()[:8]
    out = args.out or f"runs/fofa-{slug}.jsonl"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        print(f"[cache] {out} exists, skip", file=sys.stderr)
        return 0

    seen, rows, requests_made, last_ts = set(), [], 0, 0.0
    for page in range(1, args.pages + 1):
        wait = args.interval - (time.time() - last_ts)
        if wait > 0:
            time.sleep(wait)
        status, data = None, None
        for attempt, backoff in enumerate([0] + RETRY_BACKOFF):
            if attempt:
                print(f"[backoff] wait {backoff}s (page={page})", file=sys.stderr)
                time.sleep(backoff)
            last_ts = time.time()
            requests_made += 1
            if requests_made > HARD_BUDGET:
                print(f"[stop] budget {HARD_BUDGET}", file=sys.stderr)
                break
            status, data = fetch(args.query, page, args.size)
            if status == 200 and not data.get("error"):
                break
            errmsg = str(data.get("errmsg", ""))
            if "45012" in errmsg or status == 429:
                continue
            print(f"[stop] non-retryable http={status} {errmsg}", file=sys.stderr)
            data = None
            break
        if data is None:
            break
        results = data.get("results") or []
        print(f"[page {page}] {len(results)} rows (size={data.get('size')})", file=sys.stderr)
        for item in results:
            if isinstance(item, str):
                item = [item]
            host = item[0] if len(item) > 0 else ""
            ip = item[1] if len(item) > 1 else ""
            port = item[2] if len(item) > 2 else ""
            key = (host, ip, port)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "host": host, "ip": ip, "port": port,
                "title": item[3] if len(item) > 3 else "",
                "domain": item[4] if len(item) > 4 else "",
                "server": item[5] if len(item) > 5 else "",
                "protocol": item[6] if len(item) > 6 else "",
            })
        if len(results) < args.size:
            break

    with open(out, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[done] {len(rows)} rows -> {out} ({requests_made} reqs)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
