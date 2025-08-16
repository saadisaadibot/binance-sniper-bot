# -*- coding: utf-8 -*-
import os, time, json, math, requests, redis
from collections import deque, defaultdict
from threading import Thread, Lock
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
SCAN_INTERVAL        = int(os.getenv("SCAN_INTERVAL", 5))        # كل كم ثانية نقرأ الأسعار
BATCH_INTERVAL_SEC   = int(os.getenv("BATCH_INTERVAL_SEC", 180))  # كل كم ثانية نحدّث الغرفة
MAX_ROOM             = int(os.getenv("MAX_ROOM", 20))             # حجم غرفة المراقبة
RANK_FILTER          = int(os.getenv("RANK_FILTER", 10))          # لا إشعار إلا إذا Top N عند الإرسال

# أنماط الإشعار الأساسية (قبل التكييف)
BASE_STEP_PCT        = float(os.getenv("BASE_STEP_PCT", 1.0))     # نمط top10: 1% + 1%
BASE_STRONG_SEQ      = os.getenv("BASE_STRONG_SEQ", "2,1,2")      # نمط top1: 2% ثم 1% ثم 2% خلال 5 دقائق
SEQ_WINDOW_SEC       = int(os.getenv("SEQ_WINDOW_SEC", 300))      # نافذة النمط القوي (ثواني)
STEP_WINDOW_SEC      = int(os.getenv("STEP_WINDOW_SEC", 180))     # نافذة 1% + 1% (ثواني)

# تكييف حسب حرارة السوق
HEAT_LOOKBACK_SEC    = int(os.getenv("HEAT_LOOKBACK_SEC", 120))   # نقيس الحرارة عبر آخر دقيقتين
HEAT_RET_PCT         = float(os.getenv("HEAT_RET_PCT", 0.6))      # كم % خلال 60 ث لنحسبها حركة
HEAT_SMOOTH          = float(os.getenv("HEAT_SMOOTH", 0.3))       # EWMA لنعومة الحرارة

# منع السبام
BUY_COOLDOWN_SEC     = int(os.getenv("BUY_COOLDOWN_SEC", 900))    # كولداون لكل عملة
GLOBAL_WARMUP_SEC    = int(os.getenv("GLOBAL_WARMUP_SEC", 30))    # مهلة إحماء بعد التشغيل

# توصيلات
BOT_TOKEN            = os.getenv("BOT_TOKEN")
CHAT_ID              = os.getenv("CHAT_ID")
REDIS_URL            = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# =========================
# 🧠 الحالة
# =========================
r = redis.from_url(REDIS_URL)
lock = Lock()
watchlist = set()                       # رموز مثل "ADA"
prices = defaultdict(lambda: deque())   # لكل رمز: deque[(ts, price)]
last_alert = {}                         # coin -> ts
heat_ewma = 0.0                         # حرارة السوق الملسّاة
start_time = time.time()

# =========================
# 🛰️ دوال مساعدة (Bitvavo)
# =========================
BASE_URL = "https://api.bitvavo.com/v2"

def http_get(url, params=None, timeout=8):
    # User-Agent بسيط لتحسين القبول من المزود
    headers = {"User-Agent": "predictor/1.0"}
    for _ in range(2):
        try:
            return requests.get(url, params=params, timeout=timeout, headers=headers)
        except Exception:
            time.sleep(0.5)
    return None

def get_price(symbol):  # symbol مثل "ADA"
    market = f"{symbol}-EUR"
    resp = http_get(f"{BASE_URL}/ticker/price", {"market": market})
    if not resp or resp.status_code != 200:
        return None
    try:
        data = resp.json()
        return float(data["price"])
    except Exception:
        return None

def get_5m_top_symbols(limit=MAX_ROOM):
    """
    نجمع أفضل العملات بفريم 5m اعتمادًا على الشموع (فرق الإغلاق الحالي عن إغلاق قبل 5m).
    """
    resp = http_get(f"{BASE_URL}/markets")
    if not resp or resp.status_code != 200:
        return []

    symbols = []
    try:
        markets = resp.json()
        for m in markets:
            if m.get("quote") == "EUR" and m.get("status") == "trading":
                base = m.get("base")
                if base and base.isalpha() and len(base) <= 6:
                    symbols.append(base)
    except Exception:
        pass

    # تغيّر 5m من deque + سعر حالي
    now = time.time()
    changes = []
    for base in symbols:
        dq = prices[base]
        old = None
        for ts, pr in reversed(dq):
            if now - ts >= 270:
                old = pr
                break
        cur = get_price(base)
        if cur is None:
            continue
        if old:
            ch = (cur - old) / old * 100.0
        else:
            ch = 0.0
        changes.append((base, ch))

        # تحديث السلسلة
        dq.append((now, cur))
        cutoff = now - 900
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    changes.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in changes[:limit]]

