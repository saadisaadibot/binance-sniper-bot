# -*- coding: utf-8 -*-
import os, time, requests
from collections import deque, defaultdict
from threading import Thread
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# =========================
# ⚙️ إعدادات أساسية (قابلة للتعديل)
# =========================
SCAN_INTERVAL_SEC     = float(os.getenv("SCAN_INTERVAL_SEC", 2.0))   # مسح الأسعار كل N ثوانٍ
HISTORY_SEC           = int(os.getenv("HISTORY_SEC", 900))           # نحتفظ بـ 15 دقيقة
FOLLOWUP_WINDOW_SEC   = int(os.getenv("FOLLOWUP_WINDOW_SEC", 300))   # تقييم الإشعار بعد 5 دقائق
ALERT_COOLDOWN_SEC    = int(os.getenv("ALERT_COOLDOWN_SEC", 300))    # كولداون 5 دقائق لكل عملة
TG_BOT_TOKEN          = os.getenv("BOT_TOKEN")
TG_CHAT_ID            = os.getenv("CHAT_ID")
BASE_URL              = os.getenv("BITVAVO_URL", "https://api.bitvavo.com/v2")
DEBUG_LOG             = os.getenv("DEBUG_LOG", "0") == "1"           # اطبع قيَم التحليل إن أردت

# ورم-أب: يلزمنا على الأقل 3 دقائق بيانات قبل التنبؤ
WARMUP_SEC = int(os.getenv("WARMUP_SEC", 180))

# =========================
# 🧠 الحالة
# =========================
# تخزين تسلسل الأسعار (ts, price)
prices = defaultdict(lambda: deque())                # base -> deque[(ts, price)]
# أساس نشاط طويل الأجل داخل دالة الميكروفوليوم (لا حاجة لحالة إضافية خارجية)
last_alert_ts = {}                                   # base -> ts
predictions = {}                                     # base -> {"time","expected","start_price","status"}
history_results = deque(maxlen=500)                  # [(ts, base, status, expected, actual)]

# عتبات متكيفة (مخففة كبداية)
thresholds = {
    "MIN_SPEED_PM": 0.40,   # %/د من r30s * 2
    "MIN_ACCEL_PM": 0.08,   # فرق السرعة الآن - سرعة الدقيقة السابقة
    "VOL_RATIO_MIN": 1.00,  # نشاط قصير/طويل
    "EXPECTED_MIN": 1.80    # أقل قفزة متوقعة
}
# أداء آخر فترة للتعلم الذاتي
learning_window = deque(maxlen=40)  # "hit" أو "miss"

# =========================
# 📡 مساعدات
# =========================
def send_message(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[NO_TG]", text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=6
        )
    except Exception as e:
        print("TG error:", e)

def http_get_json(url, params=None, timeout=6):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

# =========================
# 📊 حسابات لحظية
# =========================
def pct_change(a, b):
    try:
        return (a - b) / b * 100.0 if (b and b > 0) else 0.0
    except Exception:
        return 0.0

def window_price(base, lookback_sec, now):
    """
    يرجع أول سعر أقدم من/يساوي lookback_sec داخل الديك.
    """
    dq = prices[base]
    ref = None
    for ts, pr in reversed(dq):
        if now - ts >= lookback_sec:
            ref = pr
            break
    return ref

def micro_volatility(base, now):
    """
    نشاط قصير (30s) مقارنة بأساس طويل (300s).
    نسبة > 1 تعني حرارة راهنة أعلى من المعتاد.
    """
    dq = prices[base]
    if len(dq) < 5:
        return 1.0

    def avg_abs_ret(window_sec):
        rets, prev = [], None
        for ts, p in dq:
            if now - ts <= window_sec:
                if prev is not None:
                    rets.append(abs(pct_change(p, prev)))
                prev = p
        return (sum(rets) / len(rets)) if rets else 0.0

    short = avg_abs_ret(30)    # 30 ثانية
    long  = avg_abs_ret(300)   # 5 دقائق

    if long <= 0:
        return 1.0
    ratio = short / long
    return max(0.7, min(ratio, 3.0))

