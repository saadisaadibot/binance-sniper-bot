# -*- coding: utf-8 -*-
import os, time, math, random, requests, traceback
from collections import deque, defaultdict
from threading import Thread, Lock, Event
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# =========================
# Boot
# =========================
load_dotenv()
app = Flask(__name__)

# =========================
# ⚙️ الإعدادات (قابلة للتعديل عبر .env)
# =========================
BASE_URL             = os.getenv("BITVAVO_URL", "https://api.bitvavo.com/v2")
HTTP_TIMEOUT         = float(os.getenv("HTTP_TIMEOUT", 8.0))

SCAN_INTERVAL        = int(os.getenv("SCAN_INTERVAL", 5))           # سحب الأسعار (ث)
MARKETS_REFRESH_SEC  = int(os.getenv("MARKETS_REFRESH_SEC", 60))    # تحديث قائمة الأسواق
MAX_ROOM             = int(os.getenv("MAX_ROOM", 24))               # حجم غرفة المراقبة
RESELECT_EVERY_SEC   = int(os.getenv("RESELECT_EVERY_SEC", 180))    # إعادة انتقاء الغرفة

RANK_FILTER          = int(os.getenv("RANK_FILTER", 10))            # لا إشعار إلا إذا ضمن Top N

# أنماط الإشارة الأساسية
BASE_STEP_PCT        = float(os.getenv("BASE_STEP_PCT", 1.0))       # top10: 1% + 1%
BASE_STRONG_SEQ      = os.getenv("BASE_STRONG_SEQ", "2,1,2")        # top1: 2 ثم 1 ثم 2 %
SEQ_WINDOW_SEC       = int(os.getenv("SEQ_WINDOW_SEC", 300))        # نافذة النمط القوي
STEP_WINDOW_SEC      = int(os.getenv("STEP_WINDOW_SEC", 180))       # نافذة 1% + 1%

# حرارة السوق
HEAT_LOOKBACK_SEC    = int(os.getenv("HEAT_LOOKBACK_SEC", 120))
HEAT_RET_PCT         = float(os.getenv("HEAT_RET_PCT", 0.6))
HEAT_SMOOTH          = float(os.getenv("HEAT_SMOOTH", 0.3))

# متابعة 10 دقائق (أعلى قمة)
FOLLOWUP_WINDOW_SEC  = int(os.getenv("FOLLOWUP_WINDOW_SEC", 600))   # 10 دقائق
TARGET_PCT           = float(os.getenv("TARGET_PCT", 2.0))          # نجاح عند أفضل قمة ≥ +2%

# مضاد السيل/التكرار
ALERT_COOLDOWN_SEC   = int(os.getenv("ALERT_COOLDOWN_SEC", 420))    # كولداون لكل عملة
FLOOD_WINDOW_SEC     = int(os.getenv("FLOOD_WINDOW_SEC", 30))
FLOOD_MAX_PER_WINDOW = int(os.getenv("FLOOD_MAX_PER_WINDOW", 12))
DEDUP_SEC            = int(os.getenv("DEDUP_SEC", 5))

# إحماء
GLOBAL_WARMUP_SEC    = int(os.getenv("GLOBAL_WARMUP_SEC", 15))

# حدود أمان للتكيّف كل N
ADAPT_EVERY_N        = int(os.getenv("ADAPT_EVERY_N", 10))
ADAPT_WIN_LOW        = float(os.getenv("ADAPT_WIN_LOW", 0.40))
ADAPT_WIN_HIGH       = float(os.getenv("ADAPT_WIN_HIGH", 0.70))
STEP_MIN, STEP_MAX   = float(os.getenv("STEP_MIN", 0.6)), float(os.getenv("STEP_MAX", 2.0))
SEQ0_MIN, SEQ0_MAX   = float(os.getenv("SEQ0_MIN", 1.2)), float(os.getenv("SEQ0_MAX", 3.2))

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

symbols_all_eur = []                         # كل الأزواج مقابل EUR
last_markets_refresh = 0
start_time = time.time()

# تدفّق لحظي  (ثواني ~ 20 دقيقة)
prices = defaultdict(lambda: deque())        # base -> deque[(ts, price)]

# شموع 1 دقيقة (لـ 90 دقيقة)
candles = defaultdict(lambda: dict())        # base -> {minute:int -> (o,h,l,c)}
minute_order = defaultdict(lambda: deque(maxlen=120))  # ترتيب المفاتيح
last_snapshot_minute = -1

# حرارة سوق
heat_ewma = 0.0
latest_price_map = {}
last_bulk_ts = 0

