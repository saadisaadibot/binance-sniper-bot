# -*- coding: utf-8 -*-
import os, time, requests, traceback, random, math
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

SCAN_INTERVAL        = int(os.getenv("SCAN_INTERVAL", 5))           # سحب الأسعار (ث)
MARKETS_REFRESH_SEC  = int(os.getenv("MARKETS_REFRESH_SEC", 60))    # تحديث قائمة الأسواق
MAX_ROOM             = int(os.getenv("MAX_ROOM", 20))               # حجم غرفة المراقبة
BATCH_INTERVAL_SEC   = int(os.getenv("BATCH_INTERVAL_SEC", 180))    # إعادة اختيار الغرفة

RANK_FILTER          = int(os.getenv("RANK_FILTER", 10))            # لا إشعار إلا إذا ضمن Top N

# أنماط الإشارة (أسلوبك الحالي)
BASE_STEP_PCT        = float(os.getenv("BASE_STEP_PCT", 1.0))       # نمط top10: 1% + 1%
BASE_STRONG_SEQ      = os.getenv("BASE_STRONG_SEQ", "2,1,2")        # نمط top1: 2 ثم 1 ثم 2 %
SEQ_WINDOW_SEC       = int(os.getenv("SEQ_WINDOW_SEC", 300))        # نافذة النمط القوي
STEP_WINDOW_SEC      = int(os.getenv("STEP_WINDOW_SEC", 180))       # نافذة 1% + 1%

# حرارة السوق
HEAT_LOOKBACK_SEC    = int(os.getenv("HEAT_LOOKBACK_SEC", 120))     # نافذة الحرارة
HEAT_RET_PCT         = float(os.getenv("HEAT_RET_PCT", 0.6))        # % تعتبر حركة ضمن نافذة الحرارة
HEAT_SMOOTH          = float(os.getenv("HEAT_SMOOTH", 0.3))         # EWMA

# متابعة 10 دقائق (أعلى قمة)
FOLLOWUP_WINDOW_SEC  = int(os.getenv("FOLLOWUP_WINDOW_SEC", 600))   # 10 دقائق
TARGET_PCT           = float(os.getenv("TARGET_PCT", 2.0))          # هدف +% يعتبر نجاح
# لا ستوب مبكر؛ نقيس أعلى قمة فقط ضمن 10 دقائق

# مضاد السيل/التكرار
ALERT_COOLDOWN_SEC   = int(os.getenv("ALERT_COOLDOWN_SEC", 900))    # كولداون لكل عملة
FLOOD_WINDOW_SEC     = int(os.getenv("FLOOD_WINDOW_SEC", 30))
FLOOD_MAX_PER_WINDOW = int(os.getenv("FLOOD_MAX_PER_WINDOW", 12))   # عدواني شوي
DEDUP_SEC            = int(os.getenv("DEDUP_SEC", 5))

# إحماء
GLOBAL_WARMUP_SEC    = int(os.getenv("GLOBAL_WARMUP_SEC", 30))

# تلغراف
BOT_TOKEN            = os.getenv("BOT_TOKEN")
CHAT_ID              = os.getenv("CHAT_ID")

# تلخيص
SUMMARY_MAX_LINES    = int(os.getenv("SUMMARY_MAX_LINES", 60))      # لكل قائمة (نجاح/فشل)

# لوج
DEBUG_LOG            = os.getenv("DEBUG_LOG", "0") == "1"
STATS_EVERY_SEC      = int(os.getenv("STATS_EVERY_SEC", 60))

# =========================
# 🧠 الحالة
# =========================
lock = Lock()
started = Event()

symbols_all_eur = []                     # كل الأزواج مقابل EUR
last_markets_refresh = 0

# تاريخ لحظي (كل ثوانٍ)
prices = defaultdict(lambda: deque())    # base -> deque[(ts, price)]
# لقطات دقيقة لساعة+ (اتجاه الساعة)
minute_snapshots = defaultdict(lambda: deque(maxlen=90))  # base -> deque[(ts, price)]
last_snapshot_minute = -1