def analyze(base):
    """
    يحسب:
      - r30s, r60s, r3m, r5m
      - speed_pm (%/د = r30s*2)
      - accel_pm (speed_now - speed_prev)
      - vol_ratio (نشاط قصير/طويل)
      - expected (تقدير قفزة 2–5 دقائق)
    """
    now = time.time()
    dq = prices[base]
    if len(dq) < 5:
        return None

    cur = dq[-1][1]
    p30  = window_price(base, 30,  now)
    p60  = window_price(base, 60,  now)
    p180 = window_price(base, 180, now)
    p300 = window_price(base, 300, now)

    # منع التحليل قبل اكتمال المراجع الأساسية
    if not (p30 and p60):
        return None

    r30s = pct_change(cur, p30) if p30 else 0.0
    r60s = pct_change(cur, p60) if p60 else 0.0
    r3m  = pct_change(cur, p180) if p180 else 0.0
    r5m  = pct_change(cur, p300) if p300 else 0.0

    speed_pm_cur  = r30s * 2.0
    speed_pm_prev = r60s  # تقريب سرعة الدقيقة الماضية
    accel_pm      = speed_pm_cur - speed_pm_prev

    vol_ratio = micro_volatility(base, now)

    # تقدير قفزة 2–5 دقائق (عدواني مع عامل أفق)
    horizon_factor = 1.8
    vol_clamped    = max(0.9, min(vol_ratio, 2.5))
    expected       = speed_pm_cur * (1.0 + accel_pm / 100.0) * vol_clamped * horizon_factor

    res = {
        "symbol": base,
        "cur": cur,
        "r30s": r30s,
        "r60s": r60s,
        "r3m": r3m,
        "r5m": r5m,
        "speed_pm": speed_pm_cur,
        "accel_pm": accel_pm,
        "vol_ratio": vol_ratio,
        "expected": expected
    }
    if DEBUG_LOG:
        print("[ANALYZE]", res)
    return res

# =========================
# 🔁 جامع الأسعار (مع كاش للأسواق)
# =========================
def collector():
    symbols = []
    last_markets = 0
    while True:
        now = time.time()
        # حدّث قائمة الأسواق كل 60s فقط
        if (now - last_markets > 60) or not symbols:
            markets = http_get_json(f"{BASE_URL}/markets")
            if markets:
                symbols = [m.get("base") for m in markets
                           if m.get("quote") == "EUR" and m.get("status") == "trading"]
                last_markets = now

        # اجلب أسعار جميع الرموز الحالية
        for base in symbols:
            ticker = http_get_json(f"{BASE_URL}/ticker/price", {"market": f"{base}-EUR"})
            if not ticker:
                continue
            try:
                price = float(ticker["price"])
            except Exception:
                continue

            dq = prices[base]
            dq.append((now, price))
            # قصّ التاريخ القديم
            cutoff = now - HISTORY_SEC
            while dq and dq[0][0] < cutoff:
                dq.popleft()

        time.sleep(SCAN_INTERVAL_SEC)

# =========================
# 🤖 متنبّه الإشعارات + متابعة 5 دقائق
# =========================
def predictor():
    while True:
        now = time.time()

        # تحدّيث نتائج الإشعارات المنتهية نافذتها
        for base, pred in list(predictions.items()):
            if pred["status"] is None and now - pred["time"] >= FOLLOWUP_WINDOW_SEC:
                dq = prices.get(base)
                if not dq:
                    continue
                cur = dq[-1][1]
                actual = pct_change(cur, pred["start_price"])
                status = "✅ أصابت" if actual >= pred["expected"] else "❌ خابت"
                pred["status"] = status
                history_results.append((now, base, status, pred["expected"], actual))
                learning_window.append("hit" if "✅" in status else "miss")

        # تحليل جديد وإرسال إشعارات
        for base in list(prices.keys()):
            dq = prices.get(base)
            if not dq or (dq[-1][0] - dq[0][0]) < WARMUP_SEC:
                continue

            res = analyze(base)
            if not res:
                continue

            # فلاتر
            if res["speed_pm"] < thresholds["MIN_SPEED_PM"]:
                continue
            if res["accel_pm"] < thresholds["MIN_ACCEL_PM"]:
                continue
            if res["vol_ratio"] < thresholds["VOL_RATIO_MIN"]:
                continue
            if res["expected"] < thresholds["EXPECTED_MIN"]:
                continue

            # كولداون لكل عملة
            if base in last_alert_ts and now - last_alert_ts[base] < ALERT_COOLDOWN_SEC:
                continue

            last_alert_ts[base] = now
            predictions[base] = {
                "time": now,
                "expected": res["expected"],
                "start_price": res["cur"],
                "status": None
            }

            msg = (
                f"🚀 توقع قفزة: {base}\n"
                f"الهدف: {res['expected']:+.2f}% (≈ 2–5 د)\n"
                f"سرعة: {res['speed_pm']:+.2f}%/د | تسارع: {res['accel_pm']:+.2f}% | نشاط: ×{res['vol_ratio']:.2f}\n"
                f"r30s {res['r30s']:+.2f}% | r60s {res['r60s']:+.2f}% | r5م {res['r5m']:+.2f}%"
            )
            send_message(msg)

        time.sleep(2)