def get_rank_from_bitvavo(coin):
    """
    ترتيب العملة لحظيًا ضمن Top حسب تغيّر 5m المحلي (من deque).
    """
    now = time.time()
    scores = []
    for c in list(watchlist):
        dq = prices[c]
        old = None
        for ts, pr in reversed(dq):
            if now - ts >= 270:
                old = pr
                break
        cur = prices[c][-1][1] if dq else get_price(c)
        if cur is None:
            continue
        ch = ((cur - old) / old * 100.0) if old else 0.0
        scores.append((c, ch))

    scores.sort(key=lambda x: x[1], reverse=True)
    rank_map = {sym: i+1 for i, (sym, _) in enumerate(scores)}
    return rank_map.get(coin, 999)

def build_status_text():
    def pct_change_from_lookback(dq, lookback_sec, now_ts):
        if not dq:
            return 0.0
        cur = dq[-1][1]
        old = None
        for ts, pr in reversed(dq):
            if now_ts - ts >= lookback_sec:
                old = pr
                break
        if old and old > 0:
            return (cur - old) / old * 100.0
        return 0.0

    def drawdown_20m(dq, now_ts):
        if not dq:
            return 0.0
        cur = dq[-1][1]
        mx = max(pr for ts, pr in dq if now_ts - ts <= 1200) if dq else None
        if mx and mx > 0:
            return (cur - mx) / mx * 100.0
        return 0.0

    now = time.time()
    rows = []
    for c in list(watchlist):
        dq = prices[c]
        if not dq:
            continue
        r1m  = pct_change_from_lookback(dq, 60,  now)
        r5m  = pct_change_from_lookback(dq, 300, now)
        r15m = pct_change_from_lookback(dq, 900, now)
        dd20 = drawdown_20m(dq, now)
        rank = get_rank_from_bitvavo(c)
        rows.append((c, r1m, r5m, r15m, dd20, rank))

    rows.sort(key=lambda x: x[2], reverse=True)

    lines = []
    lines.append(f"📊 غرفة المراقبة: {len(watchlist)}/{MAX_ROOM} | Heat={heat_ewma:.2f}")
    if not rows:
        lines.append("— لا توجد بيانات كافية بعد.")
        return "\n".join(lines)

    for i, (c, r1m, r5m, r15m, dd20, rank) in enumerate(rows, 1):
        lines.append(
            f"{i:02d}. {c} #top{rank} | r1m {r1m:+.2f}% | r5m {r5m:+.2f}% | r15m {r15m:+.2f}% | DD20 {dd20:+.2f}%"
        )
        if i >= 30:
            break

    return "\n".join(lines)

# =========================
# 📣 تنبيه تنبؤ (بدون أي أمر شراء)
# =========================
def send_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[TG_DISABLED] {text}")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text})
    except Exception as e:
        print("Telegram error:", e)

def notify_signal(coin, tag, change_text=None):
    rank = get_rank_from_bitvavo(coin)
    if rank > RANK_FILTER:
        return
    now = time.time()
    if coin in last_alert and now - last_alert[coin] < BUY_COOLDOWN_SEC:
        return
    last_alert[coin] = now

    # صياغة “توقّع” بدل “اشتري”
    msg = f"🔔 تنبؤ: {coin} {tag} #top{rank}"
    if change_text:
        msg = f"🔔 تنبؤ: {coin} {change_text} #top{rank}"
    send_message(msg)

# =========================
# 🔥 حرارة السوق + تكييف
# =========================
def compute_market_heat():
    """
    حرارة السوق = نسبة العملات في الغرفة التي تحرّكت ≥ HEAT_RET_PCT خلال آخر 60ث.
    ثم EWMA لتنعيم القراءة.
    """
    global heat_ewma
    now = time.time()
    moved = 0
    total = 0
    for c in list(watchlist):
        dq = prices[c]
        if len(dq) < 2:
            continue
        old = None
        cur = dq[-1][1]
        for ts, pr in reversed(dq):
            if now - ts >= 60:
                old = pr
                break
        if old and old > 0:
            ret = (cur - old) / old * 100.0
            total += 1
            if abs(ret) >= HEAT_RET_PCT:
                moved += 1

    raw = (moved / total) if total else 0.0
    heat_ewma = (1-HEAT_SMOOTH)*heat_ewma + HEAT_SMOOTH*raw if total else heat_ewma
    return heat_ewma

def adaptive_multipliers():
    """
    سوق بارد -> 0.75x (أسهل) | متوسط -> 1.0x | ناري -> 1.25x (أصعب)
    """
    h = max(0.0, min(1.0, heat_ewma))
    if h < 0.15:   m = 0.75
    elif h < 0.35: m = 0.9
    elif h < 0.6:  m = 1.0
    else:          m = 1.25
    return m

