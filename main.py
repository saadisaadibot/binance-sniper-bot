# -*- coding: utf-8 -*-
"""
Bot A — 3-minute Snapshot Collector (Expanded, no .env, single file, Bitvavo v2)
- كل 3 دقائق على الحدّ: يجلب آخر 3 شموع 1m لكل أسواق -EUR من Bitvavo /v2
- يحسب r3m%، يرتّب تنازليًا، يرسل Top10 مع السلسلة الكاملة [(ts, close)] إلى Bot B
- يشغّل مباشرةً على Railway بأمر: python main.py
"""

import os
import time
import json
import threading
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

# =========================
# ⚙️ ثابتات (بدون .env)
# =========================
BITVAVO_URL         = "https://api.bitvavo.com/v2"  # ✅ استخدم v2 لتفادي 404
HTTP_TIMEOUT_SEC    = 8.0
HTTP_RETRIES        = 3
HTTP_BACKOFF_BASE   = 0.6

ONLY_EUR_MARKETS    = 1
MAX_THREADS         = 40
CYCLE_SEC           = 180      # كل 3 دقائق
TOP_N               = 10
CANDLE_INTERVAL     = "1m"
CANDLE_LIMIT        = 3        # آخر 3 دقائق ≈ 3 شموع

# وجهة الإرسال لبوت B
B_INGEST_URL        = "https://express-bitv.up.railway.app/ingest"
SEND_TIMEOUT_SEC    = 8.0

# كاش قائمة الأسواق
MARKETS_CACHE_TTL   = 3600

LOG_PREFIX          = "[BotA]"

# =========================
# 🧠 حالة داخلية
# =========================
app = Flask(__name__)
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=256, pool_maxsize=256, max_retries=0)
session.mount("http://", adapter)
session.mount("https://", adapter)

_markets_cache = {"ts": 0.0, "markets": []}
_last_payload  = None
_last_push_ts  = 0.0
_last_error    = None
_lock          = threading.Lock()

# =========================
# 🧰 أدوات
# =========================
def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"{LOG_PREFIX} {ts} | {msg}", flush=True)

def to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def align_to_next_3m(now: float) -> float:
    remainder = int(now) % CYCLE_SEC
    return CYCLE_SEC - remainder if remainder else CYCLE_SEC

# =========================
# 🌐 HTTP مع Retry
# =========================
def http_get(url, params=None, timeout=HTTP_TIMEOUT_SEC):
    err = "unknown"
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            err = f"status={r.status_code} body={r.text[:200]}"
        except Exception as e:
            err = str(e)
        time.sleep(HTTP_BACKOFF_BASE * attempt)
    raise RuntimeError(f"GET {url} failed after {HTTP_RETRIES} attempts: {err}")

def http_post(url, payload, timeout=SEND_TIMEOUT_SEC):
    err = "unknown"
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = session.post(url, json=payload, timeout=timeout)
            if 200 <= r.status_code < 300:
                return True, r.text
            err = f"status={r.status_code} body={r.text[:200]}"
        except Exception as e:
            err = str(e)
        time.sleep(HTTP_BACKOFF_BASE * attempt)
    return False, err

# =========================
# 📈 الأسواق المدعومة (-EUR)
# =========================
def get_supported_eur_markets():
    now = time.time()
    if (now - _markets_cache["ts"]) < MARKETS_CACHE_TTL and _markets_cache["markets"]:
        return list(_markets_cache["markets"])

    # ✅ v2
    data = http_get(f"{BITVAVO_URL}/markets")
    markets = []
    if isinstance(data, list):
        for m in data:
            market = (m.get("market") or "").upper()
            status = (m.get("status") or "trading").lower()
            if not market or status != "trading":
                continue
            if ONLY_EUR_MARKETS and not market.endswith("-EUR"):
                continue
            markets.append(market)

    markets = sorted(set(markets))
    _markets_cache["ts"] = now
    _markets_cache["markets"] = markets
    log(f"Loaded markets: {len(markets)} (EUR)")
    return markets

