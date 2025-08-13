# -*- coding: utf-8 -*-
"""
Bot A — المستكشف الشجاع (Feature Extractor)
- يجلب أسواق EUR و /ticker/24h كل دورة (افتراضيًا كل 180 ثانية)
- يختار مرشحين وسيولة واسعة + رادار أسبوعي
- يحسب ميزات 1m (r300/r600/dd300/volZ) على دفعات خفيفة
- يبني Context Vector (CV) ويرسلها إلى Bot B عبر HTTP POST /ingest
- لا يراقب لحظيًا، ولا يقرر شراء — هذا عمل Bot B

اعتماديات:
  pip install flask requests

تشغيل محلي:
  python bot_a_explorer.py
على Railway/Gunicorn:
  gunicorn -w 1 -b 0.0.0.0:$PORT bot_a_explorer:app
"""

import os, time, math, json, random, threading
from collections import deque, defaultdict
from datetime import datetime, timedelta
import requests
from flask import Flask, jsonify, request

# =========================
# ⚙️ إعدادات
# =========================
BITVAVO_URL         = os.getenv("BITVAVO_URL", "https://api.bitvavo.com")
HTTP_TIMEOUT        = float(os.getenv("HTTP_TIMEOUT", 8.0))

# دورة العمل
CYCLE_SEC           = int(os.getenv("CYCLE_SEC", 180))          # كل 3 دقائق
TOP_CANDIDATES      = int(os.getenv("TOP_CANDIDATES", 180))     # مرشحي سيولة EUR
BATCH_SIZE          = int(os.getenv("BATCH_SIZE", 12))          # شموع/دفعة
BATCH_SLEEP         = float(os.getenv("BATCH_SLEEP", 0.35))     # نوم بين الدُفعات

# مرونة 24h + إدخال فوري حسب 5/10 دقائق
EXCLUDE_24H_PCT     = float(os.getenv("EXCLUDE_24H_PCT", 12.0))  # يُتجاوز إن r5m قوي
ALLOW_STRONG_5M     = float(os.getenv("ALLOW_STRONG_5M", 0.80))  # % يسمح بتجاوز 24h
THRESH_5M_INCLUDE   = float(os.getenv("THRESH_5M_INCLUDE", 0.35)) # % إدخال فوري
THRESH_10M_INCLUDE  = float(os.getenv("THRESH_10M_INCLUDE", 0.70))# %

# حجم/سيولة/سبريد
VOLZ_BASE_N         = int(os.getenv("VOLZ_BASE_N", 20))         # شموع لحساب mu,sigma
VOLZ_MIN_LIMIT      = int(os.getenv("VOLZ_MIN_LIMIT", 20))      # الحد الأدنى للشموع المجلوّبة
INCLUDE_R1800       = int(os.getenv("INCLUDE_R1800", "0"))      # r1800 (30m) اختياري
SPREAD_FALLBACK_BP  = int(os.getenv("SPREAD_FALLBACK_BP", 60))  # إن لم تتوفر bid/ask

# ذاكرة أسبوعية خفيفة (رادار)
RADAR_PATH          = os.getenv("RADAR_PATH", "radar_weekly.json")
RADAR_MAX_SIZE      = int(os.getenv("RADAR_MAX_SIZE", 80))
RADAR_TTL_DAYS      = int(os.getenv("RADAR_TTL_DAYS", 7))

# إرسال إلى Bot B
B_INGEST_URL        = os.getenv("B_INGEST_URL", "http://localhost:8081/ingest")  # عدّلها لعنوان Bot B
SEND_TIMEOUT        = float(os.getenv("SEND_TIMEOUT", 6.0))
MAX_QUEUE           = int(os.getenv("MAX_QUEUE", 500))  # صف إعادة إرسال فاشلة

# فلتر الأسواق
MARKET_SUFFIX       = os.getenv("MARKET_SUFFIX", "-EUR")
MARKET_BLACKLIST    = set((os.getenv("MARKET_BLACKLIST", "") or "").split(",")) if os.getenv("MARKET_BLACKLIST") else set()

