# -*- coding: utf-8 -*-
"""
Bot A — Mixed Scanner (Headless)
- دورتان تغطيان السوق بالكامل على دفعتين عبر Round-Robin
- بكل دورة: Top5 (r5m) + Top5 (r1m) + Top5 (volZ) → دمج → استبعاد volZ<=0 → إرسال
- راحة بين الدورات: 90s طبيعي / 120s إذا الشريحة هابطة
- تنبيه تيليغرام اختياري عند الفشل أو الإرسال الضعيف
"""

import os, time, math, random, threading
import requests

# ============================================================
# إعدادات
# ============================================================
BITVAVO_URL           = "https://api.bitvavo.com"
HTTP_TIMEOUT          = 8.0

# الإرسال إلى Bot B
B_INGEST_URL          = os.getenv("B_INGEST_URL", "https://express-bitv.up.railway.app/ingest")
SEND_TIMEOUT          = 6.0

# الميكس بكل دورة
TOP_R5M               = 5
TOP_R1M               = 5
TOP_VOLZ              = 5
SEND_PER_CYCLE        = 15           # بعد الدمج/الفلترة

# تغطية السوق
MARKET_SUFFIX         = "-EUR"
LIQ_RANK_MAX          = 600          # أعلى سيولة مقبولة إجمالاً
LIQ_SCAN_PER_CYCLE    = 200          # كم سوق نفحص بكل دورة (بدورتين ≈ 400)

# راحة بين الدورات
CYCLE_SEC_NORMAL      = 90           # طبيعي/محايد
CYCLE_SEC_BEAR        = 120          # إذا الشريحة هابطة بوضوح

# باتشات قراءة الشموع
BATCH_SIZE            = 10
BATCH_SLEEP           = 0.35

# تنبيهات تيليغرام (اختياري)
A_BOT_TOKEN           = os.getenv("A_BOT_TOKEN", "")
A_CHAT_ID             = os.getenv("A_CHAT_ID", "")

# تحذير لو الإرسال قليل
WARN_MIN_SENT         = 3
MAX_HTTP_FAILS        = 6

# عتبات判 الشريحة الهابطة/الميتة
BEAR_MED_R5M_TH       = -0.50        # ميديان r5m <= -0.5%
BEAR_POS_FRAC_TH      = 0.20         # نسبة r5m>0 أقل من 20%

# بوابة الشريحة الميّتة (للتخفيف)
DEAD_MAX_R1M          = 0.20
DEAD_MAX_R5M          = 0.40
DEAD_MAX_VOLZ         = 1.00
DEAD_POS_FRAC_TH      = 0.10

# ============================================================
# HTTP
# ============================================================
session = requests.Session()
session.headers.update({"User-Agent": "Mixed-Scanner-A/3.0"})
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
        r.raise_for_status(); return True
    except Exception as e:
        print(f"[HTTP] POST {url} failed:", e)
        return False

def tg_notify(text):
    if not (A_BOT_TOKEN and A_CHAT_ID): return
    try:
        url = f"https://api.telegram.org/bot{A_BOT_TOKEN}/sendMessage"
        session.post(url, json={"chat_id": A_CHAT_ID, "text": text, "disable_web_page_preview": True}, timeout=6)
    except Exception as e:
        print("[TG] notify failed:", e)

# ============================================================
# أدوات
# ============================================================
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

# ============================================================
# أسواق
# ============================================================
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

# ============================================================
# شموع وميزات
# ============================================================
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

    # انضغاط 10 دقائق + اختراق بسيط 0.05%
    def range_pct(arr):
        lo, hi = min(arr), max(arr)
        return (hi - lo) / ((hi+lo)/2) * 100.0 if hi>0 and lo>0 else 0.0
    rng10 = range_pct(closes[-11:]) if len(closes) > 11 else 0.0
    hi5   = max(closes[-6:]) if len(closes) > 6 else closes[-1]
    preburst = (rng10 <= 0.80 and r5m >= 0.30 and r10m <= 1.00)
    breakout5bp = (c_now > hi5 * 1.0005)

    return r5m, r10m, volZ, closes, rng10, preburst, breakout5bp