heat_ewma = 0.0
start_time = time.time()
latest_price_map = {}
last_bulk_ts = 0

# إشعارات/أنماط
last_alert_ts = {}                       # base -> ts (كولداون)
pattern_state = defaultdict(lambda: {"top1": False, "top10": False})

# مضاد السيل/التكرار
from collections import deque as _deque
flood_times = _deque()
last_msg = {"text": None, "ts": 0.0}

# متابعة 10 دقائق
open_preds = {}          # base -> dict(time, start_price, high_price, tag, hour_change, status=None)
history_results = deque(maxlen=1000)    # [(ts, base, tag, status, target, best_change, hour_change)]
learning_window = deque(maxlen=60)      # آخر 60 نتيجة "hit"/"miss"

# =========================
# 🛰️ HTTP
# =========================
def http_get(url, params=None, timeout=HTTP_TIMEOUT):
    headers = {"User-Agent": "signals-only-predictor/1.2"}
    for _ in range(2):
        try:
            return requests.get(url, params=params, timeout=timeout, headers=headers)
        except Exception:
            time.sleep(0.4)
    return None

# =========================
# أسواق EUR + Bulk ticker
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

def bulk_prices():
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
    global heat_ewma
    now = time.time()
    moved = total = 0
    with lock:
        for base, dq in prices.items():
            if len(dq) < 2: continue
            old = None
            for ts, pr in reversed(dq):
                if now - ts >= HEAT_LOOKBACK_SEC:
                    old = pr; break
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
    h = max(0.0, min(1.0, heat_ewma))
    if h < 0.15:   return 0.75
    if h < 0.35:   return 0.9
    if h < 0.60:   return 1.0
    return 1.25

# =========================
# أدوات r5m + ترتيب + اتجاه ساعة
# =========================
def pct_change_from_lookback(dq, lookback_sec, now):
    if not dq: return 0.0
    cur = dq[-1][1]
    old = None
    for ts, pr in reversed(dq):
        if now - ts >= lookback_sec:
            old = pr; break
    if old and old > 0:
        return (cur - old) / old * 100.0
    return 0.0

def top5m_from_histories(limit):
    now = time.time()
    rows = []
    with lock:
        bases = list(symbols_all_eur) if symbols_all_eur else list(prices.keys())
        for base in bases:
            dq = prices.get(base)
            if not dq: continue
            r5m = pct_change_from_lookback(dq, 300, now)
            rows.append((base, r5m))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [b for b, _ in rows[:limit]]

def global_rank_map():
    now = time.time()
    rows = []
    with lock:
        for base, dq in prices.items():
            if not dq: continue
            r5m = pct_change_from_lookback(dq, 300, now)
            rows.append((base, r5m))
    rows.sort(key=lambda x: x[1], reverse=True)
    return {b: i+1 for i, (b, _) in enumerate(rows)}

def hour_change_at(base, at_ts, at_price):
    """يعيد تغير % خلال ساعة قبل لحظة الإشارة باستخدام لقاطات الدقيقة."""
    snaps = minute_snapshots.get(base)
    if not snaps:
        return None
    target = at_ts - 3600
    ref = None
    # اختر أقرب لقطة <= target
    for ts, pr in reversed(snaps):
        if ts <= target:
            ref = pr; break
    if ref and ref > 0:
        return (at_price - ref) / ref * 100.0
    return None

# =========================
# الأنماط (top10/top1)
# =========================
def check_top10_pattern(dq_snapshot, m):
    thresh = BASE_STEP_PCT * m
    if len(dq_snapshot) < 3: return False
    now = dq_snapshot[-1][0]
    window = [(ts, p) for ts, p in dq_snapshot if now - ts <= STEP_WINDOW_SEC]
    if len(window) < 3: return False

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
                if (pr - last_p) / last_p * 100.0 <= -thresh:
                    break
    return False

