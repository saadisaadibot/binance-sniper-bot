# -*- coding: utf-8 -*-
import os, time, requests, traceback, threading, random
from collections import deque, defaultdict
from threading import Thread, Lock, Event
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
BASE_URL             = os.getenv("BITVAVO_URL", "https://api.bitvavo.com/v2")
HTTP_TIMEOUT         = float(os.getenv("HTTP_TIMEOUT", 8.0))

SCAN_INTERVAL        = int(os.getenv("SCAN_INTERVAL", 5))         # فترة سحب الأسعار (ث)
MARKETS_REFRESH_SEC  = int(os.getenv("MARKETS_REFRESH_SEC", 60))  # تحديث قائمة الأسواق (ث)
MAX_ROOM             = int(os.getenv("MAX_ROOM", 20))              # حجم غرفة المراقبة
BATCH_INTERVAL_SEC   = int(os.getenv("BATCH_INTERVAL_SEC", 180))  # إعادة انتقاء الغرفة (ث)

RANK_FILTER          = int(os.getenv("RANK_FILTER", 10))           # لا إشعار إلا إذا Top N

# أنماط الإشارة
BASE_STEP_PCT        = float(os.getenv("BASE_STEP_PCT", 1.0))      # نمط top10: 1% + 1%
BASE_STRONG_SEQ      = os.getenv("BASE_STRONG_SEQ", "2,1,2")       # نمط top1: 2 ثم 1 ثم 2 %
SEQ_WINDOW_SEC       = int(os.getenv("SEQ_WINDOW_SEC", 300))       # نافذة النمط القوي
STEP_WINDOW_SEC      = int(os.getenv("STEP_WINDOW_SEC", 180))      # نافذة 1% + 1%

# حرارة السوق
HEAT_LOOKBACK_SEC    = int(os.getenv("HEAT_LOOKBACK_SEC", 120))    # كم ثانية نرجع لقياس الحركة
HEAT_RET_PCT         = float(os.getenv("HEAT_RET_PCT", 0.6))       # % تعتبر حركة ضمن نافذة الحرارة
HEAT_SMOOTH          = float(os.getenv("HEAT_SMOOTH", 0.3))        # EWMA

# منع السبام
ALERT_COOLDOWN_SEC   = int(os.getenv("BUY_COOLDOWN_SEC", 900))     # كولداون لكل عملة
GLOBAL_WARMUP_SEC    = int(os.getenv("GLOBAL_WARMUP_SEC", 30))     # إحماء

# تيليجرام
BOT_TOKEN            = os.getenv("BOT_TOKEN")
CHAT_ID              = os.getenv("CHAT_ID")

# لوج
DEBUG_LOG            = os.getenv("DEBUG_LOG", "0") == "1"
STATS_EVERY_SEC      = int(os.getenv("STATS_EVERY_SEC", 60))

# =========================
# 🧠 الحالة
# =========================
lock = Lock()
started = Event()

symbols_all_eur = []                       # كل الأزواج مقابل EUR
last_markets_refresh = 0

prices = defaultdict(lambda: deque())      # base -> deque[(ts, price)]
last_alert = {}                             # base -> ts
heat_ewma = 0.0
start_time = time.time()

# كاش آخر قراءة bulk
latest_price_map = {}                      # base -> price
last_bulk_ts = 0

# =========================
# 🛰️ دوال HTTP
# =========================
def http_get(url, params=None, timeout=HTTP_TIMEOUT):
    headers = {"User-Agent": "signals-only-predictor/1.0"}
    for _ in range(2):
        try:
            return requests.get(url, params=params, timeout=timeout, headers=headers)
        except Exception:
            time.sleep(0.4)
    return None

# =========================
# 🗺️ أسواق EUR
# =========================
def refresh_markets(now=None):
    global symbols_all_eur, last_markets_refresh
    now = now or time.time()
    if (now - last_markets_refresh) < MARKETS_REFRESH_SEC and symbols_all_eur:
        return
    resp = http_get(f"{BASE_URL}/markets")
    if not resp or resp.status_code != 200:
        return
    try:
        data = resp.json()
        symbols_all_eur = [
            m.get("base") for m in data
            if m.get("quote") == "EUR" and m.get("status") == "trading" and (m.get("base") or "").isalpha()
        ]
        last_markets_refresh = now
        if DEBUG_LOG:
            print(f"[MARKETS] EUR symbols: {len(symbols_all_eur)}")
    except Exception:
        pass

