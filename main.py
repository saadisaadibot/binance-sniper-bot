# -*- coding: utf-8 -*-
"""
Bot A — صياد الوحوش (Top 5m Hunter)
- يجمع كل الأسواق -EUR
- يحسب التغير آخر 5 دقائق (r5m) من شموع 1m
- يرتب ويأخذ Top 10 فقط
- يبني CV سريع: وين كانت، وين صارت، r10m, volZ, السيولة
- يرسل مباشرة لـ Bot B /ingest
"""

import os, time, math, json, random, threading
from collections import deque
import requests
from flask import Flask, jsonify

# =========================
# ⚙️ إعدادات
# =========================
BITVAVO_URL   = "https://api.bitvavo.com"
HTTP_TIMEOUT  = 8.0

CYCLE_SEC     = 180                     # كل دورة 3 دقائق
TOP_N_5M      = 10                      # كم عملة تبعت كل مرة
MARKET_SUFFIX = "-EUR"                  # فلتر الأسواق
LIQ_RANK_MAX  = 200                     # حد أقصى لترتيب السيولة المسموح

B_INGEST_URL  = "https://express-bitv.up.railway.app/ingest"  # مسار B مباشر
SEND_TIMEOUT  = 6.0

BATCH_SIZE    = 10                      # كم سوق بالدفعة لجلب الشموع
BATCH_SLEEP   = 0.35                    # نوم بين الدفعات

# =========================
# 🌐 HTTP Session
# =========================
session = requests.Session()
session.headers.update({"User-Agent": "Top5m-Hunter/1.0"})
adapter = requests.adapters.HTTPAdapter(max_retries=2, pool_connections=50, pool_maxsize=50)
session.mount("https://", adapter); session.mount("http://", adapter)

def http_get(path, params=None, base=BITVAVO_URL, timeout=HTTP_TIMEOUT):
    url = f"{base}{path}"
    try:
        r = session.get(url, params=params, timeout=timeout)
        if r.status_code == 429:
            time.sleep(0.6 + random.random()*0.6)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[HTTP] GET {path} failed:", e)
        return None

def http_post(url, payload, timeout=SEND_TIMEOUT):
    try:
        r = session.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[HTTP] POST {url} failed:", e)
        return False

# =========================
# 🧰 أدوات
# =========================
def pct(a, b):
    if b is None or b == 0: return 0.0
    return (a - b) / b * 100.0

def zscore(x, mu, sigma):
    if sigma <= 1e-12: return 0.0
    return (x - mu) / sigma

def norm_market(m: str) -> str:
    return (m or "").upper().strip()

def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

# =========================
# ✅ أسواق مدعومة
# =========================
SUPPORTED = set()
def load_markets():
    SUPPORTED.clear()
    data = http_get("/v2/markets")
    if not data: return
    for it in data:
        m = norm_market(it.get("market", ""))
        if m.endswith(MARKET_SUFFIX):
            SUPPORTED.add(m)
    print(f"[MKTS] loaded {len(SUPPORTED)} markets ({MARKET_SUFFIX})")

# =========================
# 🔬 قراءة شموع 1m
# =========================
def read_candles_1m(market, limit):
    data = http_get(f"/v2/{market}/candles", params={"interval":"1m", "limit": limit})
    if not data or not isinstance(data, list): return []
    return data  # [time, open, high, low, close, volume]

def feat_from_candles(cnd):
    closes = [float(x[4]) for x in cnd]
    vols   = [float(x[5]) for x in cnd]
    c_now  = closes[-1]
    r5m    = pct(c_now, closes[-6]) if len(closes) > 6 else 0.0
    r10m   = pct(c_now, closes[-11]) if len(closes) > 11 else 0.0
    base   = vols[-20:] if len(vols) >= 20 else vols
    mu     = sum(base)/len(base) if base else 0.0
    sigma  = math.sqrt(sum((v-mu)**2 for v in base)/len(base)) if base else 0.0
    volZ   = zscore(vols[-1] if vols else 0.0, mu, sigma)
    return r5m, r10m, volZ, closes

# =========================
# 🎯 دورة الصيد
# =========================
def once_cycle():
    load_markets()

    # جلب ticker 24h لتحديد السيولة
    tick = http_get("/v2/ticker/24h")
    if not tick:
        print("[CYCLE] /ticker/24h failed")
        return

    pool = []
    for it in tick:
        m = norm_market(it.get("market", ""))
        if m not in SUPPORTED: continue
        last = float(it.get("last", 0.0) or 0.0)
        vol  = float(it.get("volume", 0.0) or 0.0)
        eur_vol = last * vol
        pool.append({"market": m, "symbol": m.split("-")[0], "eur_volume": eur_vol})

    # ترتيب السيولة
    pool.sort(key=lambda x: x["eur_volume"], reverse=True)
    for rank, p in enumerate(pool, 1):
        p["liq_rank"] = rank

    # قراءة الشموع لكل الأسواق (بدُفعات)
    feats = {}
    limit = 12
    for batch in chunks(pool, BATCH_SIZE):
        for p in batch:
            m = p["market"]
            cnd = read_candles_1m(m, limit)
            if not cnd: continue
            r5m, r10m, volZ, closes = feat_from_candles(cnd)
            feats[m] = {
                "symbol": p["symbol"],
                "r5m": round(r5m, 4),
                "r10m": round(r10m, 4),
                "volZ": round(volZ, 4),
                "liq_rank": p["liq_rank"],
                "price_now": closes[-1],
                "price_5m_ago": closes[-6] if len(closes) > 6 else closes[0]
            }
        time.sleep(BATCH_SLEEP)

    # اختيار Top N حسب r5m فقط، مع فلتر سيولة
    ranked = sorted(
        (m, f) for m,f in feats.items() if f["liq_rank"] <= LIQ_RANK_MAX
    , key=lambda kv: kv[1]["r5m"], reverse=True)

    picked = ranked[:TOP_N_5M]

    sent = 0
    for m, f in picked:
        cv = {
            "market": m,
            "symbol": f["symbol"],
            "ts": int(time.time()),
            "feat": {
                "r5m": f["r5m"],
                "r10m": f["r10m"],
                "volZ": f["volZ"],
                "price_now": f["price_now"],
                "price_5m_ago": f["price_5m_ago"],
                "liq_rank": f["liq_rank"]
            },
            "tags": ["top5m"],
            "ttl_sec": 1800
        }
        if http_post(B_INGEST_URL, cv):
            sent += 1

    print(f"[CYCLE] Sent {sent}/{TOP_N_5M} top5m coins to B")

# =========================
# 🧵 خيط التشغيل
# =========================
def loop_runner():
    while True:
        try:
            once_cycle()
        except Exception as e:
            print("[CYCLE] error:", e)
        time.sleep(CYCLE_SEC)

# =========================
# 🌐 Flask
# =========================
app = Flask(__name__)

@app.route("/")
def root():
    return "Top5m Hunter A is alive ✅"

@app.route("/once")
def once():
    try:
        once_cycle()
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, err=str(e))

@app.route("/webhook", methods=["POST","GET"])
def wrong_webhook():
    print("[A] ❌ Wrong /webhook call — Webhook must go to Bot B.")
    return jsonify(ok=False, hint="Use https://express-bitv.up.railway.app/webhook for B"), 404

# =========================
# ▶️ الإقلاع
# =========================
def start():
    threading.Thread(target=loop_runner, daemon=True).start()

start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)