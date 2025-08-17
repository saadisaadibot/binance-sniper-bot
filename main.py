# -*- coding: utf-8 -*-
import os, time, requests, traceback
from collections import deque, defaultdict
from threading import Thread, Lock, Event
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ========= Boot =========
load_dotenv()
app = Flask(__name__)

# ========= إعدادات عامة =========
BASE_URL   = os.getenv("BITVAVO_URL", "https://api.bitvavo.com/v2")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", 6.0))

# سرعة السحب & العتبات
POLL_SEC   = int(os.getenv("POLL_SEC", 3))             # كل كم ثانية نجلب bulk
WINDOW_SEC = int(os.getenv("WINDOW_SEC", 30))          # نافذة مقارنة القفزة
PUMP_PCT   = float(os.getenv("PUMP_PCT", 6.0))         # عتبة القفزة %

# منع سبام
COOLDOWN_SEC         = int(os.getenv("COOLDOWN_SEC", 300))   # كولداون لكل عملة
FLOOD_WINDOW_SEC     = int(os.getenv("FLOOD_WINDOW_SEC", 20))
FLOOD_MAX_PER_WINDOW = int(os.getenv("FLOOD_MAX_PER_WINDOW", 6))
DEDUP_SEC            = int(os.getenv("DEDUP_SEC", 5))

# الماركِت
MARKETS_REFRESH_SEC  = int(os.getenv("MARKETS_REFRESH_SEC", 120))
QUOTE                = os.getenv("QUOTE", "EUR")  # نراقب أزواج -EUR

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")

# Redis (للساعة الماضية)
import redis
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
# decode_responses=True => مفاتيح/قيم سترنغ مباشرة
r = redis.from_url(REDIS_URL, decode_responses=True)
REDIS_TTL_SEC = int(os.getenv("REDIS_TTL_SEC", 7200))  # TTL احتياطي للمفتاح

# صقر (اختياري)
SAQAR_WEBHOOK  = os.getenv("SAQAR_WEBHOOK", "")
SAQAR_ENABLED  = os.getenv("SAQAR_ENABLED", "1") == "1"  # عطّلها بوضع 0

# لوج
DEBUG_LOG = os.getenv("DEBUG_LOG","0") == "1"
STATS_EVERY_SEC = int(os.getenv("STATS_EVERY_SEC", 60))

# ========= حالة =========
lock = Lock()
started = Event()

symbols_all = []    # كل الـ bases مع QUOTE المطلوب
last_markets_refresh = 0

# تاريخ أسعار محلي خفيف: لكل عملة deque من (ts, price) على قدر النافذة فقط
prices = defaultdict(lambda: deque(maxlen=64))
last_alert_ts = {}                               # كولداون لكل عملة
from collections import deque as _dq
flood_times = _dq()
last_msg = {"text": None, "ts": 0.0}

# إحصاءات
detections = _dq(maxlen=200)   # [(ts, base, pct)]
last_bulk_ts = 0
misses = 0

# ========= Helpers =========
def http_get(url, params=None, timeout=HTTP_TIMEOUT):
    headers = {"User-Agent":"pump-tick/1.2"}
    for _ in range(2):
        try:
            return requests.get(url, params=params, timeout=timeout, headers=headers)
        except Exception:
            time.sleep(0.25)
    return None

def pct(now_p, old_p):
    try:
        return (now_p - old_p)/old_p*100.0 if old_p else 0.0
    except Exception:
        return 0.0

# --- Redis I/O ---
def r_key(base): return f"pt:{QUOTE}:p:{base}"  # ZSET score=ts, member="ts:price"

def redis_store_price(base, ts, price):
    key = r_key(base)
    member = f"{int(ts)}:{price}"
    # أضف النقطة واحذف الأقدم من ساعة
    pipe = r.pipeline()
    pipe.zadd(key, {member: ts})
    pipe.zremrangebyscore(key, 0, ts - 3600)
    pipe.expire(key, REDIS_TTL_SEC)  # تنظيف تلقائي
    pipe.execute()