# حالة الأنماط و الإشعارات
pattern_state = defaultdict(lambda: {"top1": False, "top10": False})
last_alert_ts = {}
from collections import deque as _deque
flood_times = _deque()
last_msg = {"text": None, "ts": 0.0}

# متابعة 10 دقائق
open_preds = {}           # base -> dict(time, start_price, high_price, tag, hour_change, status)
history_results = deque(maxlen=1000)
learning_window = deque(maxlen=60)
last_adapt_total = 0

# خطوط أساس إحصائية (يتم تحديثها كل دقيقة)
PCTL_R1M = 0.25
PCTL_ACCEL = 0.10
PCTL_RNG3 = 0.80

# =========================
# 🛰️ HTTP
# =========================
def http_get(url, params=None, timeout=HTTP_TIMEOUT):
    headers = {"User-Agent": "signals-final/1.0"}
    for _ in range(2):
        try:
            return requests.get(url, params=params, timeout=timeout, headers=headers)
        except Exception:
            time.sleep(0.4)
    return None

# =========================
# Utilities
# =========================
def pct(a, b):
    try:
        return (a - b) / b * 100.0 if b else 0.0
    except Exception:
        return 0.0

def calc_percentile(values, p):
    if not values:
        return 0.0
    v = sorted(values)
    k = max(0, min(len(v) - 1, int(round(p * (len(v) - 1)))))
    return v[k]

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
# بناء شموع 1 دقيقة + r5m + ترتيب
# =========================
def pct_change_from_lookback(dq, lookback_sec, now):
    if not dq: return 0.0
    cur = dq[-1][1]
    old = None
    for ts, pr in reversed(dq):
        if now - ts >= lookback_sec:
            old = pr; break
    return pct(cur, old) if old else 0.0

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

def one_min_returns(base):
    mins = list(minute_order[base])
    if len(mins) < 2: return []
    rets = []
    for i in range(1, len(mins)):
        c_now = candles[base][mins[i]][3]
        c_prev= candles[base][mins[i-1]][3]
        if c_prev: rets.append(pct(c_now, c_prev))
    return rets

# =========================
# خطوط أساس إحصائية (تُحدَّث كل دقيقة)
# =========================
def refresh_baselines():
    global PCTL_R1M, PCTL_ACCEL, PCTL_RNG3
    with lock:
        bases = list(app.config.get("WATCHLIST", set())) or list(candles.keys())
    r1_list, accel_list, range3_list = [], [], []
    for b in bases:
        mins = list(minute_order[b])
        if len(mins) < 6:
            continue
        r1 = one_min_returns(b)
        if len(r1) >= 2:
            r1_list += r1[-10:]
            accel_list += [r1[-1] - r1[-2]]
        prev3 = mins[-4:-1]
        if len(prev3) == 3:
            highs = [candles[b][m][1] for m in prev3]
            lows  = [candles[b][m][2] for m in prev3]
            if min(lows) > 0:
                range3_list.append(pct(max(highs), min(lows)))
    PCTL_R1M  = calc_percentile(r1_list, 0.90) or 0.25
    PCTL_ACCEL= calc_percentile(accel_list, 0.80) or 0.10
    PCTL_RNG3 = calc_percentile(range3_list, 0.90) or 0.80

# =========================
# الأنماط (top10/top1) – تعمل على تدفق الثواني
# =========================
def check_top10_pattern(dq_snapshot, m):
    thresh = BASE_STEP_PCT * m
    if len(dq_snapshot) < 3: return False
    now = dq_snapshot[-1][0]
    window = [(ts, p) for ts, p in dq_snapshot if now - ts <= STEP_WINDOW_SEC]
    if len(window) < 3: return False
    for i in range(len(window) - 2):
        p0 = window[i][1]; step1 = False; last_p = p0
        for j in range(i+1, len(window)):
            pr = window[j][1]
            ch1 = pct(pr, p0)
            if not step1 and ch1 >= thresh:
                step1 = True; last_p = pr; continue
            if step1:
                ch2 = pct(pr, last_p)
                if ch2 >= thresh: return True
                if pct(pr, last_p) <= -thresh: break
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
        base_p = window[i][1]; peak_after_step = base_p; step_i = 0
        for j in range(i+1, len(window)):
            pr = window[j][1]
            ch = pct(pr, base_p)
            need = seq_parts[step_i]
            if ch >= need:
                step_i += 1; base_p = pr; peak_after_step = pr
                if step_i == len(seq_parts): return True
            else:
                if peak_after_step > 0 and pct(pr, peak_after_step) <= -slack:
                    break
    return False

