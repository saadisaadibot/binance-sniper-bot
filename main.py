# -*- coding: utf-8 -*-
import os, time, json, math, random, traceback, requests
from collections import deque, defaultdict
from threading import Thread, Lock, Event
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# =========================
# 🚀 إعداد
# =========================
load_dotenv()
app = Flask(__name__)

BASE_URL             = os.getenv("BITVAVO_URL", "https://api.bitvavo.com/v2")
HTTP_TIMEOUT         = float(os.getenv("HTTP_TIMEOUT", 8.0))

# سحب الأسعار والاختيار
SCAN_INTERVAL        = int(os.getenv("SCAN_INTERVAL", 5))          # كل كم ثانية نسحب السعر اللحظي
MARKETS_REFRESH_SEC  = int(os.getenv("MARKETS_REFRESH_SEC", 60))   # تحديث قائمة أسواق EUR
ROOM_SIZE            = int(os.getenv("ROOM_SIZE", 40))             # عدد العملات اللي نكوّن لها شمعات
RESELECT_MINUTES     = int(os.getenv("RESELECT_MINUTES", 15))      # إعادة انتقاء “البارزين” كل ربع ساعة

# اختيار البارزين من آخر 30 دقيقة
VOL30_MIN_PCT        = float(os.getenv("VOL30_MIN_PCT", 2.0))      # لازم تغيّر ≥ 2% خلال 30د ليتم مراقبته
TOP_VOL_KEEP         = int(os.getenv("TOP_VOL_KEEP", 20))          # نحصر المراقبة لأكثر العملات تقلبًا

# منطق الدخول/الخروج (افتراضيًا Long فقط)
TARGET_PCT           = float(os.getenv("TARGET_PCT", 2.0))         # نعتبر الصفقة ناجحة عند +2%
STOP_PCT             = float(os.getenv("STOP_PCT", 1.0))           # ستوب مبكر اختياري
FOLLOWUP_WINDOW_SEC  = int(os.getenv("FOLLOWUP_WINDOW_SEC", 600))  # نغلق بعد 10د إن ما وصل الهدف
MIN_BREAKOUT_PCT     = float(os.getenv("MIN_BREAKOUT_PCT", 0.6))   # اختراق بسيط فوق قمة 5د (حوالي 0.6%+)
MAX_PULLBACK_PCT     = float(os.getenv("MAX_PULLBACK_PCT", 0.5))   # ارتداد مسموح بعد الاختراق

# منع السيل/التكرار
ALERT_COOLDOWN_SEC   = int(os.getenv("ALERT_COOLDOWN_SEC", 600))   # كولداون لكل عملة
FLOOD_WINDOW_SEC     = int(os.getenv("FLOOD_WINDOW_SEC", 30))
FLOOD_MAX_PER_WINDOW = int(os.getenv("FLOOD_MAX_PER_WINDOW", 10))
DEDUP_SEC            = int(os.getenv("DEDUP_SEC", 5))

# حرارة + اتجاه
HEAT_LOOKBACK_SEC    = int(os.getenv("HEAT_LOOKBACK_SEC", 120))
HEAT_RET_PCT         = float(os.getenv("HEAT_RET_PCT", 0.6))
HEAT_SMOOTH          = float(os.getenv("HEAT_SMOOTH", 0.3))

# تيليجرام
BOT_TOKEN            = os.getenv("BOT_TOKEN")
CHAT_ID              = os.getenv("CHAT_ID")
EXPERIMENT_TAG       = os.getenv("EXPERIMENT_TAG", "جرب لتفهم")

# Redis اختياري
REDIS_URL            = os.getenv("REDIS_URL")

DEBUG_LOG            = os.getenv("DEBUG_LOG", "0") == "1"
STATS_EVERY_SEC      = int(os.getenv("STATS_EVERY_SEC", 60))
GLOBAL_WARMUP_SEC    = int(os.getenv("GLOBAL_WARMUP_SEC", 20))