# =========================
# 🌐 جلسة HTTP + Retry لطيف
# =========================
session = requests.Session()
session.headers.update({"User-Agent": "BraveExplorer/1.0"})
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
        print(f"[HTTP] GET {path} failed: {e}")
        return None

def http_post(url, payload, timeout=SEND_TIMEOUT):
    try:
        r = session.post(url, json=payload, timeout=timeout)
        if r.status_code >= 500:
            raise RuntimeError(f"5xx {r.status_code}")
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[HTTP] POST {url} failed: {e}")
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
        if m.endswith(MARKET_SUFFIX) and m not in MARKET_BLACKLIST:
            SUPPORTED.add(m)
    print(f"[MKTS] loaded {len(SUPPORTED)} markets ({MARKET_SUFFIX})")

def is_supported(m): return m in SUPPORTED

# =========================
# 💾 رادار أسبوعي (بسيط مع TTL)
# =========================
def load_radar():
    try:
        with open(RADAR_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        # تنظيف حسب TTL
        now = time.time()
        return {k:v for k,v in obj.items() if now - float(v.get("ts", now)) <= RADAR_TTL_DAYS*86400}
    except Exception:
        return {}

def save_radar(rad):
    try:
        with open(RADAR_PATH, "w", encoding="utf-8") as f:
            json.dump(rad, f, ensure_ascii=False)
    except Exception as e:
        print("[RADAR] save failed:", e)

RADAR = load_radar()  # {market: {"ts": epoch, "tag": "burst|gradual"}}

def radar_touch(market, tag):
    global RADAR
    RADAR[market] = {"ts": time.time(), "tag": tag}
    # قص الحجم إن تجاوز
    if len(RADAR) > RADAR_MAX_SIZE:
        # احذف الأقدم
        oldest = sorted(RADAR.items(), key=lambda kv: kv[1]["ts"])[:len(RADAR)-RADAR_MAX_SIZE]
        for k,_ in oldest: RADAR.pop(k, None)
    save_radar(RADAR)

# =========================
# 📦 صف إعادة إرسال فاشلة
# =========================
retry_queue = deque(maxlen=MAX_QUEUE)

def send_cv(cv):
    ok = http_post(B_INGEST_URL, cv)
    if not ok:
        retry_queue.append((time.time(), cv))

def flush_retry_queue():
    if not retry_queue: return
    keep = deque(maxlen=MAX_QUEUE)
    while retry_queue:
        ts, cv = retry_queue.popleft()
        if not http_post(B_INGEST_URL, cv):
            keep.append((ts, cv))
    while keep:
        retry_queue.append(keep.popleft())

# =========================
# 🔬 حساب الميزات من شموع 1m
# =========================
def read_candles_1m(market, limit):
    market = norm_market(market)
    data = http_get(f"/v2/{market}/candles", params={"interval":"1m", "limit": int(limit)})
    if not data or not isinstance(data, list): return []
    # شكل العنصر: [time, open, high, low, close, volume]
    return data

def feat_from_candles(cnd):
    """
    يحسب: close الآن، r300, r600, (اختياري r1800), dd300, volZ
    """
    n = len(cnd)
    if n < VOLZ_MIN_LIMIT: return None

    closes = [float(x[4]) for x in cnd]
    vols   = [float(x[5]) for x in cnd]
    c_now  = closes[-1]

    def r_change_min(minutes):
        steps = minutes  # 1m candles
        if n <= steps: return 0.0
        return pct(c_now, closes[-steps-1])

    r300  = r_change_min(5)   # 5m
    r600  = r_change_min(10)  # 10m
    r1800 = r_change_min(30) if INCLUDE_R1800 and n > 31 else 0.0

    # drawdown داخل آخر 5 دقائق اعتمادًا على الإغلاقات
    window = 5
    hi = max(closes[-window-1:]) if n > window else c_now
    dd300 = max(0.0, pct(closes[-1], hi) * -1)

    # volZ: آخر حجم مقابل mu,sigma آخر VOLZ_BASE_N أحجام
    base = vols[-VOLZ_BASE_N:] if n >= VOLZ_BASE_N else vols
    mu = sum(base)/len(base) if base else 0.0
    var = sum((v-mu)*(v-mu) for v in base)/len(base) if base else 0.0
    sigma = math.sqrt(var) if var>0 else 0.0
    volZ = zscore(vols[-1] if vols else 0.0, mu, sigma)

    return {
        "r300": round(r300, 4),
        "r600": round(r600, 4),
        "r1800": round(r1800, 4) if INCLUDE_R1800 else None,
        "dd300": round(dd300, 4),
        "volZ": round(volZ, 4),
    }

# =========================
# 🔎 دورة الاستكشاف/الإرسال
# =========================
last_markets_refresh = 0

def once_cycle():
    global last_markets_refresh

    now = time.time()
    # حدّث قائمة الأسواق كل 30 دقيقة
    if not SUPPORTED or (now - last_markets_refresh) > 1800:
        load_markets()
        last_markets_refresh = now

    # 1) جلب 24h
    tick = http_get("/v2/ticker/24h")
    if not tick:
        print("[CYCLE] /ticker/24h failed")
        return {"sent":0, "skipped":0, "candidates":0}

    # نظّم + احسب سيولة EUR
    pool = []
    for it in tick:
        m = norm_market(it.get("market",""))
        if not m.endswith(MARKET_SUFFIX): continue
        if m in MARKET_BLACKLIST: continue
        if not is_supported(m): continue

        last = float(it.get("last", it.get("lastPrice", 0.0)) or 0.0)
        vol  = float(it.get("volume", 0.0) or 0.0)
        pct24 = float(it.get("priceChangePercentage", 0.0) or 0.0)
        bid   = float(it.get("bid", 0.0) or 0.0)
        ask   = float(it.get("ask", 0.0) or 0.0)
        spread_bp = ( (ask - bid) / ((ask+bid)/2) * 10000 ) if (bid and ask) else SPREAD_FALLBACK_BP

        pool.append({
            "market": m,
            "symbol": m.split("-")[0],
            "last": last,
            "volume": vol,
            "eur_volume": last*vol,
            "pct24": pct24,
            "spread_bp": spread_bp
        })

    # 2) مرشحو سيولة واسعة
    pool.sort(key=lambda x: x["eur_volume"], reverse=True)
    liq_candidates = pool[:TOP_CANDIDATES]

    # أضف رادار أسبوعي دائمًا (حتى لو خارج أعلى السيولة)
    radar_list = [{"market":k, "symbol":k.split("-")[0], "forced":True} for k in RADAR.keys() if is_supported(k)]
    # ازالة التكرار مع الحفاظ على الترتيب
    seen = set()
    candidates = []
    for x in liq_candidates + radar_list:
        m = x["market"]
        if m not in seen:
            candidates.append(x); seen.add(m)

    # 3) احسب ميزات 1m للمرشحين على دفعات
    need_candles = candidates
    limit = max(VOLZ_BASE_N+1, 12, 31 if INCLUDE_R1800 else 20)
    feats = {}  # market -> dict
    for batch in chunks(need_candles, BATCH_SIZE):
        for x in batch:
            m = x["market"]
            cnd = read_candles_1m(m, limit=limit)
            if not cnd:
                continue
            f = feat_from_candles(cnd)
            if not f:
                continue
            # احسب r5m,r10m سريع (من نفس الشموع) لاستعمال الفلاتر المرنة
            closes = [float(c[4]) for c in cnd]
            if len(closes) >= 11:
                r5m = pct(closes[-1], closes[-6])
                r10m = pct(closes[-1], closes[-11])
            else:
                r5m = f["r300"]; r10m = f["r600"]

            # اجلب ملحقات 24h/سبريد من pool
            meta = next((p for p in pool if p["market"]==m), None)
            pct24 = float(meta["pct24"]) if meta else 0.0
            spread_bp = float(meta["spread_bp"]) if meta else SPREAD_FALLBACK_BP
            eur_liq_rank = pool.index(meta)+1 if meta in pool else 9999

            feats[m] = {
                "symbol": m.split("-")[0],
                "r5m": round(r5m, 4),
                "r10m": round(r10m, 4),
                "r300": f["r300"],
                "r600": f["r600"],
                "r1800": f.get("r1800"),
                "dd300": f["dd300"],
                "volZ": f["volZ"],
                "spread_bp": spread_bp,
                "pct24": pct24,
                "eur_liq_rank": eur_liq_rank
            }
        time.sleep(BATCH_SLEEP)

    # 4) فلتر 24h مرن + إدخال فوري
    filtered = []
    force_in = []
    for m, f in feats.items():
        # تجاوز 24h إذا r5m قوي
        if (f["pct24"] < EXCLUDE_24H_PCT) or (f["r5m"] >= ALLOW_STRONG_5M):
            filtered.append(m)
        # إدخال فوري
        if f["r5m"] >= THRESH_5M_INCLUDE or f["r10m"] >= THRESH_10M_INCLUDE or (f["volZ"] >= 1.4 and f["r5m"] >= 0.2):
            force_in.append(m)

    # رتّب حسب r5m ثم r10m
    ranked = sorted(filtered, key=lambda mm: (feats[mm]["r5m"], feats[mm]["r10m"]), reverse=True)

    wanted = []
    seen = set()
    for m in sorted(force_in, key=lambda mm: (feats[mm]["r5m"], feats[mm]["r10m"]), reverse=True):
        if m not in seen:
            wanted.append(m); seen.add(m)
    for m in ranked:
        if m not in seen:
            wanted.append(m); seen.add(m)

    # 5) إرسال CV إلى Bot B (ضعف سعة الغرفة تقريبًا)
    limit_send = max(48, int(os.getenv("ROOM_CAP", "24"))*2)
    sent = 0; skipped = 0

    for m in wanted[:limit_send]:
        f = feats[m]
        tags = []
        if f["r600"] >= 3.0 and f["dd300"] <= 1.0:
            tags.append("gradual_up")
        if f["r5m"] >= 0.6 and f["volZ"] >= 1.2:
            tags.append("burst")
        if f["pct24"] < EXCLUDE_24H_PCT and f["r5m"] >= 0.5:
            tags.append("fresh_move")

        # تحديث الرادار لو ظهر burst/gradual
        if "burst" in tags or "gradual_up" in tags:
            radar_touch(m, "burst" if "burst" in tags else "gradual")

        cv = {
            "market": m,
            "symbol": f["symbol"],
            "ts": int(time.time()),
            "feat": {
                "r300": f["r300"],
                "r600": f["r600"],
                "r1800": f.get("r1800"),
                "dd300": f["dd300"],
                "volZ": f["volZ"],
                "spread_bp": f["spread_bp"],
                "pct24": f["pct24"],
                "eur_liq_rank": f["eur_liq_rank"]
            },
            "tags": tags,
            "ttl_sec": 1800
        }

        # أرسل
        send_cv(cv)
        sent += 1

    # حاول تفريغ صف إعادة الإرسال
    flush_retry_queue()

    best = sorted(((m, feats[m]["r5m"]) for m in feats), key=lambda kv: kv[1], reverse=True)[:5]
    print("[CYCLE] candidates:", len(candidates),
          "have_feats:", len(feats),
          "picked:", min(limit_send, len(wanted)),
          "sent:", sent,
          "top5 r5m:", ", ".join(f"{k}:{v:+.2f}%" for k,v in best))

    return {"sent":sent, "skipped": skipped, "candidates": len(candidates)}

# =========================
# 🧵خيط التشغيل الدوري
# =========================
running = True
def loop_runner():
    while running:
        t0 = time.time()
        try:
            once_cycle()
        except Exception as e:
            print("[CYCLE] error:", e)
        # نوم حتى الدورة القادمة
        elapsed = time.time() - t0
        wait = max(5.0, CYCLE_SEC - elapsed)
        time.sleep(wait)

# =========================
# 🌐 Flask (صحة + تشغيل يدوي)
# =========================
app = Flask(__name__)

@app.route("/")
def root():
    return "Brave Explorer A is alive ✅"

@app.route("/health")
def health():
    return jsonify(ok=True, queue=len(retry_queue), markets=len(SUPPORTED), radar=len(RADAR))

@app.route("/once", methods=["POST","GET"])
def once():
    res = once_cycle()
    return jsonify(ok=True, **res)

# =========================
# ▶️ الإقلاع
# =========================
def start():
    threading.Thread(target=loop_runner, daemon=True).start()

start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)