def check_top1_pattern(dq_snapshot, m):
    seq_parts = [float(x.strip()) for x in BASE_STRONG_SEQ.split(",") if x.strip()]
    if not seq_parts or len(dq_snapshot) < 3: return False
    seq_parts = [x * m for x in seq_parts]

    now = dq_snapshot[-1][0]
    window = [(ts, p) for ts, p in dq_snapshot if now - ts <= SEQ_WINDOW_SEC]
    if len(window) < 3: return False

    slack = 0.3 * m
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
                        break
    return False

# =========================
# تنبيه + متابعة 10 دقائق (أعلى قمة)
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
    # كولداون لكل عملة
    now = time.time()
    if base in last_alert_ts and now - last_alert_ts[base] < ALERT_COOLDOWN_SEC:
        return
    # رتبة
    rank = rank_map.get(base, 999)
    if rank > RANK_FILTER:
        return

    # Flood control عالمي
    while flood_times and now - flood_times[0] > FLOOD_WINDOW_SEC:
        flood_times.popleft()
    if len(flood_times) >= FLOOD_MAX_PER_WINDOW:
        return

    # تحضير رسالة
    with lock:
        dq = prices.get(base)
        if not dq: return
        start_price = dq[-1][1]
        hc = hour_change_at(base, now, start_price)

    msg_suffix = f" | 1h {hc:+.2f}%" if hc is not None else ""
    msg = f"🔔 تنبؤ: {base} {tag} #top{rank}{msg_suffix}"

    # Dedup
    if last_msg["text"] == msg and (now - last_msg["ts"]) < DEDUP_SEC:
        return

    # افتح متابعة 10 دقائق بأعلى قمة
    with lock:
        open_preds[base] = {
            "time": now,
            "start_price": start_price,
            "high_price": start_price,
            "tag": tag,
            "hour_change": hc,
            "status": None
        }

    # سجّل وإرسل
    last_alert_ts[base] = now
    flood_times.append(now)
    last_msg["text"], last_msg["ts"] = msg, now
    send_message(msg)

def evaluate_open_predictions():
    """تحدّث أعلى قمة لمدة 10 دقائق، وتغلق بنتيجة عند انتهاء النافذة."""
    now = time.time()
    to_close = []
    with lock:
        for base, pred in list(open_preds.items()):
            if pred["status"] is not None:
                continue
            dq = prices.get(base)
            if not dq: 
                continue
            cur = dq[-1][1]
            if cur > pred["high_price"]:
                pred["high_price"] = cur

            elapsed = now - pred["time"]
            if elapsed >= FOLLOWUP_WINDOW_SEC:
                best_change = (pred["high_price"] - pred["start_price"]) / pred["start_price"] * 100.0
                status = "✅ أصابت" if best_change >= TARGET_PCT else "❌ خابت"
                pred["status"] = status
                history_results.append((now, base, pred["tag"], status, TARGET_PCT, best_change, pred["hour_change"]))
                learning_window.append("hit" if "✅" in status else "miss")
                to_close.append(base)
        for b in to_close:
            open_preds.pop(b, None)