# =========================
# مشغلات دخول خفيفة (تعتمد شموع الدقيقة + الأساسات الإحصائية)
# =========================
MIN_BREAKOUT_PCT = float(os.getenv("MIN_BREAKOUT_PCT", 0.3))
MAX_PULLBACK_PCT = float(os.getenv("MAX_PULLBACK_PCT", 0.5))

def can_enter_long(base):
    """يدخل إذا تحقق أحد: Momentum Burst / Breakout-Lite / Fast Bounce."""
    mins = list(minute_order[base])
    if len(mins) < 8:
        return None

    last_m = mins[-1]
    o, h, l, c = candles[base][last_m]
    if not c: return None

    # أساسات السوق الآن
    P_R1, P_ACC, P_RNG3 = PCTL_R1M, PCTL_ACCEL, PCTL_RNG3

    r1 = one_min_returns(base)
    r1m_now = r1[-1] if r1 else 0.0
    accel   = (r1[-1]-r1[-2]) if len(r1) >= 2 else 0.0

    # مبدّل بسيط حسب الحرارة
    m = adaptive_multipliers()

    # 1) Momentum Burst
    trig_momo = (r1m_now >= P_R1*m) and (accel >= P_ACC*0.9)

    # 2) Breakout-Lite عبر أعلى إغلاق 3د مع سحب بسيط
    trig_bo = False
    prev3 = mins[-4:-1]
    if len(prev3) == 3:
        prev_max_close = max(candles[base][m_][3] for m_ in prev3)
        breakout = pct(c, prev_max_close) if prev_max_close else 0.0
        pullbk   = pct(c, h)  # ≤ 0
        need_bo  = max(MIN_BREAKOUT_PCT, 0.3 * (P_RNG3/1.0)) * m
        trig_bo  = (breakout >= need_bo) and (pullbk > -MAX_PULLBACK_PCT)

    # 3) Fast Bounce: هبوط 3د قوي ثم تعافٍ دقيقتين
    trig_bounce = False
    if len(prev3) == 3:
        highs = [candles[base][m_][1] for m_ in prev3]
        lows  = [candles[base][m_][2] for m_ in prev3]
        if min(lows) > 0:
            drop3 = pct(min(lows), max(highs))  # سالبة
            if drop3 <= -1.2:
                last2 = mins[-3:-1]
                c1 = candles[base][last2[0]][3]; c2 = candles[base][last2[1]][3]
                if c1 and c2 and c2 >= c1 and r1m_now > 0:
                    trough = min(lows); recov = pct(c, trough)
                    trig_bounce = recov >= 0.4

    if trig_momo or trig_bo or trig_bounce:
        return {
            "r1m_now": r1m_now, "accel": accel,
            "triggers": {"momo": trig_momo, "bo": trig_bo, "bounce": trig_bounce},
            "need_r1": P_R1*m, "need_acc": P_ACC*0.9
        }
    return None

# =========================
# تيليجرام + متابعة
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

def notify_signal(base, tag, rank_map, extra=""):
    now = time.time()
    # كولداون لكل عملة
    if base in last_alert_ts and now - last_alert_ts[base] < ALERT_COOLDOWN_SEC:
        return
    # رتبة
    rank = rank_map.get(base, 999)
    if rank > RANK_FILTER:
        return
    # Flood control
    while flood_times and now - flood_times[0] > FLOOD_WINDOW_SEC:
        flood_times.popleft()
    if len(flood_times) >= FLOOD_MAX_PER_WINDOW:
        return

    # سجل متابعة
    with lock:
        dq = prices.get(base)
        if not dq: return
        start_price = dq[-1][1]
        open_preds[base] = {
            "time": now, "start_price": start_price, "high_price": start_price,
            "tag": tag, "hour_change": None, "status": None
        }

    msg = f"🔔 تنبؤ: {base} {tag} #top{rank}{(' ' + extra) if extra else ''}"

    if last_msg["text"] == msg and (now - last_msg["ts"]) < DEDUP_SEC:
        return
    last_alert_ts[base] = now
    flood_times.append(now)
    last_msg["text"], last_msg["ts"] = msg, now
    send_message(msg)