def redis_change_window(base, minutes=60, require_points=2):
    """
    التغيّر % بين أول وآخر سعر داخل نافذة آخر `minutes` دقيقة تحديداً.
    يرجّع None إذا نقاط النافذة أقل من `require_points`.
    """
    key = r_key(base)
    now_ts = int(time.time())
    from_ts = now_ts - minutes * 60

    # نقاط النافذة: أول وأخر داخل [from_ts, now_ts]
    first = r.zrangebyscore(key, from_ts, now_ts, start=0, num=1)
    last  = r.zrevrangebyscore(key, now_ts, from_ts, start=0, num=1)

    # تحقّق من الكفاية داخل النافذة (على الأقل نقطتان)
    cnt_window = r.zcount(key, from_ts, now_ts)
    if cnt_window < require_points or not first or not last:
        return None

    try:
        p0 = float(first[0].split(":")[1])
        p1 = float(last[0].split(":")[1])
        if p0 <= 0:
            return None
        return (p1 - p0) / p0 * 100.0
    except Exception:
        return None

def trend_top_n(n=3, minutes=60):
    """أعلى n عملات صعوداً خلال آخر minutes دقيقة (من نفس النافذة فقط)."""
    out = []
    bases = list(symbols_all) or [k.split(":")[-1] for k in r.scan_iter(f"pt:{QUOTE}:p:*")]
    for b in bases:
        ch = redis_change_window(b, minutes=minutes, require_points=2)
        if ch is not None:
            out.append((b, ch))
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:n]

# --- Bitvavo ---
def refresh_markets(now=None):
    global symbols_all, last_markets_refresh
    now = now or time.time()
    if symbols_all and (now - last_markets_refresh) < MARKETS_REFRESH_SEC:
        return
    resp = http_get(f"{BASE_URL}/markets")
    if not resp or resp.status_code != 200:
        return
    try:
        data = resp.json()
        bases = []
        for m in data:
            if m.get("quote") == QUOTE and m.get("status") == "trading":
                base = m.get("base")
                if base and base.isalpha():
                    bases.append(base)
        with lock:
            symbols_all = bases
            last_markets_refresh = now
        if DEBUG_LOG:
            print(f"[MARKETS] {len(bases)} {QUOTE} markets")
    except Exception:
        pass

def bulk_prices():
    """dict base->price"""
    resp = http_get(f"{BASE_URL}/ticker/price")
    out = {}
    if not resp or resp.status_code != 200:
        return out
    try:
        for row in resp.json():
            mk = row.get("market","")
            if mk.endswith(f"-{QUOTE}"):
                base = mk.split("-")[0]
                try:
                    out[base] = float(row["price"])
                except Exception:
                    continue
    except Exception:
        pass
    return out

# --- Pump detection ---
def detect_pump_for(dq, now_ts):
    """يرجع نسبة القفزة خلال WINDOW_SEC استنادًا إلى deque المحلية."""
    if not dq:
        return None
    ref = None
    for ts, pr in reversed(dq):
        if now_ts - ts >= WINDOW_SEC:
            ref = pr
            break
    if ref is None:
        ref = dq[0][1]
    return pct(dq[-1][1], ref) if ref else None

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
    except Exception:
        pass

def notify_saqr_buy(base):
    if not SAQAR_WEBHOOK or not SAQAR_ENABLED:
        return
    try:
        requests.post(SAQAR_WEBHOOK, json={"message":{"text": f"اشتري {base}"}}, timeout=HTTP_TIMEOUT)
    except Exception:
        pass

def maybe_alert(base, jump_pct, price_now):
    now = time.time()
    # كولداون عملة
    if base in last_alert_ts and now - last_alert_ts[base] < COOLDOWN_SEC:
        return
    # Flood
    while flood_times and now - flood_times[0] > FLOOD_WINDOW_SEC:
        flood_times.popleft()
    if len(flood_times) >= FLOOD_MAX_PER_WINDOW:
        return
    # Dedup
    msg = f"⚡️ Pump? {base} +{jump_pct:.2f}% خلال {WINDOW_SEC}s @ {price_now:.8f} {QUOTE}"
    if last_msg["text"] == msg and (now - last_msg["ts"]) < DEDUP_SEC:
        return

    last_alert_ts[base] = now
    flood_times.append(now)
    last_msg["text"], last_msg["ts"] = msg, now
    detections.append((now, base, jump_pct))
    send_message(msg)
    notify_saqr_buy(base)   # وصّلها بالصقر (اختياري)