# =========================
# 🔁 العمال
# =========================
def price_poller():
    """bulk fetch → تحديث جميع الـ EUR + لقطات دقيقة لاتجاه الساعة."""
    global latest_price_map, last_bulk_ts, last_snapshot_minute
    last_stats, misses = 0, 0
    while True:
        try:
            refresh_markets()
            mp = bulk_prices()
            now = time.time()
            if mp:
                with lock:
                    latest_price_map = mp
                    last_bulk_ts = now
                    minute = int(now // 60)
                    for base, price in mp.items():
                        if symbols_all_eur and base not in symbols_all_eur:
                            continue
                        dq = prices[base]
                        dq.append((now, price))
                        # احتفاظ ~20 دقيقة للتنبؤ اللحظي (يكفي للأنماط)
                        cutoff = now - 1200
                        while dq and dq[0][0] < cutoff:
                            dq.popleft()
                    # لقطة دقيقة لكل الرموز (لاتجاه الساعة)
                    if minute != last_snapshot_minute:
                        for base, price in mp.items():
                            minute_snapshots[base].append((now, price))
                        last_snapshot_minute = minute
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
    """اختيار الغرفة Top-5m من التاريخ المحلي (بلا طلبات إضافية)."""
    while True:
        try:
            with lock:
                ok_data = sum(1 for _, dq in prices.items() if dq and (dq[-1][0] - dq[0][0]) >= 300)
            if ok_data == 0:
                time.sleep(5); continue

            top = top5m_from_histories(MAX_ROOM * 2)
            now = time.time()
            scored = []
            with lock:
                for b in top:
                    dq = prices.get(b)
                    if not dq: continue
                    scored.append((b, pct_change_from_lookback(dq, 300, now)))
            scored.sort(key=lambda x: x[1], reverse=True)
            selected = [b for b, _ in scored[:MAX_ROOM]]

            with lock:
                app.config["WATCHLIST"] = set(selected)

            if DEBUG_LOG:
                print(f"[ROOM] selected {len(selected)} symbols")
        except Exception as e:
            if DEBUG_LOG:
                print("[ROOM][ERR]", type(e).__name__, str(e))
                traceback.print_exc()
        time.sleep(BATCH_INTERVAL_SEC)

def analyzer():
    """Edge-trigger + متابعة + تكيّف."""
    while True:
        if time.time() - start_time < GLOBAL_WARMUP_SEC:
            time.sleep(1); continue
        try:
            compute_market_heat()
            m = adaptive_multipliers()

            # لقطة آمنة
            with lock:
                room = set(app.config.get("WATCHLIST", set()))
                snapshots = {b: list(prices[b]) for b in room if prices.get(b)}

            rank_map = global_rank_map()

            # قيّم المتابعات المفتوحة دائمًا
            evaluate_open_predictions()

            # Edge-trigger
            for base, dq_snap in snapshots.items():
                prev = pattern_state[base]
                cur_top1  = check_top1_pattern(dq_snap, m)
                cur_top10 = False if cur_top1 else check_top10_pattern(dq_snap, m)

                if cur_top1 and not prev["top1"]:
                    notify_signal(base, "top1", rank_map)
                elif cur_top10 and not prev["top10"]:
                    notify_signal(base, "top10", rank_map)

                pattern_state[base]["top1"]  = cur_top1
                pattern_state[base]["top10"] = cur_top10

            # تكيّف ذاتي بسيط
            adapt_thresholds()

        except Exception as e:
            if DEBUG_LOG:
                print("[ANALYZER][ERR]", type(e).__name__, str(e))
                traceback.print_exc()
        time.sleep(1)

def adapt_thresholds():
    """يشد/يرخي الأنماط حسب نسبة النجاح مؤخراً (عدواني)."""
    total = len(learning_window)
    if total < 12:
        return
    hits = sum(1 for x in learning_window if x == "hit")
    rate = hits / total

    global BASE_STEP_PCT, BASE_STRONG_SEQ
    # عدواني: تغييرات أكبر قليلاً، لكن محدودة
    if rate < 0.35:
        BASE_STEP_PCT = min(round(BASE_STEP_PCT + 0.15, 2), 2.0)
        parts = [float(x) for x in BASE_STRONG_SEQ.split(",")]
        parts[0] = min(parts[0] + 0.25, 3.2)
        BASE_STRONG_SEQ = ",".join(f"{x:.2f}".rstrip('0').rstrip('.') for x in parts[:3])
    elif rate > 0.70:
        BASE_STEP_PCT = max(round(BASE_STEP_PCT - 0.1, 2), 0.6)
        parts = [float(x) for x in BASE_STRONG_SEQ.split(",")]
        parts[0] = max(parts[0] - 0.2, 1.2)
        BASE_STRONG_SEQ = ",".join(f"{x:.2f}".rstrip('0').rstrip('.') for x in parts[:3])

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

start_workers_once()

# =========================
# 🌐 Web & Telegram
# =========================
@app.get("/")
def health():
    return "Signals-only predictor (10m peak + learning) ✅", 200

@app.get("/stats")
def stats():
    with lock:
        room = list(app.config.get("WATCHLIST", set()))
        with_data = sum(1 for _, v in prices.items() if v)
        open_n = sum(1 for v in open_preds.values() if v.get("status") is None)
    # معدل نجاح تقريبي
    total = len(history_results)
    hits = sum(1 for *_, status, __, ___, ____ in history_results if "✅" in status)
    rate = (hits / total * 100.0) if total else None
    return {
        "markets_tracked": len(symbols_all_eur),
        "symbols_with_data": with_data,
        "room_size": len(room),
        "open_predictions": open_n,
        "heat_ewma": round(heat_ewma, 4),
        "last_bulk_age": (time.time() - last_bulk_ts) if last_bulk_ts else None,
        "base_step_pct": BASE_STEP_PCT,
        "base_strong_seq": BASE_STRONG_SEQ,
        "win_rate_pct": round(rate, 1) if rate is not None else None
    }, 200

@app.get("/peek")
def peek():
    now = time.time()
    sample = []
    with lock:
        room = list(app.config.get("WATCHLIST", set()))
        picks = random.sample(room, min(8, len(room)))
        snaps = {b: list(prices[b]) for b in picks if prices.get(b)}
    for b, dq in snaps.items():
        r5m = pct_change_from_lookback(deque(dq), 300, now)
        m = adaptive_multipliers()
        p1 = check_top1_pattern(dq, m)
        p10 = (not p1) and check_top10_pattern(dq, m)
        sample.append({
            "symbol": b, "r5m": round(r5m, 3),
            "len": len(dq), "last_age_sec": int(now - dq[-1][0]),
            "top1": bool(p1), "top10": bool(p10)
        })
    return jsonify({
        "room_size": len(room),
        "heat": round(heat_ewma, 3),
        "sample": sample
    }), 200

def send_summary():
    """ملخص: أرقام عامة + قوائم نجاح/فشل (محدودة للطول)."""
    total = len(history_results)
    if total == 0:
        send_message("📊 لا توجد نتائج بعد."); return
    hits = [(ts, b, t, s, ex, act, hc) for (ts, b, t, s, ex, act, hc) in history_results if "✅" in s]
    misses = [(ts, b, t, s, ex, act, hc) for (ts, b, t, s, ex, act, hc) in history_results if "❌" in s]
    rate = (len(hits) / total) * 100.0

    def fmt(row):
        _, b, tag, status, ex, act, hc = row
        hc_txt = f" | 1h {hc:+.2f}%" if hc is not None else ""
        return f"{b} [{tag}]: {status} | هدف {ex:+.2f}% | أفضل {act:+.2f}%{hc_txt}"

    # قصّ معقول لعدم تجاوز حد رسائل تلغرام
    show_h = hits[-SUMMARY_MAX_LINES:]
    show_m = misses[-SUMMARY_MAX_LINES:]

    lines = [
        f"📊 الملخص (آخر {total} إشارة متبوعة):",
        f"أصابت: {len(hits)} | خابت: {len(misses)} | نجاح: {rate:.1f}%",
        "",
        f"✅ الناجحة (آخر {len(show_h)}):"
    ]
    lines += [fmt(x) for x in show_h]
    lines += ["", f"❌ الخائبة (آخر {len(show_m)}):"]
    lines += [fmt(x) for x in show_m]

    send_message("\n".join(lines))

@app.post("/webhook")
def telegram_webhook():
    data = request.json or {}
    msg = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()
    if not text:
        return "ok", 200
    if text in {"الملخص", "/summary"}:
        send_summary(); return "ok", 200
    if text in {"الحالة", "/status", "/stats", "شو عم تعمل", "/شو_عم_تعمل", "status"}:
        with lock:
            room = list(app.config.get("WATCHLIST", set()))
            open_n = sum(1 for v in open_preds.values() if v.get("status") is None)
        send_message(f"📊 room {len(room)}/{MAX_ROOM} | open {open_n} | heat {heat_ewma:.2f}")
        return "ok", 200
    return "ok", 200

# =========================
# 🖥️ تشغيل
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

if __name__ == "__main__":
    start_workers_once()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))