def evaluate_open_predictions():
    now = time.time()
    to_close = []
    with lock:
        for base, pred in list(open_preds.items()):
            if pred["status"] is not None:
                continue
            dq = prices.get(base)
            if not dq: continue
            cur = dq[-1][1]
            if cur > pred["high_price"]:
                pred["high_price"] = cur
            if now - pred["time"] >= FOLLOWUP_WINDOW_SEC:
                best_change = pct(pred["high_price"], pred["start_price"])
                status = "✅ أصابت" if best_change >= TARGET_PCT else "❌ خابت"
                pred["status"] = status
                history_results.append((
                    now, base, pred["tag"], status, TARGET_PCT, best_change, None
                ))
                learning_window.append("hit" if "✅" in status else "miss")
                to_close.append(base)
        for b in to_close:
            open_preds.pop(b, None)

# =========================
# 🔁 العمال
# =========================
def price_poller():
    """Bulk fetch + تحديث تدفق الثواني + بناء شموع 1-دقيقة."""
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
                        # تدفق 20 دقيقة
                        dq = prices[base]
                        dq.append((now, price))
                        cutoff = now - 1200
                        while dq and dq[0][0] < cutoff:
                            dq.popleft()
                        # شمعة الدقيقة
                        if minute not in candles[base]:
                            candles[base][minute] = [price, price, price, price]  # o,h,l,c
                            minute_order[base].append(minute)
                        else:
                            o, h, l, _c = candles[base][minute]
                            h = max(h, price); l = min(l, price); c = price
                            candles[base][minute] = [o, h, l, c]
                    # تحديث خطوط الأساس كل دقيقة
                    if minute != last_snapshot_minute:
                        refresh_baselines()
                        last_snapshot_minute = minute
            else:
                misses += 1

            if DEBUG_LOG and (time.time() - last_stats) >= STATS_EVERY_SEC:
                last_stats = time.time()
                with lock:
                    with_data = sum(1 for _, v in prices.items() if v)
                print(f"[POLL] entries={with_data} markets={len(symbols_all_eur)} misses={misses} heat={heat_ewma:.2f}")
                misses = 0
        except Exception as e:
            if DEBUG_LOG:
                print("[POLL][ERR]", type(e).__name__, str(e))
                traceback.print_exc()
        time.sleep(SCAN_INTERVAL)

def room_refresher():
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
                    dq = prices.get(b); if not dq: continue
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
        time.sleep(RESELECT_EVERY_SEC)

def analyzer():
    while True:
        if time.time() - start_time < GLOBAL_WARMUP_SEC:
            time.sleep(1); continue
        try:
            compute_market_heat()
            m = adaptive_multipliers()

            # لقطة آمنة للغرفة
            with lock:
                room = set(app.config.get("WATCHLIST", set()))
                snapshots = {b: list(prices[b]) for b in room if prices.get(b)}

            rank_map = global_rank_map()

            # قيّم المتابعات المفتوحة دائمًا
            evaluate_open_predictions()

            # Edge-trigger (أنماط الثواني) + مشغلات الشموع
            for base, dq_snap in snapshots.items():
                prev = pattern_state[base]
                cur_top1  = check_top1_pattern(dq_snap, m)
                cur_top10 = False if cur_top1 else check_top10_pattern(dq_snap, m)
                trig = can_enter_long(base)

                if cur_top1 and not prev["top1"]:
                    notify_signal(base, "top1", rank_map)
                elif cur_top10 and not prev["top10"]:
                    notify_signal(base, "top10", rank_map)
                elif trig:
                    tag = "momo" if trig["triggers"]["momo"] else ("bo" if trig["triggers"]["bo"] else "bounce")
                    notify_signal(base, tag, rank_map)

                pattern_state[base]["top1"]  = cur_top1
                pattern_state[base]["top10"] = cur_top10

            # تكيّف كل N نتائج مغلقة
            adapt_thresholds_every_n()

        except Exception as e:
            if DEBUG_LOG:
                print("[ANALYZER][ERR]", type(e).__name__, str(e))
                traceback.print_exc()
        time.sleep(1)

def adapt_thresholds_every_n():
    global BASE_STEP_PCT, BASE_STRONG_SEQ, last_adapt_total
    total_closed = len(history_results)
    if total_closed - last_adapt_total < ADAPT_EVERY_N:
        return
    window = list(history_results)[-ADAPT_EVERY_N:]
    if not window: return

    hits = sum(1 for *_, status, __, ___, ____ in window if "✅" in status)
    rate = hits / len(window)

    # عدّل top10
    if rate <= ADAPT_WIN_LOW:
        BASE_STEP_PCT = min(round(BASE_STEP_PCT + 0.15, 2), STEP_MAX)
    elif rate >= ADAPT_WIN_HIGH:
        BASE_STEP_PCT = max(round(BASE_STEP_PCT - 0.10, 2), STEP_MIN)

    # عدّل أول عنصر من تسلسل top1
    parts = [float(x) for x in BASE_STRONG_SEQ.split(",")]
    if parts:
        if rate <= ADAPT_WIN_LOW:
            parts[0] = min(parts[0] + 0.20, SEQ0_MAX)
        elif rate >= ADAPT_WIN_HIGH:
            parts[0] = max(parts[0] - 0.20, SEQ0_MIN)
        BASE_STRONG_SEQ = ",".join(f"{x:.2f}".rstrip('0').rstrip('.') for x in parts[:3])

    last_adapt_total = total_closed
    if DEBUG_LOG:
        print(f"[ADAPT N] total={total_closed} win_rate_last_{ADAPT_EVERY_N}={rate:.2%} "
              f"=> BASE_STEP_PCT={BASE_STEP_PCT} BASE_STRONG_SEQ={BASE_STRONG_SEQ}")

