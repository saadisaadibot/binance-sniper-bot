# -*- coding: utf-8 -*-
"""
Bot A — 3-minute Snapshot Collector (Expanded, no .env, single file)
- Every 3 minutes on the exact 3m boundary (UTC): fetch last 3×1m candles for all -EUR markets
- Compute r3m% (close_last vs close_first), sort desc, pick Top10
- POST payload (with full [(ts, close)] series) to Bot B
- Rich logging + health endpoints

Run on Railway:
  Start Command → python main.py
"""

import os
import time
import math
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
BITVAVO_URL         = "https://api.bitvavo.com"
HTTP_TIMEOUT_SEC    = 8.0
HTTP_RETRIES        = 3
HTTP_BACKOFF_BASE   = 0.6     # ثواني

ONLY_EUR_MARKETS    = 1       # 1 = أسواق -EUR فقط
MAX_THREADS         = 40      # خيوط جلب الشموع (يُضبط تلقائياً إذا الأسواق قليلة)
THREAD_CHUNK_SLEEP  = 0.0     # نوم بسيط بين دفعات المستقبلات (لطف على API)

CYCLE_SEC           = 180     # كل 3 دقائق
TOP_N               = 10
CANDLE_INTERVAL     = "1m"
CANDLE_LIMIT        = 3       # آخر 3 دقائق ≈ 3 شموع دقيقة

# وجهة الإرسال لبوت B
B_INGEST_URL        = "https://express-bitv.up.railway.app/ingest"
SEND_TIMEOUT_SEC    = 8.0

# كاش قائمة الأسواق
MARKETS_CACHE_TTL   = 3600    # ثواني

# طباعة لوج كل كم عملية إرسال
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
# 🧰 أدوات عامة
# =========================
def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"{LOG_PREFIX} {ts} | {msg}", flush=True)

def sleep_s(seconds: float):
    if seconds > 0:
        time.sleep(seconds)

def to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def align_to_next_3m(now: float) -> float:
    """يعيد زمن النوم المطلوب للحد 3 دقائق التالي (UTC)."""
    # مثال: 12:00:00, 12:03:00, 12:06:00, ...
    secs = int(now)
    remainder = secs % CYCLE_SEC
    wait = CYCLE_SEC - remainder if remainder else 0
    # احتياط صغير لبدء بعد الحد بثانية
    return wait if wait > 0 else CYCLE_SEC

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
        sleep_s(HTTP_BACKOFF_BASE * attempt)
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
        sleep_s(HTTP_BACKOFF_BASE * attempt)
    return False, err

# =========================
# 📈 الأسواق المدعومة (-EUR)
# =========================
def get_supported_eur_markets():
    now = time.time()
    if (now - _markets_cache["ts"]) < MARKETS_CACHE_TTL and _markets_cache["markets"]:
        return list(_markets_cache["markets"])
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
    """يجلب /candles?interval=1m&limit=3 → يحسب r3m% ويعيد السلسلة [(ts, close)]"""
    try:
        data = http_get(f"{BITVAVO_URL}/candles", params={"market": market, "interval": CANDLE_INTERVAL, "limit": CANDLE_LIMIT})
    except Exception as e:
        log(f"candles fail {market}: {e}")
        return None

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
# 🧮 دورة واحدة (جمع ← ترتيب ← إرسال)
# =========================
def collect_once():
    global _last_payload, _last_push_ts, _last_error
    t0 = time.time()
    t0_iso = to_iso(t0)

    try:
        markets = get_supported_eur_markets()
        if not markets:
            raise RuntimeError("no EUR markets detected")

        # اضبط عدد الخيوط بحيث لا يتجاوز عدد الأسواق
        workers = max(4, min(MAX_THREADS, len(markets)))
        results = []
        failures = 0

        log(f"Collect start @ {t0_iso} | markets={len(markets)} | workers={workers}")

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(fetch_r3m_for_market, m): m for m in markets}
            for i, fut in enumerate(as_completed(futures), 1):
                res = fut.result()
                if isinstance(res, Result):
                    results.append(res)
                else:
                    failures += 1
                # throttle خفيف بين دفعات طويلة (اختياري)
                if THREAD_CHUNK_SLEEP and (i % 100 == 0):
                    sleep_s(THREAD_CHUNK_SLEEP)

        # ترتيب وأخذ TopN
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
                    "series": [(ts, float(px)) for (ts, px) in r.series]
                } for r in top
            ],
            "meta": {
                "markets_total": len(markets),
                "results_ok": len(results),
                "results_fail": failures,
                "duration_sec": round(time.time() - t0, 3)
            }
        }

        with _lock:
            _last_payload = payload
            _last_error = None

        # إرسال
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
    # تشغيل أول فوري لتعبئة /preview بسرعة
    try:
        collect_once()
    except Exception as e:
        log(f"scheduler first run error: {e}")

    while True:
        wait = align_to_next_3m(time.time())
        # ترك 1.0 ثانية تأخير بسيط بعد الحد لضمان توفر الشمعة الأخيرة
        sleep_s(wait + 1.0)
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
            "last_error": _last_error
        }), 200

@app.get("/preview")
def preview():
    with _lock:
        return jsonify(_last_payload or {"note": "no payload yet"}), 200

@app.get("/config")
def config():
    cfg = {
        "BITVAVO_URL": BITVAVO_URL,
        "ONLY_EUR_MARKETS": ONLY_EUR_MARKETS,
        "CYCLE_SEC": CYCLE_SEC,
        "CANDLE_INTERVAL": CANDLE_INTERVAL,
        "CANDLE_LIMIT": CANDLE_LIMIT,
        "TOP_N": TOP_N,
        "MAX_THREADS": MAX_THREADS,
        "HTTP_TIMEOUT_SEC": HTTP_TIMEOUT_SEC,
        "HTTP_RETRIES": HTTP_RETRIES,
        "HTTP_BACKOFF_BASE": HTTP_BACKOFF_BASE,
        "B_INGEST_URL": B_INGEST_URL,
        "MARKETS_CACHE_TTL": MARKETS_CACHE_TTL
    }
    return jsonify(cfg), 200

@app.get("/run-now")
def run_now():
    # تشغيل دورة فورية يدوياً
    threading.Thread(target=collect_once, daemon=True).start()
    return jsonify({"ok": True, "started": True}), 200

@app.get("/last-error")
def last_error():
    with _lock:
        return jsonify({"last_error": _last_error}), 200

# =========================
# ▶️ الإقلاع
# =========================
def start_threads():
    th = threading.Thread(target=scheduler_loop, name="scheduler", daemon=True)
    th.start()

start_threads()

if __name__ == "__main__":
    # تشغيل محلي أو على Railway مباشرةً دون gunicorn
    port = int(os.getenv("PORT", "8080"))
    log(f"Starting Flask on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)