# =========================
# 🧩 أنماط top10/top1
# =========================
def check_top10_pattern(coin, m):
    """نمط 1% + 1% خلال STEP_WINDOW_SEC (متكيّف بالمعامل m)."""
    thresh = BASE_STEP_PCT * m
    now = time.time()
    dq = prices[coin]
    if len(dq) < 2: return False
    start_ts = now - STEP_WINDOW_SEC
    window = [(ts, p) for ts, p in dq if ts >= start_ts]
    if len(window) < 3: return False

    p0 = window[0][1]
    step1 = False
    last_p = p0
    for ts, pr in window[1:]:
        ch1 = (pr - p0) / p0 * 100.0
        if not step1 and ch1 >= thresh:
            step1 = True
            last_p = pr
            continue
        if step1:
            ch2 = (pr - last_p) / last_p * 100.0
            if ch2 >= thresh:
                return True
            if (pr - last_p) / last_p * 100.0 <= -thresh:
                step1 = False
                p0 = pr
    return False

def check_top1_pattern(coin, m):
    """نمط قوي: تسلسل نسب مثل "2,1,2" خلال SEQ_WINDOW_SEC (متكيّف)."""
    seq_parts = [float(x.strip()) for x in BASE_STRONG_SEQ.split(",") if x.strip()]
    seq_parts = [x * m for x in seq_parts]
    now = time.time()
    dq = prices[coin]
    if len(dq) < 2: return False

    start_ts = now - SEQ_WINDOW_SEC
    window = [(ts, p) for ts, p in dq if ts >= start_ts]
    if len(window) < 3: return False

    slack = 0.3 * m  # سماحية تراجع بين الخطوات
    base_p = window[0][1]
    step_i = 0
    peak_after_step = base_p
    for ts, pr in window[1:]:
        ch = (pr - base_p) / base_p * 100.0
        need = seq_parts[step_i]
        if ch >= need:
            step_i += 1
            base_p = pr
            peak_after_step = pr
            if step_i == len(seq_parts):
                return True
        else:
            if peak_after_step > 0:
                drop = (pr - peak_after_step) / peak_after_step * 100.0
                if drop <= -(slack):
                    base_p = pr
                    peak_after_step = pr
                    step_i = 0
    return False

# =========================
# 🔁 العمال
# =========================
def room_refresher():
    while True:
        try:
            new_syms = get_5m_top_symbols(limit=MAX_ROOM)
            with lock:
                for s in new_syms:
                    watchlist.add(s)
                if len(watchlist) > MAX_ROOM:
                    ranked = sorted(list(watchlist), key=lambda c: get_rank_from_bitvavo(c))
                    watchlist.clear()
                    for c in ranked[:MAX_ROOM]:
                        watchlist.add(c)
        except Exception as e:
            print("room_refresher error:", e)
        time.sleep(BATCH_INTERVAL_SEC)

def price_poller():
    while True:
        now = time.time()
        with lock:
            syms = list(watchlist)
        for s in syms:
            pr = get_price(s)
            if pr is None:
                continue
            dq = prices[s]
            dq.append((now, pr))
            cutoff = now - 1200  # 20 دقيقة
            while dq and dq[0][0] < cutoff:
                dq.popleft()
        time.sleep(SCAN_INTERVAL)

def analyzer():
    while True:
        if time.time() - start_time < GLOBAL_WARMUP_SEC:
            time.sleep(1); continue
        try:
            compute_market_heat()
            m = adaptive_multipliers()
            with lock:
                syms = list(watchlist)
            for s in syms:
                if check_top1_pattern(s, m):
                    notify_signal(s, tag="top1")
                    continue
                if check_top10_pattern(s, m):
                    notify_signal(s, tag="top10")
        except Exception as e:
            print("analyzer error:", e)
        time.sleep(1)

# =========================
# 🌐 Web
# =========================
@app.route("/", methods=["GET"])
def health():
    return "Predictor bot (signals-only) ✅", 200

@app.route("/stats", methods=["GET"])
def stats():
    return {
        "watchlist": list(watchlist),
        "heat": round(heat_ewma, 4),
        "roomsz": len(watchlist)
    }, 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json or {}
    msg = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()
    if not text:
        return "ok", 200

    STATUS_ALIASES = {"الحالة", "/status", "/stats", "شو عم تعمل", "/شو_عم_تعمل", "status"}
    if text in STATUS_ALIASES:
        send_message(build_status_text())
        return "ok", 200
    return "ok", 200

# =========================
# 🚀 التشغيل
# =========================
_started = False
def start_workers_once():
    global _started
    if _started:
        return
    Thread(target=room_refresher, daemon=True).start()
    Thread(target=price_poller,   daemon=True).start()
    Thread(target=analyzer,       daemon=True).start()
    _started = True

# شغّل الخيوط فور الاستيراد (يناسب Gunicorn) + تأكيد قبل كل طلب
start_workers_once()
@app.before_request
def _ensure_started():
    start_workers_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))