# =========================
# 💹 Bulk ticker
# =========================
def bulk_prices():
    """يعيد dict base->price لكل -EUR من الـ API دفعة واحدة."""
    resp = http_get(f"{BASE_URL}/ticker/price")
    out = {}
    if not resp or resp.status_code != 200:
        return out
    try:
        for row in resp.json():
            mk = row.get("market", "")
            if mk.endswith("-EUR"):
                base = mk.split("-")[0]
                try:
                    out[base] = float(row["price"])
                except Exception:
                    continue
    except Exception:
        pass
    return out

# =========================
# 🔥 حرارة السوق
# =========================
def compute_market_heat():
    """نسبة العملات التي تحركت ≥ HEAT_RET_PCT خلال HEAT_LOOKBACK_SEC، مع EWMA."""
    global heat_ewma
    now = time.time()
    moved = total = 0
    with lock:
        for base, dq in prices.items():
            if len(dq) < 2:
                continue
            # أقرب نقطة للـ lookback
            old = None
            for ts, pr in reversed(dq):
                if now - ts >= HEAT_LOOKBACK_SEC:
                    old = pr
                    break
            if old and old > 0:
                cur = dq[-1][1]
                ret = (cur - old) / old * 100.0
                total += 1
                if abs(ret) >= HEAT_RET_PCT:
                    moved += 1
    raw = (moved / total) if total else 0.0
    if total:
        heat_ewma = (1 - HEAT_SMOOTH) * heat_ewma + HEAT_SMOOTH * raw
    return heat_ewma

def adaptive_multipliers():
    """سوق بارد -> 0.75x | متوسط -> 0.9-1.0x | حامي -> 1.25x."""
    h = max(0.0, min(1.0, heat_ewma))
    if h < 0.15:   return 0.75
    if h < 0.35:   return 0.9
    if h < 0.60:   return 1.0
    return 1.25

# =========================
# 🧮 أدوات تغيّر 5m
# =========================
def pct_change_from_lookback(dq, lookback_sec, now):
    """يرجع التغيّر % من أقرب نقطة >= lookback_sec. 0 إن لم تتوفر."""
    if not dq:
        return 0.0
    cur = dq[-1][1]
    old = None
    for ts, pr in reversed(dq):
        if now - ts >= lookback_sec:
            old = pr
            break
    if old and old > 0:
        return (cur - old) / old * 100.0
    return 0.0

def top5m_from_histories(limit):
    """اختيار أفضل العملات حسب r5m من تاريخ الأسعار المحلي بدون ضرب API إضافي."""
    now = time.time()
    rows = []
    with lock:
        bases = list(symbols_all_eur) if symbols_all_eur else list(prices.keys())
        for base in bases:
            dq = prices.get(base)
            if not dq:
                continue
            r5m = pct_change_from_lookback(dq, 300, now)
            rows.append((base, r5m))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [b for b, _ in rows[:limit]]

def global_rank_map():
    """رتبة العملة ضمن كل ما لدينا من تواريخ (تقريب لسوق كامل EUR)."""
    now = time.time()
    rows = []
    with lock:
        for base, dq in prices.items():
            if not dq:
                continue
            r5m = pct_change_from_lookback(dq, 300, now)
            rows.append((base, r5m))
    rows.sort(key=lambda x: x[1], reverse=True)
    return {b: i+1 for i, (b, _) in enumerate(rows)}

# =========================
# 🧩 الأنماط (top10/top1)
# =========================
def check_top10_pattern(dq_snapshot, m):
    """1% + 1% داخل STEP_WINDOW_SEC (rolling)."""
    thresh = BASE_STEP_PCT * m
    if len(dq_snapshot) < 3:
        return False
    now = dq_snapshot[-1][0]
    window = [(ts, p) for ts, p in dq_snapshot if now - ts <= STEP_WINDOW_SEC]
    if len(window) < 3:
        return False

    # نسمح بأي نقطة بداية ضمن النافذة
    for i in range(len(window) - 2):
        p0 = window[i][1]
        step1 = False
        last_p = p0
        for j in range(i+1, len(window)):
            pr = window[j][1]
            ch1 = (pr - p0) / p0 * 100.0
            if not step1 and ch1 >= thresh:
                step1 = True
                last_p = pr
                continue
            if step1:
                ch2 = (pr - last_p) / last_p * 100.0
                if ch2 >= thresh:
                    return True
                # reset إذا انعكاس بنفس المقدار
                if (pr - last_p) / last_p * 100.0 <= -thresh:
                    break
    return False