# ============================================================
# Round-Robin عبر السوق
# ============================================================
_RR_IDX = 0

def pick_liq_slice(pool_sorted):
    global _RR_IDX
    n = len(pool_sorted)
    if n == 0: return []
    start = _RR_IDX % n
    end = min(start + LIQ_SCAN_PER_CYCLE, n)
    slice_ = pool_sorted[start:end]
    _RR_IDX = end % n
    return slice_

# ============================================================
# إحصاءات الشريحة وقرارات الراحة/التخفيف
# ============================================================
def slice_stats(rankable):
    if not rankable:
        return {"pos_frac":0.0, "max_r1m":0.0, "max_r5m":0.0, "max_volZ":0.0, "median_r5m":0.0}
    r5_list = [f["r5m"] for _, f in rankable]
    r1_list = [f["r1m"] for _, f in rankable]
    vz_list = [f["volZ"] for _, f in rankable]
    r5_sorted = sorted(r5_list)
    median_r5m = r5_sorted[len(r5_sorted)//2]
    pos_frac = sum(1 for x in r5_list if x > 0) / max(1, len(r5_list))
    return {
        "pos_frac": pos_frac,
        "max_r1m":  max(r1_list or [0.0]),
        "max_r5m":  max(r5_list or [0.0]),
        "max_volZ": max(vz_list or [0.0]),
        "median_r5m": median_r5m,
    }

def is_bear_slice(stats):
    return (stats["median_r5m"] <= BEAR_MED_R5M_TH and stats["pos_frac"] <= BEAR_POS_FRAC_TH)

def is_dead_slice(stats):
    return (
        stats["pos_frac"] < DEAD_POS_FRAC_TH and
        stats["max_r1m"]  < DEAD_MAX_R1M and
        stats["max_r5m"]  < DEAD_MAX_R5M and
        stats["max_volZ"] < DEAD_MAX_VOLZ
    )

# ============================================================
# دورة واحدة
# ============================================================
def once_cycle():
    load_markets()

    tick = http_get("/v2/ticker/24h")
    if not tick:
        tg_notify("❌ [A] /ticker/24h failed")
        return CYCLE_SEC_BEAR

    # سيولة أولية
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

    liq_pool  = [p for p in pool if p["liq_rank"] <= LIQ_RANK_MAX]
    liq_slice = pick_liq_slice(liq_pool)[:LIQ_SCAN_PER_CYCLE]

    # قراءة الشموع + الميزات
    feats = {}
    limit = 12
    for batch in chunks(liq_slice, BATCH_SIZE):
        for p in batch:
            m = p["market"]
            cnd = read_candles_1m(m, limit)
            if not cnd: 
                continue
            r5m, r10m, volZ, closes, rng10, preburst, brk5bp = feat_from_candles(cnd)
            c_now  = float(closes[-1])
            c_prev = float(closes[-2]) if len(closes) >= 2 else c_now
            r1m    = pct(c_now, c_prev)

            feats[m] = {
                "symbol": p["symbol"],
                "r1m": round(r1m, 4),
                "r5m": round(r5m, 4),
                "r10m": round(r10m, 4),
                "volZ": round(volZ, 4),
                "liq_rank": p["liq_rank"],
                "price_now": c_now,
                "price_5m_ago": float(closes[-6] if len(closes) > 6 else closes[0]),
                "range10": round(rng10, 3),
                "preburst": bool(preburst),
                "brk5bp": bool(brk5bp),
            }
        time.sleep(BATCH_SLEEP)

    rankable = list(feats.items())
    if not rankable:
        tg_notify("⚠️ [A] No features collected this cycle.")
        return CYCLE_SEC_NORMAL

    # إحصاءات الشريحة وتحديد الراحة
    stats = slice_stats(rankable)
    bear  = is_bear_slice(stats)
    sleep_sec = CYCLE_SEC_BEAR if bear else CYCLE_SEC_NORMAL

    # لوائح الميكس
    top_r5m  = sorted(rankable, key=lambda kv: kv[1]["r5m"],  reverse=True)[:TOP_R5M]
    top_r1m  = sorted(rankable, key=lambda kv: kv[1]["r1m"],  reverse=True)[:TOP_R1M]
    top_volz = sorted(rankable, key=lambda kv: kv[1]["volZ"], reverse=True)[:TOP_VOLZ]

    # دمج بدون تكرار
    picked_map = {}
    def _add(lst):
        for m, f in lst:
            if m not in picked_map:
                picked_map[m] = f
                if len(picked_map) >= SEND_PER_CYCLE:
                    break

    _add(top_r5m); _add(top_r1m); _add(top_volz)

    # استبعاد volZ السالب (متطلبك)
    # ملاحظة: إذا بدك مرونة حسب السوق، عدّل هنا حسب bear/neutral
    picked = [(m,f) for m,f in picked_map.items() if f["volZ"] > 0.0][:SEND_PER_CYCLE]

    # بوابة الشريحة الميتة: خفّض أو الغِ الإرسال
    if is_dead_slice(stats):
        if len(picked) == 0:
            tg_notify("⏸️ [A] skipped dead slice (no momentum/volume).")
            return sleep_sec
        # إرسال مخفّض
        slim = min(3, len(picked))
        tg_notify(f"⚠️ [A] dead slice → throttled send to {slim}/{SEND_PER_CYCLE}.")
        picked = picked[:slim]

    # إرسال إلى B
    sent = 0
    for m, f in picked:
        cv = {
            "market": m,
            "symbol": f["symbol"],
            "ts": int(time.time()),
            "feat": {
                "r1m": f["r1m"],
                "r5m": f["r5m"],
                "r10m": f["r10m"],
                "volZ": f["volZ"],
                "price_now": f["price_now"],
                "price_5m_ago": f["price_5m_ago"],
                "liq_rank": f["liq_rank"],
                "range10": f["range10"],
                "preburst": f["preburst"],
                "brk5bp": f["brk5bp"],
            },
            "tags": ["mix:r5m,r1m,volZ"],
            "ttl_sec": 1800
        }
        if http_post(B_INGEST_URL, cv):
            sent += 1

    print(f"[CYCLE] Sent {sent}/{min(SEND_PER_CYCLE, len(picked_map))} to B | "
          f"slice stats: pos={stats['pos_frac']:.0%}, med_r5m={stats['median_r5m']:+.2f}%, "
          f"max(r1m={stats['max_r1m']:+.2f}%, r5m={stats['max_r5m']:+.2f}%, volZ={stats['max_volZ']:+.2f}) | "
          f"sleep={sleep_sec}s")

    if sent < WARN_MIN_SENT:
        tg_notify(f"⚠️ [A] low send: {sent}/{SEND_PER_CYCLE} (bear={bear})")

    return sleep_sec

# ============================================================
# تشغيل دوري (Headless)
# ============================================================
def loop_runner():
    consecutive_http_fails = 0
    while True:
        try:
            sleep_for = once_cycle()
            consecutive_http_fails = 0
            time.sleep(int(sleep_for if isinstance(sleep_for, (int, float)) else CYCLE_SEC_NORMAL))
        except Exception as e:
            consecutive_http_fails += 1
            print("[CYCLE] error:", e)
            if consecutive_http_fails >= MAX_HTTP_FAILS:
                tg_notify(f"❌ [A] cycle failing ({consecutive_http_fails}x): {e}")
            time.sleep(10)

if __name__ == "__main__":
    loop_runner()