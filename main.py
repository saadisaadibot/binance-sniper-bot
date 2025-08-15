# -*- coding: utf-8 -*-
import time
import threading
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

# =========================
# 🔧 إعدادات ثابتة
# =========================
BITVAVO_URL        = "https://api.bitvavo.com"
HTTP_TIMEOUT       = 8.0
RETRIES            = 3
BACKOFF_BASE_SEC   = 0.6

ONLY_EUR_MARKETS   = 1
MAX_THREADS        = 32
COLLECT_EVERY_SEC  = 180
TOP_N              = 10

B_INGEST_URL       = "https://express-bitv.up.railway.app/ingest"
CANDLE_INTERVAL    = "1m"
CANDLE_LIMIT       = 3
MARKETS_CACHE_TTL  = 3600

# =========================
# 🧠 حالة داخلية
# =========================
app = Flask(__name__)
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=128, pool_maxsize=128)
session.mount("http://", adapter)
session.mount("https://", adapter)

_markets_cache = {"ts": 0.0, "markets": []}
_last_payload = None
_last_push_ts = 0.0
_lock = threading.Lock()

# =========================
# 🌐 HTTP + Retry
# =========================
def http_get(url, params=None):
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(BACKOFF_BASE_SEC * attempt)
    return None

def http_post(url, json_body):
    err = "unknown error"
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.post(url, json=json_body, timeout=HTTP_TIMEOUT)
            if 200 <= r.status_code < 300:
                return True, r.text
            err = f"status={r.status_code}, body={r.text}"
        except Exception as e:
            err = str(e)
        time.sleep(BACKOFF_BASE_SEC * attempt)
    return False, err

# =========================
# 📋 أسواق EUR
# =========================
def get_supported_eur_markets():
    now = time.time()
    if (now - _markets_cache["ts"]) < MARKETS_CACHE_TTL and _markets_cache["markets"]:
        return list(_markets_cache["markets"])

    data = http_get(f"{BITVAVO_URL}/markets")
    markets = []
    if isinstance(data, list):
        for m in data:
            market = m.get("market") or ""
            status = m.get("status") or "trading"
            if not market or status != "trading":
                continue
            if ONLY_EUR_MARKETS and not market.endswith("-EUR"):
                continue
            markets.append(market)

    markets = sorted(set(markets))
    _markets_cache["ts"] = now
    _markets_cache["markets"] = markets
    return markets

# =========================
# 🔎 r3m من 3 شموع 1m
# =========================
Result = namedtuple("Result", ["market", "r3m", "last", "series"])

def fetch_r3m_for_market(market: str):
    params = {"market": market, "interval": CANDLE_INTERVAL, "limit": CANDLE_LIMIT}
    data = http_get(f"{BITVAVO_URL}/candles", params=params)
    if not isinstance(data, list) or len(data) < 2:
        return None

    rows = data[-CANDLE_LIMIT:]
    series, closes = [], []
    for row in rows:
        try:
            ts_ms, _o, _h, _l, c, _v = row
            ts = int(ts_ms // 1000)
            close = float(c)
            series.append((ts, close))
            closes.append(close)
        except Exception:
            return None

    if len(closes) < 2:
        return None

    first_close = closes[0]
    last_close  = closes[-1]
    r3m = 0.0 if first_close <= 0 else (last_close - first_close) / first_close * 100.0
    return Result(market=market, r3m=r3m, last=last_close, series=series)

# =========================
# 🧮 الجامع (كل 3 دقائق)
# =========================
def collector_loop():
    global _last_payload, _last_push_ts
    while True:
        start = time.time()
        start_iso = datetime.fromtimestamp(start, tz=timezone.utc).isoformat()

        markets = get_supported_eur_markets()
        results = []
        if markets:
            with ThreadPoolExecutor(max_workers=MAX_THREADS) as ex:
                futures = {ex.submit(fetch_r3m_for_market, m): m for m in markets}
                for fut in as_completed(futures):
                    try:
                        res = fut.result()
                        if isinstance(res, Result):
                            results.append(res)
                    except Exception:
                        pass

        results.sort(key=lambda r: r.r3m, reverse=True)
        top = results[:TOP_N]

        payload = {
            "run_ts": int(start),
            "run_iso": start_iso,
            "window_sec": 180,
            "interval": CANDLE_INTERVAL,
            "top_n": TOP_N,
            "items": [
                {
                    "market": r.market,
                    "r3m": round(r.r3m, 4),
                    "last": r.last,
                    "series": [(ts, float(price)) for (ts, price) in r.series]
                } for r in top
            ]
        }

        with _lock:
            _last_payload = payload

        if B_INGEST_URL:
            ok, msg = http_post(B_INGEST_URL, payload)
            _last_push_ts = time.time()
            if not ok:
                print(f"[BotA] Push failed → {msg}")
        else:
            print("[BotA] B_INGEST_URL not set — skipping push")

        elapsed = time.time() - start
        time.sleep(max(0.0, COLLECT_EVERY_SEC - elapsed))

# =========================
# 🌐 واجهات
# =========================
@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "last_push_ts": int(_last_push_ts),
        "top_ready": bool(_last_payload),
        "markets_cached": len(_markets_cache.get("markets", []))
    })

@app.get("/preview")
def preview():
    with _lock:
        if not _last_payload:
            return jsonify({"note": "no payload computed yet. wait for first 3m cycle."}), 200
        return jsonify(_last_payload), 200

# =========================
# ▶️ التشغيل (لـ gunicorn)
# =========================
def start_threads():
    th = threading.Thread(target=collector_loop, name="collector", daemon=True)
    th.start()

start_threads()
# لا app.run() هنا. سيتم تشغيل Flask عبر gunicorn كما في Procfile.