# ========= العمال =========
def poller():
    """
    - يجلب الأسعار bulk كل POLL_SEC ثانية
    - يخزنها في Redis (ساعة)
    - يكشف قفزات وفق WINDOW_SEC/PUMP_PCT
    - يطبع عدادات وأخطاء بشكل واضح
    """
    global last_bulk_ts, misses
    last_stats = 0
    EXPECTED_MIN = int(os.getenv("EXPECTED_MIN", 80))  # حد أدنى متوقَّع لعدد الأزواج

    while True:
        fetched = 0
        try:
            refresh_markets()
            mp = bulk_prices()          # dict: base -> price
            now = time.time()

            fetched = len(mp)
            if fetched == 0:
                misses += 1
                print(f"[POLL][WARN] bulk_prices returned 0 symbols (misses={misses})")
            else:
                # طباعة عدد الأزواج المجلبّة + المراقَبة فعليًا
                watchable = fetched if not symbols_all else sum(1 for b in mp if b in symbols_all)
                print(f"[POLL] fetched={fetched} symbols | watchable={watchable}")

                last_bulk_ts = now
                redis_errors = 0

                with lock:
                    # نضبط طول deque حسب النافذة
                    maxlen = max(20, int(WINDOW_SEC / max(POLL_SEC, 1)) + 6)

                    for base, price in mp.items():
                        if symbols_all and base not in symbols_all:
                            continue

                        # تدفق محلي
                        dq = prices[base]
                        if dq.maxlen != maxlen:
                            prices[base] = dq = deque(dq, maxlen=maxlen)
                        dq.append((now, price))

                        # تخزين في Redis
                        try:
                            redis_store_price(base, now, price)
                        except Exception as e:
                            redis_errors += 1
                            print(f"[REDIS][ERR] {base}: {type(e).__name__}: {e}")

                        # كشف pump
                        jump = detect_pump_for(dq, now)
                        if jump is not None and jump >= PUMP_PCT:
                            maybe_alert(base, jump, price)

                if fetched < EXPECTED_MIN:
                    print(f"[POLL][WARN] only {fetched} symbols (<{EXPECTED_MIN})")
                if redis_errors:
                    print(f"[REDIS] completed with {redis_errors} error(s)")

            # ملخص دوري
            if (time.time() - last_stats) >= STATS_EVERY_SEC:
                last_stats = time.time()
                with lock:
                    with_data = sum(1 for _, v in prices.items() if v)
                age = int(now - last_bulk_ts) if last_bulk_ts else None
                print(f"[STATS] with_data={with_data} markets_listed={len(symbols_all)} "
                      f"misses={misses} last_age={age}s window={WINDOW_SEC}s thr={PUMP_PCT}%")

                # صفّر عداد misses بعد طباعة الملخص
                misses = 0

        except Exception as e:
            print(f"[POLL][ERR] {type(e).__name__}: {e}")
            traceback.print_exc()

        time.sleep(POLL_SEC)

def start_workers_once():
    if started.is_set(): return
    with lock:
        if started.is_set(): return
        Thread(target=poller, daemon=True).start()
        started.set()
        if DEBUG_LOG:
            print("[BOOT] pump-tick poller started")

# ========= Web =========
@app.get("/")
def health():
    return f"Pump-Tick (win={WINDOW_SEC}s, thr={PUMP_PCT:.2f}%, quote={QUOTE}) ✅", 200

@app.get("/stats")
def stats():
    with lock:
        recent = list(detections)[-20:]
        with_data = sum(1 for _, v in prices.items() if v)
    out = [{"t": int(ts), "base": b, "jump_pct": round(p,2)} for (ts,b,p) in recent]
    return jsonify({
        "markets": len(symbols_all),
        "symbols_with_data": with_data,
        "last_bulk_age": (time.time()-last_bulk_ts) if last_bulk_ts else None,
        "window_sec": WINDOW_SEC,
        "thresh_pct": PUMP_PCT,
        "cooldown_sec": COOLDOWN_SEC,
        "detections_last": out
    }), 200

@app.get("/trend")
def trend_api():
    try:
        mins = int(request.args.get("mins", "60"))
        n    = int(request.args.get("n", "3"))
    except Exception:
        mins, n = 60, 3
    top = trend_top_n(n=n, minutes=mins)
    return jsonify({
        "minutes": mins,
        "top": [{"base": b, "chg_pct": round(c, 3 if abs(c) < 1 else 2)} for b, c in top]
    }), 200