# =========================
# 🧪 تعلّم ذاتي سريع (كل دقيقة)
# =========================
def self_learning():
    """
    يراقب hit/miss في آخر نافذة ويعدّل العتبات:
      - إذا النجاح < 40% ومعنا ≥8 نتائج: يشدّد الشروط.
      - إذا النجاح > 70% ومعنا ≥8 نتائج: يرخّي الشروط.
    """
    while True:
        time.sleep(60)
        total = len(learning_window)
        if total < 8:
            continue
        hits = sum(1 for x in learning_window if x == "hit")
        rate = hits / total

        ms = thresholds["MIN_SPEED_PM"]
        ma = thresholds["MIN_ACCEL_PM"]
        vr = thresholds["VOL_RATIO_MIN"]
        ex = thresholds["EXPECTED_MIN"]

        if rate < 0.40:
            ms = min(ms + 0.10, 1.50)
            ma = min(ma + 0.05, 0.60)
            vr = min(vr + 0.10, 2.0)
            ex = min(ex + 0.20, 3.0)
        elif rate > 0.70:
            ms = max(ms - 0.10, 0.30)
            ma = max(ma - 0.05, 0.05)
            vr = max(vr - 0.10, 1.0)
            ex = max(ex - 0.20, 1.60)
        else:
            continue

        thresholds["MIN_SPEED_PM"] = round(ms, 2)
        thresholds["MIN_ACCEL_PM"] = round(ma, 2)
        thresholds["VOL_RATIO_MIN"] = round(vr, 2)
        thresholds["EXPECTED_MIN"]  = round(ex, 2)

        if DEBUG_LOG:
            print("[ADAPT]", thresholds)

# =========================
# 🌐 تيليجرام: الملخص/الضبط
# =========================
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json or {}
    msg = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()

    if not text:
        return "ok", 200

    if text in {"الملخص", "/summary"}:
        if not history_results:
            send_message("📊 لا توجد نتائج بعد.")
        else:
            total = len(history_results)
            hits  = sum(1 for _,_,s,_,_ in history_results if "✅" in s)
            misses= total - hits
            rate  = (hits / total) * 100
            lines = [
                f"📊 الملخص (آخر {total} إشارة):",
                f"أصابت: {hits} | خابت: {misses} | نجاح: {rate:.1f}%",
                "",
                "آخر 12 نتيجة:"
            ]
            for ts, base, status, exp, act in list(history_results)[-12:]:
                lines.append(f"{base}: {status} | متوقعة {exp:+.2f}% | فعلية {act:+.2f}%")
            send_message("\n".join(lines))
        return "ok", 200

    if text in {"الضبط", "/status"}:
        t = thresholds
        lines = [
            "⚙️ الضبط الحالي (مُتكيّف):",
            f"MIN_SPEED_PM   = {t['MIN_SPEED_PM']:.2f} %/د",
            f"MIN_ACCEL_PM   = {t['MIN_ACCEL_PM']:.2f} %",
            f"VOL_RATIO_MIN  = ×{t['VOL_RATIO_MIN']:.2f}",
            f"EXPECTED_MIN   = {t['EXPECTED_MIN']:.2f} %",
            f"COOLDOWN       = {ALERT_COOLDOWN_SEC}s",
            f"WARMUP         = {WARMUP_SEC}s",
            f"FOLLOWUP_WIN   = {FOLLOWUP_WINDOW_SEC//60}m",
        ]
        send_message("\n".join(lines))
        return "ok", 200

    return "ok", 200

# =========================
# 🌐 صحة
# =========================
@app.route("/", methods=["GET"])
def health():
    return "Aggressive Predictor is alive ✅", 200

# =========================
# 🚀 التشغيل
# =========================
def start_all():
    Thread(target=collector,    daemon=True).start()
    Thread(target=predictor,    daemon=True).start()
    Thread(target=self_learning,daemon=True).start()

if __name__ == "__main__":
    start_all()
    # للتشغيل على Railway عبر Gunicorn:
    # web: gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))