# =========================
# 🧠 حالة
# =========================
lock = Lock()
started = Event()

symbols_eur = []
last_markets_refresh = 0

# أسعار خام (ثوانٍ) + شمعات 1 دقيقة محليًا
ticks = defaultdict(lambda: deque())                 # base -> deque[(ts, price)] (احتفاظ ~35د)
candles = defaultdict(lambda: dict())                # base -> {minute_ts -> [o,h,l,c]}
minute_order = defaultdict(lambda: deque(maxlen=60)) # ترتيب آخر 60 دقيقة لكل base

# Redis (اختياري)
r = None
if REDIS_URL:
    try:
        import redis as _redis
        r = _redis.from_url(REDIS_URL)
    except Exception:
        r = None

# مراقبة + تداول افتراضي
watchlist = set()                                    # عملات “بارزة” (تقلب ≥ 2% خلال 30د)
last_quarter_minute = -1

open_trades = {}                                     # base -> dict(entry_ts, entry_px, high_px, reason, bias, status)
last_alert_ts = {}                                   # كولداون لكل عملة

# تتبّع + تعلّم
history = deque(maxlen=1000)                         # [(ts, base, status, best%, reason, ctx)]
coin_perf = defaultdict(lambda: deque(maxlen=30))    # base -> 'hit'/'miss'
heat_ewma = 0.0
last_bulk_ts = 0

# Flood/Dedup
from collections import deque as _dq
flood_times = _dq()
last_msg = {"text": None, "ts": 0.0}

# =========================
# 🔌 مساعدات
# =========================
def send_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("[TG_DISABLED]", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text}, timeout=HTTP_TIMEOUT)
    except Exception as e:
        print("Telegram error:", e)

def http_get(url, params=None, timeout=HTTP_TIMEOUT):
    headers = {"User-Agent": "try-to-understand/1.0"}
    for _ in range(2):
        try:
            return requests.get(url, params=params, timeout=timeout, headers=headers)
        except Exception:
            time.sleep(0.4)
    return None

def pct(a, b):
    try:
        return (a - b) / b * 100.0 if (b and b > 0) else 0.0
    except Exception:
        return 0.0

# =========================
# 📈 أسواق وأسعار
# =========================
def refresh_markets(now=None):
    global symbols_eur, last_markets_refresh
    now = now or time.time()
    if (now - last_markets_refresh) < MARKETS_REFRESH_SEC and symbols_eur:
        return
    resp = http_get(f"{BASE_URL}/markets")
    if not resp or resp.status_code != 200: return
    try:
        data = resp.json()
        symbols_eur = [m.get("base") for m in data
                       if m.get("quote") == "EUR" and m.get("status") == "trading"
                       and (m.get("base") or "").isalpha()]
        last_markets_refresh = now
        if DEBUG_LOG: print(f"[MARKETS] {len(symbols_eur)} EUR symbols")
    except Exception:
        pass

def bulk_prices():
    resp = http_get(f"{BASE_URL}/ticker/price")
    out = {}
    if not resp or resp.status_code != 200: return out
    try:
        for row in resp.json():
            mk = row.get("market","")
            if mk.endswith("-EUR"):
                base = mk.split("-")[0]
                try: out[base] = float(row["price"])
                except: continue
    except Exception:
        pass
    return out