@app.post("/webhook")
def telegram_webhook():
    data = request.json or {}
    msg  = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()
    if not text:
        return "ok", 200

    # ===== الملخص =====
    if text in {"الملخص", "/summary"}:
        with lock:
            recent = list(detections)[-12:]
        if not recent:
            send_message("📊 لا توجد إشعارات بعد.")
        else:
            lines = ["📊 آخر الإشعارات:"]
            for ts, b, p in recent:
                lines.append(f"- {b}: +{p:.2f}% خلال {WINDOW_SEC}s")
            send_message("\n".join(lines))
        return "ok", 200

    # ===== الترند (يدعم: 'الترند', 'الترند 15', 'الترند 15 5', '/trend ...') =====
    if text.startswith("الترند") or text.startswith("/trend"):
        # أمثلة نصية:
        # "الترند"        → mins=60, n=3
        # "الترند 15"     → mins=15, n=3
        # "الترند 15 5"   → mins=15, n=5
        parts = text.replace("/trend", "الترند").split()
        mins = 60
        topn = 3
        # قراءة الأرقام إن وُجدت
        if len(parts) >= 2 and parts[1].isdigit():
            mins = int(parts[1])
        if len(parts) >= 3 and parts[2].isdigit():
            topn = int(parts[2])
        # حدود أمان بسيطة
        mins = max(5, min(mins, 360))     # من 5 دقائق حتى 6 ساعات
        topn = max(1, min(topn, 10))      # من 1 إلى 10 عملات

        top = trend_top_n(n=topn, minutes=mins)
        if not top:
            send_message(f"📈 لا توجد بيانات كافية لآخر {mins} دقيقة بعد.")
        else:
            lines = [f"📈 أقوى {len(top)} خلال آخر {mins} دقيقة:"]
            for b, c in top:
                fmt = f"{c:+.3f}%" if abs(c) < 1 else f"{c:+.2f}%"
                lines.append(f"- {b} : {fmt}")
            send_message("\n".join(lines))
        return "ok", 200

    # ===== الضبط / الحالة =====
    if text in {"الضبط", "/status", "status"}:
        lines = [
            "⚙️ Pump-Tick settings:",
            f"- POLL_SEC = {POLL_SEC}s | WINDOW_SEC = {WINDOW_SEC}s | PUMP_PCT = {PUMP_PCT:.2f}%",
            f"- COOLDOWN = {COOLDOWN_SEC}s | FLOOD = {FLOOD_MAX_PER_WINDOW}/{FLOOD_WINDOW_SEC}s | DEDUP = {DEDUP_SEC}s",
            f"- QUOTE = {QUOTE} | MARKETS_REFRESH_SEC = {MARKETS_REFRESH_SEC}s",
            f"- Redis TTL = {REDIS_TTL_SEC}s | Redis prefix = pt:{QUOTE}:p:*",
            f"- SAQAR: {'ON' if SAQAR_ENABLED and SAQAR_WEBHOOK else 'OFF'}"
        ]
        send_message("\n".join(lines))
        return "ok", 200

        # ===== فحص سريع للريدز =====
    if text in {"فحص", "/diag"}:
        try:
            keys = list(r.scan_iter(f"pt:{QUOTE}:p:*"))
            kcnt = len(keys)
            sample = keys[0] if kcnt else None
            info_lines = [f"🧰 Redis: {kcnt} مفاتيح"]
            if sample:
                cnt_all = r.zcard(sample)
                now_ts = int(time.time())
                cnt_5m  = r.zcount(sample, now_ts-300, now_ts)
                cnt_60m = r.zcount(sample, now_ts-3600, now_ts)
                info_lines.append(f"- عيّنة: {sample.split(':')[-1]} | all={cnt_all}, 5m={cnt_5m}, 60m={cnt_60m}")
            send_message("\n".join(info_lines))
        except Exception as e:
            send_message(f"Redis ERR: {type(e).__name__}: {e}")
        return "ok", 200

    return "ok", 200

# ========= Run =========

def clear_old_keys():
    """
    يمسح المفاتيح القديمة الخاصة بالبوت (pt:{QUOTE}:p:*) 
    لتجنب بيانات عالقة من دورات سابقة.
    """
    try:
        pattern = f"pt:{QUOTE}:p:*"
        keys = list(r.scan_iter(pattern))
        if keys:
            r.delete(*keys)
            print(f"[INIT] Cleared {len(keys)} old Redis keys (pattern={pattern}).")
        else:
            print(f"[INIT] No old keys found for pattern={pattern}.")
    except Exception as e:
        print(f"[INIT][ERR] {e}")

if __name__ == "__main__":
    clear_old_keys()
    start_workers_once()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))