# =========================
# 🌐 Web & Telegram
# =========================
@app.get("/")
def health():
    return "Signals FINAL (10m peak + percentiles + N-adapt) ✅", 200

@app.get("/stats")
def stats():
    with lock:
        room = list(app.config.get("WATCHLIST", set()))
        with_data = sum(1 for _, v in prices.items() if v)
        open_n = sum(1 for v in open_preds.values() if v.get("status") is None)
    total = len(history_results)
    hits = sum(1 for *_, s, __, ___, ____ in history_results if "✅" in s)
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
        "pctl": {"r1m": round(PCTL_R1M,3), "accel": round(PCTL_ACCEL,3), "rng3": round(PCTL_RNG3,3)},
        "win_rate_pct": round(rate, 1) if rate is not None else None,
        "last_adapt_after_total": last_adapt_total
    }, 200

def send_summary():
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

    show_h = hits[-60:]
    show_m = misses[-60:]

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

def get_settings_summary():
    total = len(history_results)
    hits = sum(1 for *_, status, __, ___, ____ in history_results if "✅" in status)
    win_rate = (hits / total * 100.0) if total else 0.0
    lines = [
        "⚙️ الضبط الحالي:",
        f"- BASE_STEP_PCT (top10 1%+1%): {BASE_STEP_PCT:.2f} %",
        f"- BASE_STRONG_SEQ (top1): {BASE_STRONG_SEQ}",
        f"- TARGET_PCT (هدف 10د): {TARGET_PCT:.2f} %",
        f"- FOLLOWUP_WINDOW_SEC: {FOLLOWUP_WINDOW_SEC}s",
        f"- RANK_FILTER (Top N): {RANK_FILTER}",
        f"- ALERT_COOLDOWN_SEC: {ALERT_COOLDOWN_SEC}s",
        f"- FLOOD: {FLOOD_MAX_PER_WINDOW}/{FLOOD_WINDOW_SEC}s | DEDUP: {DEDUP_SEC}s",
        f"- HEAT: lookback={HEAT_LOOKBACK_SEC}s, ret={HEAT_RET_PCT:.2f}%, smooth={HEAT_SMOOTH:.2f}",
        f"- PCTL: r1m≈{PCTL_R1M:.2f} | accel≈{PCTL_ACCEL:.2f} | rng3≈{PCTL_RNG3:.2f}",
        "",
        "🤖 سياسة التكيّف (كل N):",
        f"- كل {ADAPT_EVERY_N} إشارة متبوعة نراجع آخر حزمة ونعدّل",
        f"- حدود: STEP[{STEP_MIN:.2f},{STEP_MAX:.2f}] | SEQ0[{SEQ0_MIN:.2f},{SEQ0_MAX:.2f}]",
        f"- قرار: ≤{int(ADAPT_WIN_LOW*100)}% يشدّد | ≥{int(ADAPT_WIN_HIGH*100)}% يرخي",
        "",
        f"📈 الأداء الإجمالي: نجاح {win_rate:.1f}% ({hits}/{total})",
        f"🔁 آخر تكيّف بعد: {last_adapt_total} إشارة"
    ]
    return "\n".join(lines)

@app.post("/webhook")
def telegram_webhook():
    data = request.json or {}
    msg = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()
    if not text:
        return "ok", 200
    if text in {"الملخص", "/summary"}:
        send_summary(); return "ok", 200
    if text in {"الضبط", "/status", "status", "الحالة", "/stats", "شو عم تعمل", "/شو_عم_تعمل"}:
        send_message(get_settings_summary()); return "ok", 200
    return "ok", 200

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

# شغّل مع Gunicorn
start_workers_once()

# تشغيل محلي
if __name__ == "__main__":
    start_workers_once()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))