# =========================
# 🕯️ صناعة شمعات 1 دقيقة + حفظ Redis
# =========================
def push_tick_update(mp, now):
    minute = int(now // 60) * 60
    cutoff_ticks = now - 35*60

    for base, px in mp.items():
        dq = ticks[base]
        dq.append((now, px))
        while dq and dq[0][0] < cutoff_ticks: dq.popleft()

        cinfo = candles[base].get(minute)
        if cinfo is None:
            candles[base][minute] = [px, px, px, px]   # o,h,l,c
            minute_order[base].append(minute)
        else:
            o,h,l,c = cinfo
            candles[base][minute] = [o, max(h,px), min(l,px), px]

    # تنظيف شمعات أقدم من 60 دقيقة
    for base in list(candles.keys()):
        while minute_order[base] and (minute - minute_order[base][0]) > 60*60:
            oldm = minute_order[base].popleft()
            candles[base].pop(oldm, None)

    # إلى Redis (آخر 30 دقيقة فقط)
    if r:
        try:
            for base in list(candles.keys()):
                mins = list(minute_order[base])[-30:]
                arr = []
                for m in mins:
                    o,h,l,c = candles[base][m]
                    arr.append([m, o,h,l,c])
                r.set(f"ohlc:{base}", json.dumps(arr))
        except Exception:
            pass

# =========================
# 🌡️ حرارة السوق + اتجاه متعدد النوافذ
# =========================
def compute_heat():
    global heat_ewma
    now = time.time()
    moved = total = 0
    with lock:
        for base, dq in ticks.items():
            if len(dq) < 2: continue
            ref = None
            for ts, pr in reversed(dq):
                if now - ts >= HEAT_LOOKBACK_SEC: ref = pr; break
            if ref and ref>0:
                cur = dq[-1][1]
                ret = pct(cur, ref)
                total += 1
                if abs(ret) >= HEAT_RET_PCT: moved += 1
    raw = (moved/total) if total else 0.0
    if total:
        heat_ewma = (1-HEAT_SMOOTH)*heat_ewma + HEAT_SMOOTH*raw
    return heat_ewma

def dir_bias(base):
    """انحياز اتجاهي من 1/5/15/60 دقيقة."""
    now = time.time()
    mins = list(minute_order[base])
    if len(mins) < 3:
        return {"bias":1.0,"r5":0.0,"r15":0.0,"r60":0.0}
    def close_at(min_ts):
        o,h,l,c = candles[base].get(min_ts, [None]*4)
        return c
    last_m = mins[-1]
    c_now = close_at(last_m)
    def ch(lb):
        target = last_m - lb
        ref = None
        for m in reversed(mins):
            if m <= target:
                ref = close_at(m); break
        return pct(c_now, ref) if (ref and ref>0) else 0.0
    r5, r15, r60 = ch(300), ch(900), ch(3600)

    score = 0
    for v,w in [(r5,1.5),(r15,1.2),(r60,0.8)]:
        score += (1 if v>0 else -1 if v<0 else 0)*w
    bias = 0.92 if score>=2.0 else (1.08 if score<=-2.0 else 1.0)
    return {"bias":bias,"r5":r5,"r15":r15,"r60":r60}

def noise_regime(base):
    """قياس ضجيج آخر 3 دقائق ضمنياً من تذبذب الشموع."""
    mins = list(minute_order[base])[-4:]
    if len(mins) < 3: return "normal"
    closes = [candles[base][m][3] for m in mins]
    diffs = []
    for i in range(1,len(closes)):
        if closes[i-1] > 0:
            diffs.append(abs(pct(closes[i], closes[i-1])))
    vol = sum(diffs)/len(diffs) if diffs else 0.0
    if vol < 0.05:  return "flat"
    if vol > 0.90:  return "choppy"
    return "normal"

# =========================
# 🧮 اختيار “البارزين” (آخر 30د)
# =========================
def calc_vol30(base):
    mins = list(minute_order[base])[-30:]
    if len(mins) < 5: return 0.0, 0.0
    highs = [candles[base][m][1] for m in mins]
    lows  = [candles[base][m][2] for m in mins]
    mx, mn = max(highs), min(lows)
    if mn <= 0: return 0.0, 0.0
    amp = pct(mx, mn)         # اتساع 30 دقيقة
    net = pct(candles[base][mins[-1]][3], candles[base][mins[0]][0])  # صافي الحركة
    return amp, net

def reselect_watchlist(now=None):
    global watchlist, last_quarter_minute
    now = now or time.time()
    cur_min = int(now//60)
    # نفذ كل ربع ساعة
    if cur_min % RESELECT_MINUTES == 0 and cur_min != last_quarter_minute:
        last_quarter_minute = cur_min
        candidates = []
        with lock:
            bases = symbols_eur or list(candles.keys())
            for b in bases:
                if not minute_order[b]: continue
                amp, net = calc_vol30(b)
                if amp >= VOL30_MIN_PCT:
                    candidates.append((b, amp, net))
        # اختَر الأكثر تقلبًا
        candidates.sort(key=lambda x: (x[1], abs(x[2])), reverse=True)
        selected = [b for b,_,__ in candidates[:TOP_VOL_KEEP]]
        with lock:
            watchlist = set(selected)
        if DEBUG_LOG:
            print(f"[RESELECT] watch {len(watchlist)} / cand={len(candidates)}")

# =========================
# 🎯 منطق الدخول/الخروج
# =========================
def can_enter_long(base):
    """اختراق قمة 5د + انحياز الاتجاه/الضجيج يعدّل الشروط."""
    mins = list(minute_order[base])
    if len(mins) < 8: return None
    last = mins[-1]
    last_c = candles[base][last][3]

    # قمة آخر 5 دقائق بدون الشمعة الحالية
    prev5 = [candles[base][m][3] for m in mins[-6:-1]]
    if not prev5 or min(prev5) <= 0: return None
    prev_max = max(prev5)

    # انحيازات
    bias = dir_bias(base)
    reg  = noise_regime(base)
    heat = compute_heat()
    m = 1.0 * bias["bias"]
    if reg == "flat":   m *= 1.08
    elif reg == "choppy": m *= 0.96
    if heat < 0.1:      m *= 1.05
    if heat > 0.5:      m *= 0.97

    # شرط الاختراق
    breakout = pct(last_c, prev_max)
    need = MIN_BREAKOUT_PCT * m
    if breakout < need:
        return None

    # لا يكون هناك ارتداد قوي في الشمعة الحالية
    last_o, last_h, last_l, last_c = candles[base][last]
    pullbk = pct(last_c, last_h)  # عادة <= 0
    if pullbk <= -MAX_PULLBACK_PCT * m:
        return None

    ctx = {
        "bias": bias,
        "heat": heat,
        "reg": reg,
        "need_breakout": need,
        "breakout": breakout
    }
    return ctx

def try_open_trades():
    """جرّب فتح دخول Long افتراضي على العملات المراقبة."""
    now = time.time()
    with lock:
        bases = list(watchlist)
    for b in bases:
        # كولداون
        if b in last_alert_ts and now - last_alert_ts[b] < ALERT_COOLDOWN_SEC:
            continue
        if b in open_trades:   # صفقة مفتوحة
            continue
        ctx = can_enter_long(b)
        if not ctx:
            continue
        # افتح
        last_m = minute_order[b][-1]
        entry_px = candles[b][last_m][3]
        open_trades[b] = {
            "entry_ts": now, "entry_px": entry_px, "high_px": entry_px,
            "ctx": ctx, "status": None
        }
        last_alert_ts[b] = now
        # رسالة “دخلنا”
        arrow = "↗️" if ctx["bias"]["r15"] >= 0 else "↘️"
        msg = (f"🧪 {EXPERIMENT_TAG}\n"
               f"دخلنا: {b} (long)\n"
               f"مدخل: {entry_px:.8f} | هدف: +{TARGET_PCT:.2f}% | نافذة: 10د\n"
               f"الاتجاه 15د {ctx['bias']['r15']:+.2f}% {arrow} | heat {ctx['heat']:.2f}\n"
               f"breakout {ctx['breakout']:+.2f}% ≥ need {ctx['need_breakout']:.2f}%")
        flood_and_send(msg)

def manage_trades():
    """تحديث القمة + الخروج نجاح/فشل + تعليل."""
    now = time.time()
    to_close = []
    for b, tr in list(open_trades.items()):
        # تحديث أعلى قمة
        last_m = minute_order[b][-1] if minute_order[b] else None
        if not last_m: continue
        cur = candles[b][last_m][3]
        if cur > tr["high_px"]: tr["high_px"] = cur

        ret_best = pct(tr["high_px"], tr["entry_px"])
        ret_now  = pct(cur, tr["entry_px"])
        elapsed  = now - tr["entry_ts"]

        # نجاح مبكر
        if ret_best >= TARGET_PCT:
            tr["status"] = ("✅ أصابت", ret_best, "hit_target")
        # فشل مبكر (ستوب اختياري)
        elif ret_now <= -STOP_PCT:
            tr["status"] = ("❌ خابت", ret_best, "stop_loss")
        # انتهاء المهلة
        elif elapsed >= FOLLOWUP_WINDOW_SEC:
            reason = classify_fail_reason(b, tr, ret_best, ret_now)
            status = "✅ أصابت" if ret_best >= TARGET_PCT else "❌ خابت"
            tr["status"] = (status, ret_best, reason)

        if tr["status"] is not None:
            status, best, reason = tr["status"]
            ctx = tr.get("ctx", {})
            history.append((now, b, status, best, reason, ctx))
            coin_perf[b].append("hit" if "✅" in status else "miss")
            to_close.append(b)

            # رسالة خروج
            msg = (f"🧪 {EXPERIMENT_TAG}\n"
                   f"خروج: {b} => {status}\n"
                   f"أفضل عائد خلال 10د: {best:+.2f}%\n"
                   f"السبب: {reason}")
            flood_and_send(msg)

    for b in to_close:
        open_trades.pop(b, None)

def classify_fail_reason(base, tr, best, cur_ret):
    """تعليل بسيط يفيد التعلم."""
    ctx = tr.get("ctx", {})
    reg = ctx.get("reg", "normal")
    bias = ctx.get("bias", {})
    r15 = bias.get("r15", 0.0)
    heat = ctx.get("heat", 0.0)

    if best < 0.5:
        return "no_follow_through"
    if r15 < -0.3:
        return "against_trend_15m"
    if reg == "choppy":
        return "high_noise_regime"
    if heat < 0.1:
        return "cold_market"
    if cur_ret < -0.6:
        return "sharp_reversal"
    return "time_expired"

def flood_and_send(text):
    now = time.time()
    while flood_times and now - flood_times[0] > FLOOD_WINDOW_SEC:
        flood_times.popleft()
    if len(flood_times) >= FLOOD_MAX_PER_WINDOW:
        return
    if last_msg["text"] == text and (now - last_msg["ts"]) < DEDUP_SEC:
        return
    flood_times.append(now)
    last_msg["text"], last_msg["ts"] = text, now
    send_message(text)

# =========================
# 🔁 العمال
# =========================
def price_poller():
    global last_bulk_ts
    last_stats, misses = 0, 0
    while True:
        try:
            refresh_markets()
            mp = bulk_prices()
            now = time.time()
            if mp:
                with lock:
                    push_tick_update(mp, now)
                    last_bulk_ts = now
            else:
                misses += 1

            if DEBUG_LOG and (time.time() - last_stats) >= STATS_EVERY_SEC:
                last_stats = time.time()
                with lock:
                    with_data = sum(1 for _, v in ticks.items() if v)
                print(f"[POLL] ticks={with_data} markets={len(symbols_eur)} misses={misses}")
                misses = 0
        except Exception as e:
            if DEBUG_LOG:
                print("[POLL][ERR]", type(e).__name__, str(e))
                traceback.print_exc()
        time.sleep(SCAN_INTERVAL)

def selector_and_trader():
    while True:
        try:
            if time.time() - last_bulk_ts > 15:  # ما في بيانات
                time.sleep(1); continue

            compute_heat()
            reselect_watchlist()
            try_open_trades()
            manage_trades()
        except Exception as e:
            if DEBUG_LOG:
                print("[TRADE][ERR]", type(e).__name__, str(e))
                traceback.print_exc()
        time.sleep(1)

# =========================
# 🌐 Web + Telegram
# =========================
def fmt_settings():
    return "\n".join([
        "⚙️ الضبط الحالي:",
        f"- VOL30_MIN_PCT: {VOL30_MIN_PCT:.2f}%",
        f"- RESELECT_MINUTES: {RESELECT_MINUTES}m | ROOM_SIZE: {ROOM_SIZE} | TOP_VOL_KEEP: {TOP_VOL_KEEP}",
        f"- TARGET: +{TARGET_PCT:.2f}% | STOP: -{STOP_PCT:.2f}% | WINDOW: {FOLLOWUP_WINDOW_SEC//60}m",
        f"- BREAKOUT≥ {MIN_BREAKOUT_PCT:.2f}% | pullback≤ {MAX_PULLBACK_PCT:.2f}%",
        f"- HEAT: lookback={HEAT_LOOKBACK_SEC}s ret={HEAT_RET_PCT:.2f}% smooth={HEAT_SMOOTH:.2f}",
        f"- FLOOD: {FLOOD_MAX_PER_WINDOW}/{FLOOD_WINDOW_SEC}s | DEDUP {DEDUP_SEC}s",
        f"- TAG: {EXPERIMENT_TAG}"
    ])

def fmt_summary():
    total = len(history)
    if total == 0:
        return "📊 لا توجد نتائج بعد."
    wins = [x for x in history if "✅" in x[2]]
    misses = [x for x in history if "❌" in x[2]]
    rate = (len(wins)/total)*100.0 if total else 0.0
    lines = [
        f"📊 الملخص (آخر {total} دخول متبوع):",
        f"أصابت: {len(wins)} | خابت: {len(misses)} | نجاح: {rate:.1f}%",
        "",
        "✅ الناجحة (آخر 10):"
    ]
    for row in wins[-10:]:
        _, b, status, best, reason, ctx = row
        lines.append(f"{b}: {status} | أفضل {best:+.2f}% | سبب {reason}")
    lines += ["", "❌ الخائبة (آخر 10):"]
    for row in misses[-10:]:
        _, b, status, best, reason, ctx = row
        lines.append(f"{b}: {status} | أفضل {best:+.2f}% | سبب {reason}")
    return "\n".join(lines)

@app.get("/")
def health():
    return "Try-to-understand (30m candles, 15m reselection, 10m follow-up) ✅", 200

@app.get("/stats")
def stats():
    with lock:
        wl = list(watchlist)
        open_n = len(open_trades)
        with_data = sum(1 for _, v in ticks.items() if v)
    total = len(history)
    wins = sum(1 for x in history if "✅" in x[2])
    rate = (wins/total*100.0) if total else None
    return {
        "watchlist": wl,
        "open_trades": open_n,
        "symbols_with_data": with_data,
        "heat": round(heat_ewma,3),
        "last_bulk_age": (time.time()-last_bulk_ts) if last_bulk_ts else None,
        "win_rate_pct": round(rate,1) if rate is not None else None
    }, 200

@app.post("/webhook")
def webhook():
    data = request.json or {}
    msg = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()
    if not text: return "ok", 200

    if text in {"الملخص","/summary"}:
        send_message(fmt_summary()); return "ok", 200
    if text in {"الضبط","/status","status","الحالة","/stats"}:
        send_message(fmt_settings()); return "ok", 200
    return "ok", 200

# =========================
# 🏁 التشغيل
# =========================
def start_workers_once():
    if started.is_set(): return
    with lock:
        if started.is_set(): return
        Thread(target=price_poller, daemon=True).start()
        Thread(target=selector_and_trader, daemon=True).start()
        started.set()
        if DEBUG_LOG: print("[BOOT] threads started")

if __name__ == "__main__":
    start_workers_once()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))