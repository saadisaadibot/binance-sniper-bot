# -*- coding: utf-8 -*-
"""
Bot A — Top Hybrid (5m + 10m + Preburst) — resilient I/O
- أولويّة لـ r5m (كما في النسخة القديمة)
- إضافة r10m + التقاط preburst/brk5bp لعدم تضييع البدايات
- فلترة نهائية خفيفة بعد الدمج (volZ وعتبة نبض)
- إرسال إلى Bot B مع نفس الحقول + preburst/brk5bp
- باتشات استقرار: retries لـ HTTP/شموع + حداثة شمعة 180s + لوج واضح
"""

import os, time, math, random, threading
import requests
from flask import Flask, jsonify

# =========================
# إعدادات قابلة للتعديل
# =========================
BITVAVO_URL    = "https://api.bitvavo.com"
HTTP_TIMEOUT   = 8.0

CYCLE_SEC      = int(os.getenv("CYCLE_SEC", "180"))
TOP_N_5M       = int(os.getenv("TOP_N_5M", "10"))
TOP_N_10M      = int(os.getenv("TOP_N_10M", "6"))
TOP_N_PRE      = int(os.getenv("TOP_N_PRE", "6"))

MARKET_SUFFIX  = "-EUR"
LIQ_RANK_MAX   = int(os.getenv("LIQ_RANK_MAX", "200"))

# وجهة Bot B
B_INGEST_URL   = os.getenv("B_INGEST_URL", "https://express-bitv.up.railway.app/ingest")
SEND_TIMEOUT   = float(os.getenv("SEND_TIMEOUT", "6.0"))

# باتشات (خففنا الافتراضيات لتقليل 429 — عدّلها من env إذا بدك)
BATCH_SIZE     = int(os.getenv("BATCH_SIZE", "8"))
BATCH_SLEEP    = float(os.getenv("BATCH_SLEEP", "0.50"))

# فلترة “هجينة نظيفة” (نهائية بعد الدمج)
VOLZ_MIN       = float(os.getenv("VOLZ_MIN", "-1.0"))   # استبعاد ما دون هذا
MIN_R_BUMP     = float(os.getenv("MIN_R_BUMP", "0.3"))  # ٪: r5m أو r10m يجب أن يبلغ على الأقل هذا الحد
ALLOW_PRE_PASS = os.getenv("ALLOW_PRE_PASS", "1") == "1"   # اسمح بمرشح preburst/brk5bp مع volZ>=0

# =========================
# HTTP (مع retries)
# =========================
session = requests.Session()
session.headers.update({"User-Agent": "TopHybrid-A/1.2"})
adapter = requests.adapters.HTTPAdapter(max_retries=2, pool_connections=50, pool_maxsize=50)
session.mount("https://", adapter); session.mount("http://", adapter)

def http_get(path, params=None, base=BITVAVO_URL, timeout=HTTP_TIMEOUT):
    url = f"{base}{path}"
    for i in range(3):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                sleep = 0.5 + i*0.6
                print(f"[HTTP] 429 {path} — retry in {sleep:.1f}s")
                time.sleep(sleep)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i == 2:
                print(f"[HTTP] GET {path} failed (try {i+1}/3):", e)
                return None
            time.sleep(0.3 + i*0.4)
    return None

def http_post(url, payload, timeout=SEND_TIMEOUT):
    try:
        r = session.post(url, json=payload, timeout=timeout)
        r.raise_for_status(); return True
    except Exception as e:
        print(f"[HTTP] POST {url} failed:", e); return False

# =========================
# أدوات
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
# أسواق
# =========================
SUPPORTED = set()
def load_markets():
    SUPPORTED.clear()
    data = http_get("/v2/markets")
    if not data: 
        print("[MKTS] failed to load markets"); 
        return
    for it in data:
        m = norm_market(it.get("market", ""))
        if m.endswith(MARKET_SUFFIX):
            SUPPORTED.add(m)
    print(f"[MKTS] loaded {len(SUPPORTED)} markets ({MARKET_SUFFIX})")

# =========================
# شموع وميزات (مع retries + حداثة 180s)
# =========================
def read_candles_1m(market, limit):
    for i in range(3):
        data = http_get(f"/v2/{market}/candles", params={"interval":"1m", "limit": limit})
        if data and isinstance(data, list):
            return data  # [time, open, high, low, close, volume]
        time.sleep(0.25 + i*0.35)
    return []

def feat_from_candles(cnd):
    now_ms = int(time.time() * 1000)
    if not cnd: return None
    try:
        # توسعنا 90s → 180s لتفادي رفض أول دورة/شبكة بطيئة
        if (now_ms - int(cnd[-1][0])) > 180_000:
            return None
    except Exception:
        pass

    closes = [float(x[4]) for x in cnd]
    vols   = [float(x[5]) for x in cnd]
    c_now  = closes[-1]
    r5m    = pct(c_now, closes[-6]) if len(closes) > 6 else 0.0
    r10m   = pct(c_now, closes[-11]) if len(closes) > 11 else 0.0

    base   = vols[-20:] if len(vols) >= 20 else vols
    mu     = sum(base)/len(base) if base else 0.0
    sigma  = math.sqrt(sum((v-mu)**2 for v in base)/len(base)) if base else 0.0
    volZ   = zscore(vols[-1] if vols else 0.0, mu, sigma)

    # انضغاط آخر 10 دقائق + اختراق بسيط (0.05%)
    def range_pct(arr):
        lo, hi = min(arr), max(arr)
        return (hi - lo) / ((hi+lo)/2) * 100.0 if hi>0 and lo>0 else 0.0
    rng10 = range_pct(closes[-11:]) if len(closes) > 11 else 0.0
    hi5   = max(closes[-6:]) if len(closes) > 6 else closes[-1]
    preburst = (rng10 <= 0.80 and r5m >= 0.30 and r10m <= 1.00)
    breakout5bp = (c_now > hi5 * 1.0005)

    return {
        "price_now": c_now,
        "r5m": r5m,
        "r10m": r10m,
        "volZ": volZ,
        "range10": rng10,
        "preburst": bool(preburst),
        "brk5bp": bool(breakout5bp),
        "price_5m_ago": float(closes[-6] if len(closes) > 6 else closes[0]),
    }

