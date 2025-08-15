# -*- coding: utf-8 -*-
"""
Bot A — Multi-Frame Top Picker (5m + 15m + 30m + 60m)
- الهدف: نصيد العملات "المتحركة الآن" عبر سلال متعددة للفريمات القصيرة
- اختيار Top K من كل فريم ثم دمج بدون فلترة قاسية (B هو اللي يقرر التنفيذ)
- يحافظ على نمطك الأصلي للـ HTTP والهيكل العام
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
MARKET_SUFFIX  = "-EUR"
LIQ_RANK_MAX   = int(os.getenv("LIQ_RANK_MAX", "200"))

# حجم كل سلة (Top K من كل فريم)
TOP_K_PER_BUCKET = int(os.getenv("TOP_K_PER_BUCKET", "5"))
CAP_MAX_SEND     = int(os.getenv("CAP_MAX_SEND", "18"))   # سقف نهائي للإرسال بعد الدمج

# وجهة Bot B
B_INGEST_URL   = os.getenv("B_INGEST_URL", "https://express-bitv.up.railway.app/ingest")
SEND_TIMEOUT   = float(os.getenv("SEND_TIMEOUT", "6.0"))

# باتشات
BATCH_SIZE     = int(os.getenv("BATCH_SIZE", "10"))
BATCH_SLEEP    = float(os.getenv("BATCH_SLEEP", "0.35"))

# (ملاحظة: ما رح نستخدم فلترة volZ/r-bump هون، خلّي B يقرّر التنفيذ)
# بس رح نلتزم بـ LIQ_RANK_MAX حتى نتجنب الأزواج الميتة

# =========================
# HTTP (نفس أسلوبك)
# =========================
session = requests.Session()
session.headers.update({"User-Agent": "TopMultiFrame-A/1.0"})
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
        print(f"[HTTP] GET {path} failed:", e); return None

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
    if not data: return
    for it in data:
        m = norm_market(it.get("market", ""))
        if m.endswith(MARKET_SUFFIX):
            SUPPORTED.add(m)
    print(f"[MKTS] loaded {len(SUPPORTED)} markets ({MARKET_SUFFIX})")

# =========================
# شموع وميزات
# =========================
def read_candles_1m(market, limit):
    data = http_get(f"/v2/{market}/candles", params={"interval":"1m", "limit": limit})
    if not data or not isinstance(data, list): return []
    return data  # [time, open, high, low, close, volume]

def feat_from_candles_multiframe(cnd):
    """
    يحسب r5m/r10m/r15m/r30m/r60m + volZ + حقول مساعدة، بنفس روحك القديمة.
    """
    if not cnd: return None
    closes = [float(x[4]) for x in cnd]
    vols   = [float(x[5]) for x in cnd]
    if not closes: return None
    c_now  = closes[-1]

    def r_change(mins: int) -> float:
        # نحتاج شمعة قديمة بفرق mins دقيقة: نستخدم closes[-(mins+1)]
        if len(closes) > mins:
            return pct(c_now, closes[-(mins+1)])
        return 0.0

    r5m   = r_change(5)
    r10m  = r_change(10)
    r15m  = r_change(15)
    r30m  = r_change(30)
    r60m  = r_change(60)

    base   = vols[-20:] if len(vols) >= 20 else vols
    mu     = sum(base)/len(base) if base else 0.0
    sigma  = math.sqrt(sum((v-mu)**2 for v in base)/len(base)) if base else 0.0
    volZ   = zscore(vols[-1] if vols else 0.0, mu, sigma)

    # انضغاط آخر 10 دقائق + اختراق بسيط (0.05%) — نفس المنطق القديم
    def range_pct(arr):
        lo, hi = min(arr), max(arr)
        return (hi - lo) / ((hi+lo)/2) * 100.0 if hi>0 and lo>0 else 0.0
    rng10 = range_pct(closes[-11:]) if len(closes) > 11 else 0.0
    hi5   = max(closes[-6:]) if len(closes) > 6 else closes[-1]
    preburst = (rng10 <= 0.80 and r5m >= 0.30 and r10m <= 1.00)
    breakout5bp = (c_now > hi5 * 1.0005)

    return {
        "price_now": float(c_now),
        "price_5m_ago": float(closes[-6] if len(closes) > 6 else closes[0]),
        "r5m": float(r5m),
        "r10m": float(r10m),
        "r15m": float(r15m),
        "r30m": float(r30m),
        "r60m": float(r60m),
        "volZ": float(volZ),
        "range10": float(rng10),
        "preburst": bool(preburst),
        "brk5bp": bool(breakout5bp),
    }

# =========================
# دورة الصيد (Multi-Frame)
# =========================
def once_cycle():
    load_markets()

    tick = http_get("/v2/ticker/24h")
    if not tick:
        print("0/0"); return

    # بناء تجمع السيولة
    pool = []
    for it in tick:
        m = norm_market(it.get("market", ""))
        if m not in SUPPORTED: 
            continue
        last = float(it.get("last", 0.0) or 0.0)
        vol  = float(it.get("volume", 0.0) or 0.0)
        eur_vol = last * vol
        pool.append({"market": m, "symbol": m.split("-")[0], "eur_volume": eur_vol})

    pool.sort(key=lambda x: x["eur_volume"], reverse=True)
    for rank, p in enumerate(pool, 1):
        p["liq_rank"] = rank

    # نحتاج لغاية 60 شمعة دقيقة → limit ~ 70 احتياط
    limit = 70
    feats = {}
    for batch in chunks(pool, BATCH_SIZE):
        for p in batch:
            if p["liq_rank"] > LIQ_RANK_MAX:
                continue
            m = p["market"]
            cnd = read_candles_1m(m, limit)
            if not cnd:
                continue
            f = feat_from_candles_multiframe(cnd)
            if not f:
                continue
            feats[m] = {
                "symbol": p["symbol"],
                "liq_rank": p["liq_rank"],
                **f
            }
        time.sleep(BATCH_SLEEP)

    if not feats:
        print("0/0"); return

    # --- سلال الفريمات (Top K من كل سلة)
    K = TOP_K_PER_BUCKET
    items = list(feats.items())

    top5m  = sorted(items, key=lambda kv: kv[1]["r5m"],  reverse=True)[:K]
    top15m = sorted(items, key=lambda kv: kv[1]["r15m"], reverse=True)[:K]
    top30m = sorted(items, key=lambda kv: kv[1]["r30m"], reverse=True)[:K]
    top60m = sorted(items, key=lambda kv: kv[1]["r60m"], reverse=True)[:K]

    # دمج + إزالة تكرار (أولوية: 5m ثم 15m ثم 30m ثم 60m)
    merged = {}
    for group in (top5m, top15m, top30m, top60m):
        for m, f in group:
            if m not in merged:
                merged[m] = f
    candidates = list(merged.items())
    cand_cnt = len(candidates)
    if cand_cnt == 0:
        print("0/0"); return

    # ترتيب عام لطيف: نحافظ على روح 5m أولاً، ثم 15/30/60 وvolZ كـ tie-breaker
    final_sorted = sorted(
        candidates,
        key=lambda kv: (
            kv[1]["r5m"], kv[1]["r15m"], kv[1]["r30m"], kv[1]["r60m"], kv[1]["volZ"]
        ),
        reverse=True
    )

    # سقف إرسال نهائي
    cap = min(CAP_MAX_SEND, len(final_sorted))
    picked = final_sorted[:cap]

    # إرسال إلى Bot B
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
                "r15m": float(f["r15m"]),
                "r30m": float(f["r30m"]),
                "r60m": float(f["r60m"]),
                "volZ": float(f["volZ"]),
                "price_now": float(f["price_now"]),
                "price_5m_ago": float(f["price_5m_ago"]),
                "liq_rank": int(f["liq_rank"]),
                "range10": float(f["range10"]),
                "preburst": bool(f["preburst"]),
                "brk5bp": bool(f["brk5bp"]),
            },
            "tags": ["top:multiframe", "src:bitvavo:1m"],
            "ttl_sec": 1800
        }
        if http_post(B_INGEST_URL, cv):
            sent += 1
        time.sleep(0.05)  # خيط صغير بين الإرسالات

    # طباعة مختصرة: candidates/sent
    print(f"{cand_cnt}/{sent}")

# =========================
# تشغيل دوري + Flask
# =========================
def loop_runner():
    while True:
        try: once_cycle()
        except Exception as e: print("[CYCLE] error:", e)
        time.sleep(CYCLE_SEC)

app = Flask(__name__)

@app.route("/")
def root(): return "TopMultiFrame A is alive ✅"

@app.route("/once")
def once(): 
    try: once_cycle(); return jsonify(ok=True)
    except Exception as e: return jsonify(ok=False, err=str(e))

@app.route("/webhook", methods=["POST","GET"])
def wrong_webhook():
    print("[A] ❌ Wrong /webhook call — Webhook must go to Bot B.")
    return jsonify(ok=False, hint="Use B /webhook"), 404

def start(): threading.Thread(target=loop_runner, daemon=True).start()
start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)