def check_top1_pattern(dq_snapshot, m):
    """تسلسل نسب مثل '2,1,2' داخل SEQ_WINDOW_SEC مع سماحية تراجع صغيرة نسبية."""
    seq_parts = [float(x.strip()) for x in BASE_STRONG_SEQ.split(",") if x.strip()]
    if not seq_parts or len(dq_snapshot) < 3:
        return False
    seq_parts = [x * m for x in seq_parts]

    now = dq_snapshot[-1][0]
    window = [(ts, p) for ts, p in dq_snapshot if now - ts <= SEQ_WINDOW_SEC]
    if len(window) < 3:
        return False

    slack = 0.3 * m
    # جرّب أكثر من نقطة بداية لتفادي تفويت الانطلاقة
    for i in range(len(window) - 2):
        base_p = window[i][1]
        peak_after_step = base_p
        step_i = 0
        for j in range(i+1, len(window)):
            pr = window[j][1]
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
                    if drop <= -slack:
                        # إعادة ضبط
                        break
    return False

# =========================
# 📣 تنبيه تنبؤ
# =========================
def send_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[TG_DISABLED] {text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=HTTP_TIMEOUT
        )
    except Exception as e:
        print("Telegram error:", e)

def notify_signal(base, tag, rank_map):
    rank = rank_map.get(base, 999)
    if rank > RANK_FILTER:
        return
    now = time.time()
    if base in last_alert and now - last_alert[base] < ALERT_COOLDOWN_SEC:
        return
    last_alert[base] = now
    send_message(f"🔔 تنبؤ: {base} {tag} #top{rank}")

# =========================
# 🔁 العمال
# =========================
def price_poller():
    """bulk fetch → تحديث جميع الـ EUR (نحتفظ بقصّة 20 دقيقة)."""
    global latest_price_map, last_bulk_ts
    last_stats = 0
    misses = 0
    while True:
        try:
            refresh_markets()
            mp = bulk_prices()
            now = time.time()
            if mp:
                with lock:
                    latest_price_map = mp
                    last_bulk_ts = now
                    for base, price in mp.items():
                        if symbols_all_eur and base not in symbols_all_eur:
                            continue
                        dq = prices[base]
                        dq.append((now, price))
                        cutoff = now - 1200  # 20 دقيقة
                        while dq and dq[0][0] < cutoff:
                            dq.popleft()
            else:
                misses += 1

            if DEBUG_LOG and (time.time() - last_stats) >= STATS_EVERY_SEC:
                last_stats = time.time()
                with lock:
                    with_data = sum(1 for _, v in prices.items() if v)
                print(f"[POLL] entries={with_data} markets={len(symbols_all_eur)} misses={misses}")
                misses = 0
        except Exception as e:
            if DEBUG_LOG:
                print("[POLL][ERR]", type(e).__name__, str(e))
                traceback.print_exc()
        time.sleep(SCAN_INTERVAL)

def room_refresher():
    """اختيار الغرفة Top-5m من كل السوق اعتمادًا على التاريخ المحلي (بلا طلبات إضافية)."""
    global watchlist
    # نجعلها مجموعة مُدارة داخل هذه الدالة
    while True:
        try:
            # تأكّد أن عندنا بيانات كافية
            with lock:
                ok_data = sum(1 for _, dq in prices.items() if dq and (dq[-1][0] - dq[0][0]) >= 300)
            if ok_data == 0:
                time.sleep(5)
                continue

            top = top5m_from_histories(MAX_ROOM * 2)  # خذ ضعف العدد ثم قصّ
            # رتّب ثانيةً حسب r5m واحتفظ بـ MAX_ROOM
            now = time.time()
            scored = []
            with lock:
                for b in top:
                    dq = prices.get(b)
                    if not dq:
                        continue
                    scored.append((b, pct_change_from_lookback(dq, 300, now)))
            scored.sort(key=lambda x: x[1], reverse=True)
            selected = [b for b, _ in scored[:MAX_ROOM]]

            with lock:
                # خزّن كقائمة مراقبة ضمن dict (نستخلصها في analyzer)
                app.config["WATCHLIST"] = set(selected)

            if DEBUG_LOG:
                print(f"[ROOM] selected {len(selected)} symbols")

        except Exception as e:
            if DEBUG_LOG:
                print("[ROOM][ERR]", type(e).__name__, str(e))
                traceback.print_exc()
        time.sleep(BATCH_INTERVAL_SEC)