# =========================
# دورة الصيد (Hybrid)
# =========================
def once_cycle():
    load_markets()

    tick = http_get("/v2/ticker/24h")
    if not tick:
        print("0/0"); return  # فشل جلب البيانات

    pool = []
    for it in tick:
        m = norm_market(it.get("market", ""))
        if m not in SUPPORTED: continue
        last = float(it.get("last", 0.0) or 0.0)
        vol  = float(it.get("volume", 0.0) or 0.0)
        eur_vol = last * vol
        pool.append({"market": m, "symbol": m.split("-")[0], "eur_volume": eur_vol})

    pool.sort(key=lambda x: x["eur_volume"], reverse=True)
    for rank, p in enumerate(pool, 1):
        p["liq_rank"] = rank

    feats = {}
    limit = 12
    scanned = 0
    ok_candles = 0

    for batch in chunks(pool, BATCH_SIZE):
        for p in batch:
            m = p["market"]
            if p["liq_rank"] > LIQ_RANK_MAX:
                continue
            cnd = read_candles_1m(m, limit)
            if not cnd:
                continue
            ok_candles += 1
            f = feat_from_candles(cnd)
            if not f:
                continue
            feats[m] = {
                "symbol": p["symbol"],
                "liq_rank": p["liq_rank"],
                **{k: (round(v,4) if isinstance(v,float) else v) for k,v in f.items()}
            }
            scanned += 1
        time.sleep(BATCH_SLEEP)

    if not feats:
        # ما في مرشحين أساسًا → قبل/بعد = 0/0
        print("0/0")
        return

    # --- 1) Top by 5m
    top5m = sorted(feats.items(), key=lambda kv: kv[1]["r5m"], reverse=True)[:TOP_N_5M]

    # --- 2) Top by 10m
    top10m = sorted(feats.items(), key=lambda kv: kv[1]["r10m"], reverse=True)[:TOP_N_10M]

    # --- 3) Preburst/Breakout 5bp
    pre = [kv for kv in feats.items() if kv[1].get("preburst") or kv[1].get("brk5bp")]
    pre = sorted(pre, key=lambda kv: (kv[1]["preburst"], kv[1]["brk5bp"], kv[1]["r5m"], kv[1]["r10m"]), reverse=True)[:TOP_N_PRE]

    # دمج + إزالة تكرار
    merged = {}
    for group in (top5m, top10m, pre):
        for m, f in group:
            merged[m] = f
    candidates = list(merged.items())
    before_cnt = len(candidates)

    # فلتر نهائي نظيف
    final = []
    for m, f in candidates:
        if f["volZ"] < VOLZ_MIN:
            continue
        if f["r5m"] >= MIN_R_BUMP or f["r10m"] >= MIN_R_BUMP:
            final.append((m, f))
        elif ALLOW_PRE_PASS and (f["preburst"] or f["brk5bp"]) and f["volZ"] >= 0.0:
            final.append((m, f))

    after_cnt = len(final)
    if after_cnt == 0:
        # اطبع فقط الرقمين وارجع
        print(f"{before_cnt}/0")
        return

    # ترتيب الإرسال — r5m ثم r10m ثم volZ
    final_sorted = sorted(
        final,
        key=lambda kv: (kv[1]["r5m"], kv[1]["r10m"], kv[1]["volZ"]),
        reverse=True
    )

    # سقف العدد المرسل
    cap = max(TOP_N_5M, min(16, TOP_N_5M + TOP_N_10M//2))
    picked = final_sorted[:cap]

    sent = 0
    now_ts = int(time.time())
    for m, f in picked:
        cv = {
            "market": m,
            "symbol": f["symbol"],
            "ts": now_ts,
            "feat": {
                "r5m": float(f["r5m"]),
                "r10m": float(f["r10m"]),
                "volZ": float(f["volZ"]),
                "price_now": float(f["price_now"]),
                "price_5m_ago": float(f["price_5m_ago"]),
                "liq_rank": int(f["liq_rank"]),
                "range10": float(f["range10"]),
                "preburst": bool(f["preburst"]),
                "brk5bp": bool(f["brk5bp"]),
            },
            "tags": ["top:hybrid", "src:bitvavo:1m"],
            "ttl_sec": 1800
        }
        if http_post(B_INGEST_URL, cv):
            sent += 1
        time.sleep(0.05)

    # السطر المطلوب: قبل/بعد فقط
    print(f"{before_cnt}/{after_cnt}")
# =========================
# تشغيل دوري + Flask
# =========================
def loop_runner():
    while True:
        try: 
            once_cycle()
        except Exception as e: 
            print("[CYCLE] error:", e)
        time.sleep(CYCLE_SEC)

app = Flask(__name__)

@app.route("/")
def root(): return "TopHybrid A is alive ✅"

@app.route("/once")
def once(): 
    try: 
        once_cycle(); return jsonify(ok=True)
    except Exception as e: 
        return jsonify(ok=False, err=str(e))

@app.route("/webhook", methods=["POST","GET"])
def wrong_webhook():
    print("[A] ❌ Wrong /webhook call — Webhook must go to Bot B.")
    return jsonify(ok=False, hint="Use B /webhook"), 404

def start(): threading.Thread(target=loop_runner, daemon=True).start()
start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)