# =========================
# 🔎 r3m من آخر 3 شموع 1m
# =========================
Result = namedtuple("Result", ["market", "r3m", "last", "series"])

def fetch_r3m_for_market(market: str):
    """يجلب /v2/{market}/candles?interval=1m&limit=3 → r3m% + series"""
    try:
        data = http_get(
            f"{BITVAVO_URL}/{market}/candles",
            params={"interval": CANDLE_INTERVAL, "limit": CANDLE_LIMIT},
        )
    except Exception as e:
        log(f"candles fail {market}: {e}")
        return None

    # شكل الاستجابة: [[ts_ms, open, high, low, close, volume], ...]
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
# 🧮 دورة واحدة
# =========================
def collect_once():
    global _last_payload, _last_push_ts, _last_error
    t0 = time.time()
    t0_iso = to_iso(t0)

    try:
        markets = get_supported_eur_markets()
        if not markets:
            raise RuntimeError("no EUR markets detected")

        workers = max(4, min(MAX_THREADS, len(markets)))
        results, failures = [], 0

        log(f"Collect start @ {t0_iso} | markets={len(markets)} | workers={workers}")

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(fetch_r3m_for_market, m): m for m in markets}
            for fut in as_completed(futures):
                res = fut.result()
                if isinstance(res, Result):
                    results.append(res)
                else:
                    failures += 1

        results.sort(key=lambda r: r.r3m, reverse=True)
        top = results[:TOP_N]

        payload = {
            "run_ts": int(t0),
            "run_iso": t0_iso,
            "window_sec": 180,
            "interval": CANDLE_INTERVAL,
            "top_n": TOP_N,
            "items": [
                {
                    "market": r.market,
                    "r3m": round(r.r3m, 5),
                    "last": r.last,
                    "series": [(ts, float(px)) for (ts, px) in r.series],
                } for r in top
            ],
            "meta": {
                "markets_total": len(markets),
                "results_ok": len(results),
                "results_fail": failures,
                "duration_sec": round(time.time() - t0, 3),
            },
        }

        with _lock:
            _last_payload = payload
            _last_error = None

        if B_INGEST_URL:
            ok, msg = http_post(B_INGEST_URL, payload)
            _last_push_ts = time.time()
            if ok:
                log(f"Push OK → top={len(top)} | duration={payload['meta']['duration_sec']}s")
            else:
                log(f"Push FAIL → {msg}")
                with _lock:
                    _last_error = f"push_fail: {msg}"
        else:
            log("B_INGEST_URL not set — skipping push")

    except Exception as e:
        err = f"collect_once error: {e}"
        log(err)
        with _lock:
            _last_error = err

# =========================
# 🔁 الجدولة الدورية (حدود 3 دقائق)
# =========================
def scheduler_loop():
    try:
        collect_once()  # تشغيل فوري أول مرّة
    except Exception as e:
        log(f"scheduler first run error: {e}")

    while True:
        wait = align_to_next_3m(time.time())
        time.sleep(wait + 1.0)  # هامش 1s بعد الحد لضمان توافر الشمعة الأخيرة
        collect_once()

# =========================
# 🌐 واجهات مراقبة
# =========================
@app.get("/")
def root():
    return "Bot A (3m Snapshot) is alive ✅", 200

@app.get("/health")
def health():
    with _lock:
        return jsonify({
            "ok": True,
            "last_push_ts": int(_last_push_ts),
            "last_push_iso": to_iso(_last_push_ts) if _last_push_ts else None,
            "top_ready": bool(_last_payload),
            "markets_cached": len(_markets_cache.get("markets", [])),
            "last_error": _last_error,
        }), 200

@app.get("/preview")
def preview():
    with _lock:
        return jsonify(_last_payload or {"note": "no payload yet"}), 200

@app.get("/run-now")
def run_now():
    threading.Thread(target=collect_once, daemon=True).start()
    return jsonify({"ok": True, "started": True}), 200

# =========================
# ▶️ الإقلاع
# =========================
def start_threads():
    threading.Thread(target=scheduler_loop, name="scheduler", daemon=True).start()

start_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    log(f"Starting Flask on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)