def analyzer():
    """يفحص الأنماط ويرسل تنبؤات. يشرح سبب الرفض على DEBUG."""
    while True:
        if time.time() - start_time < GLOBAL_WARMUP_SEC:
            time.sleep(1)
            continue
        try:
            compute_market_heat()
            m = adaptive_multipliers()
            # لقطة آمنة
            with lock:
                watchlist = set(app.config.get("WATCHLIST", set()))
                snapshots = {b: list(prices[b]) for b in watchlist if prices.get(b)}
            # خريطة رتبة عالمية
            rank_map = global_rank_map()

            for base, dq_snap in snapshots.items():
                # نظم الأسباب
                reason = None
                if check_top1_pattern(dq_snap, m):
                    notify_signal(base, "top1", rank_map)
                    continue
                if check_top10_pattern(dq_snap, m):
                    notify_signal(base, "top10", rank_map)
                    continue

                if DEBUG_LOG:
                    # تحليل مبسّط للأسباب: احسب r5m و step/seq حدود
                    now = dq_snap[-1][0]
                    r5m = pct_change_from_lookback(deque(dq_snap), 300, now)
                    print(f"[SKIP] {base} r5m={r5m:+.2f}% heat={heat_ewma:.2f} m={m:.2f}")
        except Exception as e:
            if DEBUG_LOG:
                print("[ANALYZER][ERR]", type(e).__name__, str(e))
                traceback.print_exc()
        time.sleep(1)

# =========================
# 🏁 تشغيل الخيوط
# =========================
def start_workers_once():
    if started.is_set():
        return
    with lock:
        if started.is_set():
            return
        Thread(target=price_poller,   daemon=True).start()
        Thread(target=room_refresher, daemon=True).start()
        Thread(target=analyzer,       daemon=True).start()
        started.set()
        if DEBUG_LOG:
            print("[BOOT] threads started")

# شغّل عند الاستيراد (مناسب لـ Gunicorn)
start_workers_once()

# =========================
# 🌐 Web
# =========================
@app.get("/")
def health():
    return "Signals-only predictor is alive ✅", 200

@app.get("/stats")
def stats():
    with lock:
        room = list(app.config.get("WATCHLIST", set()))
        with_data = sum(1 for _, v in prices.items() if v)
    return {
        "markets_tracked": len(symbols_all_eur),
        "symbols_with_data": with_data,
        "room_size": len(room),
        "room": room,
        "heat_ewma": round(heat_ewma, 4),
        "last_bulk_age": (time.time() - last_bulk_ts) if last_bulk_ts else None
    }, 200

@app.get("/peek")
def peek():
    """عينة صغيرة تُظهر قيم r5m وأسباب الرفض بشكل مبسّط."""
    now = time.time()
    sample = []
    with lock:
        room = list(app.config.get("WATCHLIST", set()))
        picks = random.sample(room, min(8, len(room)))
        snaps = {b: list(prices[b]) for b in picks if prices.get(b)}
    for b, dq in snaps.items():
        r5m = pct_change_from_lookback(deque(dq), 300, now)
        passed_top1 = check_top1_pattern(dq, adaptive_multipliers())
        passed_top10 = check_top10_pattern(dq, adaptive_multipliers())
        reason = "PASS_TOP1" if passed_top1 else ("PASS_TOP10" if passed_top10 else "no-pattern")
        sample.append({
            "symbol": b,
            "r5m": round(r5m, 3),
            "len": len(dq),
            "last_age_sec": int(now - dq[-1][0]),
            "reason": reason
        })
    return jsonify({
        "room_size": len(room),
        "heat": round(heat_ewma, 3),
        "sample": sample
    }), 200

@app.post("/webhook")
def telegram_webhook():
    data = request.json or {}
    msg = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()
    if not text:
        return "ok", 200

    STATUS_ALIASES = {"الحالة", "/status", "/stats", "شو عم تعمل", "/شو_عم_تعمل", "status"}
    if text in STATUS_ALIASES:
        # نص موجز للحالة
        with lock:
            room = list(app.config.get("WATCHLIST", set()))
        send_message(f"📊 room {len(room)}/{MAX_ROOM} | heat {heat_ewma:.2f}")
        return "ok", 200
    return "ok", 200

# =========================
# 🖥️ تشغيل محلي
# =========================
if __name__ == "__main__":